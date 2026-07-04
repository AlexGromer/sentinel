package main

import (
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
	runs, ok := sc.listRuns()
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

func TestStoreClientFailsFastWhenUnreachable(t *testing.T) {
	// newStoreClient probes with ListRuns so a dead gateway is detected at startup (fail-open in main()).
	if _, err := newStoreClient("unix:/nonexistent/definitely-not-a-store.sock", ""); err == nil {
		t.Fatal("newStoreClient to a dead socket must error, not return a live client")
	}
}
