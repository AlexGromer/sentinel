// Command agentctl is the Sentinel control-plane CLI.
//
// Subcommands:
//
//	agentctl run --target <URL> [--planner h|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]
//	agentctl run --target <URL> --mode chat --conversation-id <id> [--goal <g>|--describe <d>]   (M9.10 multi-turn, ADR-048)
//	agentctl baseline update --plan <plan.json> [--target <URL>]   (the only golden-baseline mutation path)
//	agentctl locators clear-quarantine
//
// It spawns the Python brain (venv) via subprocess + env (no gRPC yet; M2b) and propagates the
// brain's structured exit code (0 pass / 1 step-fail / 2 golden regression / 3 integrity: plan_hash or golden HMAC).
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
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

// newToken mints the per-run shared secret that authenticates brain->store-gateway calls (#23).
// It is passed to the gateway (STORE_TOKEN env) and the brain (STORE_TOKEN run-var), never to argv.
func newToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		// #34: fail closed. A degraded "" token would silently disable authN on BOTH the gateway
		// and the brain (empty STORE_TOKEN → no interceptor), re-opening the #23 surface on a
		// (rare) RNG failure. Abort instead of degrading.
		fmt.Fprintf(os.Stderr, "agentctl: rand.Read for store token: %v\n", err)
		os.Exit(1)
	}
	return hex.EncodeToString(b)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage:")
	fmt.Fprintln(os.Stderr, "  agentctl run --target <URL> [--planner heuristic|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]")
	fmt.Fprintln(os.Stderr, "  agentctl run --target <URL> --mode chat --conversation-id <id> [--goal <g>|--describe <d>]   (M9.10 multi-turn)")
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
	// #26 (THREAT_MODEL ❹): the run dir holds trace.zip — AUT DOM snapshots + screenshots, which
	// may contain PII. Restrict it to the owner (0700) so other local users can't read it. Chmod
	// after MkdirAll to enforce 0700 even when a permissive umask would have widened the create mode.
	_ = os.MkdirAll(dir, 0o700)
	_ = os.Chmod(dir, 0o700)
	// #34 pt3: manage the runs/ root (0700) + trace retention whenever the artifact dir lives under
	// repo/runs — the default ./runs/<id> tree AND the control-api override path (runs/control-<id>,
	// what the chat-front + OpenAI-compat shim drive). Without this, control-api passed --artifact-dir
	// so override != "" and BOTH the chmod and sweepTraces were skipped, letting trace.zip (AUT
	// DOM/screenshots, possible PII) accumulate unbounded in a Docker/control-api deployment. A truly
	// external --artifact-dir (outside runs/) is still left untouched — we never chmod or sweep a
	// user-supplied directory we don't own.
	runsRoot := filepath.Join(repo, "runs")
	if isUnder(dir, runsRoot) {
		_ = os.Chmod(runsRoot, 0o700)
		sweepTraces(runsRoot)
	}
	return dir
}

// isUnder reports whether path is root itself or nested inside root. It compares cleaned absolute
// paths so a relative --artifact-dir and an absolute repo/runs (or vice versa) still match, and a
// sibling like runs-evil/ next to runs/ does not.
func isUnder(path, root string) bool {
	ap, err1 := filepath.Abs(path)
	ar, err2 := filepath.Abs(root)
	if err1 != nil || err2 != nil {
		return false
	}
	rel, err := filepath.Rel(ar, ap)
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
}

// envInt reads an int env var, falling back to def when unset or unparsable.
func envInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// sweepTraces enforces trace.zip retention (#26, THREAT_MODEL ❹). It keeps the newest
// SENTINEL_TRACE_KEEP traces (default 10; <0 disables count-pruning) and deletes trace.zip from
// older runs, plus any trace older than SENTINEL_TRACE_TTL_HOURS (default 0 = TTL off). Only the
// trace.zip is removed — plan.json / reports stay for the audit trail. Best-effort: every error is
// ignored, retention must never fail a run. The just-created run has no trace.zip yet, so it is safe.
func sweepTraces(runsRoot string) {
	keep := envInt("SENTINEL_TRACE_KEEP", 10)
	ttlHours := envInt("SENTINEL_TRACE_TTL_HOURS", 0)
	if keep < 0 && ttlHours <= 0 {
		return // both knobs disabled
	}
	entries, err := os.ReadDir(runsRoot)
	if err != nil {
		return
	}
	type trace struct {
		path string
		mod  time.Time
	}
	var traces []trace
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		p := filepath.Join(runsRoot, e.Name(), "trace.zip")
		info, err := os.Stat(p)
		if err != nil {
			continue // no trace in this run
		}
		traces = append(traces, trace{p, info.ModTime()})
	}
	sort.Slice(traces, func(i, j int) bool { return traces[i].mod.After(traces[j].mod) }) // newest first
	now := time.Now()
	for i, tr := range traces {
		tooMany := keep >= 0 && i >= keep
		tooOld := ttlHours > 0 && now.Sub(tr.mod) > time.Duration(ttlHours)*time.Hour
		if tooMany || tooOld {
			_ = os.Remove(tr.path)
		}
	}
}

// filteredEnv narrows the inherited environment to a security allowlist (GAP-SEC-001) so unrelated
// host secrets (SSH keys, cloud creds, unrelated tokens) don't leak into the brain and its children.
// DEFAULT-ON since M11.3 (ADR-035): the allowlist is active unless SENTINEL_ENV_ALLOWLIST=0 — an
// explicit opt-out escape hatch for debugging / unusual local setups. When active: runtime essentials
// + the Sentinel/LLM/OTel/Playwright/proxy/TLS families pass, plus any names the user lists in
// SENTINEL_ENV_ALLOW (comma-separated) — required for secretKeyRef secret vars (e.g. AUT_PASSWORD);
// the Helm chart (deploy/sentinel) auto-emits SENTINEL_ENV_ALLOW from extraEnv/extraSecretEnv so those
// reach the brain. Functional run vars (RUN_ID/TARGET_URL/RUN_MODE/…) bypass this filter — they are
// appended after filteredEnv() in spawnBrain, never inherited from the host.
func filteredEnv() []string {
	if os.Getenv("SENTINEL_ENV_ALLOWLIST") == "0" { // opt-out escape hatch — full host-env passthrough
		return os.Environ()
	}
	exact := map[string]bool{
		"PATH": true, "HOME": true, "USER": true, "LOGNAME": true, "SHELL": true, "PWD": true,
		"LANG": true, "LC_ALL": true, "TERM": true, "TMPDIR": true, "TZ": true,
		"ANTHROPIC_API_KEY": true, "OPENAI_API_KEY": true, "CHECKPOINT_DSN": true,
		"STORAGE_STATE": true, "STORAGE_STATE_SAVE": true, "MCP_TRANSPORT": true,
		"ORCH_ADDR": true, "STORE_ADDR": true, "BRAIN_PYTHON": true, "PYTHONPATH": true,
		// M11.3 (ADR-035): metrics push, visual-heal toggle, corporate TLS trust + proxy.
		"PROM_PUSHGATEWAY": true, "HEAL_VISUAL": true,
		"SSL_CERT_FILE": true, "SSL_CERT_DIR": true,
		"HTTP_PROXY": true, "HTTPS_PROXY": true, "NO_PROXY": true,
		"http_proxy": true, "https_proxy": true, "no_proxy": true,
		// #25 (GAP-SEC-001 remainder): the broad NODE_/GIT_ prefixes used to carry these legitimate
		// runtime/TLS vars but ALSO leaked NODE_AUTH_TOKEN (npm registry auth) and GIT_ASKPASS (a
		// program git runs to obtain credentials). Allowlist the specific names instead of the family.
		"NODE_OPTIONS": true, "NODE_EXTRA_CA_CERTS": true,
		"GIT_SSL_CAINFO": true, "GIT_SSL_CAPATH": true,
	}
	for _, n := range strings.Split(os.Getenv("SENTINEL_ENV_ALLOW"), ",") {
		if n = strings.TrimSpace(n); n != "" {
			exact[n] = true
		}
	}
	// NODE_/GIT_ are deliberately NOT prefixes (#25): the family is too broad and leaks credential
	// vars (NODE_AUTH_TOKEN, GIT_ASKPASS). The legitimate runtime/TLS members are exact-allowlisted above.
	prefixes := []string{"LLM_", "OTEL_", "PW_", "PLAYWRIGHT_", "SENTINEL_"}
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
// token is handed to the gateway via env (STORE_TOKEN) so it can authenticate brain calls (#23).
func startGateway(repo, runID, token string) (string, func()) {
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
	cmd.Env = append(os.Environ(), "STORE_TOKEN="+token) // #23: gateway authenticates against this
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
	token := newToken() // #23: per-run secret shared by the gateway and the brain only
	addr, stop := startGateway(repo, runID, token)
	defer stop()
	if addr != "" {
		// STORE_TOKEN is a run-var (appended after filteredEnv) so it always reaches the brain.
		extra = append(extra, "STORE_ADDR="+addr, "STORE_TOKEN="+token)
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
	conversationID := fs.String("conversation-id", "", "M9.10 multi-turn conversation id (use with --mode chat); resumes the thread by conversation_id->thread_id (ADR-048)")
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
		// M9.10 (ADR-048): chat-mode conversation thread key. Run-var (appended after filteredEnv), so it
		// always reaches the brain; only read when RUN_MODE=chat. The SENTINEL_ prefix is allowlisted too.
		"SENTINEL_CONVERSATION_ID=" + *conversationID,
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
