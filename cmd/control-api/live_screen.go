package main

// The real screen, relayed (LIVE-VNC, ADR-127).
//
// The `vnc` profile puts a HEADED Chromium on a virtual X display and exports it over RFB. The port
// is never published to a host, so the operator's browser has no path to it at all — and that is the
// problem this file solves: control-api becomes a WebSocket SERVER for the page and a raw TCP CLIENT
// for x11vnc, which is the shortest arrangement that keeps every promise at once.
//
// WHY A RELAY RATHER THAN PUBLISHING THE PORT. Three properties, none of which survives publishing:
//
//  1. the bearer token stays the only way in. The RFB port carries a password, but VNC's classic auth
//     is DES over the FIRST EIGHT BYTES (measured: a server holding `ABCDEFGH12345678` accepts
//     `ABCDEFGH`), over an unencrypted channel. That is a lock, not a wall, and it must never be
//     described as the equivalent of a token.
//  2. THE PASSWORD NEVER REACHES THE PAGE. control-api spends it here and offers the browser security
//     type None. Handing it to the page would mean fetching a secret over HTTP to send it back over
//     WebSocket, and putting it in every tab's memory, in ui-smoke screenshots and in any HAR attached
//     to a bug report. `internal/configguard` refuses a config document containing `password` for the
//     same reason.
//  3. taking the mouse becomes OBSERVABLE. The relay sees the bytes, so `service.screen_control_taken`
//     records what happened rather than what the interface claimed.
//
// WHY NO NEW PROTOCOL CODE. The frames written to the browser are server→client, i.e. UNMASKED, which
// `wsWriteFrame` already does; the frames read from it are client→server, i.e. masked, which
// `wsReadClientFrame` already unmasks. The upstream side is a plain TCP socket. `crypto/des` is in the
// standard library, so the obstacle ADR-111 hit — no websocket library in go.mod, and dragging one in
// has broken the air-gapped build before — does not arise here.

import (
	"bufio"
	"crypto/des"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"

	"strings"
	"time"

	"github.com/AlexGromer/sentinel/internal/vncsecret"
)

// vncAddr is the RFB address of the vnc profile's browser service (host:port), e.g. browser-vnc:5900.
// Empty means the profile is not part of this deployment — the default, and a true answer rather than
// an error.
func vncAddr() string { return strings.TrimSpace(os.Getenv("CONTROL_API_VNC_ADDR")) }

// vncServerInfo is what the RFB ServerInit message tells us about the screen.
type vncServerInfo struct {
	Width, Height int
	Desktop       string
}

// vncKey turns a password into the DES key VNC actually uses: exactly 8 bytes, zero-padded or
// TRUNCATED, with the BITS OF EACH BYTE REVERSED.
//
// ⚠ The bit reversal is the part nobody guesses and no document here would have told us: it exists
// because the original implementation fed the bytes to a DES routine that read them
// least-significant-bit first, and every VNC server has been bug-compatible with it ever since.
// Verified against a live x11vnc before this file was written — without the reversal the server
// answers "refused" to the correct password.
func vncKey(pass string) []byte {
	k := make([]byte, 8)
	copy(k, pass) // copy() stops at 8 — this IS the truncation the protocol imposes
	for i, b := range k {
		var r byte
		for j := 0; j < 8; j++ {
			r |= ((b >> j) & 1) << (7 - j)
		}
		k[i] = r
	}
	return k
}

// vncHandshakeUpstream performs RFB 3.8 VncAuth against x11vnc and stops right after SecurityResult,
// leaving the connection exactly where a client would be: waiting to send ClientInit.
//
// Errors are phrased for a PERSON, because every one of them ends up in the `reason` a human reads in
// the hub. "dial: connection refused" and "the password was refused" are different problems with
// different fixes, and a single "screen unavailable" would hide which.
func vncHandshakeUpstream(conn net.Conn, pass string) error {
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))

	banner := make([]byte, 12)
	if _, err := io.ReadFull(conn, banner); err != nil {
		return fmt.Errorf("no RFB banner (is this a VNC server?): %w", err)
	}
	if !strings.HasPrefix(string(banner), "RFB 003.") {
		return fmt.Errorf("not an RFB 3.x server: banner %q", strings.TrimSpace(string(banner)))
	}
	if _, err := conn.Write([]byte("RFB 003.008\n")); err != nil {
		return fmt.Errorf("could not send the protocol version: %w", err)
	}

	var n [1]byte
	if _, err := io.ReadFull(conn, n[:]); err != nil {
		return fmt.Errorf("no security-type list: %w", err)
	}
	if n[0] == 0 {
		return fmt.Errorf("the server refused the connection before authentication")
	}
	types := make([]byte, n[0])
	if _, err := io.ReadFull(conn, types); err != nil {
		return fmt.Errorf("truncated security-type list: %w", err)
	}
	hasVNCAuth := false
	for _, t := range types {
		if t == 2 {
			hasVNCAuth = true
		}
	}
	if !hasVNCAuth {
		// Worth its own sentence: a server offering only None is a screen ANYONE on that network can
		// drive, and the profile promises the opposite.
		return fmt.Errorf("the VNC server does not require a password (offered types %v) — refusing to relay an unauthenticated desktop", types)
	}
	if pass == "" {
		return fmt.Errorf("the VNC server requires a password and none is readable (SENTINEL_VNC_PASSWORD, or state/vnc.password written by `agentctl vnc-password`)")
	}
	if _, err := conn.Write([]byte{2}); err != nil {
		return fmt.Errorf("could not choose VNC authentication: %w", err)
	}

	challenge := make([]byte, 16)
	if _, err := io.ReadFull(conn, challenge); err != nil {
		return fmt.Errorf("no authentication challenge: %w", err)
	}
	blk, err := des.NewCipher(vncKey(pass))
	if err != nil {
		return fmt.Errorf("cannot build the DES key: %w", err)
	}
	resp := make([]byte, 16)
	blk.Encrypt(resp[0:8], challenge[0:8]) // ECB, both halves under the same key
	blk.Encrypt(resp[8:16], challenge[8:16])
	if _, err := conn.Write(resp); err != nil {
		return fmt.Errorf("could not send the authentication response: %w", err)
	}
	var res [4]byte
	if _, err := io.ReadFull(conn, res[:]); err != nil {
		return fmt.Errorf("no authentication result: %w", err)
	}
	if binary.BigEndian.Uint32(res[:]) != 0 {
		return fmt.Errorf("the VNC server refused the password from %s", vncPassOrigin())
	}
	_ = conn.SetDeadline(time.Time{})
	return nil
}

// vncPassOrigin names WHERE the password came from, for a refusal message. Never the value.
func vncPassOrigin() string {
	if strings.TrimSpace(os.Getenv("SENTINEL_VNC_PASSWORD")) != "" {
		return "SENTINEL_VNC_PASSWORD"
	}
	return "state/vnc.password"
}

// vncServerInit completes ClientInit/ServerInit so the screen's size and desktop name can be reported.
// Used by the status path; the relay lets the BROWSER send its own ClientInit instead.
func vncServerInit(conn net.Conn) (*vncServerInfo, error) {
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := conn.Write([]byte{1}); err != nil { // shared
		return nil, err
	}
	head := make([]byte, 24)
	if _, err := io.ReadFull(conn, head); err != nil {
		return nil, err
	}
	nameLen := binary.BigEndian.Uint32(head[20:24])
	if nameLen > 4096 {
		nameLen = 4096
	}
	name := make([]byte, nameLen)
	if _, err := io.ReadFull(conn, name); err != nil {
		return nil, err
	}
	return &vncServerInfo{
		Width:   int(binary.BigEndian.Uint16(head[0:2])),
		Height:  int(binary.BigEndian.Uint16(head[2:4])),
		Desktop: string(name),
	}, nil
}

// screenAttached reports whether runs actually go to the browser whose screen this is.
//
// ⚠ THIS IS AN INFERENCE FROM TWO ADDRESSES, NOT A MEASUREMENT, and the UI must say so. With the `vnc`
// profile up, the default headless `browser` service is STILL running and PW_CDP_ENDPOINT still points
// at it — so the screen can be perfectly alive and show an IDLE browser while every run happens
// somewhere else. That is worse than an outage: it looks like it works. Hence a warning strip rather
// than a refusal — a false "not attached" (a legitimate topology we did not model) must never take the
// picture away.
func screenAttached() bool {
	vnc := vncAddr()
	cdp := strings.TrimSpace(os.Getenv("PW_CDP_ENDPOINT"))
	if vnc == "" || cdp == "" {
		return false
	}
	host := vnc
	if h, _, err := net.SplitHostPort(vnc); err == nil {
		host = h
	}
	if u, err := url.Parse(cdp); err == nil && u.Host != "" {
		return strings.EqualFold(u.Hostname(), host)
	}
	return strings.Contains(cdp, host)
}

// screenState is the `screen` member of GET /v1/live/status.
//
// It EXTENDS the existing status document rather than adding a route, and that is deliberate:
// liveTargetURL exists in this package precisely because two places once built one address and drifted
// apart invisibly. A second status document would reproduce that, and `agentctl live status` gets the
// screen for free.
//
// Three states, three DIFFERENT sentences — the distinction the hub is built around:
//   - the profile is not running here      → available:false, and the reason names the profile
//   - it is declared but does not answer   → available:false, and the reason says what failed
//   - it works                             → available:true, with size, desktop name and `attached`
func (s *server) screenState() map[string]any {
	addr := vncAddr()
	if addr == "" {
		return map[string]any{
			"available": false,
			"reason":    "no VNC screen configured (CONTROL_API_VNC_ADDR is unset) — this deployment did not start the `vnc` compose profile",
			"attached":  false,
		}
	}
	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		return map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("the VNC screen at %s did not answer: %v", addr, err),
			"attached":  false,
		}
	}
	defer conn.Close()
	pass, _ := vncsecret.Read(s.repo)
	if err := vncHandshakeUpstream(conn, pass); err != nil {
		return map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("the VNC screen at %s is declared but unusable: %v", addr, err),
			"attached":  false,
		}
	}
	info, err := vncServerInit(conn)
	if err != nil {
		return map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("the VNC screen at %s authenticated but did not describe itself: %v", addr, err),
			"attached":  false,
		}
	}
	return map[string]any{
		"available": true,
		"addr":      addr,
		"width":     info.Width,
		"height":    info.Height,
		"desktop":   info.Desktop,
		"attached":  screenAttached(),
		"auth":      "the vnc password is spent by control-api; the browser is offered None behind the bearer token",
	}
}

// handleLiveScreen upgrades to a WebSocket and relays RFB.
//
// Access is `accessOpen` at the guard and authenticated HERE, deliberately: a browser WebSocket cannot
// send an Authorization header, and the guard reads only that header. The credential rides in
// Sec-WebSocket-Protocol as `bearer.<token>` and is compared constant-time by wsAuthed — the same
// arrangement /v1/stream has, stated here rather than inherited from `legacyOpen`, whose real meaning
// is "no accounts exist yet" and which therefore stops relaxing the moment identity is switched on.
func (s *server) handleLiveScreen(w http.ResponseWriter, r *http.Request) {
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
	if origin := r.Header.Get("Origin"); origin != "" && !sameOriginRequest(r) {
		switch {
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
	who, _ := s.actorOf(r)
	addr := vncAddr()
	if addr == "" {
		// 501, not 404: the route exists and this deployment does not implement it — the same
		// distinction proxyLive makes, and the one the hub turns into a sentence naming the profile.
		writeJSON(w, http.StatusNotImplemented, map[string]string{
			"error": "no VNC screen configured (CONTROL_API_VNC_ADDR is unset) — start the `vnc` compose profile",
		})
		return
	}

	up, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("the VNC screen at %s did not answer: %v", addr, err)})
		return
	}
	defer up.Close()
	pass, _ := vncsecret.Read(s.repo)
	if err := vncHandshakeUpstream(up, pass); err != nil {
		s.journalEvent("service.screen_refused", "warn", map[string]string{
			"addr": addr, "actor": who, "reason": err.Error(),
		}, r)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
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

	s.relayScreen(conn, brw.Reader, up, addr, who)
}

// relayScreen plays the SERVER half of RFB towards the browser (version, "None" security, success)
// and then copies bytes both ways. Split out so a test can drive it over a pipe.
func (s *server) relayScreen(conn net.Conn, br *bufio.Reader, up net.Conn, addr, actor string) {
	wc := newWSConn(conn)
	started := time.Now()
	var relayed int64
	closeReason := "client closed"

	// The browser's own RFB handshake, answered by US. It is offered exactly one security type — 1
	// (None) — because the password was already spent upstream. This is the line that keeps the secret
	// out of the page, so it is not a detail: offering type 2 here would mean asking the operator's
	// browser for a credential control-api is already holding.
	if err := wc.writeFrame(wsOpBinary, []byte("RFB 003.008\n")); err != nil {
		return
	}
	if _, _, _, err := readWSBinary(br); err != nil { // the client's version string
		return
	}
	if err := wc.writeFrame(wsOpBinary, []byte{1, 1}); err != nil { // 1 type, type 1 = None
		return
	}
	if _, _, _, err := readWSBinary(br); err != nil { // the client's chosen type
		return
	}
	if err := wc.writeFrame(wsOpBinary, []byte{0, 0, 0, 0}); err != nil { // SecurityResult: ok
		return
	}

	info := map[string]string{"addr": addr, "actor": actor, "mode": "view-only until the operator takes the mouse"}
	// nil request: the socket is hijacked by now, so the actor is carried explicitly above.
	s.journalEvent("service.screen_opened", "info", info, nil)

	done := make(chan struct{}, 2)

	// browser → x11vnc. Also where taking the mouse becomes a FACT rather than a claim: RFB client
	// message types 4 (KeyEvent) and 5 (PointerEvent) are input. Recording what crossed the wire cannot
	// be bypassed by flipping a flag in devtools.
	go func() {
		defer func() { done <- struct{}{} }()
		announced := false
		for {
			op, payload, _, err := readWSBinary(br)
			if err != nil {
				return
			}
			if op == wsOpClose {
				return
			}
			if len(payload) == 0 {
				continue
			}
			if !announced && (payload[0] == 4 || payload[0] == 5) {
				announced = true
				s.journalEvent("service.screen_control_taken", "warn", map[string]string{"addr": addr, "actor": actor}, nil)
			}
			if _, err := up.Write(payload); err != nil {
				return
			}
		}
	}()

	// x11vnc → browser. One WebSocket frame per read; noVNC reassembles the RFB stream itself, so
	// there is no framing to preserve here — which is exactly why this relay needs no protocol code.
	go func() {
		defer func() { done <- struct{}{} }()
		buf := make([]byte, 32<<10)
		for {
			n, err := up.Read(buf)
			if n > 0 {
				relayed += int64(n)
				if werr := wc.writeFrame(wsOpBinary, buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				closeReason = "the screen closed the connection"
				return
			}
		}
	}()

	<-done
	_ = up.Close()
	_ = conn.Close()
	s.journalEvent("service.screen_closed", "info", map[string]string{
		"addr": addr, "reason": closeReason,
		"dur_s": fmt.Sprintf("%.0f", time.Since(started).Seconds()),
		"bytes": fmt.Sprintf("%d", relayed),
	}, nil)
}

// readWSBinary reads one client frame, answering nothing: the relay has no control protocol of its
// own, and a ping arriving mid-stream is not an error.
func readWSBinary(br *bufio.Reader) (byte, []byte, bool, error) {
	for {
		op, payload, fin, err := wsReadClientFrame(br)
		if err != nil {
			return 0, nil, false, err
		}
		switch op {
		case wsOpPing:
			continue
		case wsOpPong:
			continue
		}
		return op, payload, fin, nil
	}
}

// desCipher exposes the key schedule to the test's fake server, which has to compute the SAME
// response to prove the relay authenticated rather than echoed. Kept beside the code it mirrors so
// the two cannot drift into agreeing on a wrong answer.
func desCipher(pass string) (interface {
	Encrypt(dst, src []byte)
}, error) {
	return des.NewCipher(vncKey(pass))
}
