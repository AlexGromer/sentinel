package main

// The real screen, relayed (LIVE-VNC, ADR-127).
//
// The `vnc` profile puts a HEADED Chromium on a virtual X display and exports it over RFB — on a UNIX
// SOCKET in the shared ./state mount, with no TCP listener anywhere. control-api becomes a WebSocket
// SERVER for the page and a unix-socket CLIENT for x11vnc.
//
// ⚠ THERE IS NO PASSWORD AND NO DES, AND THAT IS THE SECURITY DECISION OF THIS FILE.
//
// The first version served RFB over TCP with the protocol's classic "VNC Authentication", and CodeQL
// was right to flag it: that scheme is DES over the FIRST EIGHT BYTES of the password, over an
// unencrypted channel. Reclassifying the alert as "mandated by the protocol" would have been true and
// beside the point — the rule here is that weak ciphers do not ship, so the algorithm had to go.
//
// What replaced it is stronger, not weaker. Measured 2026-08-18 on this image (x11vnc 0.9.16):
//
//	-unixsock <path> -rfbport 0  →  /proc/net/tcp EMPTY: no listening port at all
//	                             →  security types [1] = None: DES appears nowhere
//	                             →  a full session works (ServerInit + 655 360 bytes of pixels)
//	socket at mode 0600          →  a foreign uid gets EACCES on connect; the owner connects
//
// So the access control is FILE PERMISSIONS, enforced by the kernel before a byte of RFB is spoken.
// It also closes a surface measured the day before: the old RFB port answered on the container's
// bridge IP straight from the host, so "not published" never meant "not reachable from this machine".
// A unix socket cannot be reached that way at all.
//
// The three properties the relay exists for are unchanged:
//
//  1. the bearer token is the only way in from a browser;
//  2. nothing secret reaches the page — there is no longer a secret to reach it;
//  3. taking the mouse is OBSERVABLE, because the relay sees the bytes: `service.screen_control_taken`
//     records what happened rather than what the interface claimed.
//
// WHY NO NEW PROTOCOL CODE. Frames written to the browser are server→client, i.e. UNMASKED, which
// `wsWriteFrame` already does; frames read from it are client→server, i.e. masked, which
// `wsReadClientFrame` already unmasks. The upstream side is a plain socket. Nothing was added to
// go.mod — the obstacle ADR-111 hit (no websocket library, and dragging one in has broken the
// air-gapped build) does not arise here.

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// vncSock is the path of the RFB unix socket the `vnc` profile serves. Empty means the profile is not
// part of this deployment — the default, and a true answer rather than an error.
func vncSock() string { return strings.TrimSpace(os.Getenv("CONTROL_API_VNC_SOCK")) }

// vncServerInfo is what the RFB ServerInit message tells us about the screen.
type vncServerInfo struct {
	Width, Height int
	Desktop       string
}

// vncSocketMode reports the socket's permission bits, because THEY are the authentication. A socket
// that has been widened to 0666 authenticates nobody, and the product should say so rather than serve
// a desktop to whoever asks — the check exists so that the claim "permissions are the access control"
// is verified at runtime instead of merely written down.
func vncSocketMode(path string) (os.FileMode, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	return fi.Mode().Perm(), nil
}

// vncHandshakeUpstream performs the RFB 3.8 handshake against x11vnc and stops right after
// SecurityResult, leaving the connection where a client would be: waiting to send ClientInit.
//
// Errors are phrased for a PERSON, because every one of them ends up in the `reason` a human reads in
// the hub. "the socket refused us" and "the server wants a password" are different problems with
// different fixes, and one flat "screen unavailable" would hide which.
func vncHandshakeUpstream(conn net.Conn) error {
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
	hasNone := false
	for _, t := range types {
		if t == 1 {
			hasNone = true
		}
	}
	if !hasNone {
		// ⚠ A server that will not accept `None` wants VNC Authentication, i.e. DES over eight bytes.
		// We do not implement it and will not: the refusal is the point of this file. Over a 0600 unix
		// socket the kernel has already decided who may connect, so a password would add a weak cipher
		// to a boundary that does not need one.
		return fmt.Errorf("the VNC server offers security types %v but not 1 (None) — it is asking for "+
			"VNC Authentication, which is DES over eight bytes of a password. This relay speaks to a "+
			"unix socket whose 0600 permissions ARE the access control, and deliberately implements no "+
			"weak cipher; start x11vnc with `-unixsock <path> -rfbport 0` and no password", types)
	}
	if _, err := conn.Write([]byte{1}); err != nil { // choose None
		return fmt.Errorf("could not choose the None security type: %w", err)
	}
	var res [4]byte
	if _, err := io.ReadFull(conn, res[:]); err != nil {
		return fmt.Errorf("no security result: %w", err)
	}
	if binary.BigEndian.Uint32(res[:]) != 0 {
		return fmt.Errorf("the VNC server rejected the connection after the handshake")
	}
	_ = conn.SetDeadline(time.Time{})
	return nil
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
	if vncSock() == "" {
		return false
	}
	cdp := strings.TrimSpace(os.Getenv("PW_CDP_ENDPOINT"))
	if cdp == "" {
		return false
	}
	// ⚠ WEAKER EVIDENCE THAN BEFORE, AND SAID SO. With a TCP endpoint the screen's host could be
	// compared with the CDP host directly. A unix socket has no host, so the only thing left to compare
	// is the SERVICE NAME the runs are pointed at: `browser-vnc` is the service that owns this socket
	// (it is the one that mounts ./state and runs x11vnc). This is an inference about a deployment
	// convention, which is exactly why the hub shows it as a warning strip and never as a refusal.
	want := strings.TrimSpace(os.Getenv("CONTROL_API_VNC_SERVICE"))
	if want == "" {
		want = "browser-vnc"
	}
	if u, err := url.Parse(cdp); err == nil && u.Host != "" {
		return strings.EqualFold(u.Hostname(), want)
	}
	return strings.Contains(cdp, want)
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
	addr := vncSock()
	if addr == "" {
		return map[string]any{
			"available": false,
			"reason":    "no VNC screen configured (CONTROL_API_VNC_SOCK is unset) — this deployment did not start the `vnc` compose profile",
			"attached":  false,
		}
	}
	// The permissions ARE the access control, so a widened socket is reported rather than used. A
	// screen served to whoever asks is not the feature this profile promises, and finding out from a
	// health view beats finding out from an incident.
	if mode, merr := vncSocketMode(addr); merr == nil && mode&0o077 != 0 {
		return map[string]any{
			"available": false,
			"reason": fmt.Sprintf("the VNC socket %s is mode %#o — group/other can reach it. Access "+
				"control for this screen IS the socket's permissions (there is no password by design), "+
				"so a widened socket means an unguarded desktop. Restore 0600.", addr, mode),
			"attached": false,
		}
	}
	conn, err := net.DialTimeout("unix", addr, 5*time.Second)
	if err != nil {
		return map[string]any{
			"available": false,
			"reason":    fmt.Sprintf("the VNC screen at %s did not answer: %v", addr, err),
			"attached":  false,
		}
	}
	defer conn.Close()
	if err := vncHandshakeUpstream(conn); err != nil {
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
		"auth":      "no password: the 0600 unix socket is the access control, and the browser is offered None behind the bearer token",
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
	addr := vncSock()
	if addr == "" {
		// 501, not 404: the route exists and this deployment does not implement it — the same
		// distinction proxyLive makes, and the one the hub turns into a sentence naming the profile.
		writeJSON(w, http.StatusNotImplemented, map[string]string{
			"error": "no VNC screen configured (CONTROL_API_VNC_SOCK is unset) — start the `vnc` compose profile",
		})
		return
	}

	up, err := net.DialTimeout("unix", addr, 5*time.Second)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("the VNC screen at %s did not answer: %v", addr, err)})
		return
	}
	defer up.Close()
	if err := vncHandshakeUpstream(up); err != nil {
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
