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
package main

import (
	"bufio"
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
	"time"
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

// handleStream upgrades GET /v1/stream to a WebSocket and ingests recorder events (ADR-043). The
// pre-hijack guards (handshake/auth/origin) return JSON errors; only the 101 path hijacks.
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
	// CSWSH defense: if an Origin is present and an allowlist is configured, enforce it.
	if origin := r.Header.Get("Origin"); origin != "" && len(s.corsAllow) > 0 && !s.corsAllow[origin] {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "origin not allowed"})
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

	s.streamRecord(conn, brw.Reader)
}

// streamRecord runs the recorder read loop: persist each text/binary event line to the session's
// events.ndjson, answer pings, and stop on close / idle / cap. Split out so a test can drive it.
func (s *server) streamRecord(conn net.Conn, br *bufio.Reader) {
	session := newRunID()
	dir := filepath.Join(s.repo, "runs", "record-"+session)
	_ = os.MkdirAll(dir, 0o700)
	f, ferr := os.OpenFile(filepath.Join(dir, "events.ndjson"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if ferr == nil {
		defer f.Close()
	}

	// Greet the client with its session id (server→client text frame).
	_ = conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout))
	if ack, e := json.Marshal(map[string]string{"type": "session", "session": session}); e == nil {
		_ = wsWriteFrame(conn, wsOpText, ack)
	}

	events := 0
	for {
		_ = conn.SetReadDeadline(time.Now().Add(wsIdleTimeout))
		opcode, payload, fin, err := wsReadClientFrame(br)
		if err != nil {
			return
		}
		_ = conn.SetWriteDeadline(time.Now().Add(wsWriteTimeout)) // bound each server-side write
		switch opcode {
		case wsOpClose:
			_ = wsWriteFrame(conn, wsOpClose, nil)
			return
		case wsOpPing:
			_ = wsWriteFrame(conn, wsOpPong, payload)
		case wsOpContinuation:
			// We never send a fragmented message, so a standalone continuation is a protocol error.
			_ = wsWriteFrame(conn, wsOpClose, closePayload(1002, "unexpected continuation frame"))
			return
		case wsOpText, wsOpBinary:
			if !fin {
				// Fragmented messages aren't reassembled here — the recorder must send one event
				// per frame. Close with 1003 (unsupported data) rather than corrupt the NDJSON.
				_ = wsWriteFrame(conn, wsOpClose, closePayload(1003, "one event per frame"))
				return
			}
			line := strings.TrimRight(string(payload), "\r\n")
			if line == "" {
				continue
			}
			if ferr == nil {
				_, _ = f.WriteString(line + "\n")
			}
			events++
			if ack, e := json.Marshal(map[string]any{"type": "ack", "n": events}); e == nil {
				_ = wsWriteFrame(conn, wsOpText, ack)
			}
			if events >= wsMaxRecordEvents {
				_ = wsWriteFrame(conn, wsOpClose, closePayload(1009, "event cap reached"))
				return
			}
		default:
			// pong / reserved opcodes — ignore
		}
	}
}

// closePayload builds an RFC6455 close-frame body: 2-byte big-endian status code + UTF-8 reason.
func closePayload(code uint16, reason string) []byte {
	b := make([]byte, 2+len(reason))
	binary.BigEndian.PutUint16(b, code)
	copy(b[2:], reason)
	return b
}
