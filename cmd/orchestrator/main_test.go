package main

import (
	"context"
	"testing"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

// M9.8 F4 (ADR-054): Takeover arms a per-run pause that ReportEvent surfaces (Control.takeover); Return
// clears it. abort (budget breach / external) takes precedence over a pending takeover.

func ctrl(t *testing.T, o *orchestrator, run string) *pb.Control {
	t.Helper()
	c, err := o.ReportEvent(context.Background(), &pb.RunEvent{RunId: run, Node: "plan"})
	if err != nil {
		t.Fatalf("ReportEvent: %v", err)
	}
	return c
}

func TestTakeoverReturnFlow(t *testing.T) {
	o := newOrchestrator(0, 0, 0) // no budget gates -> never breaches on its own
	ctx := context.Background()
	const run = "r1"

	if c := ctrl(t, o, run); c.Abort || c.Takeover {
		t.Fatalf("baseline: want continue, got abort=%v takeover=%v", c.Abort, c.Takeover)
	}
	if _, err := o.Takeover(ctx, &pb.TakeoverRequest{RunId: run, Reason: "op"}); err != nil {
		t.Fatal(err)
	}
	if c := ctrl(t, o, run); c.Abort || !c.Takeover {
		t.Fatalf("after Takeover: want takeover, got abort=%v takeover=%v", c.Abort, c.Takeover)
	}
	if c := ctrl(t, o, run); !c.Takeover {
		t.Fatal("takeover must remain pending until Return (idempotent across polls)")
	}
	if _, err := o.Return(ctx, &pb.ReturnRequest{RunId: run}); err != nil {
		t.Fatal(err)
	}
	if c := ctrl(t, o, run); c.Abort || c.Takeover {
		t.Fatalf("after Return: want continue, got abort=%v takeover=%v", c.Abort, c.Takeover)
	}
}

func TestAbortPrecedenceOverTakeover(t *testing.T) {
	o := newOrchestrator(100, 0, 0) // plan budget 100
	ctx := context.Background()
	const run = "r2"

	if _, err := o.Takeover(ctx, &pb.TakeoverRequest{RunId: run}); err != nil {
		t.Fatal(err)
	}
	// Spend past the plan budget in the same event -> breach. abort must win over the pending takeover,
	// and Control.takeover must NOT be set alongside abort (a hard stop is unambiguous).
	c, err := o.ReportEvent(ctx, &pb.RunEvent{RunId: run, Node: "plan", PromptTokens: 150})
	if err != nil {
		t.Fatal(err)
	}
	if !c.Abort {
		t.Fatalf("want abort on budget breach, got abort=%v takeover=%v", c.Abort, c.Takeover)
	}
	if c.Takeover {
		t.Fatal("abort takes precedence: takeover must not be set alongside abort")
	}
}

func TestTakeoverPerRunIsolation(t *testing.T) {
	o := newOrchestrator(0, 0, 0)
	if _, err := o.Takeover(context.Background(), &pb.TakeoverRequest{RunId: "a"}); err != nil {
		t.Fatal(err)
	}
	if c := ctrl(t, o, "a"); !c.Takeover {
		t.Fatal("run a should be in takeover")
	}
	if c := ctrl(t, o, "b"); c.Takeover {
		t.Fatal("run b must be unaffected (per-run takeover isolation)")
	}
}

// --- ADR-108c: the map gate's answer channel ----------------------------------------------------

// TestMapDecisionReachesTheBrain — found by a SURVIVING mutation.
//
// The brain learns of the operator's answer through Control.map_decision on its next ReportEvent, the
// same shape as takeover. Nothing exercised that: dropping the field from the reply left every test
// passing, and the gate would simply have waited forever while a person believed they had answered.
func TestMapDecisionReachesTheBrain(t *testing.T) {
	o := newOrchestrator(0, 0, 0)
	ctx := context.Background()
	const run = "r-map"

	// Empty until somebody answers — and empty is what makes the gate WAIT rather than proceed.
	if c := ctrl(t, o, run); c.MapDecision != "" {
		t.Fatalf("baseline map_decision = %q, want empty (nobody has answered)", c.MapDecision)
	}
	if _, err := o.DecideMap(ctx, &pb.MapDecisionRequest{RunId: run, Decision: "approve", Reason: "looks fine"}); err != nil {
		t.Fatal(err)
	}
	c := ctrl(t, o, run)
	if c.MapDecision != "approve" {
		t.Fatalf("after DecideMap(approve): map_decision = %q", c.MapDecision)
	}
	if c.Reason != "looks fine" {
		t.Errorf("the person's reason did not travel: %q", c.Reason)
	}
	// Carried on EVERY reply, not only the first: the brain polls while it waits, and an answer that
	// appeared once would be a race it could not see through.
	if c2 := ctrl(t, o, run); c2.MapDecision != "approve" {
		t.Fatalf("the decision did not persist across polls: %q", c2.MapDecision)
	}
	// A refusal replaces it.
	if _, err := o.DecideMap(ctx, &pb.MapDecisionRequest{RunId: run, Decision: "reject"}); err != nil {
		t.Fatal(err)
	}
	if c3 := ctrl(t, o, run); c3.MapDecision != "reject" {
		t.Fatalf("after DecideMap(reject): map_decision = %q", c3.MapDecision)
	}
}

// TestMapDecisionRefusesAnUnknownAnswer: a typo stored verbatim would reach the brain as neither
// approve nor reject, leaving the run waiting on a gate somebody believes they answered.
func TestMapDecisionRefusesAnUnknownAnswer(t *testing.T) {
	o := newOrchestrator(0, 0, 0)
	ctx := context.Background()
	for _, bad := range []string{"", "aprove", "yes", "APPROVE", "reject "} {
		if _, err := o.DecideMap(ctx, &pb.MapDecisionRequest{RunId: "r-bad", Decision: bad}); err == nil {
			t.Errorf("DecideMap(%q) was accepted; it must be refused", bad)
		}
	}
	if c := ctrl(t, o, "r-bad"); c.MapDecision != "" {
		t.Fatalf("a refused answer was stored anyway: %q", c.MapDecision)
	}
}

// TestMapDecisionIsPerRun: two runs waiting at their gates must not answer for each other.
func TestMapDecisionIsPerRun(t *testing.T) {
	o := newOrchestrator(0, 0, 0)
	if _, err := o.DecideMap(context.Background(), &pb.MapDecisionRequest{RunId: "a", Decision: "approve"}); err != nil {
		t.Fatal(err)
	}
	if c := ctrl(t, o, "b"); c.MapDecision != "" {
		t.Fatalf("run b saw run a's answer: %q", c.MapDecision)
	}
}
