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

	eventcatalog "github.com/AlexGromer/sentinel/brain"
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
	// storeEmbedded marks a store this process hosts itself (ADR-158) rather than one an operator
	// deployed. Domains that ALREADY had a non-store implementation must keep it — see configTier,
	// where treating the embedded store as a store silently orphaned an existing state/config.json.
	storeEmbedded bool
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

	// QA-REPORT-SERVICE (ADR-119): the memo behind GET /metrics. Its own lock for the same reason
	// `ready` has one — walking runs/ is disk work that grows with the number of runs, and a scraper
	// polling it must never queue behind, or in front of, a run being created.
	metrics metricsState
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
		// ⚠ УМОЛЧАНИЯ ДЛЯ `modes`/`planner`, КОТОРЫХ ЗДЕСЬ НЕ БЫЛО ВОВСЕ, и это отсутствие само было
		// дефектом: перечень БЕЗ умолчания не даёт с чем сверяться, поэтому «ничего не трогал и
		// запустил» из терминала и из интерфейса были разными прогонами, и никакая проверка не могла
		// этого заметить — сверять было не с чем.
		//
		// Умолчание ПОКОНТЕКСТНОЕ, и это решение, а не компромисс (Alex, W15). Терминал начинает с
		// `explore`: у него нет заданной цели, он идёт смотреть. Форма прогона в хабе начинает с
		// `goal`: человек, открывший форму, уже знает, чего хочет. Раздел «Чат» начинает с
		// `describe`: там сначала описывают поток словами. Три разных начала — это три разные задачи,
		// а не дрейф трёх копий одного числа; сводить их к одному значению значило бы отнять удобство
		// у двух из трёх ради симметрии. Что БЫЛО дефектом — отсутствие места, где это записано.
		//
		// `default` — умолчание продукта (то, с чего начинает CLI). `contexts` — чем его перекрывает
		// конкретная поверхность. Гейт хаба сверяет КАЖДЫЙ преселект с умолчанием ЕГО контекста.
		"mode_default":    "explore",
		"mode_contexts":   map[string]string{"run": "goal", "chat": "describe"},
		"planner_default": "heuristic",
		"planner_contexts": map[string]string{
			// ПУСТО, И ЭТО ЗАПИСАННОЕ ОТСУТСТВИЕ ДЛЯ ОБЕИХ ПОВЕРХНОСТЕЙ, а не забытая заливка.
			//
			// `run`: форма прогона не преселектит планировщик — выбор режима уже сузил его, и второй
			// невидимый выбор поверх первого сделал бы «я не выбирал» и «я выбрал ровно это» одним актом.
			//
			// `chat`: запись `"chat": "goal"` стояла здесь и была ОБЕЩАНИЕМ БЕЗ ЕДИНОЙ СТОРОНЫ, которая
			// его держит — замерено W17, тремя наблюдениями сразу. (1) Выбрать планировщик в чате
			// НЕЛЬЗЯ: контрола `#ch-planner` нет ни в разметке, ни в JS, и заводить его запрещено
			// решением ADR-147 (второй выбор на сообщение). (2) В умолчательном режиме чат ОТПРАВЛЯЕТ
			// `heuristic`: `#ch-mode` открывается на `describe` (docs/index.html:1215), а тело строится
			// тернарником `mode==='goal'?'goal':'heuristic'` (:4233). (3) И даже присланный `goal` до
			// прогона не доезжает: `conversation_id` подменяет режим на chat, а `_run_chat` прибивает
			// `HeuristicPlanner()` гвоздём (brain/__main__.py:783) и `PLANNER` на этом маршруте не читает
			// вовсе. Публиковать перекрытие для поверхности, у которой нет ни ручки, ни читателя, —
			// значит обещать ручку, которой нет и которая всё равно не доедет.
		},
		// ADR-108b added `chat`: conversation is its own role, so an operator can point talking and
		// planning at different endpoints (the planner may be a large remote model while the chat that
		// answers a question runs on whatever is local). A role the brain honours but the schema does not
		// publish is a knob nobody can find — the same "capability nobody can reach" this milestone exists
		// to close.
		"roles": llmRoles, // ЕДИНЫЙ источник ролей (llmenv.go): пер-ролевое LLM_<KEY>_<ROLE> перекрывает глобальное LLM_<KEY>
		// ADR-107: `fields` is the per-run half of the one configuration model, and every key here is
		// settable on POST /v1/runs — asserted by TestRunRequestCoversEverySchemaField, which walks this
		// map rather than listing what it expects to find.
		//
		// `group` answers the question the field belongs to, exactly as `settings` does, so a UI can lay
		// the form out from the schema instead of hard-coding which input sits under which heading. The
		// hub used to hard-code that, which is why nine of these fields existed as inputs the submit
		// handler never read.
		"fields": map[string]any{
			// W15: ТРИ ПОЛЯ, КОТОРЫХ ЗДЕСЬ НЕ БЫЛО, хотя тело прогона их принимало. Зеркальный гейт
			// (TestEverySchemaFieldCoversRunRequest) требует дескриптор у КАЖДОГО экспортированного тега
			// `runRequest` — до него сверка шла в одну сторону и была зелёной над этой дырой: `message` —
			// текст хода в чате, `planner` — выбор планировщика, `mode` — что прогон вообще делает. Три
			// величины, меняющие прогон, и ни одна не была видна из схемы.
			// [RUN-MODE-LABELS-THE-RECORD-NOT-THE-RUN]. `set_by: flag:--mode` было ЛОЖНО для пяти
			// значений из шести, и это замер: `--mode` дописывается РОВНО в одном месте и только
			// литералом "chat" при наличии `conversation_id`. Осей в продукте ДВЕ и они разведены
			// НАМЕРЕННО: ось ИСПОЛНЕНИЯ (replay/baseline/chat) и ось АВТОРИНГА (explore/goal/describe),
			// которую по ADR-027 несут `--goal`/`--describe`, а `--mode goal|describe` отклонён. Поле
			// здесь — ось ЗАПРОСА, из которой сервер выводит и то, и другое; поэтому маршруты названы
			// все, а не один.
			"mode":            map[string]any{"type": "enum", "group": "run", "enum": []string{"explore", "goal", "describe", "replay", "baseline", "chat"}, "set_by": []string{"body:mode", "flag:--goal", "flag:--describe", "flag:--replay", "flag:--mode"}},
			"planner":         map[string]any{"type": "enum", "default": "heuristic", "group": "run", "enum": []string{"heuristic", "llm", "goal"}, "set_by": []string{"flag:--planner", "runconfig:planner"}},
			"message":         map[string]any{"type": "string", "group": "run", "set_by": []string{"flag:--message"}},
			"target":          map[string]any{"type": "string", "required": true, "group": "run", "set_by": []string{"flag:--target"}},
			"goal":            map[string]any{"type": "string", "group": "run", "set_by": []string{"flag:--goal", "runconfig:goal"}},
			"describe":        map[string]any{"type": "string", "group": "run", "set_by": []string{"flag:--describe", "runconfig:describe"}},
			"conversation_id": map[string]any{"type": "string", "group": "run", "set_by": []string{"flag:--conversation-id"}}, // M9.10: multi-turn chat thread key (resume by conversation_id)
			"coverage_target": map[string]any{"type": "number", "default": 0.85, "group": "run", "set_by": []string{"flag:--coverage-target", "runconfig:coverage_target"}},
			"max_steps":       map[string]any{"type": "int", "default": 40, "group": "run", "set_by": []string{"flag:--max-steps", "runconfig:max_steps"}},
			"scenario":        map[string]any{"type": "string", "group": "run", "set_by": []string{"flag:--scenario"}}, // --scenario: select a named scenario out of the RunConfig
			"plan_budget":     map[string]any{"type": "int", "default": 50000, "group": "budgets", "set_by": []string{"runconfig:plan_budget"}},
			"heal_budget":     map[string]any{"type": "int", "default": 20000, "group": "budgets", "set_by": []string{"runconfig:heal_budget"}},
			"total_budget":    map[string]any{"type": "int", "default": 0, "group": "budgets", "set_by": []string{"runconfig:total_budget"}},
			// Session reuse. These reach the brain through the RunConfig `auth:` block (writeRunConfig),
			// never as environment: PLAN_FILE does not survive agentctl's env allowlist, and widening that
			// allowlist to carry a convenience would spend a security boundary.
			"storage_state":      map[string]any{"type": "string", "group": "auth", "set_by": []string{"env:STORAGE_STATE", "runconfig:auth.storage_state"}},
			"storage_state_save": map[string]any{"type": "string", "group": "auth", "set_by": []string{"env:STORAGE_STATE_SAVE", "runconfig:auth.storage_state_save"}},
			"login_plan":         map[string]any{"type": "string", "group": "auth", "set_by": []string{"runconfig:auth.login_plan"}},
			"pw_no_trace":        map[string]any{"type": "bool", "default": false, "group": "auth", "set_by": []string{"env:PW_NO_TRACE", "runconfig:auth.pw_no_trace"}},
			// Determinism guards. `ci` forbids `force_replay`; the rule lives in agentctl and the API
			// rejects the pair early so a person gets a 400 instead of a run that dies at startup.
			"ci":           map[string]any{"type": "bool", "default": false, "group": "gates", "set_by": []string{"flag:--ci"}},
			"force_replay": map[string]any{"type": "bool", "default": false, "group": "gates", "set_by": []string{"flag:--force-replay"}},
			"aut_version":  map[string]any{"type": "string", "group": "gates", "set_by": []string{"flag:--aut-version"}},
			"heal_llm":     map[string]any{"type": "bool", "default": false, "group": "healing", "set_by": []string{"flag:--heal-llm", "env:HEAL_LLM"}},
			// ADR-133. Группа `gates`, а не `healing`: это ворота обхода, как `ci`, и умолчание
			// `false` означает «правила соблюдаются» — отступление требует ключа.
			"ignore_robots": map[string]any{"type": "bool", "default": false, "group": "gates", "set_by": []string{"flag:--ignore-robots", "runconfig:ignore_robots"}},
			// LIVE-MATRIX (ADR-120). The observation mode is the PERSON's choice, not something the tool
			// derives: a deployment default lives in the SAVED RUN DEFAULTS (`run.observe`, W17 — not in
			// `settings`, where it never existed despite this line once saying so), a run overrides it
			// here, and the CLI takes
			// the same name. `cost` rides along because a mode that only has a NAME leaves somebody
			// choosing by vibe — ADR-120 requires the applicability and the price to be on screen, and a
			// hub that had to hard-code those strings would be a second place for them to drift.
			//
			// ⚠ `pw_no_trace` is deliberately NOT part of this enum, though it also silences a capture.
			// It is a fail-closed secret guard with two enforcement points; putting it in the same list
			// would hand somebody a switch that removes a protection under the label "turn video off".
			"observe": map[string]any{"type": "enum", "group": "run", "default": "frames",
				"enum": []string{"off", "frames", "stream", "human", "record"}, "set_by": []string{"flag:--observe", "runconfig:observe"},
				"cost": map[string]any{
					"off":    map[string]any{"ru": "ничего не снимается — быстрее всего; смотреть будет не на что", "en": "nothing is captured — fastest; there will be nothing to look at"},
					"frames": map[string]any{"ru": "кадр на каждый шаг, их показывает хаб; замедляет прогон незначительно", "en": "one frame per step, rendered by the hub; slows a run slightly"},
					"stream": map[string]any{"ru": "живой экран без украшений — годится и человеку, и машине", "en": "the live screen, undecorated — usable by a person and a machine"},
					"human":  map[string]any{"ru": "курсор, замедление, подсветка. ⚠ МЕНЯЕТ ТАЙМИНГ — не для проверки откликов, гонок и таймаутов; с эталонным режимом не смешивается", "en": "cursor, slowdown, highlight. ⚠ CHANGES TIMING — not for response times, races or timeouts; does not mix with golden mode"},
					"record": map[string]any{"ru": "видеофайл артефактом после прогона, С КУРСОРОМ. ⚠ МЕНЯЕТ ТАЙМИНГ ровно как human, эталоном такой прогон быть не может. ⚠ НЕВОЗМОЖЕН при подключении к чужому браузеру по CDP", "en": "a video file as an artifact after the run, WITH THE CURSOR. ⚠ CHANGES TIMING exactly as human does, such a run cannot be a golden. ⚠ IMPOSSIBLE when attached to a foreign browser over CDP"},
				},
				// LIVE-HUMAN landed, so `human` came OUT of this list in the same change as the machinery
				// (brain/observe.py: SENTINEL_DECORATE). Left in, the hub would label an implemented mode
				// "not in this build" while runs in it succeed — the same lie as the reverse, and the
				// resolver's NOT_YET is the authority: tests/test_observation_modes_offline.py compares them.
				//
				// ADR-125 emptied it: `record` left with its machinery (SENTINEL_RECORD) exactly as `human`
				// did. EMPTY IS A REAL STATE and it means every mode this build declares, it performs —
				// it is not a list somebody forgot to fill. ⚠ `record` is still IMPOSSIBLE over CDP-attach,
				// but that is a property of the RUN, not of the build, so it belongs in the refusal the
				// resolver raises when it sees PW_CDP_ENDPOINT — not here, where it would tell every
				// non-CDP user their build cannot do something it does.
				"not_yet": []string{},
			},
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
		// [CFG-GROUP-LABELS-STOPPED-COVERING]. ИМЯ ГРУППЫ ПЕРЕЕХАЛО СЮДА, потому что до W17 его не было
		// нигде: `settingsSchema` объявляет у каждой записи ГРУППУ, но не то, как она называется
		// по-человечески, — и обе страницы держали свои рукописные копии (`CFG_GROUP_LABELS` в хабе,
		// `SETTING_GROUPS` в мастере). ADR-165 вырастил схему с 16 записей и 4 групп до 44 и 13, обе
		// копии остались на четырёх, и ни одна проверка этого не заметила.
		//
		// ⚠ ЦЕНА ОКАЗАЛАСЬ НЕ КОСМЕТИЧЕСКОЙ, И ЭТО ЗАМЕРЕНО. Мастер рисовал контролы ТОЛЬКО для групп из
		// своей копии — 23 настройки из 44, — `buildConfigDoc` собирает лишь отрисованное (`if (!el)
		// return`), а PUT ЗАМЕЩАЕТ секцию целиком (замерено: PUT {log_keep,heal_auto} затем PUT {log_keep}
		// оставляет в документе только `log_keep`). То есть одно сохранение из мастера УНИЧТОЖАЛО 21
		// сохранённое значение молча.
		//
		// `order` задан явным полем, а не позицией: map в Go неупорядочен, и порядок разделов не может
		// зависеть от того, как его сегодня обошли.
		"setting_groups": settingGroups,
		// ADR-109 / Alex's directive: which sections of the stored document configure the TOOL (admin
		// only) and which belong to the person using it. Published verbatim from the one map that
		// enforces it (configscope.go), so an interface disables what a caller may not change instead of
		// letting them fill in a form whose save is going to be refused.
		"config_sections": configSectionScope,
		"service":         serviceSchema,
		"not_published":   envNotPublished,
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
// settingGroups — человеческое имя каждой группы `settingsSchema`, на обоих языках, с явным порядком.
// ЕДИНСТВЕННЫЙ источник: обе страницы раньше держали свои копии, расходившиеся молча. Полноту сверяет
// TestEverySettingGroupHasBilingualLabel — В ОБЕ СТОРОНЫ, поэтому ни группа без метки, ни метка без
// группы (как мёртвая `logging` в хабе) больше не проходят.
var settingGroups = map[string]map[string]any{
	"gates":     {"ru": "Гейты — что считать провалом", "en": "Gates — what counts as a failure", "order": 1},
	"healing":   {"ru": "Самопочинка", "en": "Self-healing", "order": 2},
	"hitl":      {"ru": "Участие человека", "en": "Human involvement", "order": 3},
	"retention": {"ru": "Сроки хранения", "en": "Retention", "order": 4},
	"llm":       {"ru": "Модели и подключение", "en": "Models and connection", "order": 5},
	"chat":      {"ru": "Чат", "en": "Chat", "order": 6},
	"explore":   {"ru": "Обход приложения", "en": "Exploring the application", "order": 7},
	"browser":   {"ru": "Браузер", "en": "Browser", "order": 8},
	"adapters":  {"ru": "Адаптеры", "en": "Adapters", "order": 9},
	"security":  {"ru": "Безопасность", "en": "Security", "order": 10},
	"telemetry": {"ru": "Телеметрия", "en": "Telemetry", "order": 11},
	"health":    {"ru": "Здоровье сервисов", "en": "Service health", "order": 12},
	"app":       {"ru": "Приложение под тестом", "en": "Application under test", "order": 13},
}

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

	// ── W15: РУЧКИ, КОТОРЫЕ ПРОДУКТ ЧИТАЛ, А СХЕМА НЕ НАЗЫВАЛА ───────────────────────────────
	// Обещание `agentctl config schema` — «every knob the product has, with its env name and default».
	// Замерено: выводится 169 имён, схема публиковала 42 дескриптора, и лишь 22 из них несли имя
	// переменной. Ниже — те, что читает ПРОГОН и что доезжают до него через окружение; ручки самой
	// службы лежат в `serviceSchema`, а всё остальное закрыто записанной причиной в `envNotPublished`.
	// Третьего состояния больше нет, и это утверждает гейт tests/test_env_inventory_offline.py.
	//
	// ⚠ ЗАПИСЬ ЗДЕСЬ — ОБЕЩАНИЕ ДОСТАВКИ, а не подпись: её держит TestEverySettingIsPersistable, и
	// нарушенной она уже была — `HEAL_LLM` стоял в схеме и до прогона не доезжал ВООБЩЕ.
	"adapters": map[string]any{
		"env": "SENTINEL_ADAPTERS", "type": "string", "default": "", "group": "adapters",
		"hint": map[string]string{
			"ru": "Имена importable-модулей через запятую, импорт которых регистрирует внешние адаптеры модели, аутентификации и развёртывания; неимпортируемое имя роняет конфигурацию, а не замалчивается.",
			"en": "Comma-separated importable module names whose import registers out-of-tree model, auth and deploy adapters; a module that cannot be imported raises rather than being ignored.",
		},
	},
	"adaptive_tokens": map[string]any{
		"env": "LLM_ADAPTIVE_TOKENS", "type": "bool", "default": true, "group": "llm",
		"hint": map[string]string{
			"ru": "Повторять тот же запрос с удвоенным потолком ответа, когда модель упёрлась в потолок и не выдала содержимого (выключается значениями 0, false, no, off).",
			"en": "Retry the same request with a doubled answer ceiling when the model hit the cap before producing any content (turned off by 0, false, no, off).",
		},
	},
	"app_log_cap": map[string]any{
		"env": "PW_APP_LOG_CAP", "type": "int", "default": 500, "group": "gates",
		"hint": map[string]string{
			"ru": "Сколько сообщений тестируемого приложения (ошибки JS, консоль, упавшие запросы, ответы 4xx-5xx, диалоги) снимать за прогон — по достижении съёмка останавливается с отметкой `app.log_capped`, и этим же числом ограничен счётчик, с которым сравнивает `SENTINEL_FAIL_ON_APP_ERRORS`.",
			"en": "How many messages from the application under test (JS errors, console, failed requests, 4xx-5xx responses, dialogs) are captured per run — on reaching it capture stops with an `app.log_capped` note, and the same number bounds the tally `SENTINEL_FAIL_ON_APP_ERRORS` compares against.",
		},
	},
	"click_nav_settle_ms": map[string]any{
		"env": "SENTINEL_CLICK_NAV_SETTLE_MS", "type": "int", "default": 250, "group": "browser",
		"hint": map[string]string{
			"ru": "Сколько миллисекунд ждать смены адреса после клика, прежде чем считать, что навигации не было; 0 — не ждать вовсе, и потолок платит только тот клик, который навигацией не оказался.",
			"en": "How many milliseconds to wait for the URL to change after a click before concluding there was no navigation; 0 — do not wait at all, and only a click that was not a navigation pays the ceiling.",
		},
	},
	"env_allow": map[string]any{
		"env": "SENTINEL_ENV_ALLOW", "type": "string", "default": "", "group": "security",
		"hint": map[string]string{
			"ru": "Дополнительные ИМЕНА переменных окружения через запятую, которые пропускать в прогон сверх встроенного списка — нужно, например, для пароля тестируемого приложения (AUT_PASSWORD).",
			"en": "Extra environment variable NAMES, comma-separated, let through to the run beyond the built-in list — needed for e.g. the application-under-test password (AUT_PASSWORD).",
		},
	},
	"env_allowlist": map[string]any{
		"env": "SENTINEL_ENV_ALLOWLIST", "type": "bool", "default": true, "group": "security",
		"hint": map[string]string{
			"ru": "Пропускать в прогон только разрешённые переменные окружения; выключение значением 0 отдаёт прогону всё окружение хоста целиком, включая посторонние ключи и токены.",
			"en": "Pass only allowlisted environment variables into the run; turning it off with 0 hands the run the host's entire environment, unrelated keys and tokens included.",
		},
	},
	"explore_fail_limit": map[string]any{
		"env": "SENTINEL_EXPLORE_FAIL_LIMIT", "type": "int", "default": 2, "group": "explore",
		"hint": map[string]string{
			"ru": "Сколько раз обход пробует один и тот же элемент, прежде чем выбросить его из кандидатов; значения меньше 1 подтягиваются до 1.",
			"en": "How many times explore retries the same element before dropping it from the candidate set; values below 1 are raised to 1.",
		},
	},
	"health_skip": map[string]any{
		"env": "SENTINEL_HEALTH_SKIP", "type": "string", "default": "", "group": "health",
		"hint": map[string]string{
			"ru": "Список компонентов через запятую (store, llm, orchestrator), стартовую проверку которых не выполнять; каждый пропуск объявляется в журнале и ухудшает вердикт.",
			"en": "Comma-separated components (store, llm, orchestrator) whose start-up health check is skipped; every skip is announced in the log and degrades the verdict.",
		},
	},
	"ignore_https_errors": map[string]any{
		"env": "PW_IGNORE_HTTPS_ERRORS", "type": "bool", "default": false, "group": "gates",
		"hint": map[string]string{
			"ru": "Не считать ошибку TLS-сертификата цели отказом — только для стенда с самоподписанным или просроченным сертификатом; действует у прогона, который сам запускает браузер, а при подключении к браузеру-службе по CDP контекст не создаётся и настройка не применяется.",
			"en": "Do not treat the target's TLS certificate error as a failure — only for a stand with a self-signed or expired certificate; it applies to a run that launches its own browser, while a CDP-attached run adopts an existing context and the option is not applied.",
		},
	},
	"llm_live_probe": map[string]any{
		"env": "SENTINEL_LLM_LIVE_PROBE", "type": "bool", "default": false, "group": "llm",
		"hint": map[string]string{
			"ru": "Перед стартом прогона спрашивать `/models` у LLM_BASE_URL и отказывать кодом 3, если ответа нет за 2 секунды; при пустом LLM_BASE_URL проверка ничего не спрашивает.",
			"en": "Before a run starts, ask LLM_BASE_URL for `/models` and refuse with exit code 3 when nothing answers within 2 seconds; with LLM_BASE_URL empty the probe asks nothing.",
		},
	},
	"llm_seed": map[string]any{
		"env": "SENTINEL_LLM_SEED", "type": "int", "default": 0, "group": "llm",
		"hint": map[string]string{
			"ru": "Целое число, которое ЗАПРАШИВАЕТСЯ сидом у OpenAI-совместимого эндпоинта (провайдер вправе его проигнорировать); пусто — сид не отправляется вовсе, а у Anthropic такого параметра нет.",
			"en": "An integer REQUESTED as a seed from an OpenAI-compatible endpoint (a provider may ignore it); empty means no seed is sent at all, and Anthropic has no such parameter.",
		},
	},
	"map_gate": map[string]any{
		"env": "SENTINEL_MAP_GATE", "type": "bool", "default": true, "group": "hitl",
		"hint": map[string]string{
			"ru": "Спрашивать человека, одобряет ли он изученную карту сайта, прежде чем писать по ней тест; выключается значением 0, а без подключённого оркестратора гейт пропускает себя сам.",
			"en": "Ask a person to approve the explored site map before authoring a test over it; 0 turns it off, and with no orchestrator wired the gate skips itself.",
		},
	},
	"map_gate_timeout": map[string]any{
		"env": "SENTINEL_MAP_GATE_TIMEOUT", "type": "int", "default": 300, "group": "hitl",
		"hint": map[string]string{
			"ru": "Сколько секунд ждать решения человека по карте — по истечении прогон получает ОТКАЗ, а не одобрение.",
			"en": "How many seconds to wait for the human decision on the map — on expiry the run gets a REJECTION, not an approval.",
		},
	},
	"max_tokens_goal_page": map[string]any{
		"env": "LLM_MAX_TOKENS_GOAL_PAGE", "type": "int", "default": 2048, "group": "llm",
		"hint": map[string]string{
			"ru": "Потолок токенов ответа в запросе «какая страница целевая»; на рассуждающей модели урезанный потолок обрывает ответ до закрывающей скобки.",
			"en": "Answer-token ceiling for the “which page is the goal page” request; on a reasoning model a tight ceiling truncates the reply before its closing brace.",
		},
	},
	"max_tokens_hard": map[string]any{
		"env": "LLM_MAX_TOKENS_HARD", "type": "int", "default": 16384, "group": "llm",
		"hint": map[string]string{
			"ru": "Абсолютный предел, выше которого удвоение потолка ответа не поднимается.",
			"en": "The absolute cap the adaptive ceiling never escalates past.",
		},
	},
	"max_tokens_pick": map[string]any{
		"env": "LLM_MAX_TOKENS_PICK", "type": "int", "default": 1024, "group": "llm",
		"hint": map[string]string{
			"ru": "Потолок токенов ответа, когда модель выбирает одно следующее действие.",
			"en": "Answer-token ceiling when the model picks a single next action.",
		},
	},
	"max_tokens_scenario": map[string]any{
		"env": "LLM_MAX_TOKENS_SCENARIO", "type": "int", "default": 3072, "group": "llm",
		"hint": map[string]string{
			"ru": "Потолок токенов ответа, когда модель пишет сценарий целиком.",
			"en": "Answer-token ceiling when the model authors a whole scenario.",
		},
	},
	"otel_endpoint": map[string]any{
		"env": "OTEL_EXPORTER_OTLP_ENDPOINT", "type": "string", "default": "", "group": "telemetry",
		"hint": map[string]string{
			"ru": "Адрес OTLP-коллектора для трассировки прогона; пусто — трассировка выключена (пустой трассер без накладных расходов), а службы читают то же имя из своего окружения развёртывания.",
			"en": "OTLP collector endpoint for the run's tracing; empty turns tracing off (a no-op tracer, zero overhead), and the services read the same name from their own deployment environment.",
		},
	},
	"prom_pushgateway": map[string]any{
		"env": "PROM_PUSHGATEWAY", "type": "string", "default": "", "group": "telemetry",
		"hint": map[string]string{
			"ru": "Адрес Prometheus Pushgateway, куда после прогона отправляются метрики его отчёта; пусто — не отправлять.",
			"en": "Prometheus Pushgateway address the run's report metrics are pushed to after a run; empty means no push.",
		},
	},
	"refine_history_keep": map[string]any{
		"env": "SENTINEL_REFINE_HISTORY_KEEP", "type": "int", "default": 6, "group": "chat",
		"hint": map[string]string{
			"ru": "Сколько последних реплик человека попадает в запрос на уточнение целиком — всё, что старше, сворачивается в одну строку, чтобы стоимость беседы не росла с её длиной.",
			"en": "How many of the most recent user turns go into the refine prompt in full — anything older collapses into a single line so the conversation's cost stays bounded as it grows.",
		},
	},
	"refine_reverify": map[string]any{
		"env": "SENTINEL_REFINE_REVERIFY", "type": "bool", "default": false, "group": "chat",
		"hint": map[string]string{
			"ru": "Изучать сайт заново на каждом ходе беседы вместо уточнения по сохранённой карте — дороже и с браузером, но снимает риск устаревшей карты.",
			"en": "Re-explore the site on every chat turn instead of refining over the stored map — more expensive and with a browser, but it removes the risk of a stale map.",
		},
	},
	"scenario_map_chars": map[string]any{
		"env": "LLM_SCENARIO_MAP_CHARS", "type": "int", "default": 8000, "group": "llm",
		"hint": map[string]string{
			"ru": "Сколько символов карты сайта уезжает в промпт авторинга; невлезшее объявляется остатком, а не отбрасывается молча.",
			"en": "How many characters of the site map go into the authoring prompt; whatever does not fit is declared as a remainder rather than dropped silently.",
		},
	},
	"slow_load_ms": map[string]any{
		"env": "SENTINEL_SLOW_LOAD_MS", "type": "int", "default": 0, "group": "app",
		"hint": map[string]string{
			"ru": "С какого времени загрузки документа в миллисекундах (по данным самого браузера) сообщать, что приложение отвечало медленно; 0 — не сообщать, и покрыты только навигации, не клики.",
			"en": "From what document load time in milliseconds (measured by the browser itself) to report the application as slow; 0 — never report, and only navigations are covered, not clicks.",
		},
	},
	"step_menu_chars": map[string]any{
		"env": "LLM_STEP_MENU_CHARS", "type": "int", "default": 8000, "group": "llm",
		"hint": map[string]string{
			"ru": "Сколько символов перечня кандидатов уезжает в промпт одного шага.",
			"en": "How many characters of the candidate menu go into a single step's prompt.",
		},
	},
	"trace_on_degraded": map[string]any{
		"env": "SENTINEL_TRACE_ON_DEGRADED", "type": "bool", "default": false, "group": "retention",
		"hint": map[string]string{
			"ru": "Сохранять `trace.zip` и у прогона, который вышел с кодом 0, но неполно — обход не дошёл до конца или остались несопоставленные шаги; по умолчанию трейс остаётся только у прогона с ненулевым кодом (ADR-084).",
			"en": "Keep `trace.zip` also for a run that exited 0 but incomplete — the crawl did not finish or steps stayed unmatched; by default a trace survives only a non-zero exit (ADR-084).",
		},
	},
	"trace_raw": map[string]any{
		"env": "SENTINEL_TRACE_RAW", "type": "bool", "default": false, "group": "security",
		"hint": map[string]string{
			"ru": "Сохранять трейс без очистки — в нём остаются введённые пароли и токены, это режим диагностики самого инструмента, и он объявляется в журнале каждый раз.",
			"en": "Keep the trace unredacted — typed passwords and tokens stay in it; this is a mode for diagnosing the tool itself, and it is announced in the log every time.",
		},
	},
	"video_always": map[string]any{
		"env": "SENTINEL_VIDEO_ALWAYS", "type": "bool", "default": false, "group": "retention",
		"hint": map[string]string{
			"ru": "Сохранять `video.webm` даже у прогона, завершившегося чисто; по умолчанию запись, сделанную режимом `observe=record`, у зелёного прогона удаляют после прогона.",
			"en": "Keep `video.webm` even when the run finished clean; by default a recording made by `observe=record` is deleted on a green run.",
		},
	},
	"video_on_degraded": map[string]any{
		"env": "SENTINEL_VIDEO_ON_DEGRADED", "type": "bool", "default": false, "group": "retention",
		"hint": map[string]string{
			"ru": "Сохранять `video.webm` у прогона, который вышел с кодом 0, но неполно (обход не дошёл до конца или остались несопоставленные шаги); действует только на прогон, который вообще записывал видео — то есть на `observe=record`.",
			"en": "Keep `video.webm` for a run that exited 0 but incomplete (crawl unfinished or steps unmatched); it affects only a run that recorded at all, i.e. `observe=record`.",
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
	Scenario    string `json:"scenario"`     // --scenario: pick a named scenario out of the RunConfig
	AutVersion  string `json:"aut_version"`  // --aut-version: app-under-test sha, keys flake quarantine
	CI          bool   `json:"ci"`           // --ci: forbids --force-replay
	ForceReplay bool   `json:"force_replay"` // --force-replay: bypass the plan_hash hard-abort
	// ⚠ УКАЗАТЕЛЬ, А НЕ bool, и по той же причине, что у `PWNoTrace` ниже: «снял» и «не выбирал»
	// обязаны быть РАЗНЫМИ фактами. `heal_llm` — ЕДИНСТВЕННАЯ ручка, которую схема публикует на ОБОИХ
	// слоях сразу: как настройку развёртывания (`settings.heal_llm`, env HEAL_LLM) и как поле прогона
	// (`fields.heal_llm`, флаг --heal-llm). Правила старшинства для этой пары не было написано нигде,
	// а пер-прогонный слой умел говорить только «да»: с обычным bool снятая галочка неотличима от
	// незаполненного поля, и человек, у которого в развёртывании сохранено HEAL_LLM=1, не мог
	// выключить самопочинку на ОДИН прогон — нечем. nil = «не выбирал», и тогда действует сохранённое.
	HealLLM      *bool `json:"heal_llm"`      // --heal-llm: allow LLM re-grounding during heal
	IgnoreRobots bool  `json:"ignore_robots"` // --ignore-robots: ADR-133, a person's explicit choice
	// LIVE-MATRIX (ADR-120): what this run observes. Empty = the saved `run.observe` if the deployment
	// has one, else the product default `frames`; W17 built that layer — before it, "the deployment
	// default" named a constant nothing could change. The form
	// SHOWS rather than implies — an invisible default makes "I did not choose" and "I chose exactly
	// this" the same act, and then nobody can say what the run will produce.
	Observe          string `json:"observe"`
	PlanBudget       string `json:"plan_budget"`   // RunConfig only — no flag exists
	HealBudget       string `json:"heal_budget"`   // RunConfig only
	TotalBudget      string `json:"total_budget"`  // RunConfig only
	StorageState     string `json:"storage_state"` // RunConfig auth.* — reuse a saved session
	StorageStateSave string `json:"storage_state_save"`
	LoginPlan        string `json:"login_plan"`
	// ⚠ УКАЗАТЕЛЬ, А НЕ bool, и по той же причине, что у `Vision`/`Structured` в llmenv.go: «снят» и
	// «не указан» обязаны быть РАЗНЫМИ фактами. С обычным bool они один и тот же байт, и как только у
	// секции `auth` появляется сохранённый слой (а он появляется), выключить защиту на один прогон
	// становится нечем — сохранённое `true` перебить было бы невозможно. nil = «не выбирал».
	PWNoTrace *bool `json:"pw_no_trace"`

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

// modeFromArgs derives a run's mode brand from the argv that will REALLY be executed, so the record
// and the run cannot disagree. [RUN-MODE-LABELS-THE-RECORD-NOT-THE-RUN]: the brand used to be copied
// straight from `req.Mode`, a string no validation ever checked against the enum, and it lied in BOTH
// directions — an explore run branded `goal` (the person picked «Цель», left the goal empty, and
// nothing passed `--goal`), and a real chat turn branded `describe`. The brand is not cosmetic: it
// reaches GET /v1/runs, ResultRecord.Mode and the `{mode,target}` metric labels, so a lie here is a
// lie in the record, the report and the dashboard at once.
//
// Derived from argv rather than recomputed from the request on purpose: a second copy of the same
// formula would agree with the first at every error, which is exactly what a gate cannot detect.
// argv is the artefact another process reads.
//
// Precedence follows what agentctl itself does with these flags: the subcommand first, then replay,
// then an explicit `--mode`, then the authoring flags (ADR-027: authoring is carried by
// `--goal`/`--describe`, and `--mode goal|describe` is refused), and `explore` last — which is
// agentctl's own default when nothing says otherwise.
func modeFromArgs(args []string) string {
	if len(args) > 0 && args[0] == "baseline" {
		return "baseline"
	}
	for i, a := range args {
		switch {
		case a == "--replay":
			return "replay"
		case a == "--mode" && i+1 < len(args):
			return args[i+1]
		}
	}
	for _, a := range args {
		if a == "--goal" {
			return "goal"
		}
	}
	for _, a := range args {
		if a == "--describe" {
			return "describe"
		}
	}
	return "explore"
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
	// Явный выбор уезжает В ОБЕ СТОРОНЫ. `--heal-llm=false` нужен именно потому, что унаследованное
	// HEAL_LLM=1 иначе победит: run-var дописывается ТОЛЬКО когда флаг передан (cmd/agentctl), значит
	// без флага сохранённое доезжает до мозга — это и есть починка ADR-165, и её тут нельзя терять.
	if req.HealLLM != nil {
		args = append(args, fmt.Sprintf("--heal-llm=%t", *req.HealLLM))
	}
	if req.IgnoreRobots {
		args = append(args, "--ignore-robots")
	}
	// LIVE-MATRIX (ADR-120). This rides argv, not environment, and that is not a style choice.
	// agentctl builds the brain's run-vars unconditionally — `"SENTINEL_OBSERVE=" + *observe` is
	// appended whether or not --observe was given — and those vars go AFTER the inherited env, where
	// os/exec keeps the LAST value. An inherited SENTINEL_OBSERVE=off was therefore overwritten by the
	// empty string, and the brain resolved its default while honestly reporting "nothing was asked
	// for": the hub's choice vanished en route and the log said the person never made one. Empty stays
	// empty here too — an absent flag and an explicit one must remain different facts.
	if v := strings.TrimSpace(req.Observe); v != "" {
		args = append(args, "--observe", v)
	}
	if runCfgPath != "" {
		args = append(args, "--run-config", runCfgPath)
	}
	return args
}

// baselineArgs builds the argv for `agentctl baseline update`. A named function rather than an
// inline block so a gate can assert what the server REALLY passes: a claim about the shape of this
// source would be a surrogate, and mutations walk straight through those.
//
// `baseline update` executes a FROZEN plan, so the flags that BUILD a run (planner, coverage target,
// step ceiling, goal) do not belong to it and are deliberately absent. What does belong is every
// channel the answer already promised the caller: the RunConfig file — the only way budgets and the
// `auth` block reach the brain — and the observation mode.
func baselineArgs(artDir, runCfgPath string, req *runRequest) []string {
	args := []string{"baseline", "update", "--plan", req.plan, "--artifact-dir", artDir}
	if req.Target != "" {
		args = append(args, "--target", req.Target)
	}
	if runCfgPath != "" {
		args = append(args, "--run-config", runCfgPath)
	}
	if req.Observe != "" {
		args = append(args, "--observe", req.Observe)
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
	if req.PWNoTrace != nil {
		// Пишем ОБА значения, а не только истину. Явный `false` — это и есть отмена сохранённого
		// `true`; строка, которую не пишут, ничего не отменяет.
		authLines = append(authLines, fmt.Sprintf("  pw_no_trace: %t", *req.PWNoTrace))
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
		// ⚠ ПРЕЖНИЙ ДОВОД ЗДЕСЬ БЫЛ ВЕРЕН ПО ФАКТУ И ЛОЖЕН ПО СУЩЕСТВУ. Он гласил: «у `baseline update`
		// свой маленький набор флагов, поэтому run-флаги ниже сюда намеренно НЕ дописываются». Флагов
		// действительно не было — но из этого следовало не «их не нужно передавать», а «передать их
		// нечем». Замерено: сервер для `mode=baseline` принимает настройки, ПРИМЕНЯЕТ личные умолчания,
		// НАЗЫВАЕТ применённое в ответе 202 полем `inherited_defaults` и пишет выше `run.yaml` — файл,
		// который по своему заголовку есть «конфигурация, под которой прогон РЕАЛЬНО шёл». Прогон под
		// ней не шёл: бюджеты и блок `auth` доезжают до мозга ТОЛЬКО этим файлом, а путь к нему в argv
		// не попадал. Интерфейс подтверждал несделанное.
		//
		// Подкоманда получила `--run-config` и `--observe` (W16); остальные run-флаги ей по-прежнему не
		// принадлежат — `baseline update` исполняет ЗАМОРОЖЕННЫЙ план, а не строит новый.
		args = baselineArgs(artDir, runCfgPath, &req)
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
	// Клеймо — ПРОИЗВОДНОЕ от argv, а не копия поля запроса: см. modeFromArgs. Под мьютексом, потому
	// что запись уже лежит в s.runs (выше) и её читают маршруты и SSE.
	s.mu.Lock()
	rec.Mode = modeFromArgs(args)
	s.mu.Unlock()
	cmd := exec.Command(s.agentctl, args...)
	cmd.Dir = s.repo
	// ADR-063 + ADR-146: layer the LLM connection into the spawn env — process env > per-run >
	// persisted config > stored provider keys. os.Environ() (operator-controlled) still wins;
	// resolveRunEnv only fills LLM_* it does not already set, so a key saved through the UI is the
	// last resort and never displaces one the host passed in.
	cmd.Env = resolveRunEnv(os.Environ(), req.llm, s.mergedPersistedEnv(), s.providerKeyEnvLayer())
	// ONE store for the whole deployment. agentctl starts its own gateway over repo/state/locators.db
	// when it inherits no address, so a run launched from here used to persist into a database this
	// process never reads — the `chats` projection the brain writes landed there, and GET /v1/chats
	// answered 0 about a conversation that plainly existed. Passing our own address makes the run write
	// where the API reads. Only when the gateway actually answered at boot (s.store != nil): handing
	// down an address that did not dial would replace a working local fallback with a dead one.
	if s.store != nil && s.storeAddr != "" {
		cmd.Env = append(cmd.Env, "STORE_ADDR="+s.storeAddr)
	}
	// LIVE-PER-RUN. THIS process names the run — the hub, the store and every route already use `id`.
	// agentctl would otherwise mint a second, unrelated id for the same run, and the live view (which
	// is asked about the id the hub knows) would never resolve a page. One run, one name.
	cmd.Env = append(cmd.Env, "SENTINEL_RUN_ID="+id)
	// ADR-126: the brain reports its per-node token deltas to the orchestrator and reads its verdict
	// (continue | abort | takeover, plus the map gate's answer) from the reply. `ORCH_ADDR` is the one
	// variable that turns `brain/runcontrol.py` from a no-op into a client, and it passes agentctl's
	// env allowlist because of the underscore-free prefix rule in `filteredEnv`. Empty when no
	// orchestrator is wired — the brain then behaves exactly as it did before, by construction.
	if s.orchAddr != "" {
		cmd.Env = append(cmd.Env, "ORCH_ADDR="+s.orchAddr)
	}
	// LIVE-MATRIX: the chosen mode does NOT ride here. It is an argv flag like every other per-run knob
	// (appendRunFlags), because agentctl's own run-vars overwrite an inherited SENTINEL_OBSERVE with the
	// empty string. Read that comment before moving this back into the environment.
	// Capture combined stdout+stderr into the run's stream (ring buffer + SSE fan-out). Setting
	// cmd.Stdout == cmd.Stderr makes os/exec merge them into ONE pipe with a single copy goroutine,
	// so lineWriter is intentionally not thread-safe — do NOT split Stdout/Stderr without a mutex.
	rec.sink = newLogSink(artDir) // M9-LIVE: runs/<id>/logs/{run.jsonl,run.log,events.jsonl}
	lw := &lineWriter{rs: rec.stream, sink: rec.sink}
	cmd.Stdout = lw
	cmd.Stderr = lw
	setProcGroup(cmd) // M9-LIVE: own process group, so a cancel reaches brain + executor + Chromium

	// ADR-126. BEFORE the spawn, so the orchestrator knows this run and its budgets by the time the
	// brain's first `ReportEvent` arrives. Registering after would leave a window in which the run is
	// reconciled against the deployment defaults instead of its own limits — a quiet wrong answer
	// rather than a loud missing one. Fail-open inside: an absent or unreachable orchestrator costs
	// one line and changes nothing about the run.
	s.registerRun(id, req.PlanBudget, req.HealBudget, req.TotalBudget)

	go func() {
		if err := cmd.Start(); err != nil {
			// A run that never started still has to FINISH properly: emit run.finished, release SSE
			// subscribers and close the log files, or the UI waits forever on a run that will never speak.
			s.mu.Lock()
			rec.State, rec.Error = "failed", err.Error()
			// [READYZ-BLIND-TO-AGENTCTL] / W6: THE EXIT CODE IS SET HERE, not left at Go's zero value.
			// 0 is the one number in this field that means "finished cleanly", and the record carried it
			// while the state beside it said `failed`: GET /v1/runs/{id} answered `state: failed,
			// exit_code: 0`. The run.finished frame five lines below has emitted -1 for this case since
			// ADR-089 — the STRUCT simply never agreed with the frame, so a client that polled and a
			// client that listened were told different things about one run.
			rec.ExitCode = -1
			rec.FinishedAt = time.Now().UTC().Format(time.RFC3339)
			// HEALTH-004: a run that could not be spawned is OURS. Set before upsertRun so the stored
			// row and the frame below say the same thing about the same run.
			rec.FaultDomain = faultDomain("failed", -1, "")
			finishedAt := rec.FinishedAt
			owner := rec.Owner
			s.mu.Unlock()
			// HEALTH-005 / [READYZ-BLIND-TO-AGENTCTL]: measured, the service journal held exactly ONE
			// line about this — `service.api_call POST /v1/runs → 202`, the line that makes the
			// deployment look like it worked. The failure belongs to the SERVICE, not to this run: it is
			// the same failure probeAgentctl now predicts, and it is identical for every run this process
			// will ever accept. Recording it only in the run's own stream would hide it where nobody
			// looks, because a run that produced no output is a run nobody opens.
			//
			// journalSubject rather than journalEvent, and the subject is the run's OWNER: an ownerless
			// service event is admin-only (svcjournal_read.go), so a non-admin account whose run just
			// died would have been unable to read the one record explaining why. `nil` for the request —
			// this goroutine outlives it, and the caller is already named by the owner.
			s.journalSubject(codeRunSpawnFailed, "warn", map[string]string{
				"run_id": rec.ID, "path": s.agentctl, "reason": err.Error(),
			}, nil, owner)
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
		// ADR-126: started AFTER the pid is published, because a supervisor that decides to stop a run
		// before there is anything to signal can only wait — and started as its own goroutine because
		// this one is about to block in cmd.Wait() for the whole life of the run. It returns by itself
		// when the run leaves "running"; there is no lifetime to manage.
		go s.superviseRun(rec)
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
				// -1 for the same reason as the failed spawn above: `failed` beside `exit_code: 0` reads
				// as a clean exit. exitForEvent below already compensated for the FRAME; the struct that
				// GET /v1/runs/{id} serialises did not.
				rec.State, rec.ExitCode, rec.Error = "failed", -1, err.Error()
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

// codeRunSpawnFailed is catalogued in brain/events.json (emitter: control-api). A named constant
// rather than an inline string for the same reason as codeReportFailed below:
// tests/test_event_catalog_offline.py finds codes by regexing quoted literals out of cmd/control-api,
// and a code spliced into a longer expression is invisible to it.
const codeRunSpawnFailed = "service.run_spawn_failed"

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

// verdictEnum maps the structured exit code to ResultRecord.verdict, READ FROM THE CATALOGUE
// (brain/events.json -> exit_codes[N].verdict). ADR-141.
//
// It used to be a switch naming 0, 2 and 3 with a `default: return "problem"`. That default is what
// the ADR-113 consolidation missed: brain/outcome.py already produced seven words, and three of them
// — `tool_failure` (4), `tool_failure_salvaged` (5) and `not_started` (-1) — were swallowed here and
// recorded as `problem`. Measured 2026-08-31, all three. The Results domain therefore showed an amber
// "the run found a problem" for a run whose own diagnosis was "OUR tool broke", which is the precise
// confusion the `fault` axis exists to prevent.
//
// An undeclared code is NOT invented into a word. It becomes "unknown", which the hub renders as
// "UNKNOWN EXIT CODE" — the same choice ExitInfoOf documents and the same one renderBuildVerdict
// already made. Returning "problem" instead would be the old defect in a new place: it reads as a
// finding about the application, which is exactly what an unexplainable exit is not known to be.
func verdictEnum(exit int) string {
	if info, ok := eventcatalog.ExitInfoOf(exit); ok && info.Verdict != "" {
		return info.Verdict
	}
	return "unknown"
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
		// HEALTH-004: quoted from the run record, not recomputed. `verdict` says WHAT happened and
		// `fault_domain` says WHOSE problem it is — two axes, neither derivable from the other.
		// ⚠ ADR-141 CORRECTS THE SENTENCE THAT STOOD HERE. It said `verdict` "is still one of the
		// coarse four", and that was true of this file only: brain/outcome.py had been producing seven
		// words since ADR-131/139, and the three it added were destroyed by verdictEnum's `default` on
		// the way in. Measured 2026-08-31: exit 4, 5 and -1 all arrived as `tool_failure`,
		// `tool_failure_salvaged` and `not_started` and were all stored as `problem`. The vocabulary is
		// now the catalogue's, and it is the same one on both sides of the boundary.
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
	// ADR-109: чей это прогон, решается ДО валидации, а не после: от владельца зависят и личные
	// умолчания ниже, и клеймо на самом прогоне. Раньше владелец ставился в самом конце — пока
	// умолчаний не было, порядок не имел значения.
	if c, ok := s.callerOf(r); ok {
		req.owner = c.owner()
	}
	// W15: сохранённые ЛИЧНЫЕ настройки прогона наконец действуют. Заполняются ТОЛЬКО поля, которых
	// запрос не нёс, и заполненное НАЗЫВАЕТСЯ в ответе — довод и замер записаны у
	// applyPersonalRunDefaults (configscope.go).
	inherited := s.applyPersonalRunDefaults(&req)

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
	rec := s.spawnRun(req)
	resp := map[string]any{"run_id": rec.ID, "artifact_dir": rec.ArtifactDir, "state": "running"}
	if len(inherited) > 0 {
		// Названо поимённо, а не сосчитано: «применено 3 умолчания» не отвечает на вопрос, который
		// человек задаёт («почему прогон пошёл на ДРУГОЙ адрес»), а перечень отвечает.
		resp["inherited_defaults"] = inherited
	}
	writeJSON(w, http.StatusAccepted, resp)
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
	// ADR-125 (LIVE-RECORD): the video of a run. Present only when it was asked for with
	// observe=record AND the run did not finish clean (or SENTINEL_VIDEO_ALWAYS=1) — so a 404 here is
	// the ORDINARY answer, not a fault. Listed by exact name for the same reason as every line above:
	// this route reads files a caller names, and a pattern would be a traversal wearing a convenience.
	// The per-step frames keep their own pattern (`frameNamePattern`) because there are many of them
	// and exactly one of these.
	"video.webm": true,
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
	} else if strings.HasSuffix(name, ".webm") {
		// ADR-125: the run's video, served INLINE — the hub plays it in the page, so an attachment
		// disposition would turn "watch what happened" into "download and find a player".
		//
		// ⚠ AND THE TYPE IS NOT COSMETIC HERE, which is how this was found. Measured against the
		// running product: the video reached the page and played — but only because Chromium sniffs a
		// blob's container for itself. We send `X-Content-Type-Options: nosniff` two lines above, so
		// relying on that is relying on a behaviour we have explicitly asked the browser not to
		// perform; a stricter engine is entitled to refuse, and the failure would look like a broken
		// recording rather than a wrong header.
		w.Header().Set("Content-Type", "video/webm")
	} else if isFrameName(name) {
		// Same defect, found in the same measurement and fixed in the same breath rather than left
		// beside its twin: a per-step PNG was going out as `application/json` too. It renders today
		// for the same accidental reason, and is wrong for the same real one.
		w.Header().Set("Content-Type", "image/png")
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

// refuseIfDeleteFailed answers the caller when a deletion could not be attempted or did not succeed,
// and reports whether it has already written the response.
//
// ⚠ ОДНА ФУНКЦИЯ НА ЧЕТЫРЕ УДАЛЕНИЯ — ПОТОМУ ЧТО ЧЕТЫРЕ КОПИИ РАЗОШЛИСЬ БЫ. Раньше каждый обработчик
// отвечал «deleted» БЕЗУСЛОВНО, в том числе когда хранилища нет вовсе: подтверждение приходило на
// работу, которой не делалось. Здесь различаются ТРИ исхода, которые прежний код складывал в один:
//   - хранилища нет      -> 503: удалять негде, и сказать «удалено» было бы неправдой;
//   - хранилище отказало -> 502: виновато ХРАНИЛИЩЕ, а не запрос, и код это называет;
//   - объект отсутствует -> УСПЕХ, и это не исключение: идемпотентность даёт SQL, гейтвей отвечает
//     OK на `DELETE ... WHERE id=?` по пустому множеству. Терять её нельзя — повторное удаление
//     обязано оставаться успехом.
func (s *server) refuseIfDeleteFailed(w http.ResponseWriter, what string, err error) bool {
	// ⚠ РАЗВЁРТЫВАНИЕ БЕЗ ХРАНИЛИЩА — НЕ ОТКАЗ, и запись реестра здесь ПЕРЕОЦЕНИЛА дефект. Она винила
	// и этот случай тоже («отвечает deleted даже когда store == nil, то есть когда попытки не было»).
	// Замерено: там, где хранилища нет, объектов нет ВООБЩЕ — список отдаёт пусто, `GET` по адресу
	// отдаёт 404. Удаление того, чего заведомо не существует, УСПЕШНО по той же причине, по которой
	// успешно удаление отсутствующей строки. И это закреплённый контракт вехи: M14_CONTRACT.md §3
	// («writes stay idempotent-safe, nothing 503s»), утверждаемый TestScenariosTestsChatsFailOpenNoStore.
	// Настоящий дефект — второй случай: хранилище ЕСТЬ и ОТКАЗАЛО. Складывать его с первым значит
	// отвечать «готово» на «не смог».
	if s.store == nil {
		return false
	}
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"error": "хранилище не смогло удалить " + what + ": " + err.Error()})
		return true
	}
	return false
}

func (s *server) handleDeleteScenario(w http.ResponseWriter, r *http.Request) {
	var err error
	if s.store != nil {
		err = s.store.deleteScenario(r.PathValue("id"))
	}
	if s.refuseIfDeleteFailed(w, "сценарий", err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
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
	var err error
	if s.store != nil {
		err = s.store.deleteTest(r.PathValue("id"))
	}
	if s.refuseIfDeleteFailed(w, "тест", err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
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
	var err error
	if s.store != nil {
		err = s.store.deleteChat(r.PathValue("id"))
	}
	if s.refuseIfDeleteFailed(w, "разговор", err) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
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
		// ⚠ ЧЕТВЁРТАЯ ДВЕРЬ, И ADR-166 НАЗЫВАЕТ ЕЁ ПОИМЁННО. Довод, по которому читатель личных
		// умолчаний живёт на сервере, звучит так: «тело POST /v1/runs шлют четыре разных места, и
		// читатель в интерфейсе чинил бы одну дверь из четырёх». Заглушка — одна из этих четырёх, и
		// она собирала запрос в процессе и звала spawnRun напрямую, минуя единственный вызов. Человек,
		// работающий через OpenAI-совместимый эндпоинт (Open WebUI, SDK, curl) — способность
		// `openai-shim` каталога, — получал прогон БЕЗ своих сохранённых умолчаний и не узнавал об этом.
		s.applyPersonalRunDefaults(&rr)
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
	s.applyPersonalRunDefaults(&rr) // та же четвёртая дверь — см. довод у соседнего вызова выше
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
	// ⚠ ОТКАЗ ЗДЕСЬ, ДО ВСЕГО ОСТАЛЬНОГО (ADR-160, решение Alex). Непригодный `CONTROL_API_TOKEN` —
	// единственный случай, когда запуск прерывается из-за кредентиала, и прерывается он ГРОМКО:
	// оператор задал секрет, секрет не защищает, и продолжить значило бы поднять развёртывание,
	// которое ВЫГЛЯДИТ защищённым. Предупредить и запуститься — тот же дефект с отсрочкой:
	// предупреждение листают, а слабый токен остаётся жить.
	//
	// Печатаем в stderr сами: до `s` ещё нет журнала, а причина обязана оказаться на экране.
	if tokSrc == tokenRefused {
		for _, w := range tokWarnings {
			fmt.Fprintf(os.Stderr, "control-api: FATAL — %s\n", w)
		}
		os.Exit(2)
	}
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
		s.journalEvent("service.stopped", "info", map[string]string{"svc": "control-api", "reason": "process exit"}, nil)
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
		s.journalEvent("service.stopped", "info", map[string]string{"svc": "control-api", "reason": "signal " + got.String()}, nil)
		s.journal.Close()
		signal.Stop(sig)
		// Platform-split (reraise_unix.go / reraise_windows.go): `syscall.Kill` does not exist on
		// Windows, and this line broke the cross-build for eleven days without anything noticing —
		// nothing compiles for Windows except `release.yml`, which had not run since the last tag.
		dieOfSignal(got.(syscall.Signal))
	}()
	s.journalEvent("service.started", "info", map[string]string{
		"svc": "control-api", "version": version, "supervisor": svclog.Supervisor(),
		"pid": strconv.Itoa(os.Getpid()), "detail": " — addr: " + addr,
	}, nil)

	// HEALTH-006: the PREVENTIVE half. Started here, after the journal exists so a transition has
	// somewhere to be recorded, and given a stop channel tied to the process rather than to a request.
	proberStop := make(chan struct{})
	defer close(proberStop)
	go s.runReadinessProber(proberStop)

	// M13 (ADR-050): connect to a persistent store-gateway if configured; else runs stay in-memory
	// (standalone/offline path, unchanged). Fail-open — an unreachable gateway only warns.
	if sa := os.Getenv("CONTROL_API_STORE_ADDR"); sa != "" {
		s.storeAddr = sa // remembered even on failure — see server.storeAddr / configTier (ADR-075)
		sc, err := newStoreClient(sa, os.Getenv("STORE_TOKEN"))
		// ⚠ HEALTH-006: the client is kept even when the boot probe failed. grpc.ClientConn heals on
		// its own (measured: killing and restarting the gateway moved /readyz store error -> ok with
		// no restart of this process), so what used to make a boot miss PERMANENT was discarding the
		// client here. Keeping it means a gateway that comes up a second later is simply used, and the
		// background prober below is what tells the operator when that happened.
		if sc != nil {
			s.store = sc
			defer sc.close()
		}
		if err != nil {
			fmt.Fprintf(os.Stderr, "control-api: WARNING — store-gateway %q did not answer at start: %v "+
				"(runs stay in memory until it does; the readiness prober will report when it comes up)\n", sa, err)
			// The one event a store-backed journal could never record: the store being unreachable.
			s.journalEvent("service.store_unreachable", "warn", map[string]string{"addr": sa, "reason": err.Error()}, nil)
		} else {
			fmt.Fprintf(os.Stderr, "control-api: persisting runs to store-gateway at %s\n", sa)
		}
	} else {
		// ADR-158: no external gateway configured — host one IN PROCESS rather than run without a
		// store. This is what makes the standalone tier the same PRODUCT as the multi-user one
		// (Alex's «одна версия» directive): local accounts, sign-in and per-account scoping stop
		// depending on whether an operator started a second process. See embedstore.go for why this
		// is the same implementation rather than a second one.
		if es, err := startEmbeddedStore(s.repo); err != nil {
			fmt.Fprintf(os.Stderr, "control-api: WARNING — could not start the embedded store: %v "+
				"(runs stay in memory and local accounts are unavailable)\n", err)
			s.journalEvent("service.store_unreachable", "warn",
				map[string]string{"addr": "embedded", "reason": err.Error()}, nil)
		} else {
			defer es.stop()
			sc, err := newEmbeddedStoreClient(es)
			if sc != nil {
				s.store = sc
				s.storeEmbedded = true
				defer sc.close()
			}
			if err != nil {
				fmt.Fprintf(os.Stderr, "control-api: WARNING — the embedded store did not answer: %v\n", err)
				s.journalEvent("service.store_unreachable", "warn",
					map[string]string{"addr": "embedded", "reason": err.Error()}, nil)
			} else {
				fmt.Fprintf(os.Stderr, "control-api: persisting runs to the embedded store at %s\n", es.path)
			}
		}
	}
	// ADR-159: первый администратор заводится САМ, пока оператор ещё смотрит в терминал.
	//
	// ⚠ СИНХРОННО И ДО `ListenAndServe`, в отличие от соседних строк. Две причины, и обе про порядок,
	// а не про скорость: строка с паролем обязана оказаться в выводе РЯДОМ с адресом интерфейса, а не
	// после первых запросов, иначе она утонет; и появление аккаунта закрывает `legacyOpen`, поэтому
	// сделать это надо ДО того, как первый запрос получит ответ по правилам, которых через миг не
	// будет. Стоимость — один вызов `listUsers` и, единожды за жизнь развёртывания, один KDF.
	s.ensureDefaultAdmin()

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
	s.journalEvent("service.token_source", "info", map[string]string{"source": string(tokSrc), "detail": " — warnings: " + strconv.Itoa(len(tokWarnings))}, nil)
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

// serviceSchema — ручки процессов СЛУЖБЫ: control-api, ретранслятора CDP, контейнера браузера,
// журнала сервиса. Отдельный блок, а не часть `settingsSchema`, и это не вкус: запись в настройках
// есть обещание, что значение ДОЕДЕТ ДО ПРОГОНА, и его держит гейт. Эти же читает сам сервис при
// старте — положить их туда значило бы пообещать доставку, которой нет, то есть завести второй
// `HEAL_LLM`: настройку, которую предлагают задать, принимают, подтверждают, и которая не делает
// ничего. Поэтому они публикуются ЧЕСТНО: с именем и умолчанием, но как то, что задаётся
// окружением развёртывания (compose/systemd), а не формой и не телом прогона.
var serviceSchema = map[string]any{
	"autotoken": map[string]any{
		"env": "CONTROL_API_AUTOTOKEN", "type": "bool", "default": true, "group": "control_api",
		"hint": map[string]string{
			"ru": "Заводить ли control-api свой бирер-токен при старте — значение «0» оставляет развёртывание без токена, и изменяющие запросы получают 403.",
			"en": "Whether control-api mints its own bearer token at startup — a value of 0 leaves the deployment tokenless and mutating requests get 403.",
		},
	},
	"cdp_live": map[string]any{
		"env": "CONTROL_API_CDP_LIVE", "type": "string", "default": "", "group": "control_api",
		"hint": map[string]string{
			"ru": "Адрес живого эндпоинта браузерной службы, который control-api проксирует; пусто — браузерной службы в развёртывании нет, и живое видео так и говорит.",
			"en": "Base URL of the browser service live endpoint that control-api proxies; empty means this deployment has no browser service and the live view says so.",
		},
	},
	"claim_ttl_ms": map[string]any{
		"env": "CDP_LIVE_CLAIM_TTL_MS", "type": "int", "default": 43200000, "group": "browser",
		"hint": map[string]string{
			"ru": "Сколько миллисекунд заявка прогона на браузер переживает свой последний признак жизни, прежде чем её забудут.",
			"en": "How many milliseconds a run's claim on the browser outlives its last sign of life before it is dropped.",
		},
	},
	"cors_origins": map[string]any{
		"env": "CONTROL_API_CORS_ORIGINS", "type": "string", "default": "", "group": "control_api",
		"hint": map[string]string{
			"ru": "Список origin'ов через запятую, которым разрешён доступ из браузера; пустой список не разрешает ни одного.",
			"en": "Comma-separated allowlist of browser origins; an empty list allows none.",
		},
	},
	"every_nth": map[string]any{
		"env": "CDP_LIVE_EVERY_NTH", "type": "int", "default": 2, "group": "browser",
		"hint": map[string]string{
			"ru": "Каждый какой кадр скринкаста отдавать (2 — каждый второй): больше число — реже картинка и дешевле прогон.",
			"en": "Which nth screencast frame to deliver (2 = every second one): a larger number means a rarer picture and a cheaper run.",
		},
	},
	"headed": map[string]any{
		"env": "PW_HEADED", "type": "bool", "default": false, "group": "browser",
		"hint": map[string]string{
			"ru": "Запускать Chromium браузера-службы видимым — этим включается профиль `vnc`, где браузер рисует в виртуальный X-дисплей и его видно по VNC; по умолчанию служба headless, потому что байтовая стабильность скриншотов замерена только в headless.",
			"en": "Launch the browser service's Chromium visible — this is what the `vnc` profile turns on, where the browser draws into a virtual X display exported over VNC; the service stays headless by default because screenshot byte-stability is measured only in headless.",
		},
	},
	"idle_ms": map[string]any{
		"env": "CDP_LIVE_IDLE_MS", "type": "int", "default": 15000, "group": "browser",
		"hint": map[string]string{
			"ru": "Через сколько миллисекунд без единого запроса скринкаст останавливается.",
			"en": "After how many milliseconds with nobody asking the screencast stops.",
		},
	},
	"log_max_mb": map[string]any{
		"env": "SENTINEL_LOG_MAX_MB", "type": "int", "default": 50, "group": "journal",
		"hint": map[string]string{
			"ru": "До скольких мегабайт растёт текстовый лог прогона (run.log), прежде чем его переименуют в одно предыдущее поколение; 0 — не вращать никогда.",
			"en": "How many megabytes a run's text log (run.log) grows to before it is rotated into a single previous generation; 0 means never rotate.",
		},
	},
	"max_height": map[string]any{
		"env": "CDP_LIVE_MAX_HEIGHT", "type": "int", "default": 720, "group": "browser",
		"hint": map[string]string{
			"ru": "Максимальная высота кадра живого экрана в пикселях — CDP ужимает кадр до неё.",
			"en": "Maximum live-screen frame height in pixels — CDP scales the frame down to it.",
		},
	},
	"max_width": map[string]any{
		"env": "CDP_LIVE_MAX_WIDTH", "type": "int", "default": 960, "group": "browser",
		"hint": map[string]string{
			"ru": "Максимальная ширина кадра живого экрана в пикселях — CDP ужимает кадр до неё.",
			"en": "Maximum live-screen frame width in pixels — CDP scales the frame down to it.",
		},
	},
	"print_token": map[string]any{
		"env": "CONTROL_API_PRINT_TOKEN", "type": "bool", "default": true, "group": "control_api",
		"hint": map[string]string{
			"ru": "Печатать ли токен в stderr при старте; печатается только сгенерированный или прочитанный из файла токен и только когда этот же процесс не обслуживает интерфейс.",
			"en": "Whether the token is printed to stderr at startup; only a generated or file-read token is printed, and only when this same process does not serve the UI.",
		},
	},
	"quality": map[string]any{
		"env": "CDP_LIVE_QUALITY", "type": "int", "default": 55, "group": "browser",
		"hint": map[string]string{
			"ru": "Качество JPEG у кадров живого экрана — значение уходит в CDP как есть, и им же снимается одиночный кадр.",
			"en": "JPEG quality of the live-screen frames — passed to CDP as is, and used for the single-frame capture too.",
		},
	},
	"serve_ui": map[string]any{
		"env": "CONTROL_API_SERVE_UI", "type": "bool", "default": false, "group": "control_api",
		"hint": map[string]string{
			"ru": "Обслуживать ли этим же процессом встроенный веб-интерфейс; CONTROL_API_UI_DIR включает интерфейс с диска и проверяется раньше.",
			"en": "Whether this process also serves the embedded WebUI; CONTROL_API_UI_DIR turns it on from disk instead and is checked first.",
		},
	},
	"service_log_level": map[string]any{
		"env": "SENTINEL_SERVICE_LOG_LEVEL", "type": "enum", "default": "info", "enum": []string{"debug", "info", "warn", "error"}, "group": "journal",
		"hint": map[string]string{
			"ru": "С какого уровня записи попадают в служебный журнал; нераспознанное значение пропускает всё, включая debug.",
			"en": "The lowest level that reaches the service journal; an unrecognized value lets everything through, debug included.",
		},
	},
	"service_log_max_mb": map[string]any{
		"env": "SENTINEL_SERVICE_LOG_MAX_MB", "type": "int", "default": 32, "group": "journal",
		"hint": map[string]string{
			"ru": "До скольких мегабайт растёт служебный журнал, прежде чем его вращают с одним сохранённым поколением; 0 — не вращать никогда.",
			"en": "How many megabytes the service journal grows to before it is rotated with a single kept generation; 0 means never rotate.",
		},
	},
	"session_ttl": map[string]any{
		"env": "CONTROL_API_SESSION_TTL", "type": "string", "default": "12h0m0s", "group": "control_api",
		"hint": map[string]string{
			"ru": "Сколько живёт сессия входа; читается как длительность Go, непарсимое или неположительное значение молча заменяется умолчанием.",
			"en": "How long a login session lives; parsed as a Go duration, an unparseable or non-positive value silently falls back to the default.",
		},
	},
	"ui_bootstrap_ttl": map[string]any{
		"env": "CONTROL_API_UI_BOOTSTRAP_TTL", "type": "string", "default": "5m0s", "group": "control_api",
		"hint": map[string]string{
			"ru": "Сколько времени одноразовый нонс первого входа остаётся обмениваемым; неположительное значение выключает вход по ссылке совсем, и токен копируют из файла руками.",
			"en": "How long the one-time first-login nonce stays exchangeable; a non-positive value disables the bootstrap link entirely and the token is copied out of the file by hand.",
		},
	},
	"vnc_geometry": map[string]any{
		"env": "SENTINEL_VNC_GEOMETRY", "type": "string", "default": "1280x800x24", "group": "browser",
		"hint": map[string]string{
			"ru": "Геометрия экрана Xvfb в контейнере браузера (ШИРИНАxВЫСОТАxГЛУБИНА); из неё же берётся размер окна Chromium в головном режиме, чтобы экран и окно не разошлись.",
			"en": "The Xvfb screen geometry in the browser container (WIDTHxHEIGHTxDEPTH); the headed Chromium window size is taken from the same value so screen and window cannot drift apart.",
		},
	},
}

// envNotPublished — имена, которые продукт читает, но схема НЕ публикует, и у каждого записана
// причина. Это вторая половина закрытого знаменателя: обещание «every knob» выполнимо только если
// у каждого выведенного имени есть либо дескриптор, либо объяснение, почему его нет. Молчание —
// третье состояние, и именно оно было нормой: 130 имён не имели ни того, ни другого.
//
// Три класса: проводка развёртывания (адреса, порты, пути, имена бинарей), секреты (описываются,
// но значением не публикуются — правило уже действует для LLM_API_KEY) и производные, которые
// продукт ставит сам. Причина пишется ДОСЛОВНО и читается человеком: «infra» без объяснения — это
// ярлык, а не причина.
var envNotPublished = map[string]string{
	"MESSAGE":                    "поверхность ОПУБЛИКОВАНА полем `fields.message`, а путь через окружение — не тот: `agentctl` дописывает `MESSAGE=` безусловно ПОСЛЕ унаследованного окружения, и os/exec берёт последнее значение. Задаётся флагом `--message`, как и записано в `set_by` поля.",
	"PLANNER":                    "поверхность ОПУБЛИКОВАНА полем `fields.planner` и перечнем `planner` верхнего уровня; переменная задаётся безусловным run-var из флага `--planner`, поэтому унаследованное значение до мозга не доезжает. Исключение — MCP-путь brain/server.py, где `setdefault(\"PLANNER\",\"llm\")` имеет собственное обоснование рядом.",
	"SENTINEL_CONVERSATION_ID":   "поверхность ОПУБЛИКОВАНА полем `fields.conversation_id`; переменная — безусловный run-var из флага `--conversation-id`, то есть окружением её не задать. Тот же случай, что MESSAGE и PLANNER: опубликовано не имя переменной, а способ, которым поле реально задаётся.",
	"SCENARIO":                   "поверхность ОПУБЛИКОВАНА полем `fields.scenario` (main.go:410, `set_by: flag:--scenario`); переменная — безусловный run-var из флага (cmd/agentctl/main.go, литерал `extra`), поэтому унаследованное значение до мозга не доезжает. Тот же случай, что MESSAGE и PLANNER: опубликовано не имя переменной, а способ, которым поле реально задаётся. ⚠ Имя было НЕВИДИМО обходу до W16: `brain/runconfig.py` читает его как `env.get(\"SCENARIO\")`, а форма «отображение» ловила только ОДНОБУКВЕННОГО получателя.",
	"CI":                         "поверхность ОПУБЛИКОВАНА полем `fields.ci` (main.go:423, `set_by: flag:--ci`); переменная — безусловный run-var из флага, окружением её не задать. Решает `fatal.force_replay_in_ci` (brain/__main__.py). ⚠ Имя было НЕВИДИМО обходу до W16 по ДЛИНЕ: `NAME` требовал трёх символов, поэтому оба читателя (`os.environ.get(\"CI\")` и `e.get(\"CI\")`) проходили мимо, хотя обе их формы поддерживались.",
	"SENTINEL_EXPLICIT":          "Не ручка человека вовсе: `agentctl` ВЫВОДИТ её из `fs.Visit` — списка флагов, которые пользователь реально передал, — и кладёт мозгу, чтобы `_overridable` отличал «задано явно» от «взято из файла или умолчания» (brain/runconfig.py:168). Задавать её снаружи бессмысленно и вредно: значение описывает argv этого самого прогона. Дескриптор в `settings` завести НЕЛЬЗЯ — запись там есть обещание доставки, а доставлять тут нечего. ⚠ Была НЕВИДИМА обходу до W16 по той же причине, что SCENARIO.",
	"ANTHROPIC_API_KEY":          "Ключ провайдера. Правило схемы уже написано и уже НАЗЫВАЕТ это имя: cmd/control-api/main.go:461 — `\"note\": \"secrets (LLM_API_KEY/ANTHROPIC_API_KEY) go in the control-api process env, never in this payload\"`, и main.go:348-349 — «Secrets are DESCRIBED (api_key.secret) but never VALUED». Доставка есть: имя стоит в exact-аллоулисте (cmd/agentctl/main.go:406) и приходит через secretKeyRef в чарте (deploy/sentinel/temp…",
	"ARTIFACT_DIR":               "Продукт ставит сам из флага `--artifact-dir` (cmd/agentctl/main.go:595) и run_id. Экспорт переменной мёртв ДВАЖДЫ: имени нет ни в exact-аллоулисте, ни под одним из префиксов LLM_/OTEL_/PW_/PLAYWRIGHT_/SENTINEL_ (main.go:400-424), поэтому filteredEnv его срезает; и даже если бы не срезал, run-var дописывается ПОСЛЕ (main.go:457 — `cmd.Env = append(filteredEnv(), append([]string{…}, extra...)...)`), последнее значен…",
	"AUT_VERSION":                "⚠ ЭТО ПЕРЕМЕННАЯ СУЩЕСТВУЮЩЕГО ПОЛЯ БЛОКА `fields`. Поле `aut_version` уже опубликовано (cmd/control-api/main.go:394) — ему нужно НЕ новое поле, а ключ `\"env\": \"AUT_VERSION\"` у существующего. Но публиковать его честно можно только вместе с починкой доставки (см. delivery), иначе схема пообещает второе имя, которое, будучи экспортировано, ничего не делает.",
	"BRAIN_PYTHON":               "Имя/путь интерпретатора, которым запускается brain — ровно «имя бинаря» из определения infra. Задаётся окружением развёртывания, а не человеком в интерфейсе; docs/DEVELOPMENT.md:48 — «`agentctl` автоматически использует `./.venv/bin/python` для запуска brain (переопределяется через `BRAIN_PYTHON`)». Побочно: это тестовый шов — три сьюта подменяют им brain на шелл-скрипт (cmd/agentctl/observe_flag_test.go:36, inher…",
	"CDP_INTERNAL_PORT":          "Порт петлевого интерфейса внутри контейнера браузера — проводка, не ручка. ⚠ Смежная открытая находка, попадающая ровно сюда: BACKLOG.md:182 [GATE-STEALS-ANOTHER-BROWSER] — сьют живого видео рандомизирует CDP_LISTEN_PORT и CDP_LIVE_PORT, а этот не задаёт вовсе, и при чужом сервисе на 9222 гейт молча меряет ЧУЖОЙ браузер.",
	"CDP_LISTEN_ADDR":            "Адрес привязки форвардера. Задаётся составом развёртывания; pw-executor/src/launch.ts:141 и server.ts:439 прямо разбирают случай `CDP_LISTEN_ADDR=::` как «IPv6-only deployment», то есть это выбор топологии, а не поведения прогона.",
	"CDP_LISTEN_PORT":            "Публикуемый порт релея — проводка. Задаётся compose (сервис `browser`, docker-compose.yml:308) и сьютами (tests/test_live_video_offline.py:90).",
	"CDP_LIVE_PORT":              "Порт живого сервиса — проводка. Второе чтение в server.ts подставляет этот порт в АДРЕС заявки, выводимый из PW_CDP_ENDPOINT (ADR-121, ARCHITECTURE.md:246: «порт заменяется на `CDP_LIVE_PORT`, путь — на `/live/claim`»), то есть значение участвует в вычислении адреса, а не в поведении. Публиковать как ручку значило бы предложить менять порт, по которому два процесса развёртывания находят друг друга.",
	"CHECKPOINT_DSN":             "Строка подключения к Postgres — кредентиал: собственный чарт продукта доставляет её через secretKeyRef и относит к секретам, а плоское значение помечено «dev/offline fallback: plaintext DSN» (cronjob.yaml:69-72). По правилу main.go:348-349 такое описывается, но не отдаётся значением. ⚠ Двойственность записать честно: это ОДНОВРЕМЕННО проводка (адрес хранилища чекпойнтов) — brain/adapters.py:310-311 объявляет её кл…",
	"CONTROL_API_ADDR":           "Адрес привязки самого сервиса — проводка развёртывания. Задаётся compose/systemd; менять его из интерфейса, который сам по этому адресу и отвечает, бессмысленно.",
	"CONTROL_API_AGENTCTL":       "Путь до бинаря, который сервис запускает на каждый прогон — ровно «имя бинаря» из определения infra.",
	"CONTROL_API_ORCH_ADDR":      "Путь unix-сокета оркестратора — прямо «путь сокета» из определения infra, и комментарий у читателя приводит именно такой пример значения.",
	"CONTROL_API_STORE_ADDR":     "Адрес gRPC-шлюза хранилища — проводка. В compose-стеке он уже проставлен, и session.go:543-546 прямо пишет, что задавать его вручную приходится только вне compose.",
	"CONTROL_API_TOKEN":          "Bearer-токен сервиса — по правилу main.go:348-349 описывается, но не отдаётся значением. Полезное для дескриптора, если он будет: с ADR-160 у env-ветки появился ПОЛ и отказ старта — token.go:143-149 `if !usableToken(v) { … return \"\", tokenRefused, path, warnings }`, границы token.go:35-36 `tokenMinLen = 16`, `tokenMaxLen = 512`. Комментарий token.go:133-138 сам называет прошлую ошибку описания: «`CONTROL_API_TOKEN…",
	"CONTROL_API_TOKEN_FILE":     "Путь до файла с токеном — «путь каталога/файла» из определения infra, задаётся составом развёртывания (монтированием тома). ⚠ Оговорка, которую стоит записать в реестр честно: комментарий token.go:76-77 прямо допускает человека — «an operator may point CONTROL_API_TOKEN_FILE at their own secret». Класс всё равно infra, а не operator: значение — путь на файловой системе СЕРВЕРА, и место ему в compose/systemd, а не …",
	"CONTROL_API_UI_DIR":         "Путь каталога со страницами — infra по определению («пути … каталогов»), и назначение у него разработческое: docs/DISTRIBUTION.en.md:102 — «`CONTROL_API_UI_DIR=<path>` serves the pages from disk instead (for …)». Побочно ВКЛЮЧАЕТ отдачу UI даже без CONTROL_API_SERVE_UI (ветка стоит первой, ui.go:108) — это стоит сказать в подсказке к CONTROL_API_SERVE_UI, чтобы схема не описывала два независимых переключателя там,…",
	"CONTROL_API_URL":            "Адрес, по которому CLI ищет control-api — проводка развёртывания, зеркало CONTROL_API_ADDR с другой стороны провода. Не поведение прогона.",
	"CONTROL_API_VNC_LIVE":       "Адрес живого эндпойнта vnc-сервиса для пробы готовности — проводка. ⚠ Смежная открытая находка, попадающая ровно сюда, и она уже записана в коде: readyz.go:177-179 [VNC-UP-BUT-UNWIRED] — «no compose file sets CONTROL_API_VNC_LIVE for control-api, so an unset address means either \"not asked for\" or \"asked for and not wired\"». То есть имя существует, ни один compose его не задаёт, и проба честно отказывается различа…",
	"CONTROL_API_VNC_SERVICE":    "Имя compose-сервиса, с которым сверяется хост из PW_CDP_ENDPOINT — соглашение состава развёртывания. Код сам называет силу этого вывода: live_screen.go:179-183 — «This is an inference about a deployment convention, which is exactly why the hub shows it as a warning strip and never as a refusal». Менять здесь имя имеет смысл только тому, кто переименовал сервис в своём compose, то есть развёртыванию, а не человеку …",
	"CONTROL_API_VNC_SOCK":       "Путь unix-сокета RFB — прямо «путь сокета» из определения infra. ⚠ Важная деталь для реестра: у этого пути ПРАВА ДОСТУПА и есть аутентификация — live_screen.go:64-66 «vncSocketMode reports the socket's permission bits, because THEY are the authentication. A socket that has been widened to 0666 authenticates nobody». Это довод НЕ публиковать его настраиваемым: подмена пути тихо меняет модель доступа к экрану.",
	"COVERAGE_TARGET":            "Это переменная СУЩЕСТВУЮЩЕГО поля блока `fields` (`coverage_target`, main.go:377). Нужно не новое поле, а имя переменной у существующего — и вместе с ним честная пометка канала, иначе схема пообещает переменную, которой нельзя воспользоваться.",
	"DEEPSEEK_API_KEY":           "ключ стороннего провайдера, который compose передаёт роутеру моделей: секрет, и притом не наш — описывается, но не публикуется значением (то же правило, что у LLM_API_KEY).",
	"DESCRIBE":                   "Переменная существующего поля `describe` (main.go:375) — нужно имя переменной у существующего поля, а не новый дескриптор.",
	"FORCE_REPLAY":               "Переменная существующего поля `force_replay` (main.go:393) — нужно имя переменной у существующего поля.",
	"GID":                        "пара к UID — та же проводка томов, тот же способ задания.",
	"GOAL":                       "Переменная существующего поля `goal` (main.go:374) — нужно имя переменной у существующего поля.",
	"HEAL_TOKEN_LIMIT":           "Переменная существующего поля `heal_budget` (main.go:381) — нужно имя переменной у существующего поля, с честной пометкой «доезжает RunConfig-ом, не окружением».",
	"IGNORE_ROBOTS":              "Переменная существующего поля `ignore_robots` (main.go:398) — нужно имя переменной у существующего поля.",
	"IMPORT_DIR":                 "Продукт ставит это сам: человек задаёт `--from`/`--from-git`, а переменную безусловно пишет agentctl. Унаследованное значение и так недостижимо — имени нет в аллоулисте (cmd/agentctl/main.go:401-424), а run-var дописан после filteredEnv() (main.go:457-461). Публиковать как настраиваемую значило бы предложить менять путь, который команда вычисляет себе сама.",
	"IMPORT_MAP":                 "Продукт ставит это сам из флага `--map` или из результата `--verify`-обхода. Как и IMPORT_DIR, унаследованное значение не проходит аллоулист и перекрывается run-var'ом. Человеку адресован флаг, а не переменная.",
	"INVOCATION_ID":              "Не наша ручка вовсе: переменную выставляет systemd своим юнитам, продукт её только ЧИТАЕТ, чтобы ответить «кто меня поднял». Комментарий рядом это и заявляет (svclog.go:270-271: «Every signal here is one the supervisor itself sets, so nothing is inferred from our own configuration»). Задать её руками значило бы солгать журналу о способе запуска — публиковать как настраиваемую нельзя.",
	"JOURNAL_STREAM":             "То же, что и INVOCATION_ID: сигнал супервизора, который systemd экспортирует сам. Продукт его не пишет и не может. ⚠ Замерено расхождение: TS-копия правила (pw-executor/src/svcjournal.ts:63) читает ТОЛЬКО `INVOCATION_ID`, а её же комментарий (svcjournal.ts:57-59) утверждает «Kept to the same signals as the Go original, deliberately in the same order, so a reader comparing them can see they agree» — это заявление л…",
	"LITELLM_MASTER_KEY":         "мастер-ключ роутера LiteLLM в профиле литейной: секрет чужого сервиса.",
	"MAX_STEPS":                  "ЭТО ПЕРЕМЕННАЯ СУЩЕСТВУЮЩЕГО ПОЛЯ `fields.max_steps` (cmd/control-api/main.go:379) — нужно не новое поле, а имя переменной у существующего",
	"MCP_TRANSPORT":              "Выбирает ПРОВОД между brain и pw-executor (JSON-RPC против MCP SDK) — внутренняя связь двух наших процессов, а не поведение прогона. Задаётся развёртыванием: Helm подставляет его из values (deploy/sentinel/templates/cronjob.yaml:45-47 `- name: MCP_TRANSPORT / value: {{ .Values.transport | quote }}`), а brain при MCP-пути ЖЁСТКО навязывает его ребёнку (brain/executor.py:120 `env={**os.environ, \"MCP_TRANSPORT\": \"mcp…",
	"MISTRAL_API_KEY":            "ключ стороннего провайдера для роутера моделей — секрет; см. DEEPSEEK_API_KEY.",
	"OLLAMA_IMAGE_TAG":           "тег образа стороннего сервиса в профиле ollama: выбор ВЕРСИИ ЧУЖОГО контейнера, а не поведения нашего продукта.",
	"OPENAI_API_KEY":             "Ключ. Правило уже записано в коде: `api_key` помечен `\"secret\": true` и значение наружу не отдаётся (cmd/control-api/main.go:432 `api_key is flagged secret and NEVER valued here`, main.go:438 `\"note\": \"never returned in this payload; anthropic->ANTHROPIC_API_KEY, openai->OPENAI_API_KEY\"`). Имя УЖЕ названо в note дескриптора `llm.api_key` — собственного дескриптора не заводим, значение не публикуем.",
	"ORCH_ADDR":                  "Адрес gRPC-оркестратора — проводка развёртывания. Задаётся compose (docker-compose.yml:212 `CONTROL_API_ORCH_ADDR: ${CONTROL_API_ORCH_ADDR-unix:/app/state/orch.sock}`) и точечно пропущен аллоулистом agentctl как имя связи (cmd/agentctl/main.go:405 `\"ORCH_ADDR\": true, \"STORE_ADDR\": true, …`). Публиковать его настраиваемым значило бы предложить менять адрес, по которому сервисы говорят между собой.",
	"PLAN_FILE":                  "ЭТО ПЕРЕМЕННАЯ СУЩЕСТВУЮЩЕГО ПОЛЯ `fields.login_plan` (cmd/control-api/main.go:387 + brain/adapters.py:292) — нужно не новое поле, а имя переменной у существующего. ⚠ Но одно имя несёт ДВА разных смысла (план replay из `--plan` и план входа из auth), и при заданном `--plan` RunConfig-овский `login_plan` молча не применится: brain/runconfig.py `_overridable` возвращает False, когда `cur` непустое.",
	"PLAN_TOKEN_LIMIT":           "ЭТО ПЕРЕМЕННАЯ СУЩЕСТВУЮЩЕГО ПОЛЯ `fields.plan_budget` (cmd/control-api/main.go:380) — нужно не новое поле, а имя переменной у существующего, причём с пометкой, что доставка НЕ окружением, а RunConfig-ом",
	"PW_CDP_ENDPOINT":            "Адрес внешнего браузера (CDP), задаётся compose: docker-compose.yml:225 `PW_CDP_ENDPOINT: ${PW_CDP_ENDPOINT-http://browser:9223}` и docker-compose.yml:371 `PW_CDP_ENDPOINT: ${PW_CDP_ENDPOINT:-}`. brain его только ЧИТАЕТ и это оговорено прямо: brain/observe.py:289-291 `\\`PW_CDP_ENDPOINT\\` is the executor's variable and this file must never WRITE it — it is only asked whether the run is about to adopt somebody else'…",
	"PW_EXECUTOR_CMD":            "Командная строка запуска нашего же бинаря pw-executor — «имя бинаря» в чистом виде. Собирается развёртыванием и ПЕРЕЗАПИСЫВАЕТСЯ безусловно: cmd/agentctl/main.go:447 `pwExec := \"node \" + filepath.Join(repo, \"pw-executor\", \"dist\", \"server.js\")` и main.go:457-461 `cmd.Env = append(filteredEnv(), append([]string{ … \"PW_EXECUTOR_CMD=\" + pwExec, …}, extra...)...)` — то есть унаследованное значение мертво (последнее поб…",
	"PW_HEADLESS":                "Второе написание того же решения: и служба браузера (`pw-executor/src/cdp-service.ts:560`), и план запуска прогона (`pw-executor/src/launch.ts:72`) считают браузер видимым при `PW_HEADED=1` ИЛИ `PW_HEADLESS=0`. Опубликовать оба имени значит завести у одного решения двух авторов, а у противоречивой пары (`PW_HEADLESS=1` вместе с `PW_HEADED=1`) молча выигрывает видимый. Публикуется `PW_HEADED` — имя, которым это реш…",
	"PW_LIVE_CLAIM":              "Проводка развёртывания, а не операторская ручка: адрес, по которому прогон заявляет свой экран браузеру-службе, по умолчанию ВЫВОДИТСЯ из уже заданного `PW_CDP_ENDPOINT` (`liveClaimUrl`, pw-executor/src/server.ts:747-761) и объявляется в логе. Второй адрес в развёртывании — второе место, где его можно указать неверно; ровно это вывод и устраняет (измеренный случай записан в докстринге на :740-741: YAML-merge замен…",
	"REPORT_DIR":                 "Путь каталога прогона, из которого собирается отчёт — путь каталога, не ручка поведения. Задаётся вызовом (`agentctl report --run <dir>`), безусловно дописывается run-var-ом (cmd/agentctl/main.go:1026), так что унаследованное значение мертво; умолчание — сам каталог артефактов прогона (brain/__main__.py:1549).",
	"REV_A":                      "Продукт ставит переменную сам из аргумента подкоманды `agentctl revisions --rev`; человек задаёт флаг, а не переменную, и вне `RUN_MODE=revisions` имя не читается вовсе. Унаследованное значение мертво — run-var дописывается безусловно (cmd/agentctl/main.go:831).",
	"REV_B":                      "То же, что REV_A: продукт мостит аргумент подкоманды `revisions` в переменную; человек задаёт флаг. Унаследованное значение перезаписывается безусловно.",
	"REV_OP":                     "Имя операции подкоманды (list|show|diff|rollback), которое продукт переносит из позиционного аргумента в переменную; проверка перечня уже сделана в agentctl, человек переменную не задаёт. Унаследованное значение перезаписывается безусловно.",
	"RUN_CONFIG":                 "Это не ручка, а КАНАЛ доставки других ручек: путь к файлу RunConfig, который control-api создаёт сам внутри каталога артефактов прогона (cmd/control-api/main.go:695-707). Человек задаёт значения ВНУТРИ файла (plan_budget/heal_budget/total_budget, auth.*), а не эту переменную; унаследованное значение мертво — agentctl безусловно дописывает `RUN_CONFIG=` (пустую строку, если флага не было).",
	"RUN_ID":                     "agentctl генерирует идентификатор сам и дописывает его БЕЗУСЛОВНО: cmd/agentctl/main.go:457-458 «cmd.Env = append(filteredEnv(), append([]string{ \"RUN_ID=\" + runID,». runID берётся из newRunID() либо из ОТДЕЛЬНОГО имени SENTINEL_RUN_ID (main.go:688-691) — и причина названа в коде (main.go:686-687): «RUN_ID is a common shell variable, and inheriting it by accident would make two unrelated runs claim one identity». …",
	"RUN_MODE":                   "поверхность ОПУБЛИКОВАНА перечнем `modes` верхнего уровня и полем `fields.mode`; переменная — безусловный run-var: `agentctl` пишет `RUN_MODE=` на КАЖДОМ пути (общий `run` плюс восемь подкоманд), append идёт ПОСЛЕ filteredEnv, и побеждает последнее значение — значит унаследованное до мозга не доезжает никогда. Тот же класс, что MESSAGE, PLANNER и SCENARIO. ⚠ ПРЕЖНЯЯ ПРИЧИНА ЗДЕСЬ ОБРЫВАЛАСЬ МНОГОТОЧИЕМ НА ПОЛУСЛОВЕ и держала девять номеров строк — число в прозе есть обещание, которое никто не перезамеряет; счёт путей проверяется гейтом, а не переписыванием номеров.",
	"SENTINEL_AGENTCTL":          "Имя/путь БИНАРЯ — ровно то, что определение относит к проводке развёртывания. Продукт резолвит его сам двумя запасными способами (PATH, затем bin/agentctl), так что человеку задавать нечего; переменная существует для нестандартной раскладки файлов и для стенда (tests/test_trace_redaction_offline.py:56-59 «Call the real `_redact_trace` with SENTINEL_AGENTCTL pointed wherever the case needs»).",
	"SENTINEL_CONVERSATIONS_DB":  "Путь к файлу SQLite — каталог/файл, то есть проводка развёртывания. Docstring читателя сам называет назначение (__main__.py:646): «SENTINEL_CONVERSATIONS_DB (an override for tests / relocation) or the air-gapped default state/conversations.db». Плюс выбор хранилища перекрывается совсем другим именем: :645 «CHECKPOINT_DSN (Postgres) overrides this in `_checkpointer`» — операторский выбор «где живут треды» делается …",
	"SENTINEL_DECORATE":          "Пер-прогонное расширение режима `observe=human` с ровно одним автором (`brain/observe.py::apply`) и одним читателем (`pw-executor/src/decorate.ts`). Как настройка развёртывания она вредна в обе стороны: сохранённая `0` отняла бы у режима `human` его единственный механизм (значение из окружения перекрывает режим, `apply` пишет через `setdefault`), а сохранённая `1` украсила бы КАЖДЫЙ прогон — а украшенный прогон ме…",
	"SENTINEL_LIVE_FRAMES":       "Половина ОДНОГО решения о съёмке, а не отдельная ручка: переменную пишет только `brain/observe.py::apply()` из режима `observe`, и пишет через `setdefault` (brain/observe.py:266) — значение, пришедшее из окружения, перекрывает выбранный на прогон режим навсегда. А хаб при сохранении настроек кладёт в документ КАЖДУЮ булеву настройку (`cfgSave`, docs/index.html:6496-6503), не только изменённую, — одно нажатие «Сохр…",
	"SENTINEL_LLM_ATTEMPT_LOG":   "Измерительный зонд самого инструмента, а не ручка продукта, и код говорит это прямо: brain/llm.py:484 «MODEL-002 (measurement only, scripts/model_convergence.py)», :490 «(unset by default -> `_record_attempt` is a no-op, zero cost, zero behavior change for every existing caller/test)». Единственные, кто её ЗАДАЁТ, — измерительный стенд scripts/model_convergence.py:301 и его тест tests/test_model_convergence_offlin…",
	"SENTINEL_LLM_BUDGET_FILE":   "Путь к файлу состояния (выученный потолок токенов), умолчание — state/llm-budget.json. Операторская ручка здесь НЕ путь, а флаг, из которого продукт этот путь ставит сам: cmd/agentctl/main.go:721 «extra = appendBudgetIsolation(extra, dir, *rf.isolateBudget)» → appendBudgetIsolation «return append(extra, \"SENTINEL_LLM_BUDGET_FILE=\"+filepath.Join(dir, \"llm-budget.json\"))», флаг объявлен в main.go:606 «fs.Bool(\"isola…",
	"SENTINEL_LOG_LEVEL":         "уже настраивается разделом `logging` конфигурационного документа (persistedLoggingEnv, cmd/control-api/logenv.go:53) — второй дескриптор писал бы ту же переменную из второго места, а в mergedPersistedEnv (logenv.go:236-243) слой `settings` накладывается ПОСЛЕ `logging` и молча победил бы его",
	"SENTINEL_LOG_LEVELS":        "уже настраивается разделом `logging` конфигурационного документа, и только он умеет выразить поКАТЕГОРИЙНЫЕ уровни (`heal=info,llm=debug`), которых плоский дескриптор не описывает; дескриптор в `settings` завёл бы второго автора той же переменной, побеждающего первого в mergedPersistedEnv",
	"SENTINEL_OBSERVE":           "поверхность ОПУБЛИКОВАНА полем `fields.observe`; переменная — безусловный run-var из флага `--observe`, поэтому унаследованное значение до мозга НЕ ДОЕЗЖАЕТ. Тот же класс, что MESSAGE, PLANNER и SCENARIO. ⚠ ПРЕЖНЯЯ ПРИЧИНА ЗДЕСЬ БЫЛА ЛОЖНА И ПРЕДПИСЫВАЛА ДЕФЕКТ: она гласила «ей нужен ключ `env` у имеющегося поля», то есть предлагала опубликовать доставку, которой нет по контракту — схема пообещала бы слой, мёртвый по построению, и гейт каталога был бы зелен над ним. Слой у наблюдения теперь ЕСТЬ и он другой: сохранённые умолчания прогона (`personalRunDefaults`, ключ `run.observe`), откуда значение уезжает ФЛАГОМ и называется в `inherited_defaults` (решение Alex, W17). Отдельно: brain/runconfig.py:45 «\"observe\": \"SENTINEL_OBSERVE\"» — имя УЖЕ объявлено в таблице RunConfig→env, так что источник для ключа брать неоткуда, кроме этой строки; а runconfig.py:61-65 фиксирует, что в _AGENTCTL_DEFAULTS его намеренно нет («мутация её выживания ничего не покрасила»).",
	"SENTINEL_OWNER":             "Продукт ставит это сам из состояния запроса, а не человек: control-api резолвит владельца из предъявленного удостоверения и передаёт флагом — cmd/control-api/main.go:658-659 «if req.owner != \"\" { args = append(args, \"--owner\", req.owner) }», причём поле НЕэкспортируемое именно чтобы клиент не мог назвать себя чужим именем (main.go:634-637 «a client that could name its own owner could write into somebody else's set…",
	"SENTINEL_RECORD":            "Пер-прогонное расширение режима `observe=record` с одним автором (`brain/observe.py::apply`) и одним читателем (`pw-executor/src/record.ts`). Сохранённая в развёртывании, она обходит отказ, который `brain/observe.py` выносит У ДВЕРИ для пары «запись + подключение по CDP»: в развёртывании по умолчанию прогон подключается к браузеру-службе (docker-compose.yml:225 `PW_CDP_ENDPOINT: ${PW_CDP_ENDPOINT-http://browser:92…",
	"SENTINEL_REVISIONS_DIR":     "Путь КАТАЛОГА хранилища ревизий — проводка развёртывания (умолчание state/revisions, файловый стор без сетевого сервиса, brain/__main__.py:219 «file-based under state/revisions»). В дереве его задают только тесты (tests/test_revisions_offline.py:122, tests/test_revisions_surface_offline.py:39); в compose/entrypoint не встречается. ⚠ один и тот же литерал умолчания продублирован в двух читателях.",
	"SENTINEL_RUN_ID":            "Продукт ставит это сам: control-api именует прогон ДО того, как появляется agentctl (runs/control-<id>), и передаёт тот же id вниз, чтобы живой вид и хаб спрашивали про одно имя (комментарий cmd/agentctl/main.go:685-688 и cmd/control-api/main.go:907-910). Человек это не задаёт; предлагать менять id прогона значит предлагать развести хаб и прогон.",
	"SENTINEL_STATE_DIR":         "Путь КАТАЛОГА состояния внутри контейнера, привязанный к точке монтирования compose. В дереве задаётся только тестами (tests/test_browser_journal_offline.py:116,201,241; tests/test_own_tab_offline.py:76). Публиковать настраиваемым — предлагать разойтись с томом.",
	"SENTINEL_SVC_NAME":          "ИМЯ службы в журнале — проводка развёртывания: одинаковый образ поднимается дважды (browser и browser-vnc), и имя различает записи. Значение выставляет compose литералом, без ${...:-} — то есть не рассчитано на подмену человеком.",
	"SENTINEL_TEST_ID":           "Идентичность НАЗВАННОГО теста, а не операторская ручка: значение ставит только `agentctl revisions --test` (cmd/agentctl/main.go:834 флаг, :852 `\"SENTINEL_TEST_ID=\" + *testID`), у `agentctl run` такого флага нет, а в `runRequest` нет соответствующего поля — публикация в `fields` покраснила бы TestRunRequestCoversEverySchemaField (cmd/control-api/config_projection_test.go:66), а публикация в `settings` превратила б…",
	"SENTINEL_TRACE_SCREENSHOTS": "Вторая половина той же пары, что и `SENTINEL_LIVE_FRAMES`: её пишет только `brain/observe.py::apply()` из режима `observe` и через `setdefault`, поэтому сохранённое значение перекрывало бы режим на каждом прогоне, а хаб кладёт в документ каждую булеву настройку при первом же сохранении. Конфиденциальность пикселей, ради которой рычаг существует (ADR-098: снимки в трейсе нередактируемы), закрывается уже опубликован…",
	"SENTINEL_VERSION":           "Клеймо версии/тег образа — проводка развёртывания: значение ОПИСЫВАЕТ сборку, а не меняет поведение прогона (единственный читатель в коде подставляет его в строку журнала). Публиковать настраиваемым — предлагать соврать о собственной версии.",
	"SPEC_OUT":                   "Продукт ставит это сам из флага `--out` подкоманды `agentctl spec export`. Вручную заданное значение до brain не доедет ВООБЩЕ: имя без префикса LLM_/OTEL_/PW_/PLAYWRIGHT_/SENTINEL_ и не в точном перечне filteredEnv (cmd/agentctl/main.go:400-424), а agentctl дописывает своё значение ПОСЛЕ filteredEnv (spawnBrain: `cmd.Env = append(filteredEnv(), append([]string{...}, extra...)...)`). Кроме того, режим export-spec …",
	"STORE_ADDR":                 "АДРЕС (юникс-сокет state/sentinel-store-<runID>.sock либо адрес шлюза развёртывания) — ровно тот случай, о котором сказано в постановке: предлагать менять адрес, по которому сервис сам с собой разговаривает. Его вычисляет agentctl (startGateway) или передаёт control-api; пустое значение означает документированный LocalStore.",
	"STORE_DSN":                  "Строка подключения к БД — проводка развёртывания, и вдобавок РУЧКА, КОТОРАЯ УМЕЕТ ТОЛЬКО ОТКАЗАТЬ: любое непустое значение возвращает ошибку, бэкенда Postgres не существует (M13-service отложен). Опубликовать её настраиваемой значило бы рекламировать несуществующую возможность. Значение наружу не печатается — в тексте ошибки только путь SQLite, что верно.",
	"STORE_TOKEN":                "Токен аутентификации к store-gateway. Действует уже записанное в коде правило: описывать, но значение не отдавать (cmd/control-api/main.go:349-350 «Secrets are DESCRIBED (api_key.secret) but never VALUED»). ⚠ Он умышленно НЕ в allowlist filteredEnv — cmd/agentctl/main.go:527-530 говорит это прямо, — и пересылается вниз только вручную рядом с адресом; поэтому запись в реестре обязана быть помечена secret:true и БЕЗ…",
	"TARGET_URL":                 "⚠ ЭТО ПЕРЕМЕННАЯ УЖЕ СУЩЕСТВУЮЩЕГО ПОЛЯ `target` (cmd/control-api/main.go:378, единственное required в блоке fields) — ей нужно не новое поле, а решение про ключ `env` у существующего. Но ставить туда голое `TARGET_URL` НЕЛЬЗЯ без оговорки: agentctl дописывает её БЕЗУСЛОВНО и последним, поэтому заданное человеком значение всегда проигрывает — та же смерть, что у HEAL_LLM, который в схеме есть и до прогона не доезж…",
	"TOTAL_TOKEN_LIMIT":          "⚠ ЭТО ПЕРЕМЕННАЯ УЖЕ СУЩЕСТВУЮЩЕГО ПОЛЯ `total_budget` (cmd/control-api/main.go:382, type int, default 0) — ей нужно не новое поле, а решение про `env` у существующего. Из окружения она не доезжает ВООБЩЕ: имени нет ни в точном перечне, ни среди префиксов filteredEnv (cmd/agentctl/main.go:400-424), и это уже записано в коде — cmd/control-api/main.go:702-705 «The brain reads budgets as PLAN_TOKEN_LIMIT / HEAL_TOKEN…",
	"UID":                        "проводка развёртывания: compose подставляет её в `user:` каждого сервиса, чтобы файлы в томах принадлежали хозяину каталога, а не root. Задаётся окружением докера, продуктом не читается.",
}
