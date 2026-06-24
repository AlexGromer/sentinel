// Command agentctl is the Sentinel control-plane CLI.
//
// Subcommands:
//   agentctl run --target <URL> [--planner h|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]
//   agentctl baseline update --plan <plan.json> [--target <URL>]   (the only golden-baseline mutation path)
//   agentctl locators clear-quarantine
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
	cmd.Env = append(os.Environ(), append([]string{
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

func cmdRun(repo string, args []string) int {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	target := fs.String("target", "", "target URL (required)")
	artifactDir := fs.String("artifact-dir", "", "artifact dir (default ./runs/<id>)")
	mode := fs.String("mode", "explore", "run mode")
	_ = fs.Bool("explore", false, "explore mode (default; accepted for convenience)")
	planner := fs.String("planner", "heuristic", "planner: heuristic|llm")
	coverageTarget := fs.String("coverage-target", "0.85", "coverage target in [0,1]")
	maxSteps := fs.String("max-steps", "40", "max exploration steps (safety backstop)")
	replay := fs.Bool("replay", false, "replay a frozen plan, healing broken locators (M2/M3)")
	planFile := fs.String("plan", "", "path to plan.json (required with --replay)")
	healLLM := fs.Bool("heal-llm", false, "allow Sonnet LLM re-grounding during heal")
	autVersion := fs.String("aut-version", "", "app-under-test version/sha (flake quarantine)")
	ci := fs.Bool("ci", false, "CI mode (forbids --force-replay)")
	force := fs.Bool("force-replay", false, "bypass plan_hash hard-abort (disallowed under --ci)")
	_ = fs.Parse(args)

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
	return spawnBrain(repo, runID, []string{
		"RUN_MODE=" + runMode,
		"TARGET_URL=" + *target,
		"ARTIFACT_DIR=" + dir,
		"PLANNER=" + *planner,
		"COVERAGE_TARGET=" + *coverageTarget,
		"MAX_STEPS=" + *maxSteps,
		"PLAN_FILE=" + *planFile,
		"HEAL_LLM=" + boolEnv(*healLLM),
		"AUT_VERSION=" + *autVersion,
		"CI=" + boolEnv(*ci),
		"FORCE_REPLAY=" + boolEnv(*force),
	})
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
	return spawnBrain(repo, runID, []string{
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
	return spawnBrain(repo, runID, []string{
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
	return spawnBrain(repo, runID, []string{
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
