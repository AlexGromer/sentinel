// Command control-api is Sentinel's NON-MCP HTTP control plane (M9.3, ADR-023 / ADR-032).
//
// It is the second way to drive Sentinel (the first is brain-as-MCP-server, M7): a thin HTTP API
// that the setup-WebUI (or any script/CI) can call to start a run and poll its status. It spawns
// `agentctl run` exactly like the orchestrator — it does NOT reimplement the run.
//
// SECURITY (ADR-032) — spawning runs is a sensitive surface (RCE-class if exposed):
//   - Binds 127.0.0.1 by default (CONTROL_API_ADDR). Public bind (0.0.0.0) is opt-in + warned.
//   - Mutations (POST /v1/runs) require a bearer token (CONTROL_API_TOKEN); 403 if unset/mismatch.
//   - CORS is an explicit allowlist (CONTROL_API_CORS_ORIGINS) so a Pages-hosted WebUI can drive a
//     LOCAL instance (localhost is mixed-content-exempt) without opening the API to arbitrary sites.
//   - Only the known agentctl binary is spawned; the target URL scheme is validated.
//
// Endpoints (v1): GET /healthz · GET /v1/config-schema · POST /v1/runs · GET /v1/runs · GET /v1/runs/{id}
package main

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const version = "0.1.0"

// run is the tracked state of a spawned agentctl run.
type run struct {
	ID          string `json:"run_id"`
	State       string `json:"state"` // running | done | failed
	ExitCode    int    `json:"exit_code"`
	Target      string `json:"target"`
	ArtifactDir string `json:"artifact_dir"`
	StartedAt   string `json:"started_at"`
	FinishedAt  string `json:"finished_at,omitempty"`
	Error       string `json:"error,omitempty"`
}

type server struct {
	repo      string
	agentctl  string
	token     string
	corsAllow map[string]bool
	mu        sync.RWMutex
	runs      map[string]*run
}

func newRunID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// cors applies the explicit-allowlist CORS policy + answers preflight. A Pages origin in the
// allowlist may call a local control-API (localhost mixed-content exemption); others get nothing.
func (s *server) cors(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && s.corsAllow[origin] {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		h.ServeHTTP(w, r)
	})
}

// authed reports whether a request carries the configured bearer token (constant-time). Mutations
// require it; if no token is configured at all, mutations are refused (fail-closed).
func (s *server) authed(r *http.Request) bool {
	if s.token == "" {
		return false
	}
	got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	return subtle.ConstantTimeCompare([]byte(got), []byte(s.token)) == 1
}

func (s *server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	s.mu.RLock()
	n := len(s.runs)
	s.mu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": version, "runs": n})
}

// configSchema mirrors the RunConfig surface (brain/runconfig.py + agentctl flags) so the WebUI can
// render the form from one source of truth. Keys/defaults match the loader.
func (s *server) handleConfigSchema(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"modes":   []string{"explore", "goal", "describe"},
		"planner": []string{"heuristic", "llm", "goal"},
		"fields": map[string]any{
			"target":          map[string]any{"type": "string", "required": true},
			"goal":            map[string]any{"type": "string"},
			"describe":        map[string]any{"type": "string"},
			"coverage_target": map[string]any{"type": "number", "default": 0.85},
			"max_steps":       map[string]any{"type": "int", "default": 40},
			"plan_budget":     map[string]any{"type": "int", "default": 50000},
			"heal_budget":     map[string]any{"type": "int", "default": 20000},
			"total_budget":    map[string]any{"type": "int", "default": 0},
		},
		"note": "secrets (LLM_API_KEY/ANTHROPIC_API_KEY) go in the control-api process env, never in this payload",
	})
}

type runRequest struct {
	Target         string `json:"target"`
	Mode           string `json:"mode"`
	Goal           string `json:"goal"`
	Describe       string `json:"describe"`
	Planner        string `json:"planner"`
	CoverageTarget string `json:"coverage_target"`
	MaxSteps       string `json:"max_steps"`
}

func validTarget(t string) bool {
	return strings.HasPrefix(t, "http://") || strings.HasPrefix(t, "https://") || strings.HasPrefix(t, "file://")
}

func (s *server) handleCreateRun(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var req runRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad JSON: " + err.Error()})
		return
	}
	if !validTarget(req.Target) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "target must be an http(s):// or file:// URL"})
		return
	}
	id := newRunID()
	artDir := filepath.Join(s.repo, "runs", "control-"+id)
	rec := &run{ID: id, State: "running", Target: req.Target, ArtifactDir: artDir, StartedAt: time.Now().UTC().Format(time.RFC3339)}
	s.mu.Lock()
	s.runs[id] = rec
	s.mu.Unlock()

	// Build agentctl args from the request (no shell — args are passed directly, no injection).
	args := []string{"run", "--target", req.Target, "--artifact-dir", artDir}
	if req.Planner != "" {
		args = append(args, "--planner", req.Planner)
	}
	if req.Goal != "" {
		args = append(args, "--goal", req.Goal)
	}
	if req.Describe != "" {
		args = append(args, "--describe", req.Describe)
	}
	if req.CoverageTarget != "" {
		args = append(args, "--coverage-target", req.CoverageTarget)
	}
	if req.MaxSteps != "" {
		args = append(args, "--max-steps", req.MaxSteps)
	}
	cmd := exec.Command(s.agentctl, args...)
	cmd.Dir = s.repo
	cmd.Env = os.Environ() // inherits LLM_* etc. from the control-api process (operator-controlled)

	go func() {
		err := cmd.Run()
		s.mu.Lock()
		defer s.mu.Unlock()
		rec.FinishedAt = time.Now().UTC().Format(time.RFC3339)
		if err == nil {
			rec.State, rec.ExitCode = "done", 0
			return
		}
		if ee, ok := err.(*exec.ExitError); ok {
			rec.State, rec.ExitCode = "done", ee.ExitCode() // structured exit (0/1/2/3) is a valid outcome
			return
		}
		rec.State, rec.Error = "failed", err.Error() // could not spawn (agentctl missing, etc.)
	}()

	writeJSON(w, http.StatusAccepted, map[string]string{"run_id": id, "artifact_dir": artDir, "state": "running"})
}

func (s *server) handleListRuns(w http.ResponseWriter, _ *http.Request) {
	s.mu.RLock()
	out := make([]*run, 0, len(s.runs))
	for _, rr := range s.runs {
		out = append(out, rr)
	}
	s.mu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

func (s *server) handleGetRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mu.RLock()
	rec, ok := s.runs[id]
	s.mu.RUnlock()
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

func (s *server) mux() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /healthz", s.handleHealthz)
	m.HandleFunc("GET /v1/config-schema", s.handleConfigSchema)
	m.HandleFunc("POST /v1/runs", s.handleCreateRun)
	m.HandleFunc("GET /v1/runs", s.handleListRuns)
	m.HandleFunc("GET /v1/runs/{id}", s.handleGetRun)
	return s.cors(m)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "control-api: cwd: %v\n", err)
		os.Exit(1)
	}
	addr := envOr("CONTROL_API_ADDR", "127.0.0.1:8090")
	s := &server{
		repo:      repo,
		agentctl:  envOr("CONTROL_API_AGENTCTL", filepath.Join(repo, "bin", "agentctl")),
		token:     os.Getenv("CONTROL_API_TOKEN"),
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
	}
	for _, o := range strings.Split(os.Getenv("CONTROL_API_CORS_ORIGINS"), ",") {
		if o = strings.TrimSpace(o); o != "" {
			s.corsAllow[o] = true
		}
	}
	if s.token == "" {
		fmt.Fprintln(os.Stderr, "control-api: WARNING — CONTROL_API_TOKEN unset; POST /v1/runs will 403 (read-only).")
	}
	if !strings.HasPrefix(addr, "127.0.0.1") && !strings.HasPrefix(addr, "localhost") {
		fmt.Fprintf(os.Stderr, "control-api: WARNING — binding non-local %q; spawning runs is sensitive (ADR-032).\n", addr)
	}
	fmt.Fprintf(os.Stderr, "control-api: listening on http://%s (agentctl=%s)\n", addr, s.agentctl)
	srv := &http.Server{Addr: addr, Handler: s.mux(), ReadHeaderTimeout: 5 * time.Second}
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintf(os.Stderr, "control-api: %v\n", err)
		os.Exit(1)
	}
}
