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
	mode := fs.String("mode", "explore", "run mode (M0: explore only)")
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

	fmt.Printf("[agentctl] run_id=%s mode=%s target=%s\n", runID, *mode, *target)
	fmt.Printf("[agentctl] artifacts=%s\n", *artifactDir)

	cmd := exec.Command("python3", "-m", "brain")
	cmd.Dir = repo
	cmd.Env = append(os.Environ(),
		"TARGET_URL="+*target,
		"RUN_ID="+runID,
		"RUN_MODE="+*mode,
		"ARTIFACT_DIR="+*artifactDir,
		"PW_EXECUTOR_CMD="+pwExec,
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
