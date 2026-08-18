package main

// ADR-109, second half: ownership that reaches the READ paths.
//
// The first half (local accounts, sessions, owner columns) scoped the LIST endpoints and stopped
// there. Measuring the route table afterwards showed what that left: thirteen routes read or mutated
// rows that carry an owner without ever asking whose they were, so a library listing hid another
// account's scenario while `GET /v1/scenarios/{id}` handed it over to anyone who knew the id — and
// `DELETE` removed it. Three more routes (`GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/stream`)
// required no credential at all, a pre-identity decision ("a bare status poll is open") that turned
// into "an anonymous caller is unscoped, and therefore sees everybody's runs" the moment accounts
// existed.
//
// The fix is not thirteen edits. Thirteen edits is how the first half produced this defect: the same
// three lines copied into each handler, and the ones that were forgotten looked exactly like the ones
// that were not. So access becomes a DECLARATION beside the route, and the mux is built from those
// declarations — a route cannot be served without stating what it requires, because there is nowhere
// else to register it.
//
// A route declares two independent things, because they answer different questions:
//
//	access — WHO may call it at all: accessOpen (nobody needs a credential), accessAuthed (any
//	         accepted credential), accessAdmin (the machine token or an admin account).
//	domain — WHOSE rows it touches. Non-empty means the route names one row by {id}, and that row
//	         must belong to the caller. Enforced here, once, for every such route.
//
// plus one transitional flag, legacyOpen, described at its declaration below.
//
// Two rules the scoping obeys, both inherited from the first half rather than invented here:
//
//   - A machine caller is unscoped. CI, agentctl and the air-gapped bundle authenticate that way and
//     none of them is a person with rows of their own.
//   - A row with an EMPTY owner belongs to nobody and is visible to everybody. That is what keeps
//     identity opt-in: a deployment that never created an account has only unowned rows, so nothing
//     is scoped and nothing changes.
//
// And one rule chosen here: a foreign row answers exactly as a MISSING row does — 404 for a read,
// and the idempotent 200 for a delete. Answering 403 would confirm that the id exists, which is the
// one fact scoping is meant to withhold; answering 404 to a delete that would have succeeded for its
// owner would make "does this id exist?" answerable by watching the status code change.

import (
	"net/http"
	"strings"
	"sync"
	"time"
)

type accessMode string

const (
	accessOpen   accessMode = "open"
	accessAuthed accessMode = "authed"
	accessAdmin  accessMode = "admin"
)

// ownedDomain names a table whose rows carry an `owner` column, so the guard knows where to look up
// the row {id} names. A domain is added here only when its rows are owned — the guard's whole job is
// to compare owners, and a domain with no owner column would silently pass everything.
type ownedDomain string

const (
	domainRun      ownedDomain = "run"
	domainScenario ownedDomain = "scenario"
	domainTest     ownedDomain = "test"
	domainChat     ownedDomain = "chat"
	domainResult   ownedDomain = "result"
)

type routeSpec struct {
	pattern string
	access  accessMode
	// domain is set when the route names ONE row by {id}; the guard then requires that row to be the
	// caller's. Empty for lists (which filter in the handler, having no single id to resolve) and for
	// routes that touch no owned row at all.
	domain ownedDomain
	// legacyOpen marks a route that required no credential before accounts existed and must keep
	// behaving that way until one does. Without it, adding identity to the product would break every
	// anonymous status poll (scripts, dashboards, a bare curl) at upgrade time, for deployments that
	// never asked for accounts. With it, the tightening happens exactly when it starts to mean
	// something: the first account turns "unscoped" from "single team" into "everybody's rows".
	legacyOpen bool
	// probe marks a route whose NON-2XX answer is its contract rather than a failure. /readyz answers
	// 503 until every configured dependency responds, and a standalone deployment with no LLM
	// configured answers 503 for as long as it runs — correctly. Without this the journal filed every
	// one of those as `service.api_refused` at `error`, so an orchestrator polling readiness painted a
	// healthy deployment red, several times a minute, and buried the one line somebody was looking for.
	// (Found by looking at a screenshot of the journal view, not by any gate.)
	//
	// Declared beside the route rather than matched by path in the journal hook, for the same reason
	// `access` and `domain` are: a rule about a route belongs to the route, and a hook that knows path
	// names is a second place that has to be edited when one is added.
	probe bool
	// why is mandatory for accessOpen: an open route in a product that has accounts is a decision, and
	// a decision with no stated reason is an oversight waiting to be copied.
	why string
	h   http.HandlerFunc
}

// routes is the whole HTTP surface. The mux is built from it and from nothing else, which is what
// makes the gate below exhaustive by construction rather than by review.
func (s *server) routes() []routeSpec {
	return []routeSpec{
		{pattern: "GET /healthz", access: accessOpen, probe: true, h: s.handleHealthz,
			why: "liveness probe: a process-level fact (version, live-run count) that names no row, read by container orchestration before any credential exists. It must not start failing because someone created an account"},
		{pattern: "GET /readyz", access: accessOpen, probe: true, h: s.handleReadyz,
			why: "readiness probe, same contract as /healthz. It already withholds the topology from an anonymous caller itself (readyz.go: Detail is blanked unless authed) — the verdict is all it publishes"},
		// QA-REPORT-SERVICE (ADR-119). Root-level and unversioned like the two probes above, because
		// `/metrics` is what a scraper is configured to expect and what docs/OBSERVABILITY has promised
		// since M4 — the /v1 prefix belongs to the product's own surface, not to a convention older than
		// this repository.
		//
		// `authed`, NOT open, and that is the difference between this and the report-service handler it
		// replaces. That one answered anybody who could reach the port. In Mode 3 the port it would sit
		// on is the one serving the browser UI, so `open` here would publish every run's numbers to
		// everyone who can load a page. No `domain` either: an aggregate names no single row, so there is
		// no {id} for the guard to resolve and the scoping lives in the handler — the same shape, and the
		// same reason, as handleListRuns and GET /v1/service-log.
		{pattern: "GET /metrics", access: accessAuthed, h: s.handleMetricsAgg},
		{pattern: "GET /v1/config-schema", access: accessOpen, h: s.handleConfigSchema,
			why: "the SHAPE of configuration, not its values: field names, types, defaults. The setup wizard renders the form from it before a token has been entered, so requiring one would make first-run configuration impossible"},
		{pattern: "GET /v1/events-catalog", access: accessOpen, h: s.handleEventsCatalog,
			why: "the bilingual message catalogue — static product text, identical for every deployment and every caller"},
		{pattern: "POST /v1/login", access: accessOpen, h: s.handleLogin,
			why: "where a credential is first presented; it cannot require one. It answers identically for a wrong name and a wrong password, so it cannot enumerate accounts either"},
		{pattern: "POST /v1/logout", access: accessOpen, h: s.handleLogout,
			why: "ends whatever token was sent. Requiring a live one would make logging out fail exactly when it matters — on an expired or already-dropped session"},
		{pattern: "GET /v1/me", access: accessAuthed, h: s.handleMe},
		// ADR-111: the live video mode, proxied from the browser service. `authed`, not `open`: the
		// browser service's live port has no credential of its own (same as its CDP port — internal
		// network is the whole control), so this route IS the credential. A screencast shows whatever
		// the browser has open, including a logged-in application under test.
		//
		// No `domain`: these name no row. The picture is of the browser SERVICE, which is shared by
		// construction — one browser, whoever is driving it — and a per-run scoping here would be a
		// promise the topology cannot keep. That is stated in the UI rather than implied.
		{pattern: "GET /v1/live/status", access: accessAuthed, h: s.handleLiveStatus},
		{pattern: "GET /v1/live/frame.jpg", access: accessAuthed, h: s.handleLiveFrame},
		{pattern: "GET /v1/live/mjpeg", access: accessAuthed, h: s.handleLiveStream},
		// LIVE-VNC (ADR-127). `open` at the guard and authenticated INSIDE the handler, deliberately: a
		// browser WebSocket cannot send an Authorization header, and the guard's check reads only that
		// header (session.go::bearerOf). The credential rides in Sec-WebSocket-Protocol as
		// `bearer.<token>` and is compared constant-time by wsAuthed — the same arrangement /v1/stream
		// has, stated here rather than inherited from `legacyOpen`, whose real meaning is "no accounts
		// exist yet" and which therefore stops relaxing the moment identity is switched on.
		{pattern: "GET /v1/live/screen", access: accessOpen, h: s.handleLiveScreen,
			why: "the bearer travels in the WebSocket subprotocol, not in a header the guard can read; " +
				"the handler refuses with 403 before hijacking, and the VNC port it reaches is never " +
				"published to a host"},
		{pattern: "POST /v1/users", access: accessAdmin, h: s.handleCreateUser},
		{pattern: "GET /v1/users", access: accessAdmin, h: s.handleListUsers},
		{pattern: "DELETE /v1/users/{id}", access: accessAdmin, h: s.handleDeleteUser},
		{pattern: "GET /v1/config", access: accessAuthed, h: s.handleGetConfig},
		{pattern: "PUT /v1/config", access: accessAuthed, h: s.handlePutConfig},
		{pattern: "POST /v1/runs", access: accessAuthed, h: s.handleCreateRun},
		{pattern: "POST /v1/chat/completions", access: accessAuthed, h: s.handleChatCompletions},
		{pattern: "POST /v1/import", access: accessAuthed, h: s.handleImport},
		// The revision store hangs off a TEST, so a revision is scoped by the test that owns it —
		// including rollback, which mutates.
		{pattern: "GET /v1/tests/{id}/revisions", access: accessAuthed, domain: domainTest, h: s.handleRevisionsList},
		{pattern: "GET /v1/tests/{id}/revisions/diff", access: accessAuthed, domain: domainTest, h: s.handleRevisionsDiff},
		{pattern: "GET /v1/tests/{id}/revisions/show", access: accessAuthed, domain: domainTest, h: s.handleRevisionsShow},
		{pattern: "POST /v1/tests/{id}/revisions/rollback", access: accessAuthed, domain: domainTest, h: s.handleRevisionsRollback},
		// The three pre-identity open reads. handleListRuns filters by owner itself (a list has no {id}
		// for the guard to resolve); the guard supplies the credential requirement it never had.
		{pattern: "GET /v1/runs", access: accessAuthed, legacyOpen: true, h: s.handleListRuns},
		{pattern: "GET /v1/runs/{id}", access: accessAuthed, legacyOpen: true, domain: domainRun, h: s.handleGetRun},
		{pattern: "GET /v1/stream", access: accessAuthed, legacyOpen: true, h: s.handleStream},
		{pattern: "GET /v1/runs/{id}/events", access: accessAuthed, domain: domainRun, h: s.handleRunEvents},
		{pattern: "GET /v1/runs/{id}/logs", access: accessAuthed, domain: domainRun, h: s.handleRunLogs},
		// HEALTH-005 PR-B. `authed` and NOT `admin`: an account must be able to read the record of its
		// OWN sign-ins and deletions, which is half of what an audit journal is for. No `domain` either —
		// it names no single row, so there is no owner for the guard to resolve; the scoping is per
		// RECORD and lives in the handler, the same shape as handleListRuns.
		{pattern: "GET /v1/service-log", access: accessAuthed, h: s.handleServiceLog},
		{pattern: "POST /v1/runs/{id}/cancel", access: accessAuthed, domain: domainRun, h: s.handleCancelRun},
		{pattern: "GET /v1/runs/{id}/artifact", access: accessAuthed, domain: domainRun, h: s.handleRunArtifact},
		{pattern: "GET /v1/scenarios", access: accessAuthed, h: s.handleListScenarios},
		{pattern: "GET /v1/scenarios/{id}", access: accessAuthed, domain: domainScenario, h: s.handleGetScenario},
		{pattern: "DELETE /v1/scenarios/{id}", access: accessAuthed, domain: domainScenario, h: s.handleDeleteScenario},
		{pattern: "GET /v1/tests", access: accessAuthed, h: s.handleListTests},
		{pattern: "GET /v1/tests/{id}", access: accessAuthed, domain: domainTest, h: s.handleGetTest},
		{pattern: "POST /v1/tests/promote", access: accessAuthed, h: s.handlePromoteTest},
		{pattern: "DELETE /v1/tests/{id}", access: accessAuthed, domain: domainTest, h: s.handleDeleteTest},
		{pattern: "GET /v1/chats", access: accessAuthed, h: s.handleListChats},
		{pattern: "GET /v1/chats/{id}", access: accessAuthed, domain: domainChat, h: s.handleGetChat},
		{pattern: "DELETE /v1/chats/{id}", access: accessAuthed, domain: domainChat, h: s.handleDeleteChat},
		{pattern: "GET /v1/results", access: accessAuthed, h: s.handleListResults},
		{pattern: "GET /v1/results/{id}", access: accessAuthed, domain: domainResult, h: s.handleGetResult},
		{pattern: "GET /v1/trends", access: accessAuthed, h: s.handleTrends},
	}
}

// guard applies a route's declaration before its handler runs.
//
// Credential checks live here INSTEAD of in the handlers, not in addition to them: two places that
// decide the same thing drift, and the drift is invisible — a route that quietly stopped requiring a
// token looks exactly like one that never did. (A duplicated check would also make the mutation test
// for this file meaningless: removing the guard would change nothing observable.) The handlers keep
// the decisions only they can make — what to read, what to write, what the answer looks like.
func (s *server) guard(sp routeSpec) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// HEALTH-005: every call is journalled from HERE, the one place all 42 routes pass through.
		// Forty-two hand-placed log lines is how ADR-109's defect was produced, and the ones that were
		// forgotten looked exactly like the ones that were not.
		started := time.Now()
		rec := &statusRecorder{ResponseWriter: w}
		w = rec
		defer func() { s.journalCall(sp, rec, r, started) }()

		needsCredential := sp.access == accessAuthed || sp.access == accessAdmin
		if sp.legacyOpen && !s.accountsExist() {
			needsCredential = false
		}
		if needsCredential && !s.authed(r) {
			s.denyCredential(w)
			return
		}
		if sp.access == accessAdmin {
			c, _ := s.callerOf(r)
			if !c.machine && !c.admin {
				writeJSON(w, http.StatusForbidden, map[string]string{
					"error": "this route needs the machine token or an admin account"})
				return
			}
		}
		if sp.domain != "" && !s.mayTouch(w, r, sp.domain) {
			return
		}
		sp.h(w, r)
	}
}

func (s *server) denyCredential(w http.ResponseWriter) {
	writeJSON(w, http.StatusForbidden, map[string]string{
		"error": "missing/invalid credential: send the machine token (CONTROL_API_TOKEN) or a session from POST /v1/login"})
}

// mayTouch answers "is the row {id} names the caller's to see?" and writes the refusal itself when it
// is not. It reports true — proceed — in every case where scoping does not apply: a machine caller, a
// deployment with no accounts, a row with no owner, and a row that does not exist (which the handler
// must answer for itself, since only it knows what "missing" looks like for its domain).
func (s *server) mayTouch(w http.ResponseWriter, r *http.Request, domain ownedDomain) bool {
	c, _ := s.callerOf(r)
	if c.machine || c.owner() == "" {
		return true
	}
	id := r.PathValue("id")
	if id == "" {
		return true
	}
	owner, found := s.ownerOfRow(domain, id)
	if !found || owner == "" || owner == c.owner() {
		return true
	}
	// Foreign. Answer as though it were missing — see the note at the top of this file.
	// HEALTH-005: mark it for the journal HERE. Inferring "foreign" from the 404 downstream would file
	// every genuinely missing row as somebody reaching for another account's data.
	if sr, ok := w.(*statusRecorder); ok {
		sr.foreign = true
	}
	if r.Method == http.MethodDelete {
		writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
		return false
	}
	writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such " + string(domain)})
	return false
}

// ownerOfRow reads the owner of one row. found=false means "no such row" — NOT "unowned"; the two are
// different answers, and collapsing them would make a missing row indistinguishable from one that
// belongs to nobody, which is precisely the distinction the opt-in rule rests on.
func (s *server) ownerOfRow(domain ownedDomain, id string) (string, bool) {
	if domain == domainRun {
		// A live run is in memory; one from a previous process is in the store. Both are the same run to
		// the person looking at it, so both are consulted.
		s.mu.RLock()
		rec, live := s.runs[id]
		var owner string
		if live {
			owner = rec.Owner
		}
		s.mu.RUnlock()
		if live {
			return owner, true
		}
	}
	if s.store == nil {
		return "", false
	}
	switch domain {
	case domainRun:
		if rec, ok := s.store.getRun(id); ok {
			return rec.Owner, true
		}
	case domainScenario:
		if sc, ok := s.store.getScenario(id); ok {
			return sc.Owner, true
		}
	case domainTest:
		if t, ok := s.store.getTest(id); ok {
			return t.Owner, true
		}
	case domainChat:
		if ch, ok := s.store.getChat(id); ok {
			return ch.Owner, true
		}
	case domainResult:
		if rr, ok := s.store.getResult(id); ok {
			return rr.Owner, true
		}
	}
	return "", false
}

// --- "does this deployment have accounts?" -------------------------------------------------------
//
// The question legacyOpen turns on, asked of the store and then remembered briefly. It is asked on
// routes that are meant to be cheap (a status poll, an SSE connect), so it must not become a gRPC
// round trip per request; and it changes rarely — only when an account is created or removed, both of
// which invalidate the memo directly, so the TTL is a backstop for a second process editing the same
// store, not the primary mechanism.

type accountsMemo struct {
	mu     sync.Mutex
	known  bool
	exists bool
	at     time.Time
}

const accountsMemoTTL = 5 * time.Second

func (s *server) accountsExist() bool {
	if s.store == nil {
		return false // an account needs somewhere to live; with no gateway none can exist
	}
	s.accounts.mu.Lock()
	if s.accounts.known && time.Since(s.accounts.at) < accountsMemoTTL {
		defer s.accounts.mu.Unlock()
		return s.accounts.exists
	}
	s.accounts.mu.Unlock()

	list, ok := s.store.listUsers()
	// A gateway that did not answer must not be read as "no accounts": that would re-open every
	// legacyOpen route for as long as the store is unreachable, i.e. turn a transport failure into an
	// access-control decision. Keep the last known answer; assume accounts exist if there is none.
	if !ok {
		s.accounts.mu.Lock()
		defer s.accounts.mu.Unlock()
		if s.accounts.known {
			return s.accounts.exists
		}
		return true
	}
	s.accounts.mu.Lock()
	defer s.accounts.mu.Unlock()
	s.accounts.known, s.accounts.exists, s.accounts.at = true, len(list.Users) > 0, time.Now()
	return s.accounts.exists
}

// forgetAccounts drops the memo, so a just-created (or just-removed) account takes effect on the very
// next request instead of up to accountsMemoTTL later.
func (s *server) forgetAccounts() {
	s.accounts.mu.Lock()
	s.accounts.known = false
	s.accounts.mu.Unlock()
}

// routePath is the path half of a "METHOD /path" pattern, for tests and diagnostics that need to
// build a request from a declaration.
func routePath(pattern string) string {
	if i := strings.IndexByte(pattern, ' '); i >= 0 {
		return pattern[i+1:]
	}
	return pattern
}

// routeMethod is the method half of a "METHOD /path" pattern.
func routeMethod(pattern string) string {
	if i := strings.IndexByte(pattern, ' '); i >= 0 {
		return pattern[:i]
	}
	return http.MethodGet
}
