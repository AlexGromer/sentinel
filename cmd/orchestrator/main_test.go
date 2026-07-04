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
