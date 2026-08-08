package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/grpc"

	"github.com/AlexGromer/sentinel/internal/store"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// startTestGateway runs a real store-gateway (StoreService, SQLite) on a unix socket and returns its
// gRPC target. Token-authed when token != "" (mirrors the production interceptor).
func startTestGateway(t *testing.T, token string) string {
	t.Helper()
	os.Unsetenv("STORE_DSN")
	sock := filepath.Join(t.TempDir(), "store.sock")
	lis, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := store.New(filepath.Join(t.TempDir(), "s.db"))
	if err != nil {
		t.Fatal(err)
	}
	var opts []grpc.ServerOption
	if token != "" {
		opts = append(opts, grpc.ChainUnaryInterceptor(store.TokenAuthInterceptor(token)))
	}
	g := grpc.NewServer(opts...)
	storepb.RegisterStoreServiceServer(g, srv)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(func() { g.Stop(); _ = srv.Close() })
	return "unix:" + sock
}

func TestControlAPIStorePersistsAndSurvivesRestart(t *testing.T) {
	const token = "sekret-store-tok"
	sc, err := newStoreClient(startTestGateway(t, token), token)
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()

	r := &run{ID: "run1", State: "running", Target: "http://x", Mode: "chat", ConversationID: "conv1",
		ArtifactDir: "/tmp/a", StartedAt: "2026-07-04T00:00:00Z"}
	sc.upsertRun(r) // running
	r.State, r.ExitCode, r.FinishedAt = "done", 0, "2026-07-04T00:01:00Z"
	sc.upsertRun(r) // terminal — upsert updates in place

	got, found := sc.getRun("run1")
	if !found || got.State != "done" || got.ConversationID != "conv1" || got.Mode != "chat" {
		t.Fatalf("getRun = %+v found=%v (conversation_id must persist for the runs<->chats join)", got, found)
	}
	runs, ok := sc.listRuns("")
	if !ok || len(runs) != 1 || runs[0].ID != "run1" {
		t.Fatalf("listRuns = %+v ok=%v", runs, ok)
	}

	// Restart survival: a FRESH control-API server (empty in-memory map) still serves the run from the
	// gateway via handleGetRun — the whole point of M13's runs domain.
	s := &server{store: sc, runs: map[string]*run{}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/run1", nil)
	req.SetPathValue("id", "run1")
	s.handleGetRun(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("handleGetRun after restart: got %d want 200", rec.Code)
	}
	var body run
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.ID != "run1" || body.State != "done" || body.ConversationID != "conv1" {
		t.Fatalf("restart GetRun body=%+v", body)
	}
}

func TestControlAPINoStoreStaysInMemory(t *testing.T) {
	// store nil (standalone/offline): unknown run 404s, no gateway calls — backward-compatible.
	s := &server{runs: map[string]*run{}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/nope", nil)
	req.SetPathValue("id", "nope")
	s.handleGetRun(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("no-store unknown run: got %d want 404", rec.Code)
	}
}

// TestConcurrentRunReadDuringMutationNoRace exercises the M13 verify fix: handleGetRun/handleListRuns
// must snapshot a live run's mutable fields UNDER s.mu, not marshal them after releasing the lock while
// the completion goroutine writes them. Run under -race: the pre-fix code (marshal after RUnlock) trips
// the detector; the snapshot-under-lock version is clean.
func TestConcurrentRunReadDuringMutationNoRace(t *testing.T) {
	s := &server{runs: map[string]*run{}}
	rec := &run{ID: "r", State: "running", stream: newRunStream()}
	s.mu.Lock()
	s.runs["r"] = rec
	s.mu.Unlock()

	done := make(chan struct{})
	go func() { // mimic spawnRun's completion goroutine writing mutable fields under s.mu
		for i := 0; i < 500; i++ {
			s.mu.Lock()
			rec.State, rec.ExitCode, rec.FinishedAt, rec.Error = "done", i, "2026-07-04T00:00:00Z", "e"
			s.mu.Unlock()
		}
		close(done)
	}()
	for i := 0; i < 500; i++ { // concurrent reads must be race-free (snapshot under the lock)
		g := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/v1/runs/r", nil)
		req.SetPathValue("id", "r")
		s.handleGetRun(g, req)
		s.handleListRuns(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/runs", nil))
	}
	<-done
}

func TestStoreClientReportsAnUnreachableGatewayAndKeepsTheClient(t *testing.T) {
	// ⚠ HEALTH-006 changed the second half of this claim. The probe still SAYS the gateway did not
	// answer — that is what main() journals and what the operator sees. What it no longer does is
	// throw the client away: measured, grpc.ClientConn heals on its own (kill the gateway, /readyz
	// store goes error; bring it back, it goes ok, with no restart of this process). Discarding the
	// client here was the thing that made a boot miss PERMANENT, and keeping it is what lets a
	// gateway started one second later be used without a restart.
	sc, err := newStoreClient("unix:/nonexistent/definitely-not-a-store.sock", "")
	if err == nil {
		t.Fatal("a dead socket must be REPORTED — silence here is what leaves an operator guessing")
	}
	if sc == nil {
		t.Fatal("the client was discarded, so a gateway that comes up later stays invisible until a restart")
	}
	sc.close()
}

// --- M14 wave W3: scenarios/tests/chats HTTP surface + scenario-persist-on-finish ------------------

// storeBackedTestServer is newTestServer() (main_test.go) wired to a real store-gateway.
func storeBackedTestServer(sc *storeClient) *server {
	s := newTestServer()
	s.store = sc
	return s
}

// doJSON drives a request through the real mux (route registration + s.authed gating, like the rest
// of the suite) and decodes the JSON body into a map for loose field assertions.
func doJSON(t *testing.T, s *server, method, path string, body []byte, token string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var req *http.Request
	if body != nil {
		req = httptest.NewRequest(method, path, bytes.NewReader(body))
	} else {
		req = httptest.NewRequest(method, path, nil)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out) // best-effort: some responses (e.g. 403) still decode fine
	return rec, out
}

func TestScenariosHTTPRoundTrip(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	sc.saveScenario(&storepb.Scenario{ScenarioId: "scn1", Name: "https://x", Target: "https://x",
		RunMode: "goal", PlanHash: "hash-abc", SourceRunId: "run1"})

	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/scenarios", nil, ""); rec.Code != http.StatusForbidden {
		t.Fatalf("list scenarios without token: got %d want 403", rec.Code)
	}

	rec, body := doJSON(t, s, http.MethodGet, "/v1/scenarios", nil, s.token)
	scenarios, _ := body["scenarios"].([]any)
	if rec.Code != http.StatusOK || len(scenarios) != 1 {
		t.Fatalf("list scenarios: code=%d body=%v", rec.Code, body)
	}

	rec, body = doJSON(t, s, http.MethodGet, "/v1/scenarios/scn1", nil, s.token)
	if rec.Code != http.StatusOK || body["plan_hash"] != "hash-abc" || body["source_run_id"] != "run1" {
		t.Fatalf("get scenario: code=%d body=%v", rec.Code, body)
	}

	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/scenarios/nope", nil, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("get missing scenario: got %d want 404", rec.Code)
	}

	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/scenarios/scn1", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete scenario: got %d want 200", rec.Code)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/scenarios", nil, s.token)
	if scenarios, _ := body["scenarios"].([]any); rec.Code != http.StatusOK || len(scenarios) != 0 {
		t.Fatalf("scenarios after delete: code=%d body=%v", rec.Code, body)
	}
	// delete-nonexistent is idempotent success, not an error
	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/scenarios/nope", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete nonexistent scenario: got %d want 200", rec.Code)
	}
}

func TestTestsPromoteAndHTTPRoundTrip(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	sc.saveScenario(&storepb.Scenario{ScenarioId: "scn2", Name: "https://y", Target: "https://y",
		RunMode: "describe", PlanHash: "frozen-hash-1"})

	promoteBody, _ := json.Marshal(map[string]string{"scenario_id": "scn2", "name": "nightly smoke"})
	rec, body := doJSON(t, s, http.MethodPost, "/v1/tests/promote", promoteBody, s.token)
	if rec.Code != http.StatusOK {
		t.Fatalf("promote: got %d body=%v", rec.Code, body)
	}
	testID, _ := body["test_id"].(string)
	if testID == "" || body["plan_hash"] != "frozen-hash-1" || body["name"] != "nightly smoke" {
		t.Fatalf("promote body: %v", body)
	}

	// the scenario's plan_hash changes AFTER promotion — the test's frozen plan_hash must NOT follow it
	// (ADR-052: test = scenario + FROZEN plan_hash).
	sc.saveScenario(&storepb.Scenario{ScenarioId: "scn2", Name: "https://y", Target: "https://y",
		RunMode: "describe", PlanHash: "mutated-hash-2"})

	rec, body = doJSON(t, s, http.MethodGet, "/v1/tests", nil, s.token)
	tests, _ := body["tests"].([]any)
	if rec.Code != http.StatusOK || len(tests) != 1 {
		t.Fatalf("list tests: code=%d body=%v", rec.Code, body)
	}

	rec, body = doJSON(t, s, http.MethodGet, "/v1/tests/"+testID, nil, s.token)
	if rec.Code != http.StatusOK || body["plan_hash"] != "frozen-hash-1" {
		t.Fatalf("get test after scenario mutation: code=%d body=%v (plan_hash must stay frozen)", rec.Code, body)
	}

	// promoting an unknown scenario is 404-ish, never a 503 (M14_CONTRACT.md §3)
	badPromote, _ := json.Marshal(map[string]string{"scenario_id": "does-not-exist"})
	if rec, _ := doJSON(t, s, http.MethodPost, "/v1/tests/promote", badPromote, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("promote unknown scenario: got %d want 404", rec.Code)
	}

	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/tests/"+testID, nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete test: got %d want 200", rec.Code)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/tests", nil, s.token)
	if tests, _ := body["tests"].([]any); rec.Code != http.StatusOK || len(tests) != 0 {
		t.Fatalf("tests after delete: code=%d body=%v", rec.Code, body)
	}
	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/tests/"+testID, nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete nonexistent test: got %d want 200", rec.Code)
	}
}

func TestChatsHTTPRoundTrip(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	// UpsertChat has no control-api wrapper (nothing calls it yet — GAP-M9-20); seed via the raw client.
	if _, err := sc.cl.UpsertChat(context.Background(), &storepb.ChatProjection{
		ConversationId: "conv1", LastTarget: "https://z", TurnCount: 3, LastGoal: "smoke test"}); err != nil {
		t.Fatal(err)
	}

	rec, body := doJSON(t, s, http.MethodGet, "/v1/chats", nil, s.token)
	chats, _ := body["chats"].([]any)
	if rec.Code != http.StatusOK || len(chats) != 1 {
		t.Fatalf("list chats: code=%d body=%v", rec.Code, body)
	}

	rec, body = doJSON(t, s, http.MethodGet, "/v1/chats/conv1", nil, s.token)
	if rec.Code != http.StatusOK || body["last_target"] != "https://z" || body["turn_count"] != float64(3) {
		t.Fatalf("get chat: code=%d body=%v", rec.Code, body)
	}

	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/chats/nope", nil, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("get missing chat: got %d want 404", rec.Code)
	}

	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/chats/conv1", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete chat: got %d want 200", rec.Code)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/chats", nil, s.token)
	if chats, _ := body["chats"].([]any); rec.Code != http.StatusOK || len(chats) != 0 {
		t.Fatalf("chats after delete: code=%d body=%v", rec.Code, body)
	}
	if rec, _ := doJSON(t, s, http.MethodDelete, "/v1/chats/nope", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("delete nonexistent chat: got %d want 200", rec.Code)
	}
}

// TestPersistScenarioOnFinish exercises the finish-goroutine wiring (main.go's persistScenario): a
// scenario.json artifact must be indexed with the plan_hash READ from the artifact, never recomputed.
func TestPersistScenarioOnFinish(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	dir := t.TempDir()
	artifact := `{"plan_id":"run9-scenario","plan_hash":"frozen-from-artifact","target_url":"https://persisted.example",` +
		`"run_mode":"scenario","mode":"goal","unmatched":0,"steps":[{"step_id":1,"action":"click"}]}`
	if err := os.WriteFile(filepath.Join(dir, "scenario.json"), []byte(artifact), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: "run9", Target: "https://persisted.example", ArtifactDir: dir}
	s.persistScenario(rec)

	got, ok := sc.getScenario("run9-scenario")
	if !ok {
		t.Fatal("persistScenario: scenario not indexed")
	}
	if got.PlanHash != "frozen-from-artifact" {
		t.Fatalf("persistScenario: plan_hash = %q, want the artifact's own value (must not be recomputed)", got.PlanHash)
	}
	if got.SourceRunId != "run9" || got.Target != "https://persisted.example" || got.RunMode != "goal" {
		t.Fatalf("persistScenario record = %+v", got)
	}

	// no scenario.json in the artifact dir -> silent no-op, never an error
	rec2 := &run{ID: "run10", ArtifactDir: t.TempDir()}
	s.persistScenario(rec2)
	if _, ok := sc.getScenario("run10-scenario"); ok {
		t.Fatal("persistScenario: indexed something without a scenario.json artifact")
	}

	// no store configured -> must not panic (fail-open)
	storeless := newTestServer()
	storeless.persistScenario(rec)
}

// TestScenariosTestsChatsFailOpenNoStore mirrors TestControlAPINoStoreStaysInMemory for the new
// domains: with no store-gateway configured, reads degrade to empty/404, writes stay idempotent-safe,
// and nothing 503s or panics (M14_CONTRACT.md §3).
func TestScenariosTestsChatsFailOpenNoStore(t *testing.T) {
	s := newTestServer() // s.store is nil

	for _, path := range []string{"/v1/scenarios", "/v1/tests", "/v1/chats"} {
		if rec, _ := doJSON(t, s, http.MethodGet, path, nil, s.token); rec.Code != http.StatusOK {
			t.Fatalf("list %s with no store: got %d want 200 (graceful empty)", path, rec.Code)
		}
	}
	for _, path := range []string{"/v1/scenarios/x", "/v1/tests/x", "/v1/chats/x"} {
		if rec, _ := doJSON(t, s, http.MethodGet, path, nil, s.token); rec.Code != http.StatusNotFound {
			t.Fatalf("get %s with no store: got %d want 404", path, rec.Code)
		}
	}
	for _, path := range []string{"/v1/scenarios/x", "/v1/tests/x", "/v1/chats/x"} {
		if rec, _ := doJSON(t, s, http.MethodDelete, path, nil, s.token); rec.Code != http.StatusOK {
			t.Fatalf("delete %s with no store: got %d want 200 (idempotent)", path, rec.Code)
		}
	}
	promoteBody, _ := json.Marshal(map[string]string{"scenario_id": "x"})
	if rec, _ := doJSON(t, s, http.MethodPost, "/v1/tests/promote", promoteBody, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("promote with no store: got %d want 404", rec.Code)
	}
}
