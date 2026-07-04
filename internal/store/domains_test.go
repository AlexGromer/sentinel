package store

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// newDomServer builds a fresh SQLite-backed store-gateway for the StoreService (M13) domain tests.
func newDomServer(t *testing.T) *Server {
	t.Helper()
	os.Unsetenv("STORE_DSN")
	s, err := New(filepath.Join(t.TempDir(), "s.db"))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestRunsRoundTrip(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.UpsertRun(ctx, &pb.RunRecord{RunId: "r1", ConversationId: "c1", State: "running", Target: "http://x", Mode: "chat"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.UpsertRun(ctx, &pb.RunRecord{RunId: "r1", ConversationId: "c1", State: "done", Target: "http://x", Mode: "chat"}); err != nil {
		t.Fatal(err) // upsert updates in place
	}
	got, err := s.GetRun(ctx, &pb.RunId{RunId: "r1"})
	if err != nil {
		t.Fatal(err)
	}
	if !got.Found || got.State != "done" || got.ConversationId != "c1" {
		t.Fatalf("GetRun = %+v", got)
	}
	if miss, _ := s.GetRun(ctx, &pb.RunId{RunId: "nope"}); miss.Found {
		t.Fatal("missing run must have Found=false")
	}
	if _, err := s.UpsertRun(ctx, &pb.RunRecord{RunId: "r2", State: "running", Target: "http://y"}); err != nil {
		t.Fatal(err)
	}
	all, _ := s.ListRuns(ctx, &pb.ListRunsReq{})
	if all.Total != 2 || len(all.Runs) != 2 {
		t.Fatalf("ListRuns total=%d n=%d", all.Total, len(all.Runs))
	}
	running, _ := s.ListRuns(ctx, &pb.ListRunsReq{State: "running"})
	if running.Total != 1 || len(running.Runs) != 1 || running.Runs[0].RunId != "r2" {
		t.Fatalf("state filter: %+v", running)
	}
}

func TestScenarioPromoteTest(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.SaveScenario(ctx, &pb.Scenario{ScenarioId: "sc1", Name: "login", Target: "http://x",
		RunMode: "goal", PlanHash: "abc", StepsJson: "[]", Unmatched: 0}); err != nil {
		t.Fatal(err)
	}
	sc, _ := s.GetScenario(ctx, &pb.ScenarioId{ScenarioId: "sc1"})
	if !sc.Found || sc.PlanHash != "abc" || sc.Name != "login" {
		t.Fatalf("GetScenario=%+v", sc)
	}
	tr, err := s.PromoteTest(ctx, &pb.PromoteReq{ScenarioId: "sc1", Name: "login-test", Schedule: "@daily"})
	if err != nil {
		t.Fatal(err)
	}
	if !tr.Found || tr.PlanHash != "abc" || tr.TestId == "" || !tr.Enabled || tr.Schedule != "@daily" {
		t.Fatalf("PromoteTest=%+v (test must freeze the scenario plan_hash)", tr)
	}
	got, _ := s.GetTest(ctx, &pb.TestId{TestId: tr.TestId})
	if !got.Found || got.ScenarioId != "sc1" || got.PlanHash != "abc" || !got.Enabled {
		t.Fatalf("GetTest=%+v", got)
	}
	if miss, _ := s.PromoteTest(ctx, &pb.PromoteReq{ScenarioId: "nope"}); miss.Found {
		t.Fatal("promoting a missing scenario must be Found=false, not a crash")
	}
	if tests, _ := s.ListTests(ctx, &pb.ListTestsReq{}); tests.Total != 1 {
		t.Fatalf("ListTests total=%d", tests.Total)
	}
}

func TestChatsAndResults(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.UpsertChat(ctx, &pb.ChatProjection{ConversationId: "c1", TurnCount: 2, LastGoal: "log in",
		LastTarget: "http://x", Summary: "s"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.UpsertChat(ctx, &pb.ChatProjection{ConversationId: "c1", TurnCount: 3, LastGoal: "again"}); err != nil {
		t.Fatal(err)
	}
	c, _ := s.GetChat(ctx, &pb.ConversationId{ConversationId: "c1"})
	if !c.Found || c.TurnCount != 3 {
		t.Fatalf("GetChat=%+v (upsert must update turn_count)", c)
	}
	if chats, _ := s.ListChats(ctx, &pb.ListChatsReq{}); chats.Total != 1 {
		t.Fatalf("ListChats total=%d", chats.Total)
	}
	if _, err := s.SaveResult(ctx, &pb.ResultRecord{RunId: "r1", PlanId: "p1", Verdict: "pass",
		Healed: 2, Coverage: 0.9, DurationMs: 1234}); err != nil {
		t.Fatal(err)
	}
	res, _ := s.GetResult(ctx, &pb.RunId{RunId: "r1"})
	if !res.Found || res.Verdict != "pass" || res.Healed != 2 || res.DurationMs != 1234 {
		t.Fatalf("GetResult=%+v", res)
	}
}

func TestMetricsIngestQueryTrends(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.IngestMetrics(ctx, &pb.MetricsBatch{Points: []*pb.MetricPoint{
		{RunId: "r1", Ts: 1, Name: "coverage", Value: 0.5},
		{RunId: "r2", Ts: 2, Name: "coverage", Value: 0.8},
		{RunId: "r1", Ts: 1, Name: "healed", Value: 3},
	}}); err != nil {
		t.Fatal(err)
	}
	q, _ := s.QueryMetrics(ctx, &pb.MetricsQuery{Name: "coverage"})
	if len(q.Points) != 2 || q.Points[0].Value != 0.5 || q.Points[1].Value != 0.8 {
		t.Fatalf("QueryMetrics coverage (chronological) = %+v", q.Points)
	}
	win, _ := s.QueryMetrics(ctx, &pb.MetricsQuery{Name: "coverage", SinceTs: 2})
	if len(win.Points) != 1 || win.Points[0].RunId != "r2" {
		t.Fatalf("QueryMetrics since-window = %+v", win.Points)
	}
	tr, _ := s.Trends(ctx, &pb.TrendReq{Metric: "coverage", Window: 10})
	if len(tr.Points) != 2 || tr.Points[0].Ts != 1 || tr.Points[1].Ts != 2 {
		t.Fatalf("Trends (chronological) = %+v", tr.Points)
	}
}

func TestStoreDSNScaffoldRefuses(t *testing.T) {
	t.Setenv("STORE_DSN", "postgres://user@host/db")
	if _, err := New(filepath.Join(t.TempDir(), "s.db")); err == nil {
		t.Fatal("STORE_DSN set must refuse to start (Postgres deferred to M13-service)")
	}
}
