// Command agentctl is the Sentinel control-plane CLI.
//
// Subcommands:
//
//	agentctl run --target <URL> [--planner h|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]
//	agentctl baseline update --plan <plan.json> [--target <URL>]   (the only golden-baseline mutation path)
//	agentctl locators clear-quarantine
//
// It spawns the Python brain (venv) via subprocess + env (no gRPC yet; M2b) and propagates the
// brain's structured exit code (0 pass / 1 step-fail / 2 golden regression / 3 plan-integrity).
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func newRunID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage:")
	fmt.Fprintln(os.Stderr, "  agentctl run --target <URL> [--planner heuristic|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]")
	fmt.Fprintln(os.Stderr, "  agentctl baseline update --plan <plan.json> [--target <URL>]")
	fmt.Fprintln(os.Stderr, "  agentctl locators clear-quarantine")
}

func boolEnv(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func mkArtifactDir(repo, runID, override string) string {
	dir := override
	if dir == "" {
		dir = filepath.Join(repo, "runs", runID)
	}
	_ = os.MkdirAll(dir, 0o755)
	return dir
}

// filteredEnv narrows the inherited environment to a security allowlist (GAP-SEC-001) so unrelated
// host secrets (SSH keys, cloud creds, unrelated tokens) don't leak into the brain and its children.
// OPT-IN via SENTINEL_ENV_ALLOWLIST=1; default is unchanged full inheritance (zero behaviour change).
// When enabled: runtime essentials + the Sentinel/LLM/OTel/Playwright families pass, plus any names
// the user explicitly lists in SENTINEL_ENV_ALLOW (comma-separated) — required for secretRef secret
// vars (e.g. AUT_PASSWORD). M11.3 makes the allowlist the default after container-integration tests.
func filteredEnv() []string {
	if os.Getenv("SENTINEL_ENV_ALLOWLIST") != "1" {
		return os.Environ()
	}
	exact := map[string]bool{
		"PATH": true, "HOME": true, "USER": true, "LOGNAME": true, "SHELL": true, "PWD": true,
		"LANG": true, "LC_ALL": true, "TERM": true, "TMPDIR": true, "TZ": true,
		"ANTHROPIC_API_KEY": true, "OPENAI_API_KEY": true, "CHECKPOINT_DSN": true,
		"STORAGE_STATE": true, "STORAGE_STATE_SAVE": true, "MCP_TRANSPORT": true,
		"ORCH_ADDR": true, "STORE_ADDR": true, "BRAIN_PYTHON": true, "PYTHONPATH": true,
	}
	for _, n := range strings.Split(os.Getenv("SENTINEL_ENV_ALLOW"), ",") {
		if n = strings.TrimSpace(n); n != "" {
			exact[n] = true
		}
	}
	prefixes := []string{"LLM_", "OTEL_", "PW_", "PLAYWRIGHT_", "SENTINEL_", "NODE_", "GIT_"}
	var out []string
	for _, kv := range os.Environ() {
		k := kv
		if i := strings.IndexByte(kv, '='); i >= 0 {
			k = kv[:i]
		}
		if exact[k] {
			out = append(out, kv)
			continue
		}
		for _, p := range prefixes {
			if strings.HasPrefix(k, p) {
				out = append(out, kv)
				break
			}
		}
	}
	return out
}

// spawnBrain runs the brain with the common env + extra vars, streams I/O, returns its exit code.
func spawnBrain(repo, runID string, extra []string) int {
	pwExec := "node " + filepath.Join(repo, "pw-executor", "dist", "server.js")
	brainPython := filepath.Join(repo, ".venv", "bin", "python")
	if _, err := os.Stat(brainPython); err != nil {
		brainPython = "python3"
	}
	if v := os.Getenv("BRAIN_PYTHON"); v != "" {
		brainPython = v
	}
	cmd := exec.Command(brainPython, "-m", "brain")
	cmd.Dir = repo
	cmd.Env = append(filteredEnv(), append([]string{
		"RUN_ID=" + runID,
		"PW_EXECUTOR_CMD=" + pwExec,
		"PYTHONPATH=" + repo,
	}, extra...)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		fmt.Fprintf(os.Stderr, "[agentctl] failed to run brain: %v\n", err)
		return 1
	}
	return 0
}

// startGateway launches the Go store-gateway over a Unix socket (ADR-015). If the binary isn't
// built it returns "" so the brain falls back to its LocalStore. Returns (STORE_ADDR, stop()).
func startGateway(repo, runID string) (string, func()) {
	gw := filepath.Join(repo, "bin", "store-gateway")
	if _, err := os.Stat(gw); err != nil {
		return "", func() {}
	}
	// socket lives under repo/state (on the project volume), NOT /tmp — /tmp may be full and
	// net.Listen("unix",...) then fails to create the socket, silently dropping to LocalStore.
	_ = os.MkdirAll(filepath.Join(repo, "state"), 0o755)
	sock := filepath.Join(repo, "state", "sentinel-store-"+runID+".sock")
	cmd := exec.Command(gw, "--addr", sock, "--db", filepath.Join(repo, "state", "locators.db"))
	cmd.Dir = repo
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return "", func() {}
	}
	ok := false
	for i := 0; i < 100; i++ { // wait up to ~5s for the socket to appear
		if _, err := os.Stat(sock); err == nil {
			ok = true
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !ok {
		fmt.Fprintln(os.Stderr, "[agentctl] store-gateway socket never appeared -> LocalStore")
		_ = cmd.Process.Kill()
		return "", func() {}
	}
	return sock, func() {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		_, _ = cmd.Process.Wait()
		_ = os.Remove(sock)
	}
}

// runWithStore starts the gateway, injects STORE_ADDR, runs the brain, then stops the gateway.
func runWithStore(repo, runID string, extra []string) int {
	addr, stop := startGateway(repo, runID)
	defer stop()
	if addr != "" {
		extra = append(extra, "STORE_ADDR="+addr)
	}
	return spawnBrain(repo, runID, extra)
}

func cmdRun(repo string, args []string) int {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	target := fs.String("target", "", "target URL (required)")
	artifactDir := fs.String("artifact-dir", "", "artifact dir (default ./runs/<id>)")
	mode := fs.String("mode", "explore", "run mode")
	_ = fs.Bool("explore", false, "explore mode (default; accepted for convenience)")
	planner := fs.String("planner", "heuristic", "planner: heuristic|llm|goal")
	goal := fs.String("goal", "", "NL goal -> goal-mode authoring (GoalPlanner, M9.2a); empty = explore")
	describe := fs.String("describe", "", "NL flow description -> describe-mode (M9.2b); mutually exclusive with --goal")
	scenario := fs.String("scenario", "", "RunConfig scenario name to select (M9.2b)")
	runConfig := fs.String("run-config", "", "path to a RunConfig YAML (mode/goal/planner/budgets/auth/scenarios)")
	coverageTarget := fs.String("coverage-target", "0.85", "coverage target in [0,1]")
	maxSteps := fs.String("max-steps", "40", "max exploration steps (safety backstop)")
	replay := fs.Bool("replay", false, "replay a frozen plan, healing broken locators (M2/M3)")
	planFile := fs.String("plan", "", "path to plan.json (required with --replay)")
	healLLM := fs.Bool("heal-llm", false, "allow Sonnet LLM re-grounding during heal")
	autVersion := fs.String("aut-version", "", "app-under-test version/sha (flake quarantine)")
	ci := fs.Bool("ci", false, "CI mode (forbids --force-replay)")
	force := fs.Bool("force-replay", false, "bypass plan_hash hard-abort (disallowed under --ci)")
	_ = fs.Parse(args)

	// M9.2a (ADR-027): record which flags the user actually set, so RunConfig precedence (flag > file)
	// holds even when the explicit value equals the default. fs.Visit walks ONLY the flags that were set.
	setFlags := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { setFlags[f.Name] = true })
	var explicit []string
	for _, n := range []string{"planner", "coverage-target", "max-steps", "goal", "describe", "scenario"} {
		if setFlags[n] {
			explicit = append(explicit, n)
		}
	}

	if *target == "" {
		fmt.Fprintln(os.Stderr, "error: --target is required")
		return 2
	}
	if *replay && *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan <plan.json> is required with --replay")
		return 2
	}
	runMode := *mode
	if *replay {
		runMode = "replay"
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, *artifactDir)
	fmt.Printf("[agentctl] run_id=%s mode=%s planner=%s target=%s\n", runID, runMode, *planner, *target)
	fmt.Printf("[agentctl] artifacts=%s\n", dir)
	extra := []string{
		"RUN_MODE=" + runMode,
		"TARGET_URL=" + *target,
		"ARTIFACT_DIR=" + dir,
		"PLANNER=" + *planner,
		"COVERAGE_TARGET=" + *coverageTarget,
		"MAX_STEPS=" + *maxSteps,
		"GOAL=" + *goal,
		"DESCRIBE=" + *describe,
		"SCENARIO=" + *scenario,
		"RUN_CONFIG=" + *runConfig,
		"SENTINEL_EXPLICIT=" + strings.Join(explicit, ","),
		"PLAN_FILE=" + *planFile,
		"HEAL_LLM=" + boolEnv(*healLLM),
		"AUT_VERSION=" + *autVersion,
		"CI=" + boolEnv(*ci),
		"FORCE_REPLAY=" + boolEnv(*force),
	}
	if *replay { // replay needs the locator/golden/quarantine store
		return runWithStore(repo, runID, extra)
	}
	return spawnBrain(repo, runID, extra) // explore needs no store
}

func cmdBaseline(repo string, args []string) int {
	if len(args) < 1 || args[0] != "update" {
		fmt.Fprintln(os.Stderr, "usage: agentctl baseline update --plan <plan.json> [--target <URL>]")
		return 2
	}
	fs := flag.NewFlagSet("baseline", flag.ExitOnError)
	planFile := fs.String("plan", "", "path to plan.json (required)")
	target := fs.String("target", "", "target URL (default: the plan's target_url)")
	artifactDir := fs.String("artifact-dir", "", "artifact dir")
	_ = fs.Parse(args[1:])
	if *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan is required")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, *artifactDir)
	fmt.Printf("[agentctl] baseline update run_id=%s plan=%s\n", runID, *planFile)
	return runWithStore(repo, runID, []string{
		"RUN_MODE=baseline",
		"TARGET_URL=" + *target,
		"ARTIFACT_DIR=" + dir,
		"PLAN_FILE=" + *planFile,
	})
}

func cmdLocators(repo string, args []string) int {
	if len(args) < 1 || args[0] != "clear-quarantine" {
		fmt.Fprintln(os.Stderr, "usage: agentctl locators clear-quarantine")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return runWithStore(repo, runID, []string{
		"RUN_MODE=clear-quarantine",
		"ARTIFACT_DIR=" + dir,
	})
}

// cmdExportSpec: agentctl export-spec --plan <p> [-o <file>]  (M4)
func cmdExportSpec(repo string, args []string) int {
	fs := flag.NewFlagSet("export-spec", flag.ExitOnError)
	planFile := fs.String("plan", "", "path to plan.json (required)")
	out := fs.String("o", "", "output .spec.ts path (default <run>/exported.spec.ts)")
	_ = fs.Parse(args)
	if *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan is required")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return spawnBrain(repo, runID, []string{
		"RUN_MODE=export-spec",
		"ARTIFACT_DIR=" + dir,
		"PLAN_FILE=" + *planFile,
		"SPEC_OUT=" + *out,
	})
}

// cmdReport: agentctl report --run <dir>  (M4) — HTML+JSON report + Prometheus metrics
func cmdReport(repo string, args []string) int {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	runDir := fs.String("run", "", "run directory containing heal-report.json (required)")
	_ = fs.Parse(args)
	if *runDir == "" {
		fmt.Fprintln(os.Stderr, "error: --run <dir> is required")
		return 2
	}
	return spawnBrain(repo, newRunID(), []string{
		"RUN_MODE=report",
		"ARTIFACT_DIR=" + *runDir,
		"REPORT_DIR=" + *runDir,
	})
}

// cmdCalibrate: agentctl calibrate  (M4) — heal precision/histogram from healing_audit
func cmdCalibrate(repo string, args []string) int {
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return runWithStore(repo, runID, []string{
		"RUN_MODE=calibrate",
		"ARTIFACT_DIR=" + dir,
	})
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cwd: %v\n", err)
		os.Exit(1)
	}
	var code int
	switch os.Args[1] {
	case "run":
		code = cmdRun(repo, os.Args[2:])
	case "baseline":
		code = cmdBaseline(repo, os.Args[2:])
	case "locators":
		code = cmdLocators(repo, os.Args[2:])
	case "export-spec":
		code = cmdExportSpec(repo, os.Args[2:])
	case "report":
		code = cmdReport(repo, os.Args[2:])
	case "calibrate":
		code = cmdCalibrate(repo, os.Args[2:])
	default:
		usage()
		code = 2
	}
	os.Exit(code)
}
