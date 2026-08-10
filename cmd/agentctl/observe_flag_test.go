package main

// LIVE-MATRIX (ADR-120) — the observation mode chosen on a SURFACE has to arrive at the brain.
//
// WHAT WAS MEASURED. control-api handed the choice down as environment
// (`cmd.Env = append(cmd.Env, "SENTINEL_OBSERVE="+v)`), while this binary builds the brain's run-vars
// unconditionally — `"SENTINEL_OBSERVE=" + *observe` is appended whether or not `--observe` was given.
// Run-vars go AFTER filteredEnv, and os/exec keeps the LAST value for a duplicated key, so an
// inherited `SENTINEL_OBSERVE=off` was overwritten by the empty string. The run then captured frames
// while `run.observation` truthfully reported "by default (nothing was asked for)" — the person's
// choice disappeared en route and the log said they had never made one. That is the exact class of
// silence this arc exists to remove, and no gate on the branch could see it: the offline suite checks
// that the schema, the CLI and the resolver name the same SET of modes, never that a value travels.
//
// WHY THE TEST LOOKS LIKE THIS. Asserting the shape of control-api's source would be a surrogate —
// mutations pass straight through a claim about text. So this runs the real `cmdRun`, with a stand-in
// for the brain that writes down the environment it was handed, and asserts BOTH directions:
//
//  1. the flag reaches the brain and BEATS an inherited value — otherwise a later "fix" that let the
//     environment win would silently ignore the argv control-api now passes;
//  2. an inherited value ALONE does NOT reach the brain — this is the measured fact that makes argv
//     necessary. Delete it and the reason for `--observe` in appendRunFlags stops being recorded, so
//     the next person moves it back to the environment and reopens the defect.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func brainEnvAfterRun(t *testing.T, args []string) string {
	t.Helper()
	repo := t.TempDir()
	t.Setenv("BRAIN_PYTHON", brainStub(t, repo))
	if rc := cmdRun(repo, args); rc != 0 {
		t.Fatalf("cmdRun%v = %d, want 0", args, rc)
	}
	env, err := os.ReadFile(filepath.Join(repo, "env.txt"))
	if err != nil {
		t.Fatalf("the brain stub never ran: %v", err)
	}
	return string(env)
}

func TestTheChosenObservationModeReachesTheBrain(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the brain stub is a /bin/sh script")
	}
	const target = "file:///dev/null"

	// 1. The flag travels, and it wins over an inherited value. `SENTINEL_OBSERVE` survives
	// filteredEnv (the SENTINEL_ prefix is allowlisted), so without this assertion "the flag works"
	// could be satisfied by the environment happening to carry the same value.
	t.Setenv("SENTINEL_OBSERVE", "stream")
	got := brainEnvAfterRun(t, []string{"--target", target, "--observe", "off"})
	if !strings.Contains(got, "SENTINEL_OBSERVE=off") {
		t.Errorf("--observe off did not reach the brain; the run would capture frames and report that "+
			"nothing was asked for.\nenv:\n%s", got)
	}
	if strings.Contains(got, "SENTINEL_OBSERVE=stream") {
		t.Errorf("the inherited value beat the explicit flag — then control-api's argv is decorative "+
			"and the surface's choice is decided by whatever the parent process happened to export.\nenv:\n%s", got)
	}

	// 2. The environment ALONE does not reach the brain. This is why control-api passes `--observe`
	// (appendRunFlags) instead of setting SENTINEL_OBSERVE on the child: the run-vars below overwrite
	// it with the empty string, unconditionally.
	t.Setenv("SENTINEL_OBSERVE", "off")
	got = brainEnvAfterRun(t, []string{"--target", target})
	if !strings.Contains(got, "SENTINEL_OBSERVE=\n") && !strings.HasSuffix(got, "SENTINEL_OBSERVE=") {
		t.Errorf("an inherited SENTINEL_OBSERVE now survives to the brain. If that is deliberate, the "+
			"comment in cmd/control-api/main.go appendRunFlags is stale and this test must be rewritten — "+
			"do not simply delete it, it records why the flag exists.\nenv:\n%s", got)
	}
}
