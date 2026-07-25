// WebSocket recorder transport for the control-API (M9.8, ADR-043).
//
// GET /v1/stream is the client→server half of the extension↔brain channel (the server→client half is
// the M9.3-tail SSE, ADR-040). The MV3 recorder's service-worker opens a WebSocket here and streams
// captured DOM events (one JSON event per text frame) which we persist to runs/record-<session>/
// events.ndjson for the future record→scenario bridge (M9.2b reuse). Full grounding is MV3-impl
// (0xCoDSnet); this endpoint is the transport + ingest.
//
// HAND-ROLLED RFC6455 (no dependency): Go's net/http has no WebSocket server, and ADR-040 chose SSE
// to stay stdlib-only. Rather than reverse that to pull golang.org/x/net/websocket (legacy API), we
// implement the minimal handshake + frame codec on top of http.Hijacker. This keeps control-api
// dependency-free and gives a true duplex socket for the later takeover/return signals (ADR-039).
//
// AUTH: a browser WebSocket cannot set an Authorization header, so the bearer token rides in the
// Sec-WebSocket-Protocol list as `bearer.<token>` (K8s-style). The client offers two subprotocols —
// the protocol name `sentinel.recorder.v1` AND `bearer.<token>` — and the server validates the token
// (constant-time) but echoes back ONLY the non-secret protocol name, never the token. Reuses the same
// 127.0.0.1 bind + token + Origin allowlist as the rest of the control-API (ADR-032).
//
// M14 W2: the SAME endpoint also serves the server→client half — `GET /v1/stream?run_id=<id>` subscribes
// a browser to a live run's AG-UI event stream (replayed from the run's runStream ring buffer, then
// pushed as new lines arrive). `?session=` (recorder ingest) and `?run_id=` (event subscription) are
// mutually exclusive modes on the same hijacked socket; a run subscription still answers ping/control
// frames from the read loop while a second goroutine pushes events, so every server→client write funnels
// through wsConn's mutex (see below) to keep the two goroutines from interleaving frame writes.
package main

import (
	"bufio"
	"context"
	"crypto/sha1"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

const (
	wsGUID        = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11" // RFC6455 §4.2.2 magic
	wsSubprotocol = "sentinel.recorder.v1"                 // the non-secret subprotocol we echo back
	wsTokenProto  = "bearer."                              // Sec-WebSocket-Protocol: bearer.<token>

	wsOpContinuation = 0x0
	wsOpText         = 0x1
	wsOpBinary       = 0x2
	wsOpClose        = 0x8
	wsOpPing         = 0x9
	wsOpPong         = 0xA

	wsMaxFramePayload = 1 << 20          // 1 MiB per frame — a recorder event is tiny; cap to bound memory
	wsMaxRecordEvents = 10000            // per-session event cap (bounds an unbounded/hostile client)
	wsIdleTimeout     = 5 * time.Minute  // close an idle recorder socket
	wsWriteTimeout    = 10 * time.Second // bound a slow client blocking a server-frame write

	wsCtlForwardTimeout = 5 * time.Second // M9.8 F4 (ADR-054): bound a Takeover/Return RPC to the orchestrator
)

// wsAccept computes the RFC6455 Sec-WebSocket-Accept for a client key.
func wsAccept(key string) string {
	h := sha1.New()
	_, _ = io.WriteString(h, key+wsGUID)
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// wsAuthed validates the bearer token carried in the Sec-WebSocket-Protocol list (constant-time).
// Fail-closed: no configured token → never authed (mirrors s.authed).
func (s *server) wsAuthed(r *http.Request) bool {
	if s.token == "" {
		return false
	}
	for _, proto := range strings.Split(r.Header.Get("Sec-WebSocket-Protocol"), ",") {
		proto = strings.TrimSpace(proto)
		if tok, ok := strings.CutPrefix(proto, wsTokenProto); ok {
			if subtle.ConstantTimeCompare([]byte(tok), []byte(s.token)) == 1 {
				return true
			}
		}
	}
	return false
}

// wsConn synchronizes every server→client frame write for one /v1/stream connection. Before M14 a
// single goroutine (the read loop) did all writes, so no lock was needed. The M14 run-subscription
// mode adds a SECOND goroutine — the event pusher (streamRunEvents) — writing concurrently with the
// read loop's pong/control-ack replies. Two goroutines calling wsWriteFrame on the same net.Conn
// without synchronization could interleave a frame's header bytes with another frame's payload,
// corrupting the stream for the client. Every server→client write (greet, ack, pong, control reply,
// pushed event) MUST go through wc.writeFrame — this mutex is the load-bearing fix for that race.
type wsConn struct {
	conn net.Conn
	mu   sync.Mutex
}

func newWSConn(conn net.Conn) *wsConn { return &wsConn{conn: conn} }

// writeFrame writes one server→client frame under the connection's write mutex, bounding the write
// with wsWriteTimeout so a slow/stalled client can't hang the writer holding the lock indefinitely.
func (c *wsConn) writeFrame(opcode byte, payload []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	_ = c.conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
	return wsWriteFrame(c.conn, opcode, payload)
}

// wsWriteFrame writes a single unmasked server→client frame (server frames are never masked, RFC6455 §5.1).
func wsWriteFrame(w io.Writer, opcode byte, payload []byte) error {
	n := len(payload)
	var hdr []byte
	switch {
	case n < 126:
		hdr = []byte{0x80 | opcode, byte(n)}
	case n < 1<<16:
		hdr = []byte{0x80 | opcode, 126, byte(n >> 8), byte(n)}
	default:
		hdr = make([]byte, 10)
		hdr[0], hdr[1] = 0x80|opcode, 127
		binary.BigEndian.PutUint64(hdr[2:], uint64(n))
	}
	if _, err := w.Write(hdr); err != nil {
		return err
	}
	if n == 0 {
		return nil
	}
	_, err := w.Write(payload)
	return err
}

// wsReadClientFrame reads one masked client→server frame. Client frames MUST be masked (RFC6455 §5.1);
// an unmasked frame is a protocol error. Fragmentation is not reassembled here — the recorder sends one
// event per (FIN) frame; a non-final data frame is rejected by the caller.
func wsReadClientFrame(br *bufio.Reader) (opcode byte, payload []byte, fin bool, err error) {
	h := make([]byte, 2)
	if _, err = io.ReadFull(br, h); err != nil {
		return
	}
	fin = h[0]&0x80 != 0
	opcode = h[0] & 0x0f
	masked := h[1]&0x80 != 0
	length := uint64(h[1] & 0x7f)
	switch length {
	case 126:
		ext := make([]byte, 2)
		if _, err = io.ReadFull(br, ext); err != nil {
			return
		}
		length = uint64(binary.BigEndian.Uint16(ext))
	case 127:
		ext := make([]byte, 8)
		if _, err = io.ReadFull(br, ext); err != nil {
			return
		}
		length = binary.BigEndian.Uint64(ext)
	}
	if !masked {
		err = errors.New("ws: client frame not masked")
		return
	}
	// RFC6455 §5.5: control frames (close/ping/pong, opcode ≥ 0x8) must be FIN and ≤125 bytes.
	if opcode >= wsOpClose && (length > 125 || !fin) {
		err = errors.New("ws: invalid control frame (must be FIN and ≤125 bytes)")
		return
	}
	if length > wsMaxFramePayload {
		err = errors.New("ws: frame too large")
		return
	}
	mask := make([]byte, 4)
	if _, err = io.ReadFull(br, mask); err != nil {
		return
	}
	payload = make([]byte, length)
	if _, err = io.ReadFull(br, payload); err != nil {
		return
	}
	for i := range payload {
		payload[i] ^= mask[i%4]
	}
	return
}

// handleStream upgrades GET /v1/stream to a WebSocket and either ingests recorder events (ADR-043,
// ?session=) or subscribes to a run's AG-UI event stream (M14 W2, ?run_id=). The pre-hijack guards
// (handshake/auth/origin) return JSON errors; only the 101 path hijacks.
func (s *server) handleStream(w http.ResponseWriter, r *http.Request) {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") ||
		!strings.Contains(strings.ToLower(r.Header.Get("Connection")), "upgrade") {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "expected a websocket upgrade"})
		return
	}
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" || r.Header.Get("Sec-WebSocket-Version") != "13" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad handshake (need Sec-WebSocket-Key + Version 13)"})
		return
	}
	if !s.wsAuthed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer subprotocol (Sec-WebSocket-Protocol: bearer.<token>)"})
		return
	}
	// CSWSH defense (R3-hardening, M13): reject a cross-origin handshake unless the origin is explicitly
	// allowlisted. When bound PUBLICLY with no allowlist, refuse any browser Origin — exposing the socket
	// non-locally requires configuring CONTROL_API_CORS_ORIGINS (fail-closed). On a localhost bind an
	// absent allowlist stays permissive (dev), still gated by the bearer subprotocol above.
	if origin := r.Header.Get("Origin"); origin != "" {
		switch {
		case sameOriginRequest(r):
			// ADR-064 Mode 3: this server handed the page to the browser, so the handshake is same-origin
			// by construction and cannot be CSWSH — that attack needs a page on a DIFFERENT site riding
			// ambient credentials. Checked FIRST: in a container the bind is 0.0.0.0 (publicBind) with an
			// intentionally empty allowlist, which used to 403 the UI's own socket and surface as close
			// 1006. sameOriginRequest compares the browser-set (unforgeable) Origin host against r.Host
			// and demands Sec-Fetch-Site same-origin/none/absent, so a cross-site page never reaches here.
		case len(s.corsAllow) > 0:
			if !s.corsAllow[origin] {
				writeJSON(w, http.StatusForbidden, map[string]string{"error": "origin not allowed"})
				return
			}
		case s.publicBind:
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "origin present but no allowlist on a non-local bind (set CONTROL_API_CORS_ORIGINS)"})
			return
		}
	}
	// R3-hardening (M13, #58 note): a client may RESUME a prior recorder session (?session=<id>) so a
	// mid-recording reconnect appends to the SAME runs/record-<id>/events.ndjson instead of fragmenting
	// into a fresh dir. Validated to a bare session id (no path traversal); empty => a new session.
	resumeSession := r.URL.Query().Get("session")
	// M14 W2: ?run_id= subscribes this socket to a live run's AG-UI events instead of ingesting recorder
	// events — a distinct mode from ?session=, never both on the same connection. Same charset/length
	// guard as the recorder session id (validRunID); run_id here is used as an s.runs map key and echoed
	// in JSON, not as a filesystem path component, so no filepath.Base sanitizer is needed on this arm.
	runIDParam := r.URL.Query().Get("run_id")
	switch {
	case resumeSession != "" && runIDParam != "":
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "session and run_id are mutually exclusive (?session= resumes recorder ingest; ?run_id= subscribes to run events)"})
		return
	case resumeSession != "" && !validRunID(resumeSession):
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "session must be a bare recorder session id"})
		return
	case runIDParam != "" && !validRunID(runIDParam):
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "run_id must be a bare run id"})
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "streaming unsupported"})
		return
	}
	conn, brw, err := hj.Hijack()
	if err != nil {
		return
	}
	defer conn.Close()

	// Handshake response — echo only the non-secret subprotocol name, never the token.
	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + wsAccept(key) + "\r\n" +
		"Sec-WebSocket-Protocol: " + wsSubprotocol + "\r\n\r\n"
	if _, err := brw.WriteString(resp); err != nil {
		return
	}
	if err := brw.Flush(); err != nil {
		return
	}

	if runIDParam != "" {
		s.streamRunEvents(conn, brw.Reader, runIDParam)
		return
	}
	s.streamRecord(conn, brw.Reader, resumeSession)
}

// streamRecord runs the recorder read loop: persist each text/binary event line to the session's
// events.ndjson, answer pings, and stop on close / idle / cap. Split out so a test can drive it.
// resumeSession (validated by the caller) appends to an existing session; empty mints a new one.
func (s *server) streamRecord(conn net.Conn, br *bufio.Reader, resumeSession string) {
	wc := newWSConn(conn)
	session := resumeSession
	if session == "" || !validRunID(session) { // re-validate: only [A-Za-z0-9_-] (no path separators)
		session = newRunID()
	}
	// The resumed ?session= id is user input. validRunID already bars path separators; filepath.Base
	// strips any residual path components so record-<session> is always a single leaf under runs/ — a
	// path-traversal barrier that the static taint analysis also recognizes (defense-in-depth).
	dir := filepath.Join(s.repo, "runs", filepath.Base("record-"+session))
	_ = os.MkdirAll(dir, 0o700)
	f, ferr := os.OpenFile(filepath.Join(dir, "events.ndjson"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if ferr == nil {
		defer f.Close()
	}

	// Greet the client with its session id (server→client text frame).
	if ack, e := json.Marshal(map[string]string{"type": "session", "session": session}); e == nil {
		_ = wc.writeFrame(wsOpText, ack)
	}

	events, ctlFrames := 0, 0
	for {
		_ = conn.SetReadDeadline(time.Now().Add(wsIdleTimeout))
		opcode, payload, fin, err := wsReadClientFrame(br)
		if err != nil {
			return
		}
		switch opcode {
		case wsOpClose:
			_ = wc.writeFrame(wsOpClose, nil)
			return
		case wsOpPing:
			_ = wc.writeFrame(wsOpPong, payload)
		case wsOpContinuation:
			// We never send a fragmented message, so a standalone continuation is a protocol error.
			_ = wc.writeFrame(wsOpClose, closePayload(1002, "unexpected continuation frame"))
			return
		case wsOpText, wsOpBinary:
			if !fin {
				// Fragmented messages aren't reassembled here — the recorder must send one event
				// per frame. Close with 1003 (unsupported data) rather than corrupt the NDJSON.
				_ = wc.writeFrame(wsOpClose, closePayload(1003, "one event per frame"))
				return
			}
			line := strings.TrimRight(string(payload), "\r\n")
			if line == "" {
				continue
			}
			// M9.8 F4 (ADR-054): a takeover/return control frame is forwarded to the orchestrator and is
			// NOT persisted as a recorder event; everything else is a recorder DOM event (events.ndjson).
			// Control frames get their OWN per-session cap (each dials the orchestrator) so an authed
			// client can't loop them unboundedly — the recorder-event cap below doesn't bound this branch.
			if action, runID, isCtl := parseControlFrame(payload); isCtl {
				s.handleControlFrame(wc, action, runID)
				ctlFrames++
				if ctlFrames >= wsMaxRecordEvents {
					_ = wc.writeFrame(wsOpClose, closePayload(1009, "control-frame cap reached"))
					return
				}
				continue
			}
			if ferr == nil {
				_, _ = f.WriteString(line + "\n")
			}
			events++
			if ack, e := json.Marshal(map[string]any{"type": "ack", "n": events}); e == nil {
				_ = wc.writeFrame(wsOpText, ack)
			}
			if events >= wsMaxRecordEvents {
				_ = wc.writeFrame(wsOpClose, closePayload(1009, "event cap reached"))
				return
			}
		default:
			// pong / reserved opcodes — ignore
		}
	}
}

// streamRunEvents subscribes conn to a live run's AG-UI event stream (M14 W2, ?run_id=). The read loop
// (this goroutine) still answers ping/control-frames/close on the socket exactly like streamRecord;
// a second goroutine drains the run's runStream and pushes events, synchronized against the read loop
// through wc's write mutex. run_id (charset-validated by the caller) must name a KNOWN live run — an
// unknown run_id gets a graceful error + close, never a panic.
func (s *server) streamRunEvents(conn net.Conn, br *bufio.Reader, runID string) {
	wc := newWSConn(conn)

	s.mu.RLock()
	rec, ok := s.runs[runID]
	s.mu.RUnlock()
	if !ok {
		if b, e := json.Marshal(map[string]string{"type": "error", "run_id": runID, "error": "no such run"}); e == nil {
			_ = wc.writeFrame(wsOpText, b)
		}
		_ = wc.writeFrame(wsOpClose, closePayload(1008, "no such run"))
		return
	}
	if ack, e := json.Marshal(map[string]string{"type": "subscribed", "run_id": runID}); e == nil {
		_ = wc.writeFrame(wsOpText, ack)
	}

	// subscribe() replays the buffered ring + hands us a channel of future lines (nil+finished if the
	// run already completed before we subscribed — then the snapshot is the whole history and there is
	// nothing left to push). done stops the pusher goroutine on our way out; wg lets us wait for it to
	// actually exit before returning (no goroutine leak past the life of this connection).
	snapshot, ch, finished := rec.stream.subscribe()
	done := make(chan struct{})
	var wg sync.WaitGroup
	if !finished {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, line := range snapshot {
				if wc.writeFrame(wsOpText, wsAGUIFrame(runID, line)) != nil {
					return
				}
			}
			for {
				select {
				case line, open := <-ch:
					if !open { // rec.stream.finish() closed us — run completed, nothing more to push
						return
					}
					if wc.writeFrame(wsOpText, wsAGUIFrame(runID, line)) != nil {
						return
					}
				case <-done: // connection is tearing down — stop pushing
					return
				}
			}
		}()
	} else {
		for _, line := range snapshot {
			if wc.writeFrame(wsOpText, wsAGUIFrame(runID, line)) != nil {
				break
			}
		}
	}
	defer func() {
		close(done)
		if !finished {
			// Idempotent against a concurrent rec.stream.finish(): unsubscribe is a no-op if finish()
			// already deleted+closed this channel (see runStream.unsubscribe in main.go).
			rec.stream.unsubscribe(ch)
		}
		wg.Wait() // don't return (and let the caller conn.Close()) until the pusher has actually exited
	}()

	// The read loop: a run-subscription socket doesn't ingest recorder DOM events, but a subscriber may
	// still send ping/close, or a takeover/return control frame (M9.8 F4) on the same connection.
	ctlFrames := 0
	for {
		_ = conn.SetReadDeadline(time.Now().Add(wsIdleTimeout))
		opcode, payload, fin, err := wsReadClientFrame(br)
		if err != nil {
			return
		}
		switch opcode {
		case wsOpClose:
			_ = wc.writeFrame(wsOpClose, nil)
			return
		case wsOpPing:
			_ = wc.writeFrame(wsOpPong, payload)
		case wsOpContinuation:
			_ = wc.writeFrame(wsOpClose, closePayload(1002, "unexpected continuation frame"))
			return
		case wsOpText, wsOpBinary:
			if !fin {
				_ = wc.writeFrame(wsOpClose, closePayload(1003, "one event per frame"))
				return
			}
			line := strings.TrimRight(string(payload), "\r\n")
			if line == "" {
				continue
			}
			if action, ctlRunID, isCtl := parseControlFrame(payload); isCtl {
				s.handleControlFrame(wc, action, ctlRunID)
				ctlFrames++
				if ctlFrames >= wsMaxRecordEvents {
					_ = wc.writeFrame(wsOpClose, closePayload(1009, "control-frame cap reached"))
					return
				}
			}
			// Anything else on a subscription socket is not a recorder event and has nowhere to go —
			// ignored (no ack), unlike streamRecord's ingest path.
		default:
			// pong / reserved opcodes — ignore
		}
	}
}

// wsAGUIPrefix marks a runStream line as a pre-formed AG-UI JSON event (emitted by brain, M14 W4) to be
// forwarded to a subscriber verbatim, rather than wrapped as a generic "log" line.
const wsAGUIPrefix = "@@AGUI "

// wsAGUIFrame returns the payload to push for one runStream line: the raw @@AGUI JSON payload verbatim
// if it looks well-formed, else the line wrapped as a typed "log" event. We deliberately do NOT parse
// or validate the @@AGUI JSON beyond stripping the prefix and checking it starts with '{' — that's
// enough to tell a real envelope from a spoofed prefix (e.g. a recorder DOM event whose text happens to
// start with "@@AGUI "), without taking on a JSON-schema dependency here. A spoofed/malformed payload
// falls back to the log envelope using the ORIGINAL line, so it's never silently dropped.
func wsAGUIFrame(runID, line string) []byte {
	if rest, ok := strings.CutPrefix(line, wsAGUIPrefix); ok {
		if rest = strings.TrimSpace(rest); strings.HasPrefix(rest, "{") {
			return []byte(rest)
		}
	}
	// Fixed shape of string values only — json.Marshal cannot fail here (mirrors sendLog in main.go).
	b, _ := json.Marshal(map[string]any{
		"type":   "log",
		"run_id": runID,
		"data":   map[string]string{"line": line},
	})
	return b
}

// closePayload builds an RFC6455 close-frame body: 2-byte big-endian status code + UTF-8 reason.
func closePayload(code uint16, reason string) []byte {
	b := make([]byte, 2+len(reason))
	binary.BigEndian.PutUint16(b, code)
	copy(b[2:], reason)
	return b
}

// parseControlFrame recognises an M9.8 F4 control frame on the /v1/stream socket (ADR-054):
//
//	{"type":"control","action":"takeover|return","run_id":"<id>"}
//
// The dedicated `type:"control"` envelope NAMESPACES these so they cannot collide with a recorder DOM
// event's own `type` vocabulary — a key-capture recorder can legitimately emit `type:"return"` (Enter
// key) or `type:"takeover"`, which must still persist as events. Returns (action, run_id, true) for a
// well-formed control envelope (action validated downstream); any other frame is a recorder event
// (ok=false), persisted to events.ndjson as before.
func parseControlFrame(payload []byte) (action, runID string, ok bool) {
	var f struct {
		Type   string `json:"type"`
		Action string `json:"action"`
		RunID  string `json:"run_id"`
	}
	if json.Unmarshal(payload, &f) != nil || f.Type != "control" {
		return "", "", false
	}
	return f.Action, f.RunID, true
}

// validRunID bounds a run_id forwarded to the orchestrator (M9.8 F4). run_ids are hex (the orchestrator's
// newRunID is 16 hex chars); we accept a safe charset + length so a hostile control frame can't inject a
// control-char / huge string as an orchestrator map key. NOTE: this is a format guard only — binding a
// run_id to the WS session (cross-run authorization) lands with the M9-LIVE per-run socket wiring; until
// then any authed /v1/stream client can address any run_id (documented in THREAT_MODEL ❾).
func validRunID(id string) bool {
	if id == "" || len(id) > 64 {
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

// handleControlFrame validates a control envelope, forwards takeover/return to the orchestrator, and acks
// the result over the socket (control-ok / control-error). An unknown action, a missing/invalid run_id,
// or an unconfigured orchestrator is reported to the client, never persisted.
func (s *server) handleControlFrame(wc *wsConn, action, runID string) {
	reply := func(m map[string]string) {
		if b, e := json.Marshal(m); e == nil {
			_ = wc.writeFrame(wsOpText, b)
		}
	}
	if action != "takeover" && action != "return" {
		reply(map[string]string{"type": "control-error", "action": action, "error": "unknown control action (want takeover|return)"})
		return
	}
	if !validRunID(runID) {
		reply(map[string]string{"type": "control-error", "action": action, "error": "missing/invalid run_id"})
		return
	}
	if err := s.forwardControl(action, runID); err != nil {
		reply(map[string]string{"type": "control-error", "action": action, "run_id": runID, "error": err.Error()})
		return
	}
	reply(map[string]string{"type": "control-ok", "action": action, "run_id": runID})
}

// forwardControl dials the RunControl orchestrator (CONTROL_API_ORCH_ADDR — any gRPC target, e.g.
// "unix:/abs/state/sentinel-orch-<id>.sock") and issues the Takeover/Return RPC for run_id (M9.8 F4,
// ADR-054). A fresh connection per call: takeover/return are low-frequency operator signals, not the
// per-event recorder path. Fail-closed when no orchestrator is wired.
func (s *server) forwardControl(action, runID string) error {
	if s.orchAddr == "" {
		return errors.New("no orchestrator wired (set CONTROL_API_ORCH_ADDR)")
	}
	conn, err := grpc.NewClient(s.orchAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), wsCtlForwardTimeout)
	defer cancel()
	cl := pb.NewRunControlClient(conn)
	switch action {
	case "takeover":
		_, err = cl.Takeover(ctx, &pb.TakeoverRequest{RunId: runID, Reason: "operator (control-api /v1/stream)"})
	case "return":
		_, err = cl.Return(ctx, &pb.ReturnRequest{RunId: runID})
	default:
		err = errors.New("unknown control action: " + action)
	}
	return err
}
