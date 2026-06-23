// Command agentctl is the Sentinel control-plane CLI.
// M0: `run` spawns the Python brain via subprocess + env (no gRPC yet) and
// streams its output. The brain in turn drives the TypeScript pw-executor.
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

func main() {
	if len(os.Args) < 2 || os.Args[1] != "run" {
		fmt.Fprintln(os.Stderr, "usage: agentctl run --target <URL> [--artifact-dir DIR] [--mode explore]")
		os.Exit(2)
	}

	fs := flag.NewFlagSet("run", flag.ExitOnError)
	target := fs.String("target", "", "target URL to explore (required)")
	artifactDir := fs.String("artifact-dir", "", "artifact output dir (default ./runs/<run_id>)")
	mode := fs.String("mode", "explore", "run mode")
	_ = fs.Bool("explore", false, "explore mode (default; accepted for convenience)")
	planner := fs.String("planner", "heuristic", "planner: heuristic|llm")
	coverageTarget := fs.String("coverage-target", "0.85", "coverage target in [0,1]")
	maxSteps := fs.String("max-steps", "40", "max exploration steps (safety backstop)")
	_ = fs.Parse(os.Args[2:])

	if *target == "" {
		fmt.Fprintln(os.Stderr, "error: --target is required")
		os.Exit(2)
	}

	runID := newRunID()
	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cwd: %v\n", err)
		os.Exit(1)
	}
	if *artifactDir == "" {
		*artifactDir = filepath.Join(repo, "runs", runID)
	}
	if err := os.MkdirAll(*artifactDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "mkdir artifact dir: %v\n", err)
		os.Exit(1)
	}

	pwExec := "node " + filepath.Join(repo, "pw-executor", "dist", "server.js")

	// Run the brain with the project venv python (deps: langgraph, ...); fall back to python3.
	// Override with BRAIN_PYTHON.
	brainPython := filepath.Join(repo, ".venv", "bin", "python")
	if _, statErr := os.Stat(brainPython); statErr != nil {
		brainPython = "python3"
	}
	if v := os.Getenv("BRAIN_PYTHON"); v != "" {
		brainPython = v
	}

	fmt.Printf("[agentctl] run_id=%s mode=%s planner=%s target=%s\n", runID, *mode, *planner, *target)
	fmt.Printf("[agentctl] artifacts=%s\n", *artifactDir)

	cmd := exec.Command(brainPython, "-m", "brain")
	cmd.Dir = repo
	cmd.Env = append(os.Environ(),
		"TARGET_URL="+*target,
		"RUN_ID="+runID,
		"RUN_MODE="+*mode,
		"ARTIFACT_DIR="+*artifactDir,
		"PW_EXECUTOR_CMD="+pwExec,
		"PLANNER="+*planner,
		"COVERAGE_TARGET="+*coverageTarget,
		"MAX_STEPS="+*maxSteps,
		"PYTHONPATH="+repo,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			fmt.Fprintf(os.Stderr, "[agentctl] brain exited code=%d\n", ee.ExitCode())
			os.Exit(ee.ExitCode())
		}
		fmt.Fprintf(os.Stderr, "[agentctl] failed to run brain: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("[agentctl] DONE run_id=%s trace=%s\n", runID, filepath.Join(*artifactDir, "trace.zip"))
}
