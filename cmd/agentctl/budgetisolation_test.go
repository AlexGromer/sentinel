package main

import (
	"path/filepath"
	"strings"
	"testing"
)

// ADR-148. The learned token ceiling in brain/llm.py is shared across runs deliberately — it pays the
// escalation once rather than on every run. That is a feature for WORKING and a defect for MEASURING:
// two runs of the same prompt go out with different max_tokens depending on what some earlier run
// learned. Measured on this repository: state/llm-budget.json held {"qwen3:8b": 16384} — the hard
// maximum — written on 2026-08-16 and applied silently to every qwen3:8b run for weeks after.
//
// `--isolate-llm-budget` keeps the ceiling inside the run's own artifact dir. The property under test
// is what the spawned brain's ENVIRONMENT carries, which is why it is asserted on the slice agentctl
// builds rather than on the flag parser.

func TestBudgetIsolationOffByDefault(t *testing.T) {
	base := []string{"RUN_MODE=explore", "ARTIFACT_DIR=/tmp/run-1"}
	got := appendBudgetIsolation(base, "/tmp/run-1", false)

	if len(got) != len(base) {
		t.Fatalf("without the flag the env grew by %d entries: %v", len(got)-len(base), got[len(base):])
	}
	// ⚠ THE POINT OF THIS TEST, and the bug it was written after. The first implementation emitted
	// `SENTINEL_LLM_BUDGET_FILE=""` in this case, reasoning that an empty value is falsy in
	// brain/llm.py so every run's env would keep the same shape. But `extra` is appended AFTER
	// filteredEnv(), so it WINS — an empty value would have overwritten a path the operator set
	// themselves. scripts/model_convergence.py sets exactly that path, per attempt, and its whole
	// purpose is this isolation (tests/test_model_convergence_offline.py asserts it). The tidier
	// version would have silently un-isolated the one harness already doing this right.
	for _, kv := range got {
		if strings.HasPrefix(kv, "SENTINEL_LLM_BUDGET_FILE=") {
			t.Errorf("the non-isolated path still emits %q — it would overwrite an operator's own value", kv)
		}
	}
}

func TestBudgetIsolationPointsIntoTheRunDir(t *testing.T) {
	dir := t.TempDir()
	got := appendBudgetIsolation([]string{"RUN_MODE=explore"}, dir, true)

	var val string
	for _, kv := range got {
		if strings.HasPrefix(kv, "SENTINEL_LLM_BUDGET_FILE=") {
			val = strings.TrimPrefix(kv, "SENTINEL_LLM_BUDGET_FILE=")
		}
	}
	if val == "" {
		t.Fatalf("the flag was set and no SENTINEL_LLM_BUDGET_FILE was emitted: %v", got)
	}
	// Inside the run's OWN directory: a path anywhere else is shared with something, which is the
	// state the flag exists to leave.
	if filepath.Dir(val) != dir {
		t.Errorf("the ceiling file is at %q, outside the run dir %q — still shared with somebody", val, dir)
	}
	// Collected with the run's other artifacts, so the ceiling a measurement used travels with the
	// measurement instead of having to be reconstructed.
	if filepath.Base(val) != "llm-budget.json" {
		t.Errorf("unexpected file name %q — brain/llm.py and the collector both expect llm-budget.json", filepath.Base(val))
	}
	// The variable must be the ONLY thing added: this helper has no business touching the rest.
	if len(got) != 2 {
		t.Errorf("the helper added %d entries, want exactly one: %v", len(got)-1, got)
	}
}

// TestBudgetIsolationIsReachableByTheBrain guards the plumbing the two tests above assume: agentctl
// filters the environment it hands the brain (ADR-035), so a variable outside the allowlist would be
// built correctly here and then dropped on the way. `SENTINEL_` is a prefix in that allowlist, and
// this asserts it rather than trusting it — the failure mode is silent, and it looks exactly like
// "the flag does nothing".
func TestBudgetIsolationIsReachableByTheBrain(t *testing.T) {
	t.Setenv("SENTINEL_LLM_BUDGET_FILE", "/tmp/probe/llm-budget.json")
	var found bool
	for _, kv := range filteredEnv() {
		if strings.HasPrefix(kv, "SENTINEL_LLM_BUDGET_FILE=") {
			found = true
		}
	}
	if !found {
		t.Error("SENTINEL_LLM_BUDGET_FILE does not survive filteredEnv() — the flag would be built and then dropped")
	}
}
