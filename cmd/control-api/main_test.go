package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func newTestServer() *server {
	return &server{
		repo:      ".",
		agentctl:  "/nonexistent/agentctl",
		token:     "secret-tok",
		corsAllow: map[string]bool{"https://alexgromer.github.io": true},
		runs:      map[string]*run{},
	}
}

func TestHealthz(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz: got %d want 200", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["status"] != "ok" {
		t.Fatalf("healthz body: %v (err=%v)", body, err)
	}
}

func TestCreateRunRequiresToken(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"file:///x.html"}`))
	newTestServer().mux().ServeHTTP(rec, req) // no Authorization header
	if rec.Code != http.StatusForbidden {
		t.Fatalf("create-run without token: got %d want 403", rec.Code)
	}
}

func TestCreateRunRejectsBadTarget(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"javascript:alert(1)"}`))
	req.Header.Set("Authorization", "Bearer secret-tok")
	newTestServer().mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("create-run bad target: got %d want 400 (no agentctl spawned)", rec.Code)
	}
}

func TestCORSPreflightAllowed(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodOptions, "/v1/runs", nil)
	req.Header.Set("Origin", "https://alexgromer.github.io")
	newTestServer().mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight: got %d want 204", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "https://alexgromer.github.io" {
		t.Fatalf("preflight ACAO: got %q", got)
	}
}

func TestCORSDisallowedOrigin(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("Origin", "https://evil.example")
	newTestServer().mux().ServeHTTP(rec, req)
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("disallowed origin must get no ACAO, got %q", got)
	}
}

// newRunServer returns a server backed by a temp repo + a fake agentctl that echoes a line to
// stdout and one to stderr, then exits 1 (a real "the test found a problem" exit code).
func newRunServer(t *testing.T) (*server, string) {
	t.Helper()
	repo := t.TempDir()
	script := filepath.Join(repo, "fake-agentctl.sh")
	body := "#!/bin/sh\necho 'planning step 1'\necho 'walking page' 1>&2\nexit 1\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("write fake agentctl: %v", err)
	}
	return &server{
		repo:      repo,
		agentctl:  script,
		token:     "secret-tok",
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
	}, repo
}

// createRunAndWait POSTs a run and waits briefly for the spawned process to finish, returning its id.
func createRunAndWait(t *testing.T, s *server) string {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"file:///x.html"}`))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("create run: got %d want 202 (%s)", rec.Code, rec.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("create run body: %v", err)
	}
	id := resp["run_id"]
	for i := 0; i < 200; i++ {
		s.mu.RLock()
		st := s.runs[id].State
		s.mu.RUnlock()
		if st != "running" {
			return id
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("run %s did not finish in time", id)
	return ""
}

func TestRunEventsStream(t *testing.T) {
	s, _ := newRunServer(t)
	id := createRunAndWait(t, s)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/events", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("events: got %d want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("events content-type: %q want text/event-stream", ct)
	}
	body := rec.Body.String()
	for _, want := range []string{"event: state", "event: log", "planning step 1", "walking page", `"exit_code":1`, "event: done"} {
		if !strings.Contains(body, want) {
			t.Fatalf("events body missing %q:\n%s", want, body)
		}
	}
}

func TestRunEventsRequiresToken(t *testing.T) {
	s, _ := newRunServer(t)
	id := createRunAndWait(t, s)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/events", nil) // no Authorization
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("events without token: got %d want 403", rec.Code)
	}
}

func TestRunArtifact(t *testing.T) {
	s, repo := newRunServer(t)
	id := createRunAndWait(t, s)
	artDir := filepath.Join(repo, "runs", "control-"+id)
	if err := os.MkdirAll(artDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(artDir, "scenario.json"), []byte(`{"steps":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}

	// happy path — whitelisted file present
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=scenario.json", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"steps"`) {
		t.Fatalf("artifact happy: got %d body=%s", rec.Code, rec.Body.String())
	}

	// no token → 403
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=scenario.json", nil)
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("artifact no token: got %d want 403", rec.Code)
	}

	// non-whitelisted / traversal names → 400 (no file read)
	for _, bad := range []string{"evil.txt", "../../etc/passwd", "sub/scenario.json", ""} {
		rec = httptest.NewRecorder()
		req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name="+url.QueryEscape(bad), nil)
		req.Header.Set("Authorization", "Bearer secret-tok")
		s.mux().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("artifact bad name %q: got %d want 400", bad, rec.Code)
		}
	}

	// whitelisted but missing file → 404
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=report.json", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("artifact missing file: got %d want 404", rec.Code)
	}
}
