package store

import (
	"context"
	"path/filepath"
	"testing"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func newTest(t *testing.T) *Server {
	s, err := New(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestGoldenRoundTrip(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	if g, _ := s.GetGolden(ctx, &pb.PageKey{PageKey: "index.html"}); g.Found {
		t.Fatal("expected not found")
	}
	if _, err := s.SaveGolden(ctx, &pb.Golden{PageKey: "index.html", A11YHash: "a", ScreenshotHash: "s"}); err != nil {
		t.Fatal(err)
	}
	g, _ := s.GetGolden(ctx, &pb.PageKey{PageKey: "index.html"})
	if !g.Found || g.A11YHash != "a" || g.ScreenshotHash != "s" {
		t.Fatalf("got %+v", g)
	}
}

func TestLocatorRoundTrip(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	k := &pb.LocatorKey{PagePath: "p", SemanticId: "sid", DomSubtreeHash: "h"}
	if r, _ := s.Lookup(ctx, k); r.Found {
		t.Fatal("expected not found")
	}
	if _, err := s.SaveLocator(ctx, &pb.LocatorRecord{
		PagePath: "p", SemanticId: "sid", Strategy: "testid", Value: "{}",
		Confidence: 0.95, DomSubtreeHash: "h", Status: "active"}); err != nil {
		t.Fatal(err)
	}
	r, _ := s.Lookup(ctx, k)
	if !r.Found || r.Strategy != "testid" || r.Confidence != 0.95 {
		t.Fatalf("got %+v", r)
	}
}

func TestQuarantineAndReset(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	for i := 0; i < 3; i++ {
		s.RecordStep(ctx, &pb.StepResult{PlanId: "p", StepKey: "k", Passed: false, AutSha: "A"})
	}
	if q, _ := s.IsQuarantined(ctx, &pb.StepKey{PlanId: "p", StepKey: "k"}); !q.Quarantined {
		t.Fatal("expected quarantined after 3 fails")
	}
	q2, _ := s.RecordStep(ctx, &pb.StepResult{PlanId: "p", StepKey: "k", Passed: false, AutSha: "B"})
	if q2.Quarantined {
		t.Fatal("expected reset on aut-sha change")
	}
}
