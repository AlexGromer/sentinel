package main

// HEALTH-005 — the service journal, written from ONE place.
//
// Every API call passes through `guard` (access.go), which already knows the route's declaration, the
// caller, and whether the row being touched is theirs. So the journal hangs there rather than in
// forty-two handlers: a route added tomorrow is recorded because it is in the routes table, not
// because somebody remembered. That is the same reasoning ADR-109 used for the credential checks, and
// for the same reason — thirteen hand-placed copies are how the defect it fixed was produced.
//
// LEVELS DO THE WORK THAT SELECTION WOULD OTHERWISE DO (Alex's directive: record everything, and have
// levels). Reads are `debug`; the hub polls `/v1/runs/{id}` every 2s, so a ten-minute run is ~300
// records that nobody will ever read and that would bury the one line saying an account was deleted.
// Mutations are `info`. A refusal, a foreign-row reach and a failed sign-in are `warn` — those are the
// records somebody comes looking for. A 5xx is `error`.

import (
	"bufio"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/AlexGromer/sentinel/internal/eventlog"
	"github.com/AlexGromer/sentinel/internal/svclog"
)

// statusRecorder captures the status code the handler wrote. net/http gives no way to read it back,
// and a journal that records every call as 200 would be worse than none — it would look complete.
type statusRecorder struct {
	http.ResponseWriter
	status int
	// foreign is set by mayTouch when it answers for a row belonging to somebody else. It rides here
	// because the refusal is written by mayTouch, several frames below the journal, and the alternative
	// — inferring "foreign" from a 404 — would file every genuinely missing row as an access attempt.
	foreign bool
}

func (w *statusRecorder) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusRecorder) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK // an unstamped write is a 200, exactly as net/http decides it
	}
	return w.ResponseWriter.Write(b)
}

// Flush and Hijack forward the two OPTIONAL interfaces net/http handlers reach for by type assertion.
// A wrapper that implements only ResponseWriter silently removes them, and both failures are ugly:
//
//	Flusher  — /v1/stream and /v1/live/mjpeg would arrive all at once at the end instead of streaming,
//	           a break no status code reveals.
//	Hijacker — /v1/stream's WebSocket upgrade (ws.go writes the 101 on a hijacked conn) answers 500.
//
// Measured, not anticipated: the first version forwarded Flush alone and TestStreamHandshakeAndIngest
// went red on the handshake. The gate below now asserts the SET of interfaces rather than one of them,
// because the next wrapper will forget a different one.
func (w *statusRecorder) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (w *statusRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, http.ErrNotSupported
	}
	// A hijacked connection leaves net/http's status tracking behind entirely: the handler writes the
	// 101 itself. Recording it here keeps the journal honest about a call it can no longer observe.
	if w.status == 0 {
		w.status = http.StatusSwitchingProtocols
	}
	return h.Hijack()
}

// actorOf names WHO made a call, as a person reads it. Deliberately not the user id: an id answers
// "which row" and this field answers "who", and the two are different questions in a journal meant to
// be read rather than joined.
func (s *server) actorOf(r *http.Request) (actor, owner string) {
	c, ok := s.callerOf(r)
	switch {
	case !ok:
		return "anonymous", ""
	case c.machine:
		return "machine", ""
	}
	name := c.name
	if name == "" {
		name = c.owner()
	}
	if c.admin {
		return name + " (admin)", c.owner()
	}
	return name, c.owner()
}

// journalCall records one API call. Called from guard for EVERY route, after the handler ran.
func (s *server) journalCall(sp routeSpec, rec *statusRecorder, r *http.Request, started time.Time) {
	if s.journal == nil {
		return
	}
	actor, owner := s.actorOf(r)
	status := rec.status
	if status == 0 {
		status = http.StatusOK
	}
	dur := time.Since(started).Milliseconds()
	// The route PATTERN, never the concrete path: `GET /v1/runs/{id}` groups, and a journal of a
	// thousand distinct paths cannot be read by eye or counted by code. The id is not lost — it is in
	// the message, where it belongs to the sentence rather than to the index.
	route := sp.pattern
	if i := strings.IndexByte(route, ' '); i >= 0 {
		route = route[i+1:]
	}

	// A foreign-row touch is its own event, at its own level, because it is the one read anybody ever
	// comes looking for. It is NOT inferred from the 404 — mayTouch says so, so a genuinely missing
	// row is not filed as an access attempt.
	if rec.foreign {
		s.journal.Log(svclog.Record{
			Lvl: "warn", Cat: "service", Code: "service.foreign_row",
			Msg: s.renderMsg("service.foreign_row", map[string]string{
				"actor": actor, "method": r.Method, "route": route,
			}),
			Actor: actor, Owner: owner, Method: r.Method, Route: route,
			Status: status, DurMs: dur, Foreign: true,
		})
		return
	}

	code, lvl := "service.api_call", "debug"
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		lvl = "info" // a mutation is what a journal exists for
	}
	// A PROBE's non-2xx is its answer, not a fault (routeSpec.probe). /readyz replies 503 for as long
	// as a dependency is unconfigured, which for a standalone deployment is forever and is correct; an
	// orchestrator polling it would otherwise write an `error` record every few seconds and bury the
	// journal under the health of a healthy service.
	if !sp.probe {
		switch {
		case status >= 500:
			code, lvl = "service.api_refused", "error"
		case status == http.StatusForbidden || status == http.StatusUnauthorized:
			code, lvl = "service.api_refused", "warn"
		}
	}
	// The word REFUSED is in the sentence, not only in the code, and it no longer has to be spliced in
	// here: the two codes carry two templates, and the sentence comes from whichever one was chosen.
	s.journal.Log(svclog.Record{
		Lvl: lvl, Cat: "service", Code: code,
		Msg: s.renderMsg(code, map[string]string{
			"method": r.Method, "route": route,
			"status": strconv.Itoa(status), "dur_ms": strconv.FormatInt(dur, 10), "actor": actor,
		}),
		Actor: actor, Owner: owner, Method: r.Method, Route: route, Status: status, DurMs: dur,
	})
}

// renderMsg is the ONE place a service-journal sentence is produced (ADR-117). It renders the
// catalogue's template for `code`; a code the catalogue does not know gets the honest
// `eventlog.uncatalogued` line instead of an invented sentence, exactly as brain/eventlog.py does —
// because a code with no entry is a code the reader's browser cannot render either.
//
// ⚠ The code LITERALS stay at the call sites, here in cmd/control-api. tests/test_event_catalog_offline.py
// maps emitters to paths and finds codes by regexing them inside those paths; a literal that moved
// into the shared renderer would become a phantom the catalogue gate can no longer see.
func (s *server) renderMsg(code string, fields map[string]string) string {
	msg, ok := eventlog.Render(code, fields)
	if !ok {
		// Not silent, and not a guess: the same signal the Python side raises, so an emitter that
		// invents a code is visible in the very journal it was trying to write to.
		return "eventlog.uncatalogued: " + code + " is not in the catalogue"
	}
	return msg
}

// --- the messages emitted from main() ------------------------------------------------------------
//
// Functions rather than expressions assembled at the call site, and not as a style preference. The
// wording gate (svcjournal_wording_test.go) compares every emitted message against the catalogue's
// template for its code, because the catalogue's template is what the UI uses to render the sentence
// in the reader's language: when the two disagree, the extraction fails and a Russian reader silently
// gets English. A message built inline in main() cannot be reached from a test — which is how four of
// these came to disagree with the catalogue with every gate green, and stayed that way until somebody
// looked at a screenshot.

// ⚠ These four no longer ASSEMBLE anything — they name their fields and hand them to the catalogue's
// template. The functions remain (rather than the call sites calling Render directly) because the
// tests reach them, and because a named function is where the field NAMES for a code are decided
// once instead of at each caller.
func renderOrUncatalogued(code string, fields map[string]string) string {
	msg, ok := eventlog.Render(code, fields)
	if !ok {
		return "eventlog.uncatalogued: " + code + " is not in the catalogue"
	}
	return msg
}

func startedMsg(version, supervisor string, pid int, detail string) string {
	return renderOrUncatalogued("service.started", map[string]string{
		"svc": "control-api", "version": version, "supervisor": supervisor,
		"pid": strconv.Itoa(pid), "detail": detail,
	})
}

func stoppedMsg(reason string) string {
	return renderOrUncatalogued("service.stopped", map[string]string{"svc": "control-api", "reason": reason})
}

func tokenSourceMsg(source string, warnings int) string {
	return renderOrUncatalogued("service.token_source", map[string]string{
		"source": source, "detail": " — warnings: " + strconv.Itoa(warnings),
	})
}

func storeUnreachableMsg(addr, reason string) string {
	return renderOrUncatalogued("service.store_unreachable", map[string]string{
		"addr": addr, "reason": reason,
	})
}

// journalEvent records a service-plane event that is not an API call — a sign-in, a config write, an
// account change, the service coming up. `fields` are appended to the message rather than being typed
// columns: the catalogue's sentence is what a person reads, and every one of these is rare enough
// that a query over it would be a query over dozens of rows, not thousands.
func (s *server) journalEvent(code, lvl string, fields map[string]string, r *http.Request, extra ...string) {
	s.journalSubject(code, lvl, fields, r, "", extra...)
}

// journalSubject records an event ABOUT a particular account, for the cases where the request cannot
// name it. There are two, and both were recorded ownerless until a live check found them:
//
//	a successful SIGN-IN happens before the session exists, so actorOf(r) sees an anonymous caller;
//	an account CREATED or DELETED by an admin is not the caller at all — the admin is.
//
// Without a subject those records carry no owner, and an ownerless service event is admin-only
// (svcjournal_read.go). That makes the read route's whole reason for being `authed` rather than
// `admin` — an account reading the record of its own sign-ins — not work. The unit fixture did not
// catch it because the fixture SET the owner it then asserted on; the product never did.
// ⚠ THE SENTENCE IS NOT A PARAMETER (ADR-117). It used to be, and that is precisely how six codes
// came to disagree with the catalogue templates the hub matches them against. Now the caller names
// FIELDS and the template does the rest, so a hand-assembled sentence is not merely discouraged —
// there is nowhere to pass one.
//
// `extra` keeps its old shape and its old job, but it now feeds the template's `{detail}` field
// instead of being appended after rendering. That distinction is the whole point: appended text sits
// OUTSIDE the template, so the hub's extractor has to absorb it into whatever placeholder happens to
// be last, and a template edit silently changes what "the detail" means.
func (s *server) journalSubject(code, lvl string, fields map[string]string, r *http.Request, subject string, extra ...string) {
	if s.journal == nil {
		return
	}
	var actor, owner string
	if r != nil {
		actor, owner = s.actorOf(r)
	}
	if subject != "" {
		// The SUBJECT wins over the caller: an admin deleting somebody's account is actor=admin,
		// owner=the other account, which is exactly the case collapsing the two would lose.
		owner = subject
	}
	if len(extra) > 0 {
		if fields == nil {
			fields = map[string]string{}
		}
		fields["detail"] = fields["detail"] + " — " + strings.Join(extra, ", ")
	}
	s.journal.Log(svclog.Record{
		Lvl: lvl, Cat: "service", Code: code, Msg: s.renderMsg(code, fields), Actor: actor, Owner: owner,
	})
}
