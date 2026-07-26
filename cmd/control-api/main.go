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
// M14 wave W3 (ADR-055, M14_CONTRACT.md §3): GET/DELETE /v1/scenarios[/{id}] · GET/DELETE /v1/tests[/{id}] ·
// POST /v1/tests/promote · GET/DELETE /v1/chats[/{id}] — all token-gated, over the fail-open store-gateway
// client (store.go). A finished run whose artifact_dir has scenario.json is indexed into the scenarios
// domain (persistScenario), wiring it to a real caller for the first time.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// version is stamped by the release build (`go build -ldflags "-X main.version=<tag>"`, .github/workflows/
// release.yml). It MUST stay a var — the linker cannot write into a const, so declaring it const made the
// -X flag a silent no-op and /healthz reported "0.1.0" on every tagged release (fixed with ADR-064).
var version = "0.1.0"

// run is the tracked state of a spawned agentctl run.
type run struct {
	ID             string `json:"run_id"`
	State          string `json:"state"` // running | done | failed
	ExitCode       int    `json:"exit_code"`
	Target         string `json:"target"`
	Mode           string `json:"mode,omitempty"`            // M13: persisted for the runs domain
	Planner        string `json:"planner,omitempty"`         // M13
	ConversationID string `json:"conversation_id,omitempty"` // M13: the runs<->chats join (ADR-050)
	ArtifactDir    string `json:"artifact_dir"`
	StartedAt      string `json:"started_at"`
	FinishedAt     string `json:"finished_at,omitempty"`
	Error          string `json:"error,omitempty"`

	stream *runStream // live stdout/stderr capture + SSE fan-out (not serialized)
	sink   *logSink   // M9-LIVE: on-disk log artifacts under the run's dir (not serialized)
	// pid of the spawned agentctl, which leads the run's process group (see procgroup_*.go). Zero once
	// the run has finished. Needed because a cancel has to reach the whole tree, not just the top.
	pid int
	// canceled records that a human asked to stop. The waiting goroutine reads it to decide the terminal
	// state: a killed process otherwise reports exit -1, which would read as a crash rather than a
	// deliberate stop — and "did I stop it, or did it break?" is exactly what the operator needs to know.
	canceled bool
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
//
// M9-LIVE: it fans each completed line to TWO consumers — the in-memory ring buffer (live SSE/WS,
// unchanged, still carrying the @@AGUI frames the timeline needs) and the on-disk sink (logsink.go),
// which is where the narrative/diagnostics split happens. Additive on purpose: the live path is not
// touched, so the split cannot regress the timeline.
type lineWriter struct {
	rs   *runStream
	sink *logSink
	buf  []byte
}

func (w *lineWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	for {
		i := bytes.IndexByte(w.buf, '\n')
		if i < 0 {
			break
		}
		line := strings.TrimRight(string(w.buf[:i]), "\r")
		w.rs.append(line)
		w.sink.write(line) // nil-safe: a run whose log files could not be opened still runs
		w.buf = w.buf[i+1:]
	}
	return len(p), nil
}

// flush emits any trailing partial line (call after the command exits, when writes have stopped).
func (w *lineWriter) flush() {
	if len(w.buf) > 0 {
		line := strings.TrimRight(string(w.buf), "\r\n")
		w.rs.append(line)
		w.sink.write(line)
		w.buf = nil
	}
}

type server struct {
	repo      string
	agentctl  string
	token     string
	corsAllow map[string]bool
	orchAddr  string       // M9.8 F4 (ADR-054): RunControl orchestrator gRPC target for takeover/return forwarding ("" = not wired)
	store     *storeClient // M13 (ADR-050): persistent store-gateway client (nil = in-memory only)
	// storeAddr is CONTROL_API_STORE_ADDR as configured, kept even when the dial failed. It is what
	// separates "the operator chose the standalone tier" from "the operator chose a store and it is
	// down" — two situations a nil `store` alone cannot tell apart, and which the config domain must
	// answer differently (ADR-075, cmd/control-api/configfile.go).
	storeAddr  string
	publicBind bool      // M13 R3-hardening: bound to a non-loopback addr (tightens /v1/stream Origin check)
	ui         *uiServer // ADR-064 Mode 3: serves the browser UI from this port (nil/disabled = Modes 1-2)
	mu         sync.RWMutex
	runs       map[string]*run

	// M11.5 PR-5 (ADR-062): /readyz. llmBaseURL is the env-configured LLM endpoint ("" = not configured);
	// probes fall back to the persisted config's llm.base_url. ready guards its own state, NOT s.mu —
	// a readiness probe does network I/O and must never block /v1/runs.
	llmBaseURL string
	httpClient *http.Client
	ready      readyState
}

func newRunID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

// isLocalBind reports whether addr binds a loopback host (127.0.0.0/8, ::1, or "localhost"). Used to
// keep the /v1/stream Origin check dev-permissive on a local bind and fail-closed on a public one (M13).
func isLocalBind(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		host = addr
	}
	host = strings.Trim(host, "[]")
	if host == "" || host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// aguiLine builds a runStream line carrying a control-API-injected AG-UI event (M14 tail): the
// wsAGUIPrefix marker + a compact JSON envelope. It is the Go counterpart to brain/agui.py's emit — the
// control-API injects the one event only it can know (run.finished, from the process exit), while every
// other AG-UI event still comes from the brain's @@AGUI stdout lines.
//
// The envelope deliberately omits `seq`: the control-API does not share the brain's per-process seq space
// (RunState has no agui_seq; ws.go never parses @@AGUI beyond the prefix), and the UI's run.finished
// branch reads only data.exit_code (its generic branch tolerates a missing seq). `ts` is the RFC3339
// timestamp the caller already computed (rec.FinishedAt). The value shape is fixed, so json.Marshal
// cannot fail — mirrors wsAGUIFrame's own `_, _ = json.Marshal(...)` reasoning.
func aguiLine(eventType, runID, ts string, data map[string]any) string {
	b, _ := json.Marshal(map[string]any{
		"type":   eventType,
		"run_id": runID,
		"ts":     ts,
		"data":   data,
	})
	return wsAGUIPrefix + string(b)
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
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
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

// configSchema mirrors the RunConfig surface (brain/runconfig.py + agentctl flags) plus the
// LLM-backend surface (brain/llm.py make_backend) so the WebUI can render the whole form from one
// source of truth. Keys/defaults match the loaders. Secrets are DESCRIBED (api_key.secret) but never
// VALUED — actual keys live in the control-api process env, never in this payload (M11.5 PR-3, ADR-060).
func (s *server) handleConfigSchema(w http.ResponseWriter, _ *http.Request) {
	// single source for the backend enum so the top-level list and llm.backend.enum can't drift apart
	backends := llmBackends // single source (llmenv.go); mirrors brain/llm.py make_backend; "sampling" = MCP host-supplied (mcp-server mode), not a wizard preset
	writeJSON(w, http.StatusOK, map[string]any{
		"modes":    []string{"explore", "goal", "describe", "replay", "baseline", "chat"}, // replay/baseline (M9.9) need from_run; chat (M9.10) needs conversation_id
		"planner":  []string{"heuristic", "llm", "goal"},
		"backends": backends,
		"roles":    []string{"planner", "heal"}, // per-role override LLM_<KEY>_<ROLE> falls back to global LLM_<KEY>
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
		// M11.5 PR-3 (ADR-060): LLM-backend descriptors from brain/llm.py make_backend. Descriptors ONLY —
		// api_key is flagged secret and NEVER valued here. role_split: field also honours LLM_<KEY>_PLANNER/_HEAL.
		// vision/structured default false for every openai backend (opt-in LLM_VISION=1/LLM_STRUCTURED=1); anthropic is natively both.
		"llm": map[string]any{
			"backend":    map[string]any{"env": "LLM_BACKEND", "type": "enum", "enum": backends, "default": "anthropic", "role_split": true},
			"model":      map[string]any{"env": "LLM_MODEL", "type": "string", "role_split": true, "note": "required for backend=openai; anthropic defaults planner=claude-opus-4-8 / heal=claude-sonnet-4-6"},
			"base_url":   map[string]any{"env": "LLM_BASE_URL", "type": "string", "role_split": true, "note": "OpenAI-compatible /v1 endpoint; see docs/backend-presets.json"},
			"api_key":    map[string]any{"env": "LLM_API_KEY", "type": "string", "secret": true, "role_split": true, "note": "never returned in this payload; anthropic->ANTHROPIC_API_KEY, openai->OPENAI_API_KEY"},
			"vision":     map[string]any{"env": "LLM_VISION", "type": "bool", "default": false, "role_split": true, "note": "opt-in ('1'); openai backends default off (many are text-only, e.g. DeepSeek); anthropic always vision-capable"},
			"structured": map[string]any{"env": "LLM_STRUCTURED", "type": "bool", "default": false, "role_split": true, "note": "opt-in ('1'); openai backends default off (many local endpoints reject json_schema); anthropic always structured"},
		},
		"note": "secrets (LLM_API_KEY/ANTHROPIC_API_KEY) go in the control-api process env, never in this payload",
	})
}

type runRequest struct {
	Target         string          `json:"target"`
	Mode           string          `json:"mode"`
	Goal           string          `json:"goal"`
	Describe       string          `json:"describe"`
	Planner        string          `json:"planner"`
	CoverageTarget string          `json:"coverage_target"`
	MaxSteps       string          `json:"max_steps"`
	FromRun        string          `json:"from_run"`        // M9.9: prior run_id whose frozen plan to replay / baseline-update
	ConversationID string          `json:"conversation_id"` // M9.10 (ADR-048): multi-turn chat thread — resumes by conversation_id->thread_id
	LLM            json.RawMessage `json:"llm"`             // ADR-063: per-run LLM override (backend/base_url/model/vision); validated+parsed into `llm` below

	plan string        // M9.9: server-RESOLVED plan path (runs/control-<FromRun>/plan.json|scenario.json); unexported → never client-settable
	llm  *llmRunConfig // ADR-063: parsed+validated per-run LLM config; unexported → never client-settable
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
// ADR-047 follow-on: `executed-plan.json` is what a REPLAY freezes into its own directory — the plan
// it accepted and ran. Without it a replay could never be replayed: this list is what `resolveFromRun`
// probes, a replay wrote only its report, and so the re-run control on a replay was permanently
// unavailable even though the plan was known. It is LAST in resolution order on purpose: a run that
// produced its own plan should replay that, not a copy of someone else's.
var replayInputs = []string{"plan.json", "scenario.json", "executed-plan.json"}

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
	rec := &run{ID: id, State: "running", Target: req.Target, Mode: req.Mode, Planner: req.Planner,
		ConversationID: req.ConversationID, ArtifactDir: artDir,
		StartedAt: time.Now().UTC().Format(time.RFC3339), stream: newRunStream()}
	s.mu.Lock()
	s.runs[id] = rec
	s.mu.Unlock()
	if s.store != nil { // M13: persist the run at "running" so it survives a control-API restart
		s.store.upsertRun(rec)
	}

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
	// ADR-063: layer the LLM connection into the spawn env — process env > per-run > persisted config.
	// os.Environ() (operator-controlled) still wins; resolveRunEnv only fills LLM_* it does not already set.
	cmd.Env = resolveRunEnv(os.Environ(), req.llm, s.mergedPersistedEnv())
	// Capture combined stdout+stderr into the run's stream (ring buffer + SSE fan-out). Setting
	// cmd.Stdout == cmd.Stderr makes os/exec merge them into ONE pipe with a single copy goroutine,
	// so lineWriter is intentionally not thread-safe — do NOT split Stdout/Stderr without a mutex.
	rec.sink = newLogSink(artDir) // M9-LIVE: runs/<id>/logs/{run.jsonl,run.log,events.jsonl}
	lw := &lineWriter{rs: rec.stream, sink: rec.sink}
	cmd.Stdout = lw
	cmd.Stderr = lw
	setProcGroup(cmd) // M9-LIVE: own process group, so a cancel reaches brain + executor + Chromium

	go func() {
		if err := cmd.Start(); err != nil {
			// A run that never started still has to FINISH properly: emit run.finished, release SSE
			// subscribers and close the log files, or the UI waits forever on a run that will never speak.
			s.mu.Lock()
			rec.State, rec.Error = "failed", err.Error()
			rec.FinishedAt = time.Now().UTC().Format(time.RFC3339)
			finishedAt := rec.FinishedAt
			s.mu.Unlock()
			if s.store != nil {
				s.store.upsertRun(rec)
			}
			lw.flush()
			rec.sink.close()
			rec.stream.append(aguiLine("run.finished", rec.ID, finishedAt,
				map[string]any{"exit_code": -1, "state": "failed"}))
			rec.stream.finish()
			return
		}
		s.mu.Lock()
		rec.pid = cmd.Process.Pid // published before the wait, so a cancel arriving immediately can act
		s.mu.Unlock()
		err := cmd.Wait()
		s.mu.Lock()
		rec.FinishedAt = time.Now().UTC().Format(time.RFC3339)
		rec.pid = 0
		switch {
		case rec.canceled:
			// A deliberate stop is NOT a failure and must not be reported as one: the process was
			// signalled, so it exits -1, which would otherwise read identically to a crash.
			rec.State, rec.ExitCode = "canceled", -1
		case err == nil:
			rec.State, rec.ExitCode = "done", 0
		default:
			if ee, ok := err.(*exec.ExitError); ok {
				rec.State, rec.ExitCode = "done", ee.ExitCode() // structured exit (0/1/2/3) is a valid outcome
			} else {
				rec.State, rec.Error = "failed", err.Error() // could not spawn (agentctl missing, etc.)
			}
		}
		// Snapshot the terminal state for the AG-UI event under the lock; append outside it (below).
		// A "failed" run never set ExitCode (zero value 0), which would read as a clean exit — emit -1
		// so the UI's run.finished branch does not mistake a spawn failure for success. `state` is carried
		// alongside so exit_code:-1 is unambiguous: a CANCELED run is state=canceled/exit_code=-1, a
		// signal-killed run is state=done/exit_code=-1
		// (os.ProcessState.ExitCode() returns -1 for a signalled process), a failed-spawn is
		// state=failed/exit_code=-1 — same as /v1/runs pairs state with exit_code.
		exitForEvent := rec.ExitCode
		if rec.State == "failed" {
			exitForEvent = -1
		}
		stateForEvent := rec.State
		finishedAt := rec.FinishedAt
		s.mu.Unlock()
		if s.store != nil { // M13: persist the terminal state (done/failed + exit_code + finished_at)
			s.store.upsertRun(rec)
		}
		s.persistScenario(rec) // M14 wave W3: wire the scenarios domain to a real caller (no-op if no scenario.json)
		s.persistResult(rec)   // M15 (ADR-051): wire the results + metrics domains (no-op if no store/artifacts)
		lw.flush()             // emit any trailing partial line (all brain output precedes run.finished)
		rec.sink.close()       // flush the record held back for repeat-collapsing, then close the files
		// M14 tail 1: the control-API injects run.finished — the one AG-UI event only it can know (the
		// process exit). Must precede finish(): append() no-ops once the stream is done. WS subscribers
		// get a typed run.finished frame (wsAGUIFrame); SSE gets the raw line inside a log event.
		rec.stream.append(aguiLine("run.finished", rec.ID, finishedAt, map[string]any{"exit_code": exitForEvent, "state": stateForEvent}))
		rec.stream.finish() // release SSE subscribers
	}()
	return rec
}

// scenarioArtifact mirrors the fields of scenario.json that the scenarios domain needs
// (brain/__main__.py:_write_scenario, ~line 46). Unknown/extra artifact fields are ignored.
type scenarioArtifact struct {
	PlanID    string          `json:"plan_id"`
	PlanHash  string          `json:"plan_hash"`
	TargetURL string          `json:"target_url"`
	Mode      string          `json:"mode"` // goal | describe -> Scenario.RunMode ("goal | describe" per proto)
	Unmatched int64           `json:"unmatched"`
	Steps     json.RawMessage `json:"steps"`
}

// persistScenario wires the `scenarios` domain to a real caller (M14_CONTRACT.md §3): if the run's
// artifact dir carries a scenario.json, index it as a Scenario record. plan_hash is read from the
// artifact (brain writes it, never recomputed here). Fail-open + best-effort, like upsertRun — a
// missing/malformed artifact just means there's nothing to persist, never a run-time error.
func (s *server) persistScenario(rec *run) {
	if s.store == nil {
		return
	}
	raw, ok := s.readArtifact(rec, "scenario.json")
	if !ok {
		return
	}
	var art scenarioArtifact
	if err := json.Unmarshal([]byte(raw), &art); err != nil || art.PlanID == "" {
		return // malformed/partial artifact — nothing safe to index
	}
	target := art.TargetURL
	if target == "" {
		target = rec.Target
	}
	s.store.saveScenario(&storepb.Scenario{
		ScenarioId:  art.PlanID, // brain: f"{run_id}-scenario" — stable, so a re-finish upserts in place
		Name:        target,
		Target:      target,
		RunMode:     art.Mode,
		PlanHash:    art.PlanHash,
		StepsJson:   string(art.Steps),
		Unmatched:   art.Unmatched,
		SourceRunId: rec.ID,
	})
}

// tokensBlock mirrors the M15.1 `tokens` summary the brain writes into plan.json / heal-report.json.
type tokensBlock struct {
	Prompt     float64 `json:"prompt"`
	Completion float64 `json:"completion"`
	Total      float64 `json:"total"`
}

// resultArtifact mirrors the heal-report.json / baseline-report.json fields the results domain needs
// (brain/replay.py). Authoring/explore runs have no heal-report — coverage comes from plan.json below.
type resultArtifact struct {
	PlanID      string            `json:"plan_id"`
	Healed      int64             `json:"healed"`
	Failed      int64             `json:"failed"`
	Steps       []json.RawMessage `json:"steps"`
	Regressions []json.RawMessage `json:"regressions"`
	Tokens      *tokensBlock      `json:"tokens"` // M15.1
	Models      map[string]string `json:"models"` // M15.1: {heal: <model id>}
	// ADR-076. Verdict is the run's OWN word for how it ended (brain/replay.py), which since ADR-071/072
	// distinguishes pass_with_drift / pass_with_app_faults / problem_drift / problem_app_faults from the
	// plain four. Drift/AppFaults are the counts behind those words.
	Verdict   string          `json:"verdict"`
	Drift     *driftBlock     `json:"drift"`
	AppFaults *appFaultsBlock `json:"app_faults"`
}

// driftBlock mirrors report["drift"] (brain/replay.py, ADR-071): how many locators were repaired from
// the plan's own alternatives (rebind) versus re-derived from the page as it is now (reground).
type driftBlock struct {
	Rebind   int64             `json:"rebind"`
	Reground int64             `json:"reground"`
	Elements []json.RawMessage `json:"elements"`
}

// appFaultsBlock mirrors report["app_faults"] (brain/replay.py, ADR-072): what the APPLICATION under
// test did wrong, as tallied by the executor. `errors` is the gateable subset.
type appFaultsBlock struct {
	Total  int64 `json:"total"`
	Errors int64 `json:"errors"`
}

// planCoverage mirrors the plan.json coverage field written by the explore report node (brain/graph.py).
type planCoverage struct {
	PlanID           string            `json:"plan_id"`
	CoverageAchieved float64           `json:"coverage_achieved"`
	Tokens           *tokensBlock      `json:"tokens"` // M15.1
	Models           map[string]string `json:"models"` // M15.1: {plan: <model id>}
}

// metricKV is a name/value pair for a metric point.
type metricKV struct {
	n string
	v float64
}

// modelPrices maps a model ID (as the brain reports it) to {input,output} USD per 1M tokens. Best-effort
// SUBSET — the calibrated Claude defaults; other priced cloud backends (gpt/glm/deepseek/qwen/grok/o3) and
// all local models (Ollama etc.) are absent -> cost 0 (token counts stay exact). Extend as backends are
// configured; a later pass may load docs/prices.json as the single source of truth. M15.1.
var modelPrices = map[string][2]float64{
	"claude-opus-4-8":   {5, 25},
	"claude-sonnet-4-6": {2, 10},
	"claude-haiku-4-5":  {1, 5},
}

// costUSD prices a run best-effort; an unknown/local model (or empty) returns 0.
func costUSD(model string, promptTok, completionTok float64) float64 {
	p, ok := modelPrices[model]
	if !ok {
		return 0
	}
	return (promptTok*p[0] + completionTok*p[1]) / 1e6
}

// refinedVerdicts is the closed set of states a run may report BEYOND the four the exit code can carry
// (brain/replay.py, ADR-071/072). An exit code cannot express them: 0 is 0 whether the interface drifted
// under the test or not, and 1 is 1 whether a step failed or a threshold reddened the build.
//
// It is a whitelist rather than a pass-through because `verdict` is a free string in the artifact and in
// the proto, and an artifact is a file on disk: accepting whatever it says would let a stray value into
// the Results domain that no reader knows how to render. An unknown word falls back to the exit code —
// the same choice the Logs view makes for an unknown event code (ADR-065).
var refinedVerdicts = map[string]bool{
	"pass_with_drift": true, "pass_with_app_faults": true,
	"problem_drift": true, "problem_app_faults": true,
}

// resultVerdict is what the Results domain records. The run's own word wins when it is one of the
// refined states; otherwise the exit code decides.
//
// ADR-076. Until now the two were derived independently — brain wrote pass_with_drift into
// heal-report.json while verdictEnum(0) wrote "pass" into the store, so the Results domain and the hub
// saw only the coarse four and the whole point of ADR-071/072 (that a clean pass and a pass that needed
// repairs stopped being the same news) died at the process boundary.
func resultVerdict(artifactVerdict string, exit int) string {
	if refinedVerdicts[artifactVerdict] {
		return artifactVerdict
	}
	return verdictEnum(exit)
}

// verdictEnum maps the structured exit code to ResultRecord.verdict (proto: pass|problem|regression|integrity).
func verdictEnum(exit int) string {
	switch exit {
	case 0:
		return "pass"
	case 2:
		return "regression"
	case 3:
		return "integrity"
	default:
		return "problem" // exit 1 (or any other non-success): the run found a problem
	}
}

// durationMs is finish-minus-start in ms from two RFC3339 stamps (second precision); 0 if unparseable/negative.
func durationMs(startedAt, finishedAt string) int64 {
	st, e1 := time.Parse(time.RFC3339, startedAt)
	fi, e2 := time.Parse(time.RFC3339, finishedAt)
	if e1 != nil || e2 != nil {
		return 0
	}
	if d := fi.Sub(st).Milliseconds(); d > 0 {
		return d
	}
	return 0
}

// persistResult wires the `results` + `metrics` domains to a real caller (M15, ADR-051): on run finish it
// assembles a ResultRecord from the heal-report (replay/baseline: steps/heal/fail/regressions) and/or
// plan.json (authoring: coverage), plus verdict (exit enum) + duration, then ingests the same values as
// metric points for trends. Fail-open + best-effort — a missing/malformed artifact just means less data,
// never a run-time error. Metric points carry labels_json={mode,target} (the ADR-056 commercial-BI seam:
// a commercial enterprise-BI module rolls up on these labels as a pure consumer of the store, no core fork).
func (s *server) persistResult(rec *run) {
	if s.store == nil {
		return
	}
	s.mu.RLock()
	state, exit, startedAt, finishedAt, mode, target := rec.State, rec.ExitCode, rec.StartedAt, rec.FinishedAt, rec.Mode, rec.Target
	s.mu.RUnlock()
	// A run that never executed (State="failed": agentctl couldn't spawn) has no real exit code — its
	// zero-value ExitCode 0 would map verdictEnum→"pass" and inflate the pass-rate. Skip it: the runs
	// domain already records the failure (state+error); it must not pollute the results/metrics substrate.
	if state != "done" {
		return
	}

	rr := &storepb.ResultRecord{
		RunId: rec.ID, Mode: mode, Verdict: verdictEnum(exit),
		ExitCode: int64(exit), DurationMs: durationMs(startedAt, finishedAt),
	}
	var stepN, regN int64
	var tok *tokensBlock // M15.1: per-run token totals, from whichever report the run produced
	costModel := ""
	var drift *driftBlock         // ADR-076: the counts behind pass_with_drift / problem_drift
	var appFaults *appFaultsBlock // ADR-076: the counts behind pass_with_app_faults / problem_app_faults
	// Replay/baseline runs carry a heal-report; authoring/explore runs don't (they carry plan.json).
	raw, ok := s.readArtifact(rec, "heal-report.json")
	if !ok {
		raw, ok = s.readArtifact(rec, "baseline-report.json")
	}
	if ok {
		var art resultArtifact
		if json.Unmarshal([]byte(raw), &art) == nil {
			rr.PlanId, rr.Healed, rr.Failed = art.PlanID, art.Healed, art.Failed
			stepN, regN = int64(len(art.Steps)), int64(len(art.Regressions))
			if b, e := json.Marshal(art.Steps); e == nil {
				rr.StepsJson = string(b)
			}
			if b, e := json.Marshal(art.Regressions); e == nil {
				rr.RegressionsJson = string(b)
			}
			if art.Tokens != nil { // M15.1: replay heal-LLM tokens + model (for cost)
				tok, costModel = art.Tokens, art.Models["heal"]
			}
			// ADR-076: the run's own verdict outranks the exit-code derivation, and the counts behind it
			// ride the metrics domain (MetricPoint is name/value, so no schema change is involved).
			rr.Verdict = resultVerdict(art.Verdict, exit)
			drift, appFaults = art.Drift, art.AppFaults
		}
	}
	if praw, pok := s.readArtifact(rec, "plan.json"); pok { // authoring/explore: coverage_achieved
		var pc planCoverage
		if json.Unmarshal([]byte(praw), &pc) == nil {
			rr.Coverage = pc.CoverageAchieved
			if rr.PlanId == "" {
				rr.PlanId = pc.PlanID
			}
			if tok == nil && pc.Tokens != nil { // M15.1: authoring planner-LLM tokens (heal-report takes precedence)
				tok, costModel = pc.Tokens, pc.Models["plan"]
			}
		}
	}
	s.store.saveResult(rr)

	// Ingest the same values as metric points (trends). labels_json = the org/project tagging seam (ADR-056).
	ts := float64(time.Now().Unix())
	labelsB, _ := json.Marshal(map[string]string{"mode": mode, "target": target, "model": costModel})
	labels := string(labelsB)
	pass := 0.0
	if exit == 0 {
		pass = 1.0
	}
	pts := []metricKV{
		{"pass", pass}, {"coverage", rr.Coverage}, {"healed", float64(rr.Healed)},
		{"failed", float64(rr.Failed)}, {"regressions", float64(regN)}, {"steps", float64(stepN)},
		{"duration_ms", float64(rr.DurationMs)},
	}
	// ADR-076. Emitted only when the run actually produced the block, so a series never gains a zero
	// point from a mode that cannot report it: explore/goal runs carry no heal-report, and a run with no
	// drift at all is not the same fact as a run that was never able to have any.
	if drift != nil {
		pts = append(pts,
			metricKV{"drift_total", float64(drift.Rebind + drift.Reground)},
			metricKV{"drift_rebind", float64(drift.Rebind)},
			metricKV{"drift_reground", float64(drift.Reground)})
	}
	if appFaults != nil {
		pts = append(pts,
			metricKV{"app_faults_total", float64(appFaults.Total)},
			metricKV{"app_faults_errors", float64(appFaults.Errors)})
	}
	if tok != nil { // M15.1: exact token counts + best-effort cost (local/unknown model -> 0)
		pts = append(pts,
			metricKV{"tokens_total", tok.Total}, metricKV{"tokens_prompt", tok.Prompt},
			metricKV{"tokens_completion", tok.Completion},
			metricKV{"cost_usd", costUSD(costModel, tok.Prompt, tok.Completion)})
	}
	batch := &storepb.MetricsBatch{Points: make([]*storepb.MetricPoint, 0, len(pts))}
	for _, p := range pts {
		batch.Points = append(batch.Points, &storepb.MetricPoint{
			RunId: rec.ID, Ts: ts, Name: p.n, Value: p.v, LabelsJson: labels,
		})
	}
	s.store.ingestMetrics(batch)
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
	// ADR-063: a per-run LLM override applies to every mode (replay/baseline heal LLM too). Validated
	// here (backend enum, base_url shape, secret refusal) so a bad value is a 400, not a broken spawn.
	llmCfg, err := parseRunLLM(req.LLM)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "llm: " + err.Error()})
		return
	}
	req.llm = llmCfg
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
	live := make(map[string]bool, len(s.runs))
	out := make([]run, 0, len(s.runs)) // VALUE copies: snapshot mutable fields under the lock (race-free marshal)
	for id, rr := range s.runs {
		live[id] = true
		out = append(out, *rr)
	}
	s.mu.RUnlock()
	// M13 (ADR-050): fold in persisted runs from the gateway (e.g. from before a restart). The in-memory
	// copy wins for a run that's both live and stored — it has the freshest state + the live stream.
	if s.store != nil {
		if stored, ok := s.store.listRuns(); ok {
			for _, rr := range stored {
				if !live[rr.ID] {
					out = append(out, *rr)
				}
			}
		}
	}
	views := make([]runView, 0, len(out))
	for i := range out {
		views = append(views, s.runView(&out[i]))
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": views})
}

// runView is a run as the UI reads it: the record plus facts that are DERIVED at read time rather than
// stored. It exists so `has_plan` cannot become a persisted field that drifts out of step with the disk.
//
// ADR-047 follow-on: the re-run and baseline controls were always enabled, and pressing them on a run
// that had died before plan freeze answered `400 from_run: no replayable plan` — a machine sentence for
// a situation the interface already had everything it needed to prevent. A run only becomes replayable
// when the freeze wrote an artifact, so the answer lives on disk, not in the record.
type runView struct {
	*run
	HasPlan bool `json:"has_plan"`
}

// runView derives the read-time facts. hasReplayablePlan calls resolveFromRun itself rather than
// re-listing replayInputs: two lists of "what counts as a replayable plan" WILL drift, and the failure
// mode of that drift is an enabled button that 400s — the exact defect being closed.
func (s *server) runView(rec *run) runView {
	_, _, err := s.resolveFromRun(rec.ID)
	return runView{run: rec, HasPlan: err == nil}
}

func (s *server) handleGetRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.mu.RLock()
	rec, ok := s.runs[id]
	var snap run
	if ok {
		snap = *rec // snapshot under the lock — the completion goroutine writes State/ExitCode/etc. under s.mu
	}
	s.mu.RUnlock()
	if !ok && s.store != nil { // M13: a run from a prior control-API process survives in the gateway
		if hist, found := s.store.getRun(id); found {
			snap, ok = *hist, true
		}
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	writeJSON(w, http.StatusOK, s.runView(&snap))
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
	"junit.xml":             true, // ADR-073: the machine contract every CI consumes
	"executed-plan.json":    true, // ADR-047 follow-on: the plan a replay ran, so the replay is replayable
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

// --- M14 wave W3: scenarios/tests/chats HTTP surface (library + conversation management) ---------
// All token-gated, over the fail-open store-gateway client (cmd/control-api/store.go). Unlike runs,
// these domains have no in-memory fallback — a gateway error degrades to an empty list / 404-ish
// response, never a 503 (M14_CONTRACT.md §3).

// storeMarker reports whether a persistence store is wired, for the list endpoints to carry.
//
// Without it, every list answers an empty 200 in the standalone tier — and an empty 200 means BOTH
// "nothing has been saved yet" and "this deployment cannot save anything". Alex read that as "the library
// does not load", which is the correct reading of an interface that says nothing. A list cannot answer
// with a status code the way a single document can, because an empty list IS a valid answer when a store
// exists. So the fact travels alongside the data and the UI can say which case it is looking at.
// (`/v1/config` used to answer 501 here; ADR-075 gave the standalone tier a real file, so it now serves
// the request and names the tier it served it from.)
func (s *server) storeMarker() map[string]any {
	if s.store != nil {
		return map[string]any{"store": true}
	}
	return map[string]any{
		"store": false,
		"store_reason": "this deployment has no store-gateway, so nothing is persisted — start it with " +
			"`docker compose --profile store up -d store-gateway` and set CONTROL_API_STORE_ADDR",
	}
}

// withStore merges the store marker into a response body.
func (s *server) withStore(body map[string]any) map[string]any {
	for k, v := range s.storeMarker() {
		body[k] = v
	}
	return body
}

func (s *server) handleListScenarios(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var scenarios []*storepb.Scenario
	var total int64
	if s.store != nil {
		if sl, ok := s.store.listScenarios(r.URL.Query().Get("target")); ok {
			scenarios, total = sl.Scenarios, sl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"scenarios": scenarios, "total": total}))
}

func (s *server) handleGetScenario(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var sc *storepb.Scenario
	var ok bool
	if s.store != nil {
		sc, ok = s.store.getScenario(r.PathValue("id"))
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such scenario"})
		return
	}
	writeJSON(w, http.StatusOK, sc)
}

func (s *server) handleDeleteScenario(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	if s.store != nil {
		s.store.deleteScenario(r.PathValue("id"))
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"}) // idempotent: missing id (or no store) is still success
}

func (s *server) handleListTests(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var tests []*storepb.TestRecord
	var total int64
	if s.store != nil {
		if tl, ok := s.store.listTests(); ok {
			tests, total = tl.Tests, tl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"tests": tests, "total": total}))
}

func (s *server) handleGetTest(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var t *storepb.TestRecord
	var ok bool
	if s.store != nil {
		t, ok = s.store.getTest(r.PathValue("id"))
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such test"})
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) handleDeleteTest(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	if s.store != nil {
		s.store.deleteTest(r.PathValue("id"))
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"}) // idempotent
}

type promoteRequest struct {
	ScenarioID string `json:"scenario_id"`
	Name       string `json:"name"`
	Schedule   string `json:"schedule"` // reserved: stored, NOT executed (no scheduler in M13/M14)
}

// handlePromoteTest freezes a saved scenario into a test (ADR-052). No in-memory fallback exists for
// a brand-new test record, so an unreachable/absent store surfaces as 404 (same "404-ish, not 503"
// fail-open shape as the reads above), same as promoting an unknown scenario_id.
func (s *server) handlePromoteTest(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var req promoteRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad JSON: " + err.Error()})
		return
	}
	if req.ScenarioID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "scenario_id is required"})
		return
	}
	var t *storepb.TestRecord
	var ok bool
	if s.store != nil {
		t, ok = s.store.promoteTest(&storepb.PromoteReq{ScenarioId: req.ScenarioID, Name: req.Name, Schedule: req.Schedule})
	}
	if !ok || !t.Found {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such scenario to promote (or store-gateway unavailable)"})
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) handleListChats(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var chats []*storepb.ChatProjection
	var total int64
	if s.store != nil {
		if cl, ok := s.store.listChats(); ok {
			chats, total = cl.Chats, cl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"chats": chats, "total": total}))
}

func (s *server) handleGetChat(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var c *storepb.ChatProjection
	var ok bool
	if s.store != nil {
		c, ok = s.store.getChat(r.PathValue("id"))
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such chat"})
		return
	}
	writeJSON(w, http.StatusOK, c)
}

func (s *server) handleDeleteChat(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	if s.store != nil {
		s.store.deleteChat(r.PathValue("id"))
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"}) // idempotent
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

// parseIntQuery reads a non-negative int query param, or def when absent/invalid.
func parseIntQuery(r *http.Request, key string, def int64) int64 {
	if v := r.URL.Query().Get(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n >= 0 {
			return n
		}
	}
	return def
}

// --- M15 (ADR-051): results + metrics-trends HTTP surface (native charts in the SPA) --------------
// Token-gated, over the fail-open store-gateway client. Like scenarios/tests/chats, these have no
// in-memory fallback — a gateway error / no store degrades to an empty list, never a 503.

func (s *server) handleListResults(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var results []*storepb.ResultRecord
	var total int64
	if s.store != nil {
		if rl, ok := s.store.listResults(parseIntQuery(r, "limit", 200), parseIntQuery(r, "offset", 0)); ok {
			results, total = rl.Results, rl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"results": results, "total": total}))
}

func (s *server) handleGetResult(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var rr *storepb.ResultRecord
	var ok bool
	if s.store != nil {
		rr, ok = s.store.getResult(r.PathValue("id"))
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such result"})
		return
	}
	writeJSON(w, http.StatusOK, rr)
}

func (s *server) handleTrends(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	metric := r.URL.Query().Get("metric")
	if metric == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "metric query param required (e.g. pass, coverage, duration_ms, healed, failed, regressions, steps)"})
		return
	}
	var points []*storepb.TrendPoint
	if s.store != nil {
		if tr, ok := s.store.trends(metric, parseIntQuery(r, "window", 50)); ok {
			points = tr.Points
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"metric": metric, "points": points}))
}

func (s *server) mux() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /healthz", s.handleHealthz)
	m.HandleFunc("GET /readyz", s.handleReadyz) // M11.5 PR-5 (ADR-062): unauth like /healthz, but probes real deps
	m.HandleFunc("GET /v1/config-schema", s.handleConfigSchema)
	// M11.5 PR-5: the service tier of the tiered config (ADR-049). Token-gated both ways.
	m.HandleFunc("GET /v1/config", s.handleGetConfig)
	m.HandleFunc("PUT /v1/config", s.handlePutConfig)
	m.HandleFunc("POST /v1/runs", s.handleCreateRun)
	m.HandleFunc("POST /v1/chat/completions", s.handleChatCompletions)
	m.HandleFunc("GET /v1/runs", s.handleListRuns)
	m.HandleFunc("GET /v1/runs/{id}", s.handleGetRun)
	m.HandleFunc("GET /v1/runs/{id}/events", s.handleRunEvents)
	m.HandleFunc("GET /v1/runs/{id}/logs", s.handleRunLogs)       // M9-LIVE: structured diagnostics
	m.HandleFunc("POST /v1/runs/{id}/cancel", s.handleCancelRun)  // M9-LIVE: stop a running run
	m.HandleFunc("GET /v1/events-catalog", s.handleEventsCatalog) // M9-LIVE: bilingual message list
	m.HandleFunc("GET /v1/runs/{id}/artifact", s.handleRunArtifact)
	m.HandleFunc("GET /v1/stream", s.handleStream)
	// M14 wave W3: scenarios/tests/chats HTTP surface (library + conversation management)
	m.HandleFunc("GET /v1/scenarios", s.handleListScenarios)
	m.HandleFunc("GET /v1/scenarios/{id}", s.handleGetScenario)
	m.HandleFunc("DELETE /v1/scenarios/{id}", s.handleDeleteScenario)
	m.HandleFunc("GET /v1/tests", s.handleListTests)
	m.HandleFunc("GET /v1/tests/{id}", s.handleGetTest)
	m.HandleFunc("POST /v1/tests/promote", s.handlePromoteTest)
	m.HandleFunc("DELETE /v1/tests/{id}", s.handleDeleteTest)
	m.HandleFunc("GET /v1/chats", s.handleListChats)
	m.HandleFunc("GET /v1/chats/{id}", s.handleGetChat)
	m.HandleFunc("DELETE /v1/chats/{id}", s.handleDeleteChat)
	// M15 (ADR-051): results + metrics-trends surface for the SPA native charts
	m.HandleFunc("GET /v1/results", s.handleListResults)
	m.HandleFunc("GET /v1/results/{id}", s.handleGetResult)
	m.HandleFunc("GET /v1/trends", s.handleTrends)
	// ADR-064 Mode 3 — registered only when the UI is actually served, so Modes 1/2 keep exactly the
	// mux they had. Order does not matter: net/http picks the most specific pattern, so "/v1/" only
	// ever sees paths no real endpoint claimed, and "GET /" only what is neither /v1/ nor /healthz.
	if s.ui != nil && s.ui.enabled {
		if s.token != "" {
			m.HandleFunc("GET /v1/ui-token", s.handleUIToken)
		}
		// Method-scoped on purpose: a bare "/v1/" conflicts with "GET /" — net/http refuses to rank a
		// pattern matching fewer methods but a more general path, and panics at registration. A "GET"
		// pattern also matches HEAD, so these two cover every method that has a catch-all below; an
		// unknown POST /v1/... still falls through to the mux's own 404, exactly as it does today.
		m.HandleFunc("GET /v1/", s.handleV1NotFound)
		m.Handle("GET /", s.ui.handler())
	}
	return s.cors(m)
}

// displayAddr turns a bind address into something clickable in a terminal: a wildcard bind (which is
// what the container uses so the compose port map works) is shown as loopback, because that is where
// the operator actually reaches it.
func displayAddr(addr string) string {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return addr
	}
	switch strings.Trim(host, "[]") {
	case "", "0.0.0.0", "::":
		host = "127.0.0.1"
	}
	return net.JoinHostPort(host, port)
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
	// M-UI-MODES (ADR-064): the operator no longer has to invent a secret before the first start —
	// resolveToken reuses (or creates, 0600) state/control-api.token. CONTROL_API_TOKEN still wins, and
	// CONTROL_API_AUTOTOKEN=0 keeps the pre-ADR-064 fail-closed read-only instance. See token.go.
	tok, tokSrc, tokPath, tokWarnings := resolveToken(repo)
	s := &server{
		repo:       repo,
		agentctl:   envOr("CONTROL_API_AGENTCTL", filepath.Join(repo, "bin", "agentctl")),
		token:      tok,
		corsAllow:  map[string]bool{},
		orchAddr:   os.Getenv("CONTROL_API_ORCH_ADDR"), // M9.8 F4 (ADR-054): e.g. "unix:/abs/state/sentinel-orch-<id>.sock"
		publicBind: !isLocalBind(addr),
		ui:         newUIServer(), // ADR-064: disabled unless CONTROL_API_SERVE_UI / CONTROL_API_UI_DIR
		runs:       map[string]*run{},
		llmBaseURL: os.Getenv("LLM_BASE_URL"), // M11.5 PR-5: the /readyz llm probe target (env wins over the stored config)
	}
	for _, o := range strings.Split(os.Getenv("CONTROL_API_CORS_ORIGINS"), ",") {
		if o = strings.TrimSpace(o); o != "" {
			s.corsAllow[o] = true
		}
	}
	// M13 (ADR-050): connect to a persistent store-gateway if configured; else runs stay in-memory
	// (standalone/offline path, unchanged). Fail-open — an unreachable gateway only warns.
	if sa := os.Getenv("CONTROL_API_STORE_ADDR"); sa != "" {
		s.storeAddr = sa // remembered even on failure — see server.storeAddr / configTier (ADR-075)
		if sc, err := newStoreClient(sa, os.Getenv("STORE_TOKEN")); err != nil {
			fmt.Fprintf(os.Stderr, "control-api: WARNING — store-gateway %q unreachable: %v (runs stay in-memory, lost on restart)\n", sa, err)
		} else {
			s.store = sc
			defer sc.close()
			fmt.Fprintf(os.Stderr, "control-api: persisting runs to store-gateway at %s\n", sa)
		}
	}
	// M11.5 PR-5 (ADR-062): informational log; must not delay ListenAndServe. ADR-075 moved it out of the
	// store branch — the standalone tier has a config to report too, and the configured-but-down case has
	// a warning worth printing at the moment the operator is still looking at the terminal.
	go s.loadStartupConfig()
	for _, w := range tokWarnings {
		fmt.Fprintf(os.Stderr, "control-api: WARNING — %s\n", w)
	}
	switch tokSrc {
	case tokenDisabled:
		fmt.Fprintln(os.Stderr, "control-api: WARNING — no bearer token (CONTROL_API_AUTOTOKEN=0); POST /v1/runs will 403 (read-only).")
	case tokenFromEnv:
		fmt.Fprintln(os.Stderr, "control-api: bearer token from CONTROL_API_TOKEN")
	default:
		fmt.Fprintf(os.Stderr, "control-api: bearer token (%s) → %s\n", tokSrc, tokPath)
		// In Modes 1-2 the operator has to get the value into the UI's Settings field (or a script) and
		// their terminal is the only channel we have. Mode 3 replaces this with the single-use bootstrap
		// link below, so the secret never needs to sit in the log. Opt out: CONTROL_API_PRINT_TOKEN=0.
		if !s.ui.enabled && !envDisabled("CONTROL_API_PRINT_TOKEN") {
			fmt.Fprintf(os.Stderr, "control-api: CONTROL_API_TOKEN=%s\n", tok)
		}
	}
	// ADR-064 Mode 3: announce the UI and mint the one-time bootstrap nonce. The nonce appears ONLY
	// here, on the operator's own terminal — that is what keeps "can reach the port" from meaning
	// "holds the token" once the process is up.
	if s.ui.enabled {
		base := "http://" + displayAddr(addr)
		fmt.Fprintf(os.Stderr, "control-api: serving the UI (%s) at %s/\n", s.ui.source, base)
		switch {
		case s.token == "":
			fmt.Fprintln(os.Stderr, "control-api: no token → the UI is read-only; unset CONTROL_API_AUTOTOKEN=0 to enable runs.")
		default:
			if n := s.ui.arm(bootstrapTTL()); n != "" {
				fmt.Fprintf(os.Stderr, "control-api: open %s/?bootstrap=%s  (one-time, valid %s)\n", base, n, bootstrapTTL())
			} else {
				fmt.Fprintf(os.Stderr, "control-api: bootstrap disabled — paste the token from %s into the UI\n", tokPath)
			}
		}
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
