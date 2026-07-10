package store

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

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

// TestDeleteScenarioTestChat covers the M14 wave W3 library-management RPCs: delete is idempotent
// (deleting a missing id is success, not an error) and only removes the targeted row.
func TestDeleteScenarioTestChat(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()

	if _, err := s.SaveScenario(ctx, &pb.Scenario{ScenarioId: "sc1", Name: "login", Target: "http://x", PlanHash: "abc"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SaveScenario(ctx, &pb.Scenario{ScenarioId: "sc2", Name: "logout", Target: "http://x", PlanHash: "def"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DeleteScenario(ctx, &pb.ScenarioId{ScenarioId: "sc1"}); err != nil {
		t.Fatal(err)
	}
	if got, _ := s.GetScenario(ctx, &pb.ScenarioId{ScenarioId: "sc1"}); got.Found {
		t.Fatal("DeleteScenario: sc1 still found")
	}
	if got, _ := s.GetScenario(ctx, &pb.ScenarioId{ScenarioId: "sc2"}); !got.Found {
		t.Fatal("DeleteScenario: sc2 (untouched) must still be found")
	}
	if _, err := s.DeleteScenario(ctx, &pb.ScenarioId{ScenarioId: "nope"}); err != nil {
		t.Fatalf("DeleteScenario of a missing id must be idempotent success, got %v", err)
	}

	tr, err := s.PromoteTest(ctx, &pb.PromoteReq{ScenarioId: "sc2", Name: "logout-test"})
	if err != nil || !tr.Found {
		t.Fatalf("PromoteTest = %+v, err=%v", tr, err)
	}
	if _, err := s.DeleteTest(ctx, &pb.TestId{TestId: tr.TestId}); err != nil {
		t.Fatal(err)
	}
	if got, _ := s.GetTest(ctx, &pb.TestId{TestId: tr.TestId}); got.Found {
		t.Fatal("DeleteTest: test still found")
	}
	if _, err := s.DeleteTest(ctx, &pb.TestId{TestId: "nope"}); err != nil {
		t.Fatalf("DeleteTest of a missing id must be idempotent success, got %v", err)
	}

	if _, err := s.UpsertChat(ctx, &pb.ChatProjection{ConversationId: "c1", TurnCount: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DeleteChat(ctx, &pb.ConversationId{ConversationId: "c1"}); err != nil {
		t.Fatal(err)
	}
	if got, _ := s.GetChat(ctx, &pb.ConversationId{ConversationId: "c1"}); got.Found {
		t.Fatal("DeleteChat: chat still found")
	}
	if _, err := s.DeleteChat(ctx, &pb.ConversationId{ConversationId: "nope"}); err != nil {
		t.Fatalf("DeleteChat of a missing id must be idempotent success, got %v", err)
	}
}

func TestStoreDSNScaffoldRefuses(t *testing.T) {
	t.Setenv("STORE_DSN", "postgres://user@host/db")
	if _, err := New(filepath.Join(t.TempDir(), "s.db")); err == nil {
		t.Fatal("STORE_DSN set must refuse to start (Postgres deferred to M13-service)")
	}
}

// --- config domain (M11.5 PR-5, ADR-062) ------------------------------------

func TestConfigRoundTripAndUpsert(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	doc := `{"llm":{"backend":"openai","base_url":"http://ollama:11434/v1"},"run":{"max_steps":40}}`
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: doc}); err != nil {
		t.Fatalf("PutConfig: %v", err)
	}
	got, err := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"})
	if err != nil {
		t.Fatal(err)
	}
	if !got.Found || got.ValueJson != doc {
		t.Fatalf("round-trip mismatch: found=%v value=%q", got.Found, got.ValueJson)
	}
	if got.UpdatedAt == "" {
		t.Fatal("updated_at must be defaulted on write")
	}

	// upsert: same key, new document -> one row, new value
	doc2 := `{"llm":{"backend":"anthropic"}}`
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: doc2}); err != nil {
		t.Fatalf("PutConfig upsert: %v", err)
	}
	got, _ = s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"})
	if got.ValueJson != doc2 {
		t.Fatalf("upsert did not replace: %q", got.ValueJson)
	}
	lst, err := s.ListConfig(ctx, &pb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(lst.Items) != 1 {
		t.Fatalf("upsert must not insert a second row; got %d", len(lst.Items))
	}

	// missing key -> Found=false, not an error
	miss, err := s.GetConfig(ctx, &pb.ConfigKey{Key: "nope"})
	if err != nil || miss.Found {
		t.Fatalf("missing key: err=%v found=%v", err, miss.Found)
	}
	// delete is idempotent
	if _, err := s.DeleteConfig(ctx, &pb.ConfigKey{Key: "setup"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DeleteConfig(ctx, &pb.ConfigKey{Key: "setup"}); err != nil {
		t.Fatalf("delete must be idempotent: %v", err)
	}
}

// The guard is the whole point of the domain: a wizard must never be able to park a credential here.
func TestConfigRejectsSecrets(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	bad := []struct{ name, doc string }{
		{"top-level api_key", `{"api_key":"sk-live-1"}`},
		{"nested api_key", `{"llm":{"backend":"openai","api_key":"sk-live-2"}}`},
		{"uppercase env name", `{"LLM_API_KEY":"sk-live-3"}`},
		{"anthropic env name", `{"ANTHROPIC_API_KEY":"sk-ant-1"}`},
		{"bare key", `{"key":"sk-live-4"}`},
		{"bearer token", `{"control":{"bearer_token":"t"}}`},
		{"exact token", `{"token":"t"}`},
		{"password", `{"auth":{"password":"hunter2"}}`},
		{"secret", `{"client_secret":"s"}`},
		{"inside an array", `{"backends":[{"name":"a"},{"apikey":"sk-live-5"}]}`},
		{"deeply nested", `{"a":{"b":{"c":{"private_key":"pk"}}}}`},
		// a bare JSON string has no member names — it must not slip past a name-based guard
		{"bare JSON string", `"sk-live-6"`},
		{"JSON array document", `["sk-live-7"]`},
		{"not JSON at all", `sk-live-8`},
	}
	for _, tc := range bad {
		_, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: tc.doc})
		if err == nil {
			t.Errorf("%s: PutConfig must refuse %q", tc.name, tc.doc)
			continue
		}
		if status.Code(err) != codes.InvalidArgument {
			t.Errorf("%s: want InvalidArgument, got %v (%v)", tc.name, status.Code(err), err)
		}
	}
	// nothing was written by any rejected call
	lst, _ := s.ListConfig(ctx, &pb.Empty{})
	if len(lst.Items) != 0 {
		t.Fatalf("rejected documents must not be persisted; got %d rows", len(lst.Items))
	}

	// counters that merely CONTAIN "token" are legitimate and must pass
	good := `{"llm":{"max_tokens":4096,"total_tokens":0},"run":{"plan_budget":50000}}`
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: good}); err != nil {
		t.Fatalf("max_tokens/total_tokens must not be treated as secrets: %v", err)
	}
	// an empty key is a client bug, not a silent no-op
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "", ValueJson: `{}`}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty key must be InvalidArgument, got %v", err)
	}
}

// The size bound is enforced at the GATEWAY, not only the HTTP layer — the gateway is the trust
// boundary reachable by any STORE_TOKEN holder, so a direct gRPC client cannot exceed it either.
func TestConfigGatewayRejectsOversized(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	pad := make([]byte, 64*1024+1024)
	for i := range pad {
		pad[i] = 'a'
	}
	doc := `{"pad":"` + string(pad) + `"}`
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: doc}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("oversized document at the gateway must be InvalidArgument, got %v", err)
	}
	lst, _ := s.ListConfig(ctx, &pb.Empty{})
	if len(lst.Items) != 0 {
		t.Fatalf("an oversized document must not be persisted; got %d rows", len(lst.Items))
	}
}
