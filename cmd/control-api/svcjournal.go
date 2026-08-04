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

	"github.com/AlexGromer/sentinel/internal/redact"
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
			Msg:   redact.Line(actor + " reached for someone else's row: " + r.Method + " " + route),
			Actor: actor, Owner: owner, Method: r.Method, Route: route,
			Status: status, DurMs: dur, Foreign: true,
		})
		return
	}

	code, lvl := "service.api_call", "debug"
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		lvl = "info" // a mutation is what a journal exists for
	}
	switch {
	case status >= 500:
		code, lvl = "service.api_refused", "error"
	case status == http.StatusForbidden || status == http.StatusUnauthorized:
		code, lvl = "service.api_refused", "warn"
	}
	msg := r.Method + " " + route + " → " + strconv.Itoa(status) +
		" in " + strconv.FormatInt(dur, 10) + " ms, called by " + actor
	s.journal.Log(svclog.Record{
		Lvl: lvl, Cat: "service", Code: code, Msg: redact.Line(msg),
		Actor: actor, Owner: owner, Method: r.Method, Route: route, Status: status, DurMs: dur,
	})
}

// journalEvent records a service-plane event that is not an API call — a sign-in, a config write, an
// account change, the service coming up. `fields` are appended to the message rather than being typed
// columns: the catalogue's sentence is what a person reads, and every one of these is rare enough
// that a query over it would be a query over dozens of rows, not thousands.
func (s *server) journalEvent(code, lvl, msg string, r *http.Request, extra ...string) {
	if s.journal == nil {
		return
	}
	var actor, owner string
	if r != nil {
		actor, owner = s.actorOf(r)
	}
	if len(extra) > 0 {
		msg += " — " + strings.Join(extra, ", ")
	}
	s.journal.Log(svclog.Record{
		Lvl: lvl, Cat: "service", Code: code, Msg: redact.Line(msg), Actor: actor, Owner: owner,
	})
}
