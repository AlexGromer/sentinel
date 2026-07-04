// Command control-api is Sentinel's NON-MCP HTTP control plane (M9.3, ADR-023 / ADR-032 / ADR-040 / ADR-041).
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
// M9.3-tail (ADR-040): GET /v1/runs/{id}/events (SSE, token-gated) · GET /v1/runs/{id}/artifact (token-gated whitelist)
// M12 (ADR-041): POST /v1/chat/completions (OpenAI-compat shim — one chat turn → one run, token-gated)
// M9.8-prep (ADR-043): GET /v1/stream (hand-rolled WebSocket recorder ingest — client→server, token via subprotocol; see ws.go)
// M9.8 F4 (ADR-054): the SAME /v1/stream socket also accepts {"type":"takeover|return","run_id":"<id>"} control
// frames, forwarded to the RunControl orchestrator (CONTROL_API_ORCH_ADDR) as Takeover/Return RPCs so an
// in-flight brain pauses (interrupt+persist) / resumes. Everything else on the socket is a recorder event (ws.go).
// M9.9 (ADR-047): POST /v1/runs also accepts mode=replay|baseline + from_run:<prior run_id> — an in-tool
// re-run / golden-baseline update of a PRIOR run's frozen plan. from_run resolves under runs/control-<id>/
// to a whitelisted plan (plan.json|scenario.json), path-traversal-guarded — never an arbitrary --plan path.
// It spawns `agentctl run --replay --plan <p>` (replay) or `agentctl baseline update --plan <p>` (baseline).
// M9.10 (ADR-048): POST /v1/runs also accepts conversation_id:<id> — a multi-turn chat turn. The id is the
// resumable thread key (conversation_id→thread_id; charset/length-validated); turn-1 explores+authors,
// turn-N refines over the persisted site map. spawnRun adds `agentctl run --mode chat --conversation-id`.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
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

	stream *runStream // live stdout/stderr capture + SSE fan-out (not serialized)
}

const maxStreamLines = 1000

// runStream captures a run's combined stdout/stderr into a capped ring buffer and fans new lines
// out to live SSE subscribers (ADR-040). All fields are guarded by mu.
type runStream struct {
	mu    sync.Mutex
	lines []string                 // capped ring buffer (last maxStreamLines)
	subs  map[chan string]struct{} // live SSE subscribers
	done  bool
}

func newRunStream() *runStream { return &runStream{subs: map[chan string]struct{}{}} }

// append records a line and non-blockingly fans it out to subscribers. A slow SSE client drops
// lines (default branch) but the ring buffer keeps recent history, so a reconnect still catches up.
func (rs *runStream) append(line string) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	if rs.done {
		return
	}
	rs.lines = append(rs.lines, line)
	if len(rs.lines) > maxStreamLines {
		rs.lines = rs.lines[len(rs.lines)-maxStreamLines:]
	}
	for ch := range rs.subs {
		select {
		case ch <- line:
		default: // never block the run on a slow client
		}
	}
}

// subscribe returns a snapshot of buffered lines plus a channel of future lines. If the stream is
// already finished the channel is nil and finished is true.
func (rs *runStream) subscribe() (snapshot []string, ch chan string, finished bool) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	snapshot = append([]string(nil), rs.lines...)
	if rs.done {
		return snapshot, nil, true
	}
	ch = make(chan string, 256)
	rs.subs[ch] = struct{}{}
	return snapshot, ch, false
}

func (rs *runStream) unsubscribe(ch chan string) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	if _, ok := rs.subs[ch]; ok {
		delete(rs.subs, ch)
		close(ch)
	}
}

// finish marks the stream complete and closes every live subscriber channel exactly once.
func (rs *runStream) finish() {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	if rs.done {
		return
	}
	rs.done = true
	for ch := range rs.subs {
		delete(rs.subs, ch)
		close(ch)
	}
}

// lineWriter adapts an io.Writer (cmd.Stdout/Stderr) into rs.append, splitting on newlines.
type lineWriter struct {
	rs  *runStream
	buf []byte
}

func (w *lineWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	for {
		i := bytes.IndexByte(w.buf, '\n')
		if i < 0 {
			break
		}
		w.rs.append(strings.TrimRight(string(w.buf[:i]), "\r"))
		w.buf = w.buf[i+1:]
	}
	return len(p), nil
}

// flush emits any trailing partial line (call after the command exits, when writes have stopped).
func (w *lineWriter) flush() {
	if len(w.buf) > 0 {
		w.rs.append(strings.TrimRight(string(w.buf), "\r\n"))
		w.buf = nil
	}
}

type server struct {
	repo      string
	agentctl  string
	token     string
	corsAllow map[string]bool
	orchAddr  string // M9.8 F4 (ADR-054): RunControl orchestrator gRPC target for takeover/return forwarding ("" = not wired)
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
		"modes":   []string{"explore", "goal", "describe", "replay", "baseline", "chat"}, // replay/baseline (M9.9) need from_run; chat (M9.10) needs conversation_id
		"planner": []string{"heuristic", "llm", "goal"},
		"fields": map[string]any{
			"target":          map[string]any{"type": "string", "required": true},
			"goal":            map[string]any{"type": "string"},
			"describe":        map[string]any{"type": "string"},
			"conversation_id": map[string]any{"type": "string"}, // M9.10: multi-turn chat thread key (resume by conversation_id)
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
	FromRun        string `json:"from_run"`        // M9.9: prior run_id whose frozen plan to replay / baseline-update
	ConversationID string `json:"conversation_id"` // M9.10 (ADR-048): multi-turn chat thread — resumes by conversation_id->thread_id

	plan string // M9.9: server-RESOLVED plan path (runs/control-<FromRun>/plan.json|scenario.json); unexported → never client-settable
}

func validTarget(t string) bool {
	return strings.HasPrefix(t, "http://") || strings.HasPrefix(t, "https://") || strings.HasPrefix(t, "file://")
}

// validConversationID guards the M9.10 multi-turn thread key (ADR-048). It is NOT used in a filesystem
// path (the conversation store is fixed at state/conversations.db; the per-turn run_id is the artifact
// dir), and it is passed as a discrete argv element (no shell). We still bound it to a safe charset +
// length so a hostile client can't stuff control chars / a huge string into the persisted thread key.
func validConversationID(id string) bool {
	if id == "" || len(id) > 128 {
		return false
	}
	for _, r := range id {
		ok := r == '-' || r == '_' || (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')
		if !ok {
			return false
		}
	}
	return true
}

// replayInputs lists the frozen-plan artifacts a replay/baseline (M9.9) may consume from a prior run,
// in resolution order. Only these names are accepted as a replay input — never an arbitrary path.
var replayInputs = []string{"plan.json", "scenario.json"}

// resolveFromRun maps a prior run_id (from_run) to its frozen-plan path under runs/control-<id>/ plus
// the plan's target_url (M9.9, ADR-047). from_run is path-traversal-guarded exactly like artifact names
// (handleRunArtifact) and the input filename is whitelisted (replayInputs), so a replay is a re-run of a
// KNOWN prior plan, not the spawning of an arbitrary file on disk (THREAT_MODEL replay-surface).
func (s *server) resolveFromRun(fromRun string) (planPath, planTarget string, err error) {
	if fromRun == "" || strings.ContainsAny(fromRun, `/\`) || strings.Contains(fromRun, "..") {
		return "", "", fmt.Errorf("must be a bare prior run_id (no path separators)")
	}
	dir := filepath.Join(s.repo, "runs", "control-"+fromRun)
	for _, name := range replayInputs {
		p := filepath.Join(dir, name)
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			continue
		}
		var meta struct {
			TargetURL string `json:"target_url"`
		}
		_ = json.Unmarshal(b, &meta) // best-effort: agentctl/brain re-read the plan; we only need a fallback target
		return p, meta.TargetURL, nil
	}
	return "", "", fmt.Errorf("no replayable plan (plan.json|scenario.json) for run %q", fromRun)
}

// spawnRun starts an `agentctl run` from req (caller validates the target) and returns the tracked
// run record. Combined stdout+stderr is captured into rec.stream (ring buffer + SSE fan-out).
// Shared by POST /v1/runs and the OpenAI-compat /v1/chat/completions shim (ADR-041).
func (s *server) spawnRun(req runRequest) *run {
	id := newRunID()
	artDir := filepath.Join(s.repo, "runs", "control-"+id)
	rec := &run{ID: id, State: "running", Target: req.Target, ArtifactDir: artDir, StartedAt: time.Now().UTC().Format(time.RFC3339), stream: newRunStream()}
	s.mu.Lock()
	s.runs[id] = rec
	s.mu.Unlock()

	// Build agentctl args from the request (no shell — args are passed directly, no injection).
	// req.plan is server-resolved (resolveFromRun); req.Target for replay/baseline is the effective
	// target (request target, else the prior plan's target_url) decided in handleCreateRun.
	var args []string
	switch req.Mode {
	case "replay": // M9.9: re-run a prior frozen plan, healing locators — `agentctl run --replay --plan`
		args = []string{"run", "--target", req.Target, "--artifact-dir", artDir, "--replay", "--plan", req.plan}
	case "baseline": // M9.9: update golden baseline from a prior frozen plan (the only golden-write path)
		args = []string{"baseline", "update", "--plan", req.plan, "--artifact-dir", artDir}
		if req.Target != "" {
			args = append(args, "--target", req.Target)
		}
	default: // explore / goal / describe / chat (mode inferred from goal/describe + conversation_id as before)
		args = []string{"run", "--target", req.Target, "--artifact-dir", artDir}
		if req.ConversationID != "" { // M9.10 (ADR-048): multi-turn — resume the thread by conversation_id
			args = append(args, "--mode", "chat", "--conversation-id", req.ConversationID)
		}
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
	}
	cmd := exec.Command(s.agentctl, args...)
	cmd.Dir = s.repo
	cmd.Env = os.Environ() // inherits LLM_* etc. from the control-api process (operator-controlled)
	// Capture combined stdout+stderr into the run's stream (ring buffer + SSE fan-out). Setting
	// cmd.Stdout == cmd.Stderr makes os/exec merge them into ONE pipe with a single copy goroutine,
	// so lineWriter is intentionally not thread-safe — do NOT split Stdout/Stderr without a mutex.
	lw := &lineWriter{rs: rec.stream}
	cmd.Stdout = lw
	cmd.Stderr = lw

	go func() {
		err := cmd.Run()
		s.mu.Lock()
		rec.FinishedAt = time.Now().UTC().Format(time.RFC3339)
		if err == nil {
			rec.State, rec.ExitCode = "done", 0
		} else if ee, ok := err.(*exec.ExitError); ok {
			rec.State, rec.ExitCode = "done", ee.ExitCode() // structured exit (0/1/2/3) is a valid outcome
		} else {
			rec.State, rec.Error = "failed", err.Error() // could not spawn (agentctl missing, etc.)
		}
		s.mu.Unlock()
		lw.flush()          // emit any trailing partial line
		rec.stream.finish() // release SSE subscribers
	}()
	return rec
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
	switch req.Mode {
	case "replay", "baseline": // M9.9: re-run / baseline-update a prior run's frozen plan
		planPath, planTarget, err := s.resolveFromRun(req.FromRun)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "from_run: " + err.Error()})
			return
		}
		req.plan = planPath
		if !validTarget(req.Target) { // request target wins; else fall back to the plan's target_url — but only if IT is valid
			if validTarget(planTarget) {
				req.Target = planTarget
			} else {
				req.Target = "" // never forward a scheme-less/invalid target as --target (baseline omits it; replay 400s below)
			}
		}
		// `agentctl run --replay` requires a target (cmd/agentctl/main.go); baseline derives it from the
		// plan when omitted, so only replay hard-requires one here.
		if req.Mode == "replay" && !validTarget(req.Target) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "replay needs a target — none on the request and no target_url in the prior plan"})
			return
		}
	default: // explore / goal / describe / chat
		if !validTarget(req.Target) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "target must be an http(s):// or file:// URL"})
			return
		}
		// M9.10 (ADR-048): a chat turn carries a conversation_id (thread key) — validate it before spawn.
		if req.ConversationID != "" && !validConversationID(req.ConversationID) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "conversation_id must be 1-128 chars of [A-Za-z0-9_-]"})
			return
		}
	}
	rec := s.spawnRun(req)
	writeJSON(w, http.StatusAccepted, map[string]string{"run_id": rec.ID, "artifact_dir": rec.ArtifactDir, "state": "running"})
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

// artifactWhitelist limits artifact-fetch to known run outputs (no arbitrary file reads).
var artifactWhitelist = map[string]bool{
	"scenario.json":         true,
	"reconcile-report.json": true,
	"report.json":           true,
	"report.html":           true,
	"plan.json":             true,
	"heal-report.json":      true, // M9.9: replay output (golden diff / heal log)
	"baseline-report.json":  true, // M9.9: baseline-update output
}

// handleRunEvents streams a run's state + captured log lines as Server-Sent Events (ADR-040).
// Token-gated like mutations: logs are more sensitive than a bare status poll.
func (s *server) handleRunEvents(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	id := r.PathValue("id")
	s.mu.RLock()
	rec, ok := s.runs[id]
	s.mu.RUnlock()
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "streaming unsupported"})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no") // disable proxy buffering of the stream
	w.WriteHeader(http.StatusOK)

	sendState := func() {
		s.mu.RLock()
		data, _ := json.Marshal(map[string]any{"state": rec.State, "exit_code": rec.ExitCode, "error": rec.Error})
		s.mu.RUnlock()
		fmt.Fprintf(w, "event: state\ndata: %s\n\n", data)
		flusher.Flush()
	}
	sendLog := func(line string) {
		data, _ := json.Marshal(map[string]string{"line": line})
		fmt.Fprintf(w, "event: log\ndata: %s\n\n", data)
		flusher.Flush()
	}

	sendState() // initial snapshot
	buffered, ch, finished := rec.stream.subscribe()
	for _, line := range buffered {
		sendLog(line)
	}
	if !finished {
		defer rec.stream.unsubscribe(ch)
		ctx := r.Context()
	live:
		for {
			select {
			case line, open := <-ch:
				if !open {
					break live
				}
				sendLog(line)
			case <-ctx.Done():
				return // client disconnected
			}
		}
	}
	sendState() // final state + exit_code
	fmt.Fprint(w, "event: done\ndata: {}\n\n")
	flusher.Flush()
}

// handleRunArtifact serves a whitelisted artifact from a run's artifact dir (token-gated,
// path-traversal-guarded) so the chat-front can display/download scenario.json / report.
func (s *server) handleRunArtifact(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	id := r.PathValue("id")
	s.mu.RLock()
	rec, ok := s.runs[id]
	s.mu.RUnlock()
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	name := r.URL.Query().Get("name")
	if name == "" || strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") || !artifactWhitelist[name] {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "name must be a whitelisted run artifact (e.g. scenario.json, plan.json, report.json, heal-report.json)"})
		return
	}
	f, err := os.Open(filepath.Join(rec.ArtifactDir, name))
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "artifact not found (run may be incomplete)"})
		return
	}
	defer f.Close()
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if strings.HasSuffix(name, ".html") {
		// Serve report.html as a download, never inline — avoids the browser rendering
		// agent-influenced HTML if someone navigates straight to this endpoint.
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
	} else {
		w.Header().Set("Content-Type", "application/json")
	}
	_, _ = io.Copy(w, f)
}

// readArtifact returns the contents of a whitelisted artifact for a run (or "", false). Used by the
// chat shim to fold scenario.json into its reply; the HTTP artifact endpoint streams it instead.
func (s *server) readArtifact(rec *run, name string) (string, bool) {
	if name == "" || strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") || !artifactWhitelist[name] {
		return "", false // defense-in-depth: same path-traversal guard as handleRunArtifact
	}
	b, err := os.ReadFile(filepath.Join(rec.ArtifactDir, name))
	if err != nil {
		return "", false
	}
	return string(b), true
}

// --- OpenAI-compatible chat-completions shim (ADR-041) -------------------------------------------
// Maps ONE chat turn → ONE Sentinel run (brain is one-shot). Lets any OpenAI client (Open WebUI,
// DeepSeek/Mistral clients, SDKs, our own page) drive Sentinel "as a model" (model="sentinel").

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type chatRequest struct {
	Model    string        `json:"model"`
	Messages []chatMessage `json:"messages"`
	Stream   bool          `json:"stream"`
}

// extractTarget returns the first http(s)://|file:// token in content (supports a "target: <url>" line).
func extractTarget(content string) string {
	for _, tok := range strings.Fields(content) {
		tok = strings.Trim(tok, "<>\"'`,;()[]")
		if validTarget(tok) {
			return tok
		}
	}
	return ""
}

// stripTargetLine drops any "target: ..." lines from the instruction (they are metadata).
func stripTargetLine(s string) string {
	keep := make([]string, 0)
	for _, ln := range strings.Split(s, "\n") {
		if strings.HasPrefix(strings.ToLower(strings.TrimSpace(ln)), "target:") {
			continue
		}
		keep = append(keep, ln)
	}
	return strings.TrimSpace(strings.Join(keep, "\n"))
}

// parseChatInstruction derives (mode, target, instruction) from the chat messages + model name.
// Mode: model suffix (sentinel-goal/-explore) or a leading "goal:"/"explore:"/"describe:" prefix;
// default describe. Target: the most recent (last) valid URL across all messages. Instruction: the last user turn.
func parseChatInstruction(model string, messages []chatMessage) (mode, target, text string) {
	mode = "describe"
	switch lm := strings.ToLower(model); {
	case strings.Contains(lm, "goal"):
		mode = "goal"
	case strings.Contains(lm, "explore"):
		mode = "explore"
	}
	for _, m := range messages {
		if t := extractTarget(m.Content); t != "" {
			target = t
		}
		if m.Role == "user" {
			text = m.Content
		}
	}
	text = strings.TrimSpace(text)
	for _, p := range []struct{ pfx, md string }{{"goal:", "goal"}, {"describe:", "describe"}, {"explore:", "explore"}} {
		if strings.HasPrefix(strings.ToLower(text), p.pfx) {
			mode, text = p.md, strings.TrimSpace(text[len(p.pfx):])
			break
		}
	}
	return mode, target, stripTargetLine(text)
}

// verdict summarizes a finished run (exit-code → text) and folds in the relevant artifact (scenario.json
// for authoring; heal-report.json / baseline-report.json for M9.9 replay/baseline). NOTE: exit 2 is
// overloaded — a real golden regression OR a bad invocation (missing plan, etc.); inspect heal-report.json.
func (s *server) verdict(rec *run) string {
	s.mu.RLock()
	state, code, errStr := rec.State, rec.ExitCode, rec.Error
	s.mu.RUnlock()
	var v string
	switch {
	case state == "failed":
		v = "✖ run did not start"
		if errStr != "" {
			v += " — " + errStr
		}
	case code == 0:
		v = "✓ pass (exit 0)"
	case code == 1:
		v = "⚠ the test found a problem (exit 1)"
	case code == 2:
		v = "⚠ visual/golden regression (exit 2)"
	case code == 3:
		v = "✖ integrity/config error — plan_hash or golden mismatch, or bad invocation — needs a human (exit 3)"
	default:
		v = fmt.Sprintf("exit %d", code)
	}
	if sc, ok := s.readArtifact(rec, "scenario.json"); ok {
		v += "\n\nscenario.json:\n" + sc
	}
	if hr, ok := s.readArtifact(rec, "heal-report.json"); ok { // M9.9 replay output
		v += "\n\nheal-report.json:\n" + hr
	} else if br, ok := s.readArtifact(rec, "baseline-report.json"); ok { // M9.9 baseline output
		v += "\n\nbaseline-report.json:\n" + br
	}
	return v
}

func chatChunk(w http.ResponseWriter, fl http.Flusher, id string, created int64, model string, delta map[string]any, finish any) {
	b, _ := json.Marshal(map[string]any{
		"id": id, "object": "chat.completion.chunk", "created": created, "model": model,
		"choices": []map[string]any{{"index": 0, "delta": delta, "finish_reason": finish}},
	})
	fmt.Fprintf(w, "data: %s\n\n", b)
	fl.Flush()
}

func chatCompletion(id string, created int64, model, content string) map[string]any {
	return map[string]any{
		"id": id, "object": "chat.completion", "created": created, "model": model,
		"choices": []map[string]any{{"index": 0, "message": map[string]any{"role": "assistant", "content": content}, "finish_reason": "stop"}},
		"usage":   map[string]any{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
	}
}

// handleChatCompletions is the OpenAI-compatible shim, token-gated like other mutations.
func (s *server) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var req chatRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad JSON: " + err.Error()})
		return
	}
	mode, target, text := parseChatInstruction(req.Model, req.Messages)
	model := req.Model
	if model == "" {
		model = "sentinel"
	}
	id, created := "chatcmpl-"+newRunID(), time.Now().Unix()

	// Friendly, chat-shaped guidance instead of an HTTP error when the turn is unusable.
	if !validTarget(target) {
		s.chatReply(w, req.Stream, id, created, model, "Set a target first — include a URL like `target: https://app.example` (or a `file://` URL) in your message.")
		return
	}
	if mode != "explore" && text == "" {
		s.chatReply(w, req.Stream, id, created, model, "Describe the test in words, or prefix with `goal:` / `explore:`.")
		return
	}

	rr := runRequest{Target: target, Mode: mode, Planner: "heuristic"}
	switch mode {
	case "goal":
		rr.Goal, rr.Planner = text, "goal"
	case "describe":
		rr.Describe = text
	}
	rec := s.spawnRun(rr)

	if req.Stream {
		s.streamChat(w, r, rec, id, created, model)
		return
	}
	s.blockingChat(w, rec, id, created, model)
}

// chatReply emits a single assistant message (one-shot guidance / errors), stream or not.
func (s *server) chatReply(w http.ResponseWriter, stream bool, id string, created int64, model, msg string) {
	if fl, ok := w.(http.Flusher); stream && ok {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.WriteHeader(http.StatusOK)
		chatChunk(w, fl, id, created, model, map[string]any{"role": "assistant", "content": msg}, nil)
		chatChunk(w, fl, id, created, model, map[string]any{}, "stop")
		fmt.Fprint(w, "data: [DONE]\n\n")
		fl.Flush()
		return
	}
	writeJSON(w, http.StatusOK, chatCompletion(id, created, model, msg))
}

// streamChat streams the run's log lines + final verdict as OpenAI chat.completion.chunk events.
func (s *server) streamChat(w http.ResponseWriter, r *http.Request, rec *run, id string, created int64, model string) {
	fl, ok := w.(http.Flusher)
	if !ok {
		s.blockingChat(w, rec, id, created, model)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	chatChunk(w, fl, id, created, model, map[string]any{"role": "assistant"}, nil)
	buffered, ch, finished := rec.stream.subscribe()
	for _, line := range buffered {
		chatChunk(w, fl, id, created, model, map[string]any{"content": line + "\n"}, nil)
	}
	if !finished {
		defer rec.stream.unsubscribe(ch)
		ctx := r.Context()
	loop:
		for {
			select {
			case line, open := <-ch:
				if !open {
					break loop
				}
				chatChunk(w, fl, id, created, model, map[string]any{"content": line + "\n"}, nil)
			case <-ctx.Done():
				return
			}
		}
	}
	chatChunk(w, fl, id, created, model, map[string]any{"content": "\n" + s.verdict(rec)}, nil)
	chatChunk(w, fl, id, created, model, map[string]any{}, "stop")
	fmt.Fprint(w, "data: [DONE]\n\n")
	fl.Flush()
}

// blockingChat waits for the run to finish and returns a single chat.completion (logs + verdict).
func (s *server) blockingChat(w http.ResponseWriter, rec *run, id string, created int64, model string) {
	snapshot, ch, finished := rec.stream.subscribe()
	lines := append([]string(nil), snapshot...)
	if !finished {
		defer rec.stream.unsubscribe(ch)
		for line := range ch {
			lines = append(lines, line)
		}
	}
	content := strings.Join(lines, "\n")
	if content != "" {
		content += "\n\n"
	}
	content += s.verdict(rec)
	writeJSON(w, http.StatusOK, chatCompletion(id, created, model, content))
}

func (s *server) mux() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /healthz", s.handleHealthz)
	m.HandleFunc("GET /v1/config-schema", s.handleConfigSchema)
	m.HandleFunc("POST /v1/runs", s.handleCreateRun)
	m.HandleFunc("POST /v1/chat/completions", s.handleChatCompletions)
	m.HandleFunc("GET /v1/runs", s.handleListRuns)
	m.HandleFunc("GET /v1/runs/{id}", s.handleGetRun)
	m.HandleFunc("GET /v1/runs/{id}/events", s.handleRunEvents)
	m.HandleFunc("GET /v1/runs/{id}/artifact", s.handleRunArtifact)
	m.HandleFunc("GET /v1/stream", s.handleStream)
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
		orchAddr:  os.Getenv("CONTROL_API_ORCH_ADDR"), // M9.8 F4 (ADR-054): e.g. "unix:/abs/state/sentinel-orch-<id>.sock"
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
