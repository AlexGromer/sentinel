package main

import (
	"net/http"
	"testing"
)

// ADR-047 follow-on. `resolveFromRun` probes the run's OWN directory for a frozen plan, and a replay
// used to write only its report there — so a replay could never be replayed, `has_plan` was false
// forever, and the re-run control stayed grey even though the plan was perfectly well known (it had
// arrived via `from_run`). brain now freezes `executed-plan.json` into the replay's own directory.
//
// The assertions run against the real resolution path, not against the list of names: two lists of
// "what counts as a replayable plan" would drift, and the whole point of the change is that they cannot.
func TestReplayRunIsItselfReplayable(t *testing.T) {
	s := newTestServer()

	// A prior explore run: it produced a plan, so it is replayable — the precondition.
	seedPriorPlan(t, s.repo, "explore1", "plan.json", `{"plan_id":"p1","target_url":"file:///app/x.html"}`)
	if _, _, err := s.resolveFromRun("explore1"); err != nil {
		t.Fatalf("precondition: an explore run with plan.json must resolve: %v", err)
	}

	// A replay OF it, as brain now leaves it: a report and the plan it executed.
	seedPriorPlan(t, s.repo, "replay1", "heal-report.json", `{"plan_id":"p1","mode":"replay","exit_code":0}`)
	if _, _, err := s.resolveFromRun("replay1"); err == nil {
		t.Fatal("a replay carrying ONLY its report must not resolve — otherwise the test below proves nothing")
	}
	seedPriorPlan(t, s.repo, "replay1", "executed-plan.json",
		`{"plan_id":"p1","target_url":"file:///app/x.html","steps":[]}`)

	path, target, err := s.resolveFromRun("replay1")
	if err != nil {
		t.Fatalf("a replay that froze the plan it ran must be replayable: %v", err)
	}
	if path == "" {
		t.Fatal("resolved an empty plan path")
	}
	// The target travels with it, exactly as it does for an explore run — a re-run with no target would
	// otherwise have to be told one by hand.
	if target != "file:///app/x.html" {
		t.Fatalf("target_url = %q, want the plan's own target", target)
	}
}

// A run's OWN plan outranks a copy of someone else's. The order matters for a mode that could carry
// both: replaying the copy would re-run the wrong thing while looking correct.
func TestOwnPlanWinsOverTheExecutedCopy(t *testing.T) {
	s := newTestServer()
	seedPriorPlan(t, s.repo, "both", "plan.json", `{"plan_id":"mine","target_url":"file:///app/mine.html"}`)
	seedPriorPlan(t, s.repo, "both", "executed-plan.json", `{"plan_id":"theirs","target_url":"file:///app/theirs.html"}`)

	_, target, err := s.resolveFromRun("both")
	if err != nil {
		t.Fatal(err)
	}
	if target != "file:///app/mine.html" {
		t.Fatalf("target = %q — the run's own plan must win over the executed copy", target)
	}
}

// has_plan is what the UI reads, and it must follow the same resolution rather than a second opinion.
func TestHasPlanFollowsTheSameResolution(t *testing.T) {
	s := newTestServer()
	s.runs["bare"] = &run{ID: "bare", State: "done"}
	s.runs["replayed"] = &run{ID: "replayed", State: "done"}
	seedPriorPlan(t, s.repo, "replayed", "executed-plan.json", `{"plan_id":"p","target_url":"file:///app/x.html"}`)

	_, bare := doJSON(t, s, http.MethodGet, "/v1/runs/bare", nil, "secret-tok")
	if bare["has_plan"] != false {
		t.Fatalf("a run with no plan reports has_plan=%v", bare["has_plan"])
	}
	_, replayed := doJSON(t, s, http.MethodGet, "/v1/runs/replayed", nil, "secret-tok")
	if replayed["has_plan"] != true {
		t.Fatalf("a replay carrying its executed plan reports has_plan=%v — the button stays grey", replayed["has_plan"])
	}
}

// The executed copy is a replay INPUT and therefore also has to be readable as an artifact: a plan you
// can re-run but cannot look at is a poor bargain, and the whitelist is what gates artifact reads.
func TestExecutedPlanIsFetchableAsAnArtifact(t *testing.T) {
	if !artifactWhitelist["executed-plan.json"] {
		t.Fatal("executed-plan.json is a replay input but cannot be fetched — the UI cannot show what it would re-run")
	}
	// And the guard is still a whitelist, not an opening.
	for _, name := range []string{"../../etc/passwd", "control-api.token", "checkpoint.db"} {
		if artifactWhitelist[name] {
			t.Fatalf("artifact whitelist admits %q", name)
		}
	}
}
