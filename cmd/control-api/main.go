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
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
	"github.com/AlexGromer/sentinel/internal/svclog"
)

// version is stamped by the release build (`go build -ldflags "-X main.version=<tag>"`, .github/workflows/
// release.yml, and the Dockerfile's VERSION arg since ADR-110). It MUST stay a var — the linker cannot
// write into a const, so declaring it const made the -X flag a silent no-op and /healthz reported "0.1.0"
// on every tagged release (fixed with ADR-064).
//
// The default is "dev", not a version number. It used to be "0.1.0", which meant an UNSTAMPED build did
// not look unstamped: /healthz answered a plausible release number, and the container images — which
// were not passing -X at all until ADR-110 — reported it on every published tag. A default that has to
// be hand-bumped to stay honest is the same defect the const/var note above describes, one layer up.
var version = "dev"

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
	// ADR-109: the local account that started this run, "" when nobody is logged in or the caller was
	// the machine token. Serialized so a UI can say whose a run is; scoping reads it, not the reverse.
	Owner string `json:"owner,omitempty"`
	// FaultDomain (HEALTH-004) says WHOSE problem a non-green outcome is: none | app | tool | test |
	// config. Computed ONCE when the run finishes (see faultDomain) and read by three consumers — this
	// JSON, the run.finished frame and the Results record — because deriving it three times is how the
	// verdict and the artifact came to disagree before ADR-076.
	FaultDomain string `json:"fault_domain,omitempty"`

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
	// ADR-109: live local-account sessions. In memory on purpose — a restart logging everyone out is
	// correct for a tool whose machine token is already per-process, and persisting them would mean a
	// second credential store to protect, expire and purge.
	sessions *sessionStore
	// accounts memoizes "does this deployment have any account?" — the question that decides whether
	// the pre-identity open reads still answer without a credential (cmd/control-api/access.go).
	accounts accountsMemo
	// journal is the SERVICE-plane log (HEALTH-005): what the tool itself did, as opposed to what a
	// run did. Nil when it could not be opened — every call site tolerates that, because a service
	// must not refuse to start over its own log file, and the failure is reported once by svclog.Open.
	journal *svclog.Writer
	mu      sync.RWMutex
	runs    map[string]*run

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

// authed reports whether a request carries ANY accepted credential: the configured machine token
// (constant-time) or a live local-account session (ADR-109).
//
// Changed in one place on purpose. Twenty-four call sites ask this question, and editing each to also
// accept a session would have been twenty-four chances to miss one — and a missed one is not a broken
// feature but a route that silently stays machine-only, which reads to a logged-in person as the
// product ignoring them. Authentication ("may this caller act?") and scoping ("whose rows?") stay
// separate: the handlers that read data ask callerOf for an owner.
//
// Fail-closed still holds. With no machine token configured there is no credential that can create an
// account, so no account can exist, so no session can either — the empty-token case refuses mutations
// exactly as it did.
func (s *server) authed(r *http.Request) bool {
	_, ok := s.callerOf(r)
	return ok
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
		// ADR-108b added `chat`: conversation is its own role, so an operator can point talking and
		// planning at different endpoints (the planner may be a large remote model while the chat that
		// answers a question runs on whatever is local). A role the brain honours but the schema does not
		// publish is a knob nobody can find — the same "capability nobody can reach" this milestone exists
		// to close.
		"roles": []string{"planner", "heal", "chat"}, // per-role override LLM_<KEY>_<ROLE> falls back to global LLM_<KEY>
		// ADR-107: `fields` is the per-run half of the one configuration model, and every key here is
		// settable on POST /v1/runs — asserted by TestRunRequestCoversEverySchemaField, which walks this
		// map rather than listing what it expects to find.
		//
		// `group` answers the question the field belongs to, exactly as `settings` does, so a UI can lay
		// the form out from the schema instead of hard-coding which input sits under which heading. The
		// hub used to hard-code that, which is why nine of these fields existed as inputs the submit
		// handler never read.
		"fields": map[string]any{
			"target":          map[string]any{"type": "string", "required": true, "group": "run"},
			"goal":            map[string]any{"type": "string", "group": "run"},
			"describe":        map[string]any{"type": "string", "group": "run"},
			"conversation_id": map[string]any{"type": "string", "group": "run"}, // M9.10: multi-turn chat thread key (resume by conversation_id)
			"coverage_target": map[string]any{"type": "number", "default": 0.85, "group": "run"},
			"max_steps":       map[string]any{"type": "int", "default": 40, "group": "run"},
			"scenario":        map[string]any{"type": "string", "group": "run"}, // --scenario: select a named scenario out of the RunConfig
			"plan_budget":     map[string]any{"type": "int", "default": 50000, "group": "budgets"},
			"heal_budget":     map[string]any{"type": "int", "default": 20000, "group": "budgets"},
			"total_budget":    map[string]any{"type": "int", "default": 0, "group": "budgets"},
			// Session reuse. These reach the brain through the RunConfig `auth:` block (writeRunConfig),
			// never as environment: PLAN_FILE does not survive agentctl's env allowlist, and widening that
			// allowlist to carry a convenience would spend a security boundary.
			"storage_state":      map[string]any{"type": "string", "group": "auth"},
			"storage_state_save": map[string]any{"type": "string", "group": "auth"},
			"login_plan":         map[string]any{"type": "string", "group": "auth"},
			"pw_no_trace":        map[string]any{"type": "bool", "default": false, "group": "auth"},
			// Determinism guards. `ci` forbids `force_replay`; the rule lives in agentctl and the API
			// rejects the pair early so a person gets a 400 instead of a run that dies at startup.
			"ci":           map[string]any{"type": "bool", "default": false, "group": "gates"},
			"force_replay": map[string]any{"type": "bool", "default": false, "group": "gates"},
			"aut_version":  map[string]any{"type": "string", "group": "gates"},
			"heal_llm":     map[string]any{"type": "bool", "default": false, "group": "healing"},
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
		// ADR-085: operator SETTINGS — knobs that change how a run behaves or how long its artifacts
		// live. They were reachable only by exporting an environment variable, i.e. discoverable only by
		// reading source, which is the same failure `[DOCS-REGISTERS]` describes: a capability nobody
		// can find does not exist for the person who needs it.
		//
		// `hint` is BILINGUAL and `note` is not, deliberately. The wizard renders RU/EN and the preset
		// code says why a plain `note` is never shown: "English provenance data for the JSON/docs, not
		// UI chrome — rendering it here would code-switch the RU interface" (ADR-061). A setting whose
		// whole point is to be explained needs an explanation the reader's interface can actually use.
		//
		// `min`/`step` travel with the field instead of living in the wizard's per-key branches, so a
		// new setting arrives with sane input constraints rather than inheriting `step=1000` from an
		// `else` written for token budgets.
		"settings": settingsSchema,
		// ADR-109 / Alex's directive: which sections of the stored document configure the TOOL (admin
		// only) and which belong to the person using it. Published verbatim from the one map that
		// enforces it (configscope.go), so an interface disables what a caller may not change instead of
		// letting them fill in a form whose save is going to be refused.
		"config_sections": configSectionScope,
		"note":            "secrets (LLM_API_KEY/ANTHROPIC_API_KEY) go in the control-api process env, never in this payload",
	})
}

// settingsSchema is the single source for operator-facing settings: one entry per environment
// variable a person may reasonably want to change, with the default READ FROM THE SAME PLACE the code
// reads it (see the `env` field — the pairing is asserted by TestSettingsSchemaDefaultsMatchCode).
//
// Grouping is by the question the operator is answering, not by which binary happens to read the
// variable: "how long do artifacts live" is one decision even though logs are pruned by agentctl and
// traces by the brain.
var settingsSchema = map[string]any{
	// --- retention: how long artifacts of past runs stay on disk ------------------------------------
	"log_keep": map[string]any{
		"env": "SENTINEL_LOG_KEEP", "type": "int", "default": 0, "min": 0, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Логи скольких последних прогонов сохранять. 0 — не удалять никогда.",
			"en": "How many recent runs keep their logs. 0 — never delete.",
		},
	},
	// ADR-099: the run DIRECTORY, which until now had no lifetime at all. Same shape as the two
	// above deliberately — one question ("how long does this kind of thing live"), one pair of knobs,
	// so an operator does not have to learn a second vocabulary for the same decision.
	"run_keep": map[string]any{
		"env": "SENTINEL_RUN_KEEP", "type": "int", "default": 0, "min": 0, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Каталоги скольких последних прогонов сохранять целиком. 0 — не удалять никогда. Самый свежий не удаляется в любом случае.",
			"en": "How many recent runs keep their whole directory. 0 — never delete. The newest is never removed regardless.",
		},
	},
	"run_ttl_hours": map[string]any{
		"env": "SENTINEL_RUN_TTL_HOURS", "type": "int", "default": 0, "min": 0, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Через сколько часов удалять каталог прогона целиком. 0 — не удалять по возрасту.",
			"en": "After how many hours a run's whole directory is deleted. 0 — never delete by age.",
		},
	},
	"log_ttl_hours": map[string]any{
		"env": "SENTINEL_LOG_TTL_HOURS", "type": "int", "default": 0, "min": 0, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Логи старше скольких часов удалять. 0 — не удалять никогда. Логи содержат вывод вашего приложения; секреты в них уже отредактированы при записи (ADR-081), но это данные о прогоне.",
			"en": "Delete logs older than this many hours. 0 — never delete. Logs carry your application's own output; secrets are already redacted at write time (ADR-081), but this is still run data.",
		},
	},
	"trace_keep": map[string]any{
		"env": "SENTINEL_TRACE_KEEP", "type": "int", "default": 10, "min": -1, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Трейсы скольких последних прогонов сохранять. Отрицательное значение отключает удаление по счётчику.",
			"en": "How many recent runs keep their trace. A negative value disables count-based pruning.",
		},
	},
	"trace_ttl_hours": map[string]any{
		"env": "SENTINEL_TRACE_TTL_HOURS", "type": "int", "default": 0, "min": 0, "step": 1, "group": "retention",
		"hint": map[string]string{
			"ru": "Трейсы старше скольких часов удалять. 0 — не удалять по возрасту.",
			"en": "Delete traces older than this many hours. 0 — no age-based pruning.",
		},
	},
	"trace_always": map[string]any{
		"env": "SENTINEL_TRACE_ALWAYS", "type": "bool", "default": false, "group": "retention",
		"hint": map[string]string{
			"ru": "Сохранять трейс и у зелёного прогона. По умолчанию трейс остаётся только у прогона, который завершился не нулевым кодом: он несёт живой DOM вашего приложения, и у зелёного прогона разбирать нечего (ADR-084).",
			"en": "Keep the trace even on a green run. By default a trace survives only when the run exited non-zero: it carries your application's live DOM, and a green run has nothing to diagnose (ADR-084).",
		},
	},
	// --- healing: when a repaired locator is trusted -------------------------------------------------
	"heal_auto": map[string]any{
		"env": "SENTINEL_HEAL_AUTO", "type": "number", "default": 0.85, "min": 0, "max": 1, "step": 0.05,
		"group": "healing",
		"hint": map[string]string{
			"ru": "Уверенность, начиная с которой починка применяется молча. ⚠ Число не откалибровано — это приор, а не измеренная вероятность (GAP-RISK-002).",
			"en": "Confidence at or above which a repair is applied silently. ⚠ Uncalibrated — a prior, not a measured probability (GAP-RISK-002).",
		},
	},
	"heal_flag": map[string]any{
		"env": "SENTINEL_HEAL_FLAG", "type": "number", "default": 0.60, "min": 0, "max": 1, "step": 0.05,
		"group": "healing",
		"hint": map[string]string{
			"ru": "Уверенность, начиная с которой починка применяется, но помечается для проверки человеком. Ниже — шаг падает.",
			"en": "Confidence at or above which a repair is applied but flagged for human review. Below it the step fails.",
		},
	},
	"heal_llm": map[string]any{
		"env": "HEAL_LLM", "type": "bool", "default": false, "group": "healing",
		"hint": map[string]string{
			"ru": "Разрешить ИИ выбирать элемент заново, когда ни один замороженный ключ не сработал.",
			"en": "Let the model re-pick the element when no frozen key resolves.",
		},
	},
	"heal_visual": map[string]any{
		"env": "HEAL_VISUAL", "type": "bool", "default": false, "group": "healing",
		"hint": map[string]string{
			"ru": "Разрешить поиск элемента по скриншоту (нужна vision-модель). Требует включённого LLM_VISION.",
			"en": "Allow locating the element from a screenshot (needs a vision model). Requires LLM_VISION.",
		},
	},
	// --- build gates: what turns a finished run red --------------------------------------------------
	"fail_on_heal": map[string]any{
		"env": "SENTINEL_FAIL_ON_HEAL", "type": "int", "default": 0, "min": 0, "step": 1, "group": "gates",
		"hint": map[string]string{
			"ru": "Ронять сборку, если элементов с дрейфом не меньше N. 0 — выключено (сообщать, но не гейтить).",
			"en": "Fail the build when drifted elements reach N. 0 — off (report, do not gate).",
		},
	},
	"fail_on_app_errors": map[string]any{
		"env": "SENTINEL_FAIL_ON_APP_ERRORS", "type": "int", "default": 0, "min": 0, "step": 1, "group": "gates",
		"hint": map[string]string{
			"ru": "Ронять сборку, если само приложение выдало не меньше N ошибок (исключения, ошибки консоли, упавшие запросы, 4xx-5xx). 0 — выключено. Предупреждения не считаются.",
			"en": "Fail the build when the application itself produced N or more errors (exceptions, console errors, failed requests, 4xx-5xx). 0 — off. Warnings do not count.",
		},
	},
	"visual_authoritative": map[string]any{
		"env": "SENTINEL_VISUAL_AUTHORITATIVE", "type": "bool", "default": false, "group": "gates",
		"hint": map[string]string{
			"ru": "Считать расхождение скриншота с эталоном регрессией, а не справочным сигналом. По умолчанию выключено: побайтовая стабильность кадра в реальном браузере ещё не подтверждена.",
			"en": "Treat a screenshot difference from the golden as a regression rather than advisory. Off by default: byte-stability of a frame in a real browser is not yet proven.",
		},
	},
	// --- human in the loop ---------------------------------------------------------------------------
	"auto_hitl_threshold": map[string]any{
		"env": "SENTINEL_AUTO_HITL_THRESHOLD", "type": "int", "default": 0, "min": 0, "step": 1, "group": "hitl",
		"hint": map[string]string{
			"ru": "После скольких подряд неудачных починок звать человека. 0 — не звать никогда.",
			"en": "How many consecutive failed repairs before asking a human. 0 — never ask.",
		},
	},
	"takeover_timeout": map[string]any{
		"env": "SENTINEL_TAKEOVER_TIMEOUT", "type": "int", "default": 1800, "min": 0, "step": 60, "group": "hitl",
		"hint": map[string]string{
			"ru": "Сколько секунд ждать человека, взявшего управление. По истечении прогон ПРОДОЛЖАЕТСЯ сам.",
			"en": "Seconds to wait for an operator who took control. On expiry the run CONTINUES on its own.",
		},
	},
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
	// ADR-108a: this turn's text. `goal`/`describe` DECLARE the conversation's objective; `message`
	// carries a follow-up. They were one field, so every turn arrived as a new goal and the rule "a
	// conversation has one goal — for a new goal, start a new chat" had nothing to attach to.
	Message string          `json:"message"`
	LLM     json.RawMessage `json:"llm"` // ADR-063: per-run LLM override (backend/base_url/model/vision); validated+parsed into `llm` below

	// ADR-107: everything below used to be expressible ONLY as a CLI flag or a hand-written RunConfig
	// file. The hub rendered inputs for the budgets and the auth block and then wrote them into a
	// downloadable run.yaml with "Pass via: --run-config <file>" — a form that assembled a file and
	// sent the person back to the console, because the API had nowhere to put the values.
	Scenario         string `json:"scenario"`      // --scenario: pick a named scenario out of the RunConfig
	AutVersion       string `json:"aut_version"`   // --aut-version: app-under-test sha, keys flake quarantine
	CI               bool   `json:"ci"`            // --ci: forbids --force-replay
	ForceReplay      bool   `json:"force_replay"`  // --force-replay: bypass the plan_hash hard-abort
	HealLLM          bool   `json:"heal_llm"`      // --heal-llm: allow LLM re-grounding during heal
	PlanBudget       string `json:"plan_budget"`   // RunConfig only — no flag exists
	HealBudget       string `json:"heal_budget"`   // RunConfig only
	TotalBudget      string `json:"total_budget"`  // RunConfig only
	StorageState     string `json:"storage_state"` // RunConfig auth.* — reuse a saved session
	StorageStateSave string `json:"storage_state_save"`
	LoginPlan        string `json:"login_plan"`
	PWNoTrace        bool   `json:"pw_no_trace"`

	// ADR-109: the account that asked for this run, resolved from the credential by the handler.
	// Unexported like `plan` and `llm` — a client that could name its own owner could write into
	// somebody else's set, which is the opposite of what scoping is for.
	owner string
	plan  string        // M9.9: server-RESOLVED plan path (runs/control-<FromRun>/plan.json|scenario.json); unexported → never client-settable
	llm   *llmRunConfig // ADR-063: parsed+validated per-run LLM config; unexported → never client-settable
}

func validTarget(t string) bool {
	return strings.HasPrefix(t, "http://") || strings.HasPrefix(t, "https://") || strings.HasPrefix(t, "file://")
}

// appendRunFlags adds the `agentctl run` flags that ADR-107 brought onto the HTTP contract. It is shared
// by the replay and the explore/goal/describe/chat arms so a flag cannot be wired into one and silently
// forgotten in the other — which is how `--aut-version` would have gone missing from exactly the mode
// (replay) whose flake quarantine it keys.
//
// `--ci` and `--force-replay` are BOTH passed when both are set, and agentctl refuses that combination
// itself (it is the one place that rule lives). handleCreateRun rejects it earlier with a 400 so a
// person gets an answer instead of a failed run; this stays permissive so the rule has a single owner.
func appendRunFlags(args []string, req *runRequest, runCfgPath string) []string {
	// ADR-109: the brain writes the `chats` projection, so the owner has to reach the brain — and the
	// only channel to it is this argv. Passed for every mode, not just chat: a replay or baseline also
	// belongs to whoever asked for it.
	if req.owner != "" {
		args = append(args, "--owner", req.owner)
	}
	if req.Scenario != "" {
		args = append(args, "--scenario", req.Scenario)
	}
	if req.AutVersion != "" {
		args = append(args, "--aut-version", req.AutVersion)
	}
	if req.CI {
		args = append(args, "--ci")
	}
	if req.ForceReplay {
		args = append(args, "--force-replay")
	}
	if req.HealLLM {
		args = append(args, "--heal-llm")
	}
	if runCfgPath != "" {
		args = append(args, "--run-config", runCfgPath)
	}
	return args
}

// writeRunConfig materialises the request's budget and auth values as a RunConfig YAML inside the run's
// own artifact dir, and returns its path ("" when the request carries none of them).
//
// Why a file and not environment. The brain reads budgets as PLAN_TOKEN_LIMIT / HEAL_TOKEN_LIMIT /
// TOTAL_TOKEN_LIMIT and the login plan as PLAN_FILE, and NONE of those names survives agentctl's
// env allowlist (cmd/agentctl/main.go filteredEnv: they match neither the exact set nor the LLM_/OTEL_/
// PW_/PLAYWRIGHT_/SENTINEL_ prefixes). Passing them as environment would mean widening the allowlist
// that exists to stop host secrets reaching the brain — paying in a security boundary for a plumbing
// convenience. `--run-config` already carries exactly these keys, already has tested precedence
// (brain/runconfig.py: an explicit flag beats the file, tracked through SENTINEL_EXPLICIT), and
// already parses them with validation. So the server writes the file the operator used to write.
//
// Living in the artifact dir also makes the run self-describing: the config it actually ran under is
// beside the plan it produced, rather than in a temp file nobody can find afterwards.
func writeRunConfig(artDir string, req *runRequest) (string, error) {
	var body, authLines []string
	num := func(k, v string) {
		if v != "" {
			body = append(body, fmt.Sprintf("%s: %s", k, v))
		}
	}
	num("plan_budget", req.PlanBudget)
	num("heal_budget", req.HealBudget)
	num("total_budget", req.TotalBudget)

	for _, kv := range [][2]string{
		{"storage_state", req.StorageState},
		{"storage_state_save", req.StorageStateSave},
		{"login_plan", req.LoginPlan},
	} {
		if kv[1] != "" {
			// %q is Go's quoting, which is a valid YAML double-quoted scalar — so a Windows path or a
			// value containing a colon cannot silently restructure the document.
			authLines = append(authLines, fmt.Sprintf("  %s: %q", kv[0], kv[1]))
		}
	}
	if req.PWNoTrace {
		authLines = append(authLines, "  pw_no_trace: true")
	}

	// Nothing this file exists to carry -> no file, and `--run-config` stays off the argv, so a run
	// without budgets spawns exactly the command it spawned before ADR-107. Deciding that from the
	// COLLECTED VALUES rather than by searching the rendered text keeps the emptiness test independent
	// of how the text happens to be formatted.
	if len(body) == 0 && len(authLines) == 0 {
		return "", nil
	}

	var b strings.Builder
	b.WriteString("# Written by control-api from POST /v1/runs (ADR-107). This is the configuration the\n")
	b.WriteString("# run actually used — it is an artifact of the run, not an input a human edited.\n")
	for _, l := range body {
		b.WriteString(l + "\n")
	}
	if len(authLines) > 0 {
		b.WriteString("auth:\n" + strings.Join(authLines, "\n") + "\n")
	}
	p := filepath.Join(artDir, "run.yaml")
	if err := os.WriteFile(p, []byte(b.String()), 0o600); err != nil {
		return "", err
	}
	return p, nil
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
	rec := &run{ID: id, State: "running", Target: req.Target, Mode: req.Mode, Planner: req.Planner, Owner: req.owner,
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
	// ADR-107: budgets and the auth block have no flags at all — they reach the brain only through a
	// RunConfig file. The dir has to exist before the file lands in it; agentctl would create it later,
	// which is too late for us. A write failure is not fatal: the run proceeds without the file rather
	// than being refused over a value it can default, and the reason is logged into the run's own stream
	// below (a silent downgrade would look like the budget was honoured).
	var runCfgPath string
	if err := os.MkdirAll(artDir, 0o755); err == nil {
		var cfgErr error
		if runCfgPath, cfgErr = writeRunConfig(artDir, &req); cfgErr != nil {
			runCfgPath = ""
			fmt.Fprintf(os.Stderr, "control-api: run %s: could not write run.yaml (budgets/auth NOT applied): %v\n", id, cfgErr)
		}
	} else {
		fmt.Fprintf(os.Stderr, "control-api: run %s: could not create %s (budgets/auth NOT applied): %v\n", id, artDir, err)
	}

	var args []string
	switch req.Mode {
	case "replay": // M9.9: re-run a prior frozen plan, healing locators — `agentctl run --replay --plan`
		args = []string{"run", "--target", req.Target, "--artifact-dir", artDir, "--replay", "--plan", req.plan}
		args = appendRunFlags(args, &req, runCfgPath)
	case "baseline": // M9.9: update golden baseline from a prior frozen plan (the only golden-write path)
		// `baseline update` is a different subcommand with its own small flag set — the run flags below
		// do not exist on it, so they are deliberately NOT appended here.
		args = []string{"baseline", "update", "--plan", req.plan, "--artifact-dir", artDir}
		if req.Target != "" {
			args = append(args, "--target", req.Target)
		}
	default: // explore / goal / describe / chat (mode inferred from goal/describe + conversation_id as before)
		// ADR-108b: a conversational turn has no target — the person is still deciding what to test.
		// Passing an EMPTY `--target` instead of omitting it would reach agentctl as a set-but-blank
		// flag, and RunConfig precedence treats "the user set this" as authoritative (fs.Visit,
		// cmd/agentctl/main.go), so a blank would win over a target the config file supplies.
		args = []string{"run"}
		if req.Target != "" {
			args = append(args, "--target", req.Target)
		}
		args = append(args, "--artifact-dir", artDir)
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
		// ADR-108a: only meaningful on a chat turn, and passed unconditionally rather than gated on
		// ConversationID so a client that sends one without the other gets the brain's error naming the
		// real problem, instead of a silently dropped message.
		if req.Message != "" {
			args = append(args, "--message", req.Message)
		}
		if req.CoverageTarget != "" {
			args = append(args, "--coverage-target", req.CoverageTarget)
		}
		if req.MaxSteps != "" {
			args = append(args, "--max-steps", req.MaxSteps)
		}
		args = appendRunFlags(args, &req, runCfgPath)
	}
	cmd := exec.Command(s.agentctl, args...)
	cmd.Dir = s.repo
	// ADR-063: layer the LLM connection into the spawn env — process env > per-run > persisted config.
	// os.Environ() (operator-controlled) still wins; resolveRunEnv only fills LLM_* it does not already set.
	cmd.Env = resolveRunEnv(os.Environ(), req.llm, s.mergedPersistedEnv())
	// ONE store for the whole deployment. agentctl starts its own gateway over repo/state/locators.db
	// when it inherits no address, so a run launched from here used to persist into a database this
	// process never reads — the `chats` projection the brain writes landed there, and GET /v1/chats
	// answered 0 about a conversation that plainly existed. Passing our own address makes the run write
	// where the API reads. Only when the gateway actually answered at boot (s.store != nil): handing
	// down an address that did not dial would replace a working local fallback with a dead one.
	if s.store != nil && s.storeAddr != "" {
		cmd.Env = append(cmd.Env, "STORE_ADDR="+s.storeAddr)
	}
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
			// HEALTH-004: a run that could not be spawned is OURS. Set before upsertRun so the stored
			// row and the frame below say the same thing about the same run.
			rec.FaultDomain = faultDomain("failed", -1, "")
			finishedAt := rec.FinishedAt
			s.mu.Unlock()
			if s.store != nil {
				s.store.upsertRun(rec)
			}
			lw.flush()
			rec.sink.close()
			rec.stream.append(aguiLine("run.finished", rec.ID, finishedAt,
				map[string]any{"exit_code": -1, "state": "failed", "fault_domain": rec.FaultDomain}))
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
		// HEALTH-004: decided ONCE, here, where the terminal state and the run's own log are both in
		// hand — and stored on the record so /v1/runs, the run.finished frame and the Results row all
		// quote one decision. `exitForEvent` rather than rec.ExitCode: a failed spawn leaves ExitCode at
		// its zero value, and asking the catalogue what exit 0 means would attribute a run that never
		// started to nobody.
		faultCode, _ := rec.sink.terminalFault()
		rec.FaultDomain = faultDomain(rec.State, exitForEvent, faultCode)
		faultForEvent := rec.FaultDomain
		stateForEvent := rec.State
		finishedAt := rec.FinishedAt
		// ADR-089: the report is built while the run still reads as `running`, and the terminal state is
		// published only afterwards. Otherwise a client that polls GET /v1/runs/{id}, sees `done` and
		// immediately fetches report.html races the generation and gets a 404 — a run that says it
		// finished must have finished producing what it promises.
		reportState, reportCanceled := rec.State, rec.canceled
		rec.State = "running"
		s.mu.Unlock()
		s.generateReport(rec, artDir, cmd.Env, reportState, reportCanceled)
		s.mu.Lock()
		rec.State = reportState
		s.mu.Unlock()
		if s.store != nil { // M13: persist the terminal state (done/failed + exit_code + finished_at)
			s.store.upsertRun(rec)
		}
		// ADR-089: generate the report BEFORE persisting results, because report.json is one of the
		// inputs persistResult reads. Until now nothing ever called `agentctl report` for a UI-launched
		// run, so the product's primary path produced NONE of report.html / report.json / metrics.prom /
		// junit.xml — measured on this repo: 192 runs/control-* directories, zero with metrics.prom.
		// All four sit in the artifact whitelist and answered 404 in practice.
		s.persistScenario(rec) // M14 wave W3: wire the scenarios domain to a real caller (no-op if no scenario.json)
		s.persistResult(rec)   // M15 (ADR-051): wire the results + metrics domains (no-op if no store/artifacts)
		lw.flush()             // emit any trailing partial line (all brain output precedes run.finished)
		rec.sink.close()       // flush the record held back for repeat-collapsing, then close the files
		// M14 tail 1: the control-API injects run.finished — the one AG-UI event only it can know (the
		// process exit). Must precede finish(): append() no-ops once the stream is done. WS subscribers
		// get a typed run.finished frame (wsAGUIFrame); SSE gets the raw line inside a log event.
		// HEALTH-004: the frame carries the fault alongside the exit code, so a live watcher learns
		// whose problem it is at the moment the run ends rather than on the next poll of /v1/runs.
		rec.stream.append(aguiLine("run.finished", rec.ID, finishedAt, map[string]any{
			"exit_code": exitForEvent, "state": stateForEvent, "fault_domain": faultForEvent}))
		rec.stream.finish() // release SSE subscribers
	}()
	return rec
}

// codeReportFailed is catalogued in brain/events.json (emitter: control-api).
const codeReportFailed = "test.report_failed"

// generateReport runs `agentctl report --run <dir>` for a finished run, producing report.html,
// report.json, metrics.prom and junit.xml (brain/report.py::generate) — the surfaces on which a
// person reads WHAT happened and a CI reads whether to fail.
//
// Deliberately best-effort and non-fatal: the run's own verdict is already decided and its exit code
// already recorded, so a reporting failure must not change the outcome the user is told about. It is
// not silent either — a failure emits test.report_failed, because "the artifacts are missing and
// nobody said why" is the shape this whole change exists to remove.
//
// Skipped for a run that never produced anything to report on: a failed spawn has no heal-report.json
// and `agentctl report` would exit non-zero for the honest reason that there is nothing there.
func (s *server) generateReport(rec *run, artDir string, runEnv []string, state string, canceled bool) {
	// The outcome is passed in rather than read off rec: the caller masks rec.State as "running" for
	// the duration, so that no client sees a finished run whose artifacts are still being written.
	if state == "failed" || canceled {
		return
	}
	cmd := exec.Command(s.agentctl, "report", "--run", artDir)
	cmd.Dir = s.repo
	// The SAME environment the run itself got, not a freshly resolved one. The report is part of that
	// run, and the per-run settings it needs travel there — PROM_PUSHGATEWAY decides whether the
	// metrics are pushed at all. Re-resolving would quietly give the report a different world than the
	// run it describes, which is the kind of difference nobody notices until a number goes missing.
	cmd.Env = runEnv
	out, err := cmd.CombinedOutput()
	if err == nil {
		return
	}
	// The report is optional for an explore run (heal-report.json is a replay artifact), so a missing
	// input is expected rather than exceptional — say so once, at a level the log view can filter.
	line := strings.TrimSpace(string(out))
	if len(line) > 400 {
		line = line[:400] + "…"
	}
	// The code is a literal constant rather than inlined into the message: brain/events.json is the
	// single source for what this line means, and the offline catalogue gate reads THIS file looking
	// for the code as a quoted literal. A code spliced into a longer string is invisible to it.
	rec.stream.append(aguiLine("log", rec.ID, time.Now().UTC().Format(time.RFC3339),
		map[string]any{"line": "[warn|test] " + codeReportFailed + ": " + err.Error() + " — " + line}))
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
		// ADR-109: the scenario a run produced belongs to whoever asked for the run. Inheriting rather
		// than re-deriving means a person's authored work lands in their own set without the finish
		// goroutine needing a credential it does not have.
		Owner: rec.Owner,
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
	// ADR-097: how much of the page the tool could SEE. Coverage answers "how much of what we saw did
	// we exercise" and this answers the prior question — they multiply, and until now only one of them
	// left the brain. This struct dropped the whole block at unmarshal, so `worst_ratio` had exactly
	// one reader in the repository and it was a test.
	Perception *planPerception `json:"perception"`
}

// planPerception is the page-visibility block plan.json carries ALONGSIDE steps (never inside one:
// `canonical_plan_hash` hashes every field of every step, and a measurement describing the page must
// not change the identity of the test).
type planPerception struct {
	// A POINTER on purpose. A run that never measured (an older executor, or a replay, which does not
	// audit at all) must be distinguishable from one that measured 0.0 — a nil that reads as zero is
	// the same defect as a null that reads as 1.0, pointing the other way.
	WorstRatio *float64 `json:"worst_ratio"`
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
	fault := rec.FaultDomain
	s.mu.RUnlock()
	// A run that never executed (State="failed": agentctl couldn't spawn) has no real exit code — its
	// zero-value ExitCode 0 would map verdictEnum→"pass" and inflate the pass-rate. Skip it: the runs
	// domain already records the failure (state+error); it must not pollute the results/metrics substrate.
	if state != "done" {
		return
	}

	rr := &storepb.ResultRecord{
		RunId: rec.ID, Mode: mode, Verdict: verdictEnum(exit), Owner: rec.Owner, // ADR-109: inherits the run
		ExitCode: int64(exit), DurationMs: durationMs(startedAt, finishedAt),
		// HEALTH-004: quoted from the run record, not recomputed. `verdict` says WHAT happened and is
		// still one of the coarse four; `fault_domain` says WHOSE problem it is, which is the question
		// a dashboard reader actually has and which exit 1 / exit 4 / exit -1 all answered with the
		// same word until now.
		FaultDomain: fault,
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
	var visibility *float64                                 // ADR-097: nil = never measured, which is not the same as measured at 0
	if praw, pok := s.readArtifact(rec, "plan.json"); pok { // authoring/explore: coverage_achieved
		var pc planCoverage
		if json.Unmarshal([]byte(praw), &pc) == nil {
			rr.Coverage = pc.CoverageAchieved
			if rr.PlanId == "" {
				rr.PlanId = pc.PlanID
			}
			// ADR-097: carried so the coverage number can be READ WITH the caveat it needs. A run
			// that saw two thirds of its page and exercised all of it reports coverage 1.00, and
			// that is true of the two thirds — the reader has to be told which page the fraction is
			// of. Emitted as its own metric point rather than folded into `coverage`: multiplying
			// them would produce a third number nobody could decompose again.
			if pc.Perception != nil && pc.Perception.WorstRatio != nil {
				visibility = pc.Perception.WorstRatio
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
	// ADR-097. Only when the run actually measured: a series that gains a 0 from every replay would
	// say the tool went blind, when in fact a replay never asks.
	if visibility != nil {
		pts = append(pts, metricKV{"visibility", *visibility})
	}
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
			Owner: rec.Owner, // ADR-109: a trend belongs to the account whose run produced it
		})
	}
	s.store.ingestMetrics(batch)
}

func (s *server) handleCreateRun(w http.ResponseWriter, r *http.Request) {
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
	// ADR-107: agentctl owns this rule and refuses the pair itself, but it does so after the process is
	// up — which surfaces to a caller as a run that started and died rather than as a rejected request.
	// Checked for EVERY mode, not just replay: `--ci` reaches the argv in the explore arm too, so a pair
	// rejected only under replay would still be spawnable.
	if req.CI && req.ForceReplay {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "ci and force_replay are mutually exclusive: --force-replay bypasses the plan_hash hard-abort, which CI mode exists to enforce"})
		return
	}
	// ADR-109: stamp the run with whoever asked for it, so their list shows it and nobody else's does.
	if c, ok := s.callerOf(r); ok {
		req.owner = c.owner()
	}
	rec := s.spawnRun(req)
	writeJSON(w, http.StatusAccepted, map[string]string{"run_id": rec.ID, "artifact_dir": rec.ArtifactDir, "state": "running"})
}

func (s *server) handleListRuns(w http.ResponseWriter, r *http.Request) {
	// ADR-109: "" = unscoped, which is both the machine token and a deployment with no accounts. An
	// unauthenticated caller cannot reach this route at all (the mux wraps it), so a missing caller here
	// would be a routing bug rather than an anonymous read.
	c, _ := s.callerOf(r)
	owner := c.owner()
	s.mu.RLock()
	live := make(map[string]bool, len(s.runs))
	out := make([]run, 0, len(s.runs)) // VALUE copies: snapshot mutable fields under the lock (race-free marshal)
	for id, rr := range s.runs {
		// The in-memory map is filtered too. Scoping only the STORE would leak every live run to
		// everyone — and a live run is the one a person is most likely to be looking at.
		if owner != "" && rr.Owner != owner {
			continue
		}
		live[id] = true
		out = append(out, *rr)
	}
	s.mu.RUnlock()
	// M13 (ADR-050): fold in persisted runs from the gateway (e.g. from before a restart). The in-memory
	// copy wins for a run that's both live and stored — it has the freshest state + the live stream.
	if s.store != nil {
		if stored, ok := s.store.listRuns(owner); ok {
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
	// ADR-108b: the deliverable of a CONVERSATIONAL turn — prose, not a scenario. It is what the chat
	// shim answers with, and what the hub shows as the assistant's message.
	"reply.json":            true,
	"scenario.json":         true,
	"reconcile-report.json": true,
	"report.json":           true,
	"report.html":           true,
	"plan.json":             true,
	"heal-report.json":      true, // M9.9: replay output (golden diff / heal log)
	"baseline-report.json":  true, // M9.9: baseline-update output
	"junit.xml":             true, // ADR-073: the machine contract every CI consumes
	"executed-plan.json":    true, // ADR-047 follow-on: the plan a replay ran, so the replay is replayable
	"metrics.prom":          true, // ADR-089: Prometheus textfile — the run's numbers, now that a UI run produces them
	// PROD-IMPORT: the explore map. It is what makes "does this imported step still bind to an element
	// the app HAS?" answerable — grounding could always compute it, but nothing produced the map, so
	// the capability was unreachable outside a synthetic fixture. Same class of foreign text as the
	// role+name locators plan.json already carries, and governed by the same retention.
	"site-map.json": true,
	// ADR-099: the trace. Until now the one artifact a person could not reach without shell access to
	// the server — which meant the post-mortem of a failed run was unavailable to exactly the person
	// the product is for.
	//
	// ⚠ It is opened KNOWING what is inside, and the two halves are not the same. ADR-098 redacts the
	// TEXT (typed values, credentials, the driver's own narration). It does NOT touch the SCREENSHOTS,
	// by decision — cleaning pixels means OCR plus masking, which is unreliable. So a downloaded trace
	// may show whatever was on the tested application's screen. The lever for that is
	// SENTINEL_TRACE_SCREENSHOTS=0, which stops the frames being recorded at all.
	"trace.zip": true,
	// SEC-TRACE-SWEPT-SILENTLY: the marker sweepTraces drops when it deletes a trace by retention.
	// The hub reads it to distinguish "the trace was removed" from "this run never had one" — without
	// it, both look identical (no trace.zip), and a swept trace reads as a run that was never traced.
	"trace-removed.json": true,
	// ADR-107: the RunConfig the server materialised for this run (budgets + the auth block). It is
	// what the hub's "⬇ run.yaml" button used to FABRICATE client-side from form values that never
	// reached the API — so the file a person downloaded described a run that had not happened. Serving
	// the real one makes the download an artifact of the run instead of a guess about it, and it is the
	// only way to answer "what budget did this run actually run under?" after the fact.
	//
	// It carries paths a person typed (storage_state, login_plan), which is the same class of foreign
	// text plan.json already holds, under the same retention. It carries no secret: credentials travel
	// as secretRef, never as a value (ADR-098).
	"run.yaml": true,
}

// frameNamePattern bounds what a live-frame request may name (ADR-108d). Written as an anchored
// pattern rather than a prefix check because "starts with frames/" would admit anything after it —
// including a traversal spelled in a way ContainsAny does not catch.
var frameNamePattern = regexp.MustCompile(`^frames/frame-[0-9]{4}\.png$`)

func isFrameName(name string) bool { return frameNamePattern.MatchString(name) }

// handleRunEvents streams a run's state + captured log lines as Server-Sent Events (ADR-040).
// Token-gated like mutations: logs are more sensitive than a bare status poll.
func (s *server) handleRunEvents(w http.ResponseWriter, r *http.Request) {
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
		// HEALTH-004: the SSE `state` frame is what the hub renders its verdict from (it only falls back
		// to polling /v1/runs when the stream is unavailable), so the fault has to ride here too — or the
		// live path would show the coarse badge and the polled path the attributed one, for the same run.
		data, _ := json.Marshal(map[string]any{"state": rec.State, "exit_code": rec.ExitCode,
			"error": rec.Error, "fault_domain": rec.FaultDomain})
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
	id := r.PathValue("id")
	s.mu.RLock()
	rec, ok := s.runs[id]
	s.mu.RUnlock()
	// A run from a PREVIOUS control-API process is gone from the in-memory map but alive in the store,
	// with its artifact_dir recorded — which is exactly why handleGetRun already falls back this way.
	// Without the same fallback here, every artifact of every run became unreachable at restart: the
	// hub could list the run, show its verdict, and answer "no such run" to the report it had just
	// named. The whitelist below still bounds WHICH file, and the dir comes from the record, never
	// from the request.
	if !ok && s.store != nil {
		if hist, found := s.store.getRun(id); found {
			rec, ok = hist, true
		}
	}
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	name := r.URL.Query().Get("name")
	// ADR-108d: frames live in a SUBDIRECTORY (frames/frame-0007.png) because a run produces one per
	// step and a flat whitelist cannot enumerate them. So they are matched by SHAPE instead — and the
	// shape is deliberately narrow: the fixed prefix, four digits, `.png`, nothing else. `..` and
	// separators are still refused above it, so the pattern can only ever name a file this run wrote.
	if !isFrameName(name) &&
		(name == "" || strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") || !artifactWhitelist[name]) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "name must be a whitelisted run artifact (e.g. scenario.json, plan.json, report.json, heal-report.json) or a live frame (frames/frame-0001.png)"})
		return
	}
	f, err := os.Open(filepath.Join(rec.ArtifactDir, name))
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "artifact not found (run may be incomplete)"})
		return
	}
	defer f.Close()
	// SEC-RETENTION-DOWNLOAD. The hub reads plan.json / scenario.json / heal-report.json on every run
	// open just to DRAW the run, so a retention rule of "delete once served" would destroy a run the
	// moment a human looked at it. `?download=1` is the distinction: the hub sets it ONLY on a real
	// download action, never on a display fetch. A genuine download leaves a downloaded.json marker —
	// "the human has a copy" — and nothing more. Deletion is deliberately NOT automatic here (operator
	// decision): the marker is what a later explicit policy consumes, so a download can never be the
	// thing that erases the evidence. A view writes no marker, which the negative-control test pins.
	if r.URL.Query().Get("download") == "1" {
		markDownloaded(rec.ArtifactDir, name)
	}
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if strings.HasSuffix(name, ".zip") {
		// ADR-099: binary, and always an attachment. A zip served as JSON would arrive corrupted
		// through anything that assumes text, and the browser must never be invited to open it.
		w.Header().Set("Content-Type", "application/zip")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
	} else if strings.HasSuffix(name, ".html") {
		// Serve report.html as a download, never inline — avoids the browser rendering
		// agent-influenced HTML if someone navigates straight to this endpoint.
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
	} else {
		w.Header().Set("Content-Type", "application/json")
	}
	_, _ = io.Copy(w, f)
}

// markDownloaded records, in the run's own directory, that a human took a real copy of an artifact
// (SEC-RETENTION-DOWNLOAD). It is the "the human has it" signal an explicit retention policy consumes
// — NOT a deletion. Best-effort and append-only in spirit: it records the most recent download; a
// prior marker is overwritten, which is fine because the fact it records ("this run was downloaded")
// is monotonic. It carries no foreign text — only the artifact name and a timestamp.
func markDownloaded(artifactDir, name string) {
	m := map[string]any{"downloaded": name, "at": time.Now().UTC().Format(time.RFC3339)}
	b, err := json.Marshal(m)
	if err == nil {
		err = os.WriteFile(filepath.Join(artifactDir, "downloaded.json"), b, 0o600)
	}
	// Best-effort, but not SILENT. A failed marker write makes a run that WAS downloaded
	// indistinguishable from one that never was — and the retention policy this marker exists to
	// feed would then delete it as unclaimed. The download itself still succeeds (the operator has
	// the bytes; refusing it because a marker failed would be worse), so this reports rather than
	// fails — but "the record of it is missing" must be knowable, not inferred later from a
	// deletion nobody expected.
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] download marker not written for %s: %v\n", artifactDir, err)
	}
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
	// One constant, not a second copy — see session.go::storeAbsentReason.
	return map[string]any{"store": false, "store_reason": storeAbsentReason}
}

// withStore merges the store marker into a response body.
func (s *server) withStore(body map[string]any) map[string]any {
	for k, v := range s.storeMarker() {
		body[k] = v
	}
	return body
}

func (s *server) handleListScenarios(w http.ResponseWriter, r *http.Request) {
	var scenarios []*storepb.Scenario
	var total int64
	if s.store != nil {
		c, _ := s.callerOf(r)
		if sl, ok := s.store.listScenarios(r.URL.Query().Get("target"), c.owner()); ok {
			scenarios, total = sl.Scenarios, sl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"scenarios": scenarios, "total": total}))
}

func (s *server) handleGetScenario(w http.ResponseWriter, r *http.Request) {
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
	if s.store != nil {
		s.store.deleteScenario(r.PathValue("id"))
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"}) // idempotent: missing id (or no store) is still success
}

func (s *server) handleListTests(w http.ResponseWriter, r *http.Request) {
	var tests []*storepb.TestRecord
	var total int64
	if s.store != nil {
		c, _ := s.callerOf(r)
		if tl, ok := s.store.listTests(c.owner()); ok {
			tests, total = tl.Tests, tl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"tests": tests, "total": total}))
}

func (s *server) handleGetTest(w http.ResponseWriter, r *http.Request) {
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
		// ADR-109: the promoted test belongs to whoever promoted it. Without this the test lands unowned,
		// which under a scoped list means it appears in NOBODY's library — promote a test and watch it
		// vanish, with the row sitting in the database the whole time.
		c, _ := s.callerOf(r)
		t, ok = s.store.promoteTest(&storepb.PromoteReq{ScenarioId: req.ScenarioID, Name: req.Name,
			Schedule: req.Schedule, Owner: c.owner()})
	}
	if !ok || !t.Found {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such scenario to promote (or store-gateway unavailable)"})
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *server) handleListChats(w http.ResponseWriter, r *http.Request) {
	var chats []*storepb.ChatProjection
	var total int64
	if s.store != nil {
		c, _ := s.callerOf(r)
		if cl, ok := s.store.listChats(c.owner()); ok {
			chats, total = cl.Chats, cl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"chats": chats, "total": total}))
}

func (s *server) handleGetChat(w http.ResponseWriter, r *http.Request) {
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
	// ADR-108b: the thread this turn belongs to. Without it every call to this endpoint was a fresh
	// start — the shim could spawn a run but never a CONVERSATION, and the multi-turn machinery
	// (ADR-048) was reachable only through POST /v1/runs. An OpenAI client that does not know the
	// field simply omits it and gets the previous one-shot behaviour.
	ConversationID string `json:"conversation_id"`
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

	// ADR-108b: a turn with words but no target is no longer a dead end. It used to be answered with a
	// fixed sentence telling the person to supply a URL — which is the right thing to say EVENTUALLY,
	// but saying only that means the product cannot be asked a question, and a chat that answers one
	// sentence is not a chat. A conversational turn goes to the model, which either answers or says
	// what it needs (brain/__main__.py `_run_converse`).
	//
	// A conversation needs a thread to be a conversation, so one is minted when the caller sent none.
	// The id comes back on the response, which is how an OpenAI client that knows nothing about
	// Sentinel can still continue the exchange.
	if !validTarget(target) {
		if text == "" {
			s.chatReply(w, req.Stream, id, created, model,
				"Say something, or give me a target like `target: https://app.example` and what the test should do.")
			return
		}
		conv := strings.TrimSpace(req.ConversationID)
		if conv == "" {
			conv = "conv-" + newRunID()
		}
		rr := runRequest{Mode: "chat", Planner: "heuristic", Message: text, ConversationID: conv}
		if c, ok := s.callerOf(r); ok {
			rr.owner = c.owner()
		}
		rec := s.spawnRun(rr)
		s.conversationalReply(w, req.Stream, rec, id, created, model, conv)
		return
	}
	if mode != "explore" && text == "" {
		s.chatReply(w, req.Stream, id, created, model, "Describe the test in words, or prefix with `goal:` / `explore:`.")
		return
	}

	rr := runRequest{Target: target, Mode: mode, Planner: "heuristic", ConversationID: strings.TrimSpace(req.ConversationID)}
	switch mode {
	case "goal":
		rr.Goal, rr.Planner = text, "goal"
	case "describe":
		rr.Describe = text
	}
	if c, ok := s.callerOf(r); ok { // ADR-109: the chat shim spawns runs too
		rr.owner = c.owner()
	}
	rec := s.spawnRun(rr)

	if req.Stream {
		s.streamChat(w, r, rec, id, created, model)
		return
	}
	s.blockingChat(w, rec, id, created, model)
}

// conversationalReply waits for a conversational turn and answers with what the model SAID.
//
// blockingChat answers with the run's log plus a verdict, which is right for a turn that authored a
// test and wrong for a turn that answered a question: a person who asked "what can you do?" would get
// a wall of run output with the reply somewhere inside it. The deliverable is reply.json, so that is
// what is served — and if it is missing, the log is still better than silence.
func (s *server) conversationalReply(w http.ResponseWriter, stream bool, rec *run, id string,
	created int64, model, conversationID string) {
	_, ch, finished := rec.stream.subscribe()
	if !finished {
		defer rec.stream.unsubscribe(ch)
		for range ch { // drain: the turn is over when its process is
		}
	}
	msg := ""
	if b, err := os.ReadFile(filepath.Join(rec.ArtifactDir, "reply.json")); err == nil {
		var doc struct {
			Reply string `json:"reply"`
		}
		if json.Unmarshal(b, &doc) == nil {
			msg = strings.TrimSpace(doc.Reply)
		}
	}
	if msg == "" {
		msg = "I could not produce an answer for that turn. " + s.verdict(rec)
	}
	// The thread id travels back so a client that did not send one can continue the conversation. It is
	// appended to the text rather than added as a field because an OpenAI client parses the schema it
	// knows and drops what it does not — a continuation id nobody can see is not a continuation id.
	if conversationID != "" {
		msg += "\n\n<!-- conversation_id: " + conversationID + " -->"
	}
	s.chatReply(w, stream, id, created, model, msg)
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
	var results []*storepb.ResultRecord
	var total int64
	if s.store != nil {
		c, _ := s.callerOf(r)
		if rl, ok := s.store.listResults(parseIntQuery(r, "limit", 200), parseIntQuery(r, "offset", 0), c.owner()); ok {
			results, total = rl.Results, rl.Total
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"results": results, "total": total}))
}

func (s *server) handleGetResult(w http.ResponseWriter, r *http.Request) {
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
	metric := r.URL.Query().Get("metric")
	if metric == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "metric query param required (e.g. pass, coverage, duration_ms, healed, failed, regressions, steps)"})
		return
	}
	var points []*storepb.TrendPoint
	if s.store != nil {
		c, _ := s.callerOf(r)
		if tr, ok := s.store.trends(metric, parseIntQuery(r, "window", 50), c.owner()); ok {
			points = tr.Points
		}
	}
	writeJSON(w, http.StatusOK, s.withStore(map[string]any{"metric": metric, "points": points}))
}

// mux registers every route from its declaration in routes() (cmd/control-api/access.go) and from
// nowhere else. That is what makes the access gate exhaustive: a route that forgot to state what it
// requires is not a route that quietly serves anonymously — it is a route that does not exist.
func (s *server) mux() http.Handler {
	m := http.NewServeMux()
	for _, sp := range s.routes() {
		m.HandleFunc(sp.pattern, s.guard(sp))
	}
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
		sessions:   newSessionStore(),         // ADR-109
		llmBaseURL: os.Getenv("LLM_BASE_URL"), // M11.5 PR-5: the /readyz llm probe target (env wins over the stored config)
	}
	for _, o := range strings.Split(os.Getenv("CONTROL_API_CORS_ORIGINS"), ",") {
		if o = strings.TrimSpace(o); o != "" {
			s.corsAllow[o] = true
		}
	}
	// HEALTH-005: the service journal opens BEFORE anything else worth recording happens — the store
	// dial, the token decision and the first request all belong in it. `state` rather than the repo
	// root because that is the directory already mounted as a volume in every compose stack, so the
	// journal survives `docker compose down` exactly as the SQLite databases beside it do.
	s.journal = svclog.Open(filepath.Join(repo, "state"), "control-api")
	defer func() {
		s.journalEvent("service.stopped", "info", "Service control-api stopped: process exit", nil)
		s.journal.Close()
	}()
	// A `defer` alone records nothing when the process is SIGNALLED, and being signalled is the normal
	// way this service ends: `systemctl stop` and `docker compose down` both send SIGTERM. Measured
	// live — two starts and ZERO stops in the journal, so every shutdown looked like a crash. The
	// handler writes the obituary and then dies of the same signal, so the exit status a supervisor
	// sees is unchanged.
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
		got := <-sig
		s.journalEvent("service.stopped", "info",
			"Service control-api stopped: signal "+got.String(), nil)
		s.journal.Close()
		signal.Stop(sig)
		_ = syscall.Kill(syscall.Getpid(), got.(syscall.Signal))
	}()
	s.journalEvent("service.started", "info",
		"Service control-api started: version "+version+", brought up by "+svclog.Supervisor()+
			", pid "+strconv.Itoa(os.Getpid()), nil, "addr: "+addr)

	// M13 (ADR-050): connect to a persistent store-gateway if configured; else runs stay in-memory
	// (standalone/offline path, unchanged). Fail-open — an unreachable gateway only warns.
	if sa := os.Getenv("CONTROL_API_STORE_ADDR"); sa != "" {
		s.storeAddr = sa // remembered even on failure — see server.storeAddr / configTier (ADR-075)
		if sc, err := newStoreClient(sa, os.Getenv("STORE_TOKEN")); err != nil {
			fmt.Fprintf(os.Stderr, "control-api: WARNING — store-gateway %q unreachable: %v (runs stay in-memory, lost on restart)\n", sa, err)
			// The one event a store-backed journal could never record: the store being unreachable.
			s.journalEvent("service.store_unreachable", "warn",
				"A store was declared ("+sa+") and did not answer at start: "+err.Error()+
					" — runs stay in memory", nil)
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
	// HEALTH-005: where the machine token came from. The VALUE is never journalled — only which of the
	// three decisions was taken, which is what answers "why does my script's token no longer work".
	// Worded away from the redactor: `token: <value>` is a named credential to internal/redact, so the
	// first version of this line published the SOURCE as [REDACTED] — measured live, not predicted.
	// The catalogue entry was fixed by the PR-1c property gate; this string is built in Go and the gate
	// cannot see it, which is a blind spot recorded in the backlog rather than papered over.
	s.journalEvent("service.token_source", "info", "The machine token came from "+string(tokSrc), nil,
		"warnings: "+strconv.Itoa(len(tokWarnings)))
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
