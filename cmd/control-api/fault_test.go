package main

// HEALTH-004 — the gate on WHOSE PROBLEM a finished run was.
//
// What this asserts is DISCRIMINATION, not shape. The defect it exists against was not a missing
// field; it was three genuinely different endings arriving at the reader as one word:
//
//	exit 1  a step failed          -> "problem"   (go and debug your application)
//	exit 4  our own code threw     -> "problem"   (go and debug your application — but it was us)
//	exit -1 we were killed         -> "problem"   (go and debug your application — but nothing ran)
//
// and one ending arriving as an actively wrong sentence: HEALTH-001 gave a refusal-to-start exit 3,
// exit 3 already meant `integrity`, and the hub therefore told an operator whose model endpoint was
// down to go and look at a plan_hash. Every test below is a pair that used to be indistinguishable.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFaultDomainTellsApartTheThingsExitCodeCannot(t *testing.T) {
	// Each case names the pair it breaks, because a table of expectations with no stated adversary is
	// a test that passes rather than a test that defends something.
	cases := []struct {
		name, state  string
		exit         int
		terminalCode string
		want         string
	}{
		{"a step failed — the application's problem, and a RESULT", "done", 1, "", "app"},
		{"our own code threw (ADR-087 exit 4) — ours, and NOT a finding", "done", 4, "fatal.internal_error", "tool"},
		{"our own code threw and the log was unreadable — still ours, from the exit code alone",
			"done", 4, "", "tool"},
		{"killed by a signal — nothing about the application was learned", "done", -1, "", "tool"},
		{"could not be spawned at all — the application was never contacted", "failed", -1, "", "tool"},
		{"stopped by a human — not a failure and must not be filed as one", "canceled", -1, "", "none"},
		{"a clean pass harms nobody", "done", 0, "", "none"},
		{"golden regression — the page changed under a baseline we recorded", "done", 2, "", "app"},

		// The pair the screenshot was taken of. Same exit code, opposite answers.
		{"exit 3 because OUR model endpoint is unreachable", "done", 3, "fatal.llm_required_unreachable", "tool"},
		{"exit 3 because OUR store was declared and does not answer", "done", 3, "fatal.store_unreachable", "tool"},
		{"exit 3 because the plan file is corrupt — the test's own material", "done", 3, "fatal.plan_unparseable", "test"},
		{"exit 3 because the run was asked for wrongly", "done", 3, "fatal.goal_describe_conflict", "config"},
		{"exit 3 with no terminal code — falls back to the catalogue's reading of 3", "done", 3, "", "test"},

		// Measured live 2026-08-04. A goal run whose model endpoint answered 404 authored zero steps
		// and exited 1; the product said `plan.scenario_error_empty` with the 404 quoted, and the
		// verdict said `app`. Exit 1 alone means "the test found a problem in the application", so the
		// coarse reading actively contradicted what the run had just reported about itself.
		{"authoring produced nothing because OUR endpoint failed", "done", 1, "plan.scenario_error_empty", "tool"},
		{"authoring produced nothing because OUR budget ran out", "done", 1, "plan.scenario_budget_empty", "tool"},
		{"the model's output could not be parsed — ours", "done", 1, "plan.output_unparseable", "tool"},

		// The other side of the same coin: those codes describe a degradation a run can SURVIVE, so
		// a green ending must not inherit a blame chip from one that fired along the way.
		{"a run that recovered and passed carries no fault", "done", 0, "plan.scenario_error_empty", "none"},
		{"a green run with an earlier tool degradation is still nobody's fault", "done", 0, "llm.no_anthropic_key", "none"},
	}
	for _, c := range cases {
		if got := faultDomain(c.state, c.exit, c.terminalCode); got != c.want {
			t.Errorf("%s: faultDomain(%q, %d, %q) = %q, want %q",
				c.name, c.state, c.exit, c.terminalCode, got, c.want)
		}
	}
}

func TestTheTerminalCodeOutranksTheExitCode(t *testing.T) {
	// The ordering IS the design: the exit code is a lossy summary and the code that ended the run is
	// the witness. Written as its own test because a reordering inside faultDomain would still satisfy
	// most rows of the table above — exit 3's fallback is `test`, so only a `tool`/`config` terminal
	// code proves the precedence.
	if got := faultDomain("done", 3, "fatal.llm_required_unreachable"); got != "tool" {
		t.Fatalf("the terminal code lost to the exit code: got %q, want tool", got)
	}
	if got := faultDomain("done", 1, "fatal.internal_error"); got != "tool" {
		t.Fatalf("exit 1 with an internal-error code must be ours, got %q", got)
	}
}

func TestAnUndeclaredExitCodeIsNotGuessedAt(t *testing.T) {
	// Empty, not "tool". An exit nobody declared is itself the finding, and inventing an owner for it
	// re-creates the guessing this axis removes — quietly, which is worse than the original.
	if got := faultDomain("done", 99, ""); got != "" {
		t.Fatalf("exit 99 was attributed to %q; the catalogue never declared it", got)
	}
}

func TestASpawnFailureIsOursEvenThoughItsExitCodeReadsZero(t *testing.T) {
	// The trap this defends: a run that never started leaves ExitCode at Go's zero value. Asking the
	// catalogue what exit 0 means would answer `none` — a run that never ran, filed as harming nobody,
	// on the state the operator most needs to see.
	if got := faultDomain("failed", 0, ""); got != "tool" {
		t.Fatalf("a failed spawn was attributed to %q, want tool", got)
	}
}

func TestTheSinkRemembersTheLastTerminalCodeAndPutsItOnTheRecord(t *testing.T) {
	// The other half of the wiring: faultDomain is only as good as the code handed to it. Driven
	// through the SHIPPED sink rather than a copy of its parsing — an extracted copy is a test of the
	// copy, and this project has been burned by exactly that.
	dir := t.TempDir()
	s := newLogSink(dir)
	if s == nil {
		t.Fatal("could not create a log sink")
	}
	// Two terminal codes in one run: the LAST is what decided the ending. import.files_skipped is a
	// real case of a terminal-class code a run survives — it keeps importing after one.
	s.write("[error|plan] import.files_skipped: Import: 1 file(s) were NOT read")
	s.write("[info|run] run.started: starting")
	s.write("[error|llm] fatal.llm_required_unreachable: This mode needs a model and there is none: no backend")
	s.close()

	code, fault := s.terminalFault()
	if code != "fatal.llm_required_unreachable" || fault != "tool" {
		t.Fatalf("terminalFault() = (%q, %q), want (fatal.llm_required_unreachable, tool) — LAST wins", code, fault)
	}

	// And it is on the line itself, so run.jsonl stays self-describing for whoever greps it later.
	raw, err := os.ReadFile(filepath.Join(dir, "logs", "run.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"fault":"tool"`) {
		t.Errorf("no fault on any record in run.jsonl:\n%s", raw)
	}
	// A record that cannot end a run must NOT carry one — otherwise the field stops meaning "terminal"
	// and the reader loses the only signal that separates an ending from a remark.
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if strings.Contains(line, `"code":"run.started"`) && strings.Contains(line, `"fault"`) {
			t.Errorf("run.started carries a fault — the field must mark endings, not every line: %s", line)
		}
	}
}
