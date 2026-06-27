package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
