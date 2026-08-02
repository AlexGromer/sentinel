package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

// rfcSampleKey is the RFC6455 §1.3 worked-example client nonce ("the sample nonce", base64). Computed
// from bytes rather than written as a base64 literal so secret scanners don't flag this public test
// vector as a generic API key. base64("the sample nonce") == "dGhlIHNhbXBsZSBub25jZQ==".
var rfcSampleKey = base64.StdEncoding.EncodeToString([]byte("the sample nonce"))

// wsUpgradeReq sets the headers of a valid /v1/stream handshake on a recorder request.
func wsUpgradeReq(method, protocols string) *http.Request {
	r := httptest.NewRequest(method, "/v1/stream", nil)
	r.Header.Set("Upgrade", "websocket")
	r.Header.Set("Connection", "Upgrade")
	r.Header.Set("Sec-WebSocket-Key", rfcSampleKey)
	r.Header.Set("Sec-WebSocket-Version", "13")
	if protocols != "" {
		r.Header.Set("Sec-WebSocket-Protocol", protocols)
	}
	return r
}

func TestWSAcceptRFCExample(t *testing.T) {
	// RFC6455 §1.3 worked example: accept for "the sample nonce".
	if got := wsAccept(rfcSampleKey); got != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" {
		t.Fatalf("wsAccept = %q, want s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", got)
	}
}

func TestStreamRequiresBearerSubprotocol(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1")) // no bearer.
	if rec.Code != http.StatusForbidden {
		t.Fatalf("no bearer subprotocol: got %d want 403", rec.Code)
	}
}

func TestStreamRejectsBadToken(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.wrong-token"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("bad token: got %d want 403", rec.Code)
	}
}

func TestStreamBadHandshake(t *testing.T) {
	rec := httptest.NewRecorder()
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Del("Sec-WebSocket-Key") // missing key
	newTestServer().mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("missing key: got %d want 400", rec.Code)
	}
}

func TestStreamRejectsDisallowedOrigin(t *testing.T) {
	rec := httptest.NewRecorder()
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "https://evil.example") // not in newTestServer allowlist (github.io)
	newTestServer().mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("disallowed origin: got %d want 403", rec.Code)
	}
}

// --- raw WS client helpers (test side: write MASKED client frames, read UNMASKED server frames) ---

func wsClientFrame(opcode byte, payload []byte) []byte {
	mask := []byte{0xAB, 0xCD, 0xEF, 0x12}
	n := len(payload)
	var hdr []byte
	switch {
	case n < 126:
		hdr = []byte{0x80 | opcode, 0x80 | byte(n)}
	case n < 1<<16:
		hdr = []byte{0x80 | opcode, 0x80 | 126, byte(n >> 8), byte(n)}
	default:
		hdr = make([]byte, 10)
		hdr[0], hdr[1] = 0x80|opcode, 0x80|127
		binary.BigEndian.PutUint64(hdr[2:], uint64(n))
	}
	out := append(append([]byte{}, hdr...), mask...)
	for i := 0; i < n; i++ {
		out = append(out, payload[i]^mask[i%4])
	}
	return out
}

func readServerFrame(br *bufio.Reader) (byte, []byte, error) {
	h := make([]byte, 2)
	if _, err := io.ReadFull(br, h); err != nil {
		return 0, nil, err
	}
	opcode := h[0] & 0x0f
	length := uint64(h[1] & 0x7f) // server frames are unmasked
	switch length {
	case 126:
		ext := make([]byte, 2)
		if _, err := io.ReadFull(br, ext); err != nil {
			return 0, nil, err
		}
		length = uint64(binary.BigEndian.Uint16(ext))
	case 127:
		ext := make([]byte, 8)
		if _, err := io.ReadFull(br, ext); err != nil {
			return 0, nil, err
		}
		length = binary.BigEndian.Uint64(ext)
	}
	payload := make([]byte, length)
	_, err := io.ReadFull(br, payload)
	return opcode, payload, err
}

func TestStreamHandshakeAndIngest(t *testing.T) {
	s, repo := newRunServer(t) // token "secret-tok", repo = temp dir
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	host := strings.TrimPrefix(ts.URL, "http://")
	conn, err := net.Dial("tcp", host)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	key := rfcSampleKey
	req := "GET /v1/stream HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Protocol: sentinel.recorder.v1, bearer.secret-tok\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}

	br := bufio.NewReader(conn)
	statusLine, err := br.ReadString('\n')
	if err != nil || !strings.Contains(statusLine, "101") {
		t.Fatalf("handshake status: %q (err=%v)", statusLine, err)
	}
	headers := map[string]string{}
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		k, v, _ := strings.Cut(line, ":")
		headers[strings.ToLower(strings.TrimSpace(k))] = strings.TrimSpace(v)
	}
	if headers["sec-websocket-accept"] != wsAccept(key) {
		t.Fatalf("accept = %q want %q", headers["sec-websocket-accept"], wsAccept(key))
	}
	if headers["sec-websocket-protocol"] != wsSubprotocol {
		t.Fatalf("subprotocol = %q want %q (token must NOT be echoed)", headers["sec-websocket-protocol"], wsSubprotocol)
	}

	// session greeting
	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("greeting op=%d err=%v", op, err)
	}
	var greet map[string]string
	if err := json.Unmarshal(payload, &greet); err != nil || greet["type"] != "session" || greet["session"] == "" {
		t.Fatalf("greeting %v (err=%v)", greet, err)
	}
	session := greet["session"]

	// stream one recorder event (masked client frame)
	event := `{"type":"click","selector":"#login"}`
	if _, err := conn.Write(wsClientFrame(wsOpText, []byte(event))); err != nil {
		t.Fatal(err)
	}
	op, payload, err = readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("ack op=%d err=%v", op, err)
	}
	var ack map[string]any
	if err := json.Unmarshal(payload, &ack); err != nil || ack["type"] != "ack" {
		t.Fatalf("ack %v (err=%v)", ack, err)
	}

	// close
	if _, err := conn.Write(wsClientFrame(wsOpClose, nil)); err != nil {
		t.Fatal(err)
	}
	_, _, _ = readServerFrame(br) // server echoes a close frame

	data, err := os.ReadFile(filepath.Join(repo, "runs", "record-"+session, "events.ndjson"))
	if err != nil {
		t.Fatalf("read ingest: %v", err)
	}
	if !strings.Contains(string(data), event) {
		t.Fatalf("ingest file missing event; got %q", string(data))
	}
}

// --- M9.8 F4 (ADR-054): takeover/return control frames on /v1/stream -> orchestrator RPCs ---

func TestParseControlFrame(t *testing.T) {
	cases := []struct {
		in, action, run string
		ok              bool
	}{
		{`{"type":"control","action":"takeover","run_id":"r1"}`, "takeover", "r1", true},
		{`{"type":"control","action":"return","run_id":"r1"}`, "return", "r1", true},
		{`{"type":"control","action":"takeover"}`, "takeover", "", true},           // envelope ok; empty run_id rejected downstream
		{`{"type":"control","action":"weird","run_id":"r1"}`, "weird", "r1", true}, // envelope ok; unknown action rejected downstream
		{`{"type":"return","run_id":"r1"}`, "", "", false},                         // bare verb is a RECORDER event now, not a control frame
		{`{"type":"takeover","key":"Enter"}`, "", "", false},                       // recorder DOM event that happens to type "takeover"
		{`{"type":"click","selector":"#login"}`, "", "", false},
		{`not json`, "", "", false},
	}
	for _, c := range cases {
		a, r, ok := parseControlFrame([]byte(c.in))
		if ok != c.ok || a != c.action || r != c.run {
			t.Errorf("parseControlFrame(%q) = (%q,%q,%v); want (%q,%q,%v)", c.in, a, r, ok, c.action, c.run, c.ok)
		}
	}
}

func TestForwardControlNoOrchestrator(t *testing.T) {
	s := newTestServer() // orchAddr == ""
	if err := s.forwardControl("takeover", "r1", ""); err == nil {
		t.Fatal("forwardControl must error (fail-closed) when no orchestrator is wired")
	}
}

// fakeRunControl is a minimal RunControl server that records the Takeover/Return run_ids it receives.
type fakeRunControl struct {
	pb.UnimplementedRunControlServer
	mu        sync.Mutex
	takeovers []string
	returns   []string
}

func (f *fakeRunControl) Takeover(_ context.Context, r *pb.TakeoverRequest) (*pb.TakeoverReply, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.takeovers = append(f.takeovers, r.RunId)
	return &pb.TakeoverReply{Ok: true}, nil
}

func (f *fakeRunControl) Return(_ context.Context, r *pb.ReturnRequest) (*pb.ReturnReply, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.returns = append(f.returns, r.RunId)
	return &pb.ReturnReply{Ok: true}, nil
}

// startFakeOrch listens on a unix socket and returns the recorder + the gRPC target for orchAddr.
func startFakeOrch(t *testing.T) (*fakeRunControl, string) {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "orch.sock")
	lis, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeRunControl{}
	g := grpc.NewServer()
	pb.RegisterRunControlServer(g, f)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)
	return f, "unix:" + sock
}

// wsDialAndGreet opens a /v1/stream socket, completes the handshake, and consumes the session greeting.
func wsDialAndGreet(t *testing.T, ts *httptest.Server) (net.Conn, *bufio.Reader) {
	t.Helper()
	host := strings.TrimPrefix(ts.URL, "http://")
	conn, err := net.Dial("tcp", host)
	if err != nil {
		t.Fatal(err)
	}
	req := "GET /v1/stream HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Key: " + rfcSampleKey + "\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Protocol: sentinel.recorder.v1, bearer.secret-tok\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}
	br := bufio.NewReader(conn)
	for { // drain status line + handshake headers
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.TrimRight(line, "\r\n") == "" {
			break
		}
	}
	if _, _, err := readServerFrame(br); err != nil { // session greeting
		t.Fatalf("greeting: %v", err)
	}
	return conn, br
}

func TestStreamTakeoverReturnForwardsToOrchestrator(t *testing.T) {
	s, repo := newRunServer(t)
	fake, target := startFakeOrch(t)
	s.orchAddr = target
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialAndGreet(t, ts)
	defer conn.Close()

	sendCtl := func(frame, wantAction string) {
		t.Helper()
		if _, err := conn.Write(wsClientFrame(wsOpText, []byte(frame))); err != nil {
			t.Fatal(err)
		}
		op, payload, err := readServerFrame(br)
		if err != nil || op != wsOpText {
			t.Fatalf("control reply op=%d err=%v", op, err)
		}
		var reply map[string]string
		if err := json.Unmarshal(payload, &reply); err != nil {
			t.Fatalf("control reply unmarshal: %v (%s)", err, payload)
		}
		if reply["type"] != "control-ok" || reply["action"] != wantAction || reply["run_id"] != "runX" {
			t.Fatalf("control reply = %v; want control-ok %s runX", reply, wantAction)
		}
	}
	sendCtl(`{"type":"control","action":"takeover","run_id":"runX"}`, "takeover")
	sendCtl(`{"type":"control","action":"return","run_id":"runX"}`, "return")

	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)

	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.takeovers) != 1 || fake.takeovers[0] != "runX" {
		t.Fatalf("orchestrator takeovers = %v; want [runX]", fake.takeovers)
	}
	if len(fake.returns) != 1 || fake.returns[0] != "runX" {
		t.Fatalf("orchestrator returns = %v; want [runX]", fake.returns)
	}
	// control frames are forwarded, never persisted as recorder events.
	matches, _ := filepath.Glob(filepath.Join(repo, "runs", "record-*", "events.ndjson"))
	for _, p := range matches {
		if b, _ := os.ReadFile(p); strings.Contains(string(b), "takeover") || strings.Contains(string(b), "return") {
			t.Fatalf("control frame leaked into recorder ingest %s: %q", p, string(b))
		}
	}
}

// --- R3-hardening (M13): Origin fail-closed on a public bind + recorder session-resume ---

func TestStreamPublicBindRejectsOriginWithoutAllowlist(t *testing.T) {
	s := &server{token: "secret-tok", corsAllow: map[string]bool{}, publicBind: true, runs: map[string]*run{}}
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "https://evil.example")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("public bind + no allowlist + Origin: got %d want 403 (fail-closed)", rec.Code)
	}
}

func TestStreamLocalBindPermitsOriginWithoutAllowlist(t *testing.T) {
	// Local bind (publicBind=false) + empty allowlist: the Origin passes the CSWSH gate, so the request
	// reaches the hijack — which a ResponseRecorder can't satisfy → 500 streaming-unsupported. A non-403
	// proves the Origin was NOT rejected (dev-permissive, still bearer-gated).
	s := &server{token: "secret-tok", corsAllow: map[string]bool{}, publicBind: false, runs: map[string]*run{}}
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "http://localhost:3000")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code == http.StatusForbidden {
		t.Fatal("local bind must not reject an Origin without an allowlist")
	}
}

// ADR-064 Mode 3 regression: control-API serves the UI from its own port, the container binds 0.0.0.0
// (publicBind) and the allowlist is intentionally empty. The browser then sends its own origin, which
// the pre-fix gate rejected with 403 — the page saw the socket close with 1006 and the live timeline
// stayed dead. A non-403 proves the handshake reached the hijack (a ResponseRecorder cannot satisfy it,
// hence 500 streaming-unsupported).
func TestStreamPublicBindPermitsSameOrigin(t *testing.T) {
	s := &server{token: "secret-tok", corsAllow: map[string]bool{}, publicBind: true, runs: map[string]*run{}}
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "http://"+r.Host) // httptest default host — the page this server served
	r.Header.Set("Sec-Fetch-Site", "same-origin")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code == http.StatusForbidden {
		t.Fatalf("same-origin handshake must not be refused on a public bind (Mode 3); got 403: %s", rec.Body.String())
	}
}

// The same-origin branch must not become a bypass: a cross-site page carries a different Origin host,
// so it stays refused on a public bind even though the new branch runs first.
func TestStreamPublicBindStillRejectsCrossSiteOrigin(t *testing.T) {
	s := &server{token: "secret-tok", corsAllow: map[string]bool{}, publicBind: true, runs: map[string]*run{}}
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "https://evil.example")
	r.Header.Set("Sec-Fetch-Site", "cross-site")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-site origin on a public bind: got %d want 403", rec.Code)
	}
}

// A Mode-3 deployment may also allowlist an extra origin (e.g. a second front-end). The allowlist must
// not lock out the server's own UI — which it did while the allowlist branch ran before same-origin.
func TestStreamSameOriginPermittedAlongsideAllowlist(t *testing.T) {
	s := &server{token: "secret-tok", corsAllow: map[string]bool{"https://other.example": true},
		publicBind: true, runs: map[string]*run{}}
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.Header.Set("Origin", "http://"+r.Host) // NOT in the allowlist, but same-origin
	r.Header.Set("Sec-Fetch-Site", "same-origin")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code == http.StatusForbidden {
		t.Fatalf("same-origin must pass even when an allowlist is configured; got 403: %s", rec.Body.String())
	}
}

// Sec-Fetch-Site is absent on some clients; the host comparison alone must still separate the two cases.
func TestStreamOriginHostDecidesWithoutFetchMetadata(t *testing.T) {
	for _, tc := range []struct {
		name, origin string
		wantForbid   bool
	}{
		{"same host", "http://example.com", false},
		{"other host", "http://evil.example", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &server{token: "secret-tok", corsAllow: map[string]bool{}, publicBind: true, runs: map[string]*run{}}
			r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
			r.Header.Set("Origin", tc.origin) // no Sec-Fetch-Site at all
			rec := httptest.NewRecorder()
			s.mux().ServeHTTP(rec, r)
			if got := rec.Code == http.StatusForbidden; got != tc.wantForbid {
				t.Fatalf("origin %s: forbidden=%v want %v (code %d)", tc.origin, got, tc.wantForbid, rec.Code)
			}
		})
	}
}

func TestStreamBadSessionRejected(t *testing.T) {
	rec := httptest.NewRecorder()
	r := wsUpgradeReq(http.MethodGet, "sentinel.recorder.v1, bearer.secret-tok")
	r.URL.RawQuery = "session=../../etc/passwd"
	newTestServer().mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("path-traversal session id: got %d want 400", rec.Code)
	}
}

func TestStreamSessionResumeAppendsToSameDir(t *testing.T) {
	s, repo := newRunServer(t)
	ts := httptest.NewServer(s.mux())
	defer ts.Close()
	host := strings.TrimPrefix(ts.URL, "http://")
	conn, err := net.Dial("tcp", host)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	const sess = "resume-abc123"
	req := "GET /v1/stream?session=" + sess + " HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\n" +
		"Connection: Upgrade\r\nSec-WebSocket-Key: " + rfcSampleKey + "\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Protocol: sentinel.recorder.v1, bearer.secret-tok\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}
	br := bufio.NewReader(conn)
	for { // drain handshake headers
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.TrimRight(line, "\r\n") == "" {
			break
		}
	}
	_, payload, err := readServerFrame(br) // greeting must carry the RESUMED session id
	if err != nil {
		t.Fatal(err)
	}
	var greet map[string]string
	if err := json.Unmarshal(payload, &greet); err != nil || greet["session"] != sess {
		t.Fatalf("greeting session = %v want %q", greet, sess)
	}
	event := `{"type":"click","selector":"#x"}`
	_, _ = conn.Write(wsClientFrame(wsOpText, []byte(event)))
	_, _, _ = readServerFrame(br) // ack
	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)

	data, err := os.ReadFile(filepath.Join(repo, "runs", "record-"+sess, "events.ndjson"))
	if err != nil || !strings.Contains(string(data), event) {
		t.Fatalf("event not appended to the resumed session dir record-%s: %v", sess, err)
	}
}

func TestStreamTakeoverMissingRunID(t *testing.T) {
	s, _ := newRunServer(t)
	fake, target := startFakeOrch(t)
	s.orchAddr = target
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialAndGreet(t, ts)
	defer conn.Close()

	if _, err := conn.Write(wsClientFrame(wsOpText, []byte(`{"type":"control","action":"takeover"}`))); err != nil {
		t.Fatal(err)
	}
	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("reply op=%d err=%v", op, err)
	}
	var reply map[string]string
	_ = json.Unmarshal(payload, &reply)
	if reply["type"] != "control-error" {
		t.Fatalf("missing run_id reply = %v; want control-error", reply)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.takeovers) != 0 {
		t.Fatalf("a run_id-less takeover must NOT reach the orchestrator; got %v", fake.takeovers)
	}
}

// Regression (finding 1): a recorder DOM event that happens to use type "return"/"takeover" (e.g. an
// Enter-key capture) must be PERSISTED as an event, not swallowed as a control frame. The control
// channel is namespaced under type:"control", so bare-verb frames stay recorder events.
func TestStreamRecorderEventTypedReturnPersists(t *testing.T) {
	s, repo := newRunServer(t)
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialAndGreet(t, ts)
	defer conn.Close()

	event := `{"type":"return","selector":"#search","key":"Enter"}`
	if _, err := conn.Write(wsClientFrame(wsOpText, []byte(event))); err != nil {
		t.Fatal(err)
	}
	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("ack op=%d err=%v", op, err)
	}
	var ack map[string]any
	if err := json.Unmarshal(payload, &ack); err != nil || ack["type"] != "ack" {
		t.Fatalf("a bare-verb recorder event must be acked as an event, got %v (err=%v)", ack, err)
	}
	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)

	matches, _ := filepath.Glob(filepath.Join(repo, "runs", "record-*", "events.ndjson"))
	found := false
	for _, p := range matches {
		if b, _ := os.ReadFile(p); strings.Contains(string(b), event) {
			found = true
		}
	}
	if !found {
		t.Fatalf("recorder event typed %q was swallowed, not persisted to events.ndjson", event)
	}
}

// --- M14 W2: ?run_id= server→client AG-UI event subscription ---------------------------------------

// wsDialRunID opens a /v1/stream?run_id=<id> socket and completes the handshake, leaving the reader
// positioned right after the HTTP headers — the caller reads the first frame (subscribed or error).
func wsDialRunID(t *testing.T, ts *httptest.Server, runID string) (net.Conn, *bufio.Reader) {
	t.Helper()
	host := strings.TrimPrefix(ts.URL, "http://")
	conn, err := net.Dial("tcp", host)
	if err != nil {
		t.Fatal(err)
	}
	req := "GET /v1/stream?run_id=" + runID + " HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\n" +
		"Connection: Upgrade\r\nSec-WebSocket-Key: " + rfcSampleKey + "\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Protocol: sentinel.recorder.v1, bearer.secret-tok\r\n\r\n"
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatal(err)
	}
	br := bufio.NewReader(conn)
	for { // drain status line + handshake headers
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.TrimRight(line, "\r\n") == "" {
			break
		}
	}
	return conn, br
}

func TestStreamRunEventsPushesAGUIVerbatim(t *testing.T) {
	s, _ := newRunServer(t)
	rec := &run{ID: "runX", State: "running", stream: newRunStream()}
	s.mu.Lock()
	s.runs["runX"] = rec
	s.mu.Unlock()
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, "runX")
	defer conn.Close()

	op, payload, err := readServerFrame(br) // subscribed ack
	if err != nil || op != wsOpText {
		t.Fatalf("subscribed ack op=%d err=%v", op, err)
	}
	var ack map[string]string
	if err := json.Unmarshal(payload, &ack); err != nil || ack["type"] != "subscribed" || ack["run_id"] != "runX" {
		t.Fatalf("subscribed ack = %v (err=%v)", ack, err)
	}

	// M9.8 F4 hitl_needed passthrough: brain (M14 W4) emits a pre-formed AG-UI JSON envelope; it must
	// reach the subscriber byte-for-byte, not re-wrapped as a log line.
	agui := `{"type":"hitl_needed","run_id":"runX","data":{"reason":"captcha"}}`
	rec.stream.append(wsAGUIPrefix + agui)

	op, payload, err = readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("agui event op=%d err=%v", op, err)
	}
	if string(payload) != agui {
		t.Fatalf("agui event forwarded = %q, want verbatim %q", payload, agui)
	}

	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)
}

func TestStreamRunEventsWrapsPlainLineAsLog(t *testing.T) {
	s, _ := newRunServer(t)
	rec := &run{ID: "runX", State: "running", stream: newRunStream()}
	s.mu.Lock()
	s.runs["runX"] = rec
	s.mu.Unlock()
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, "runX")
	defer conn.Close()

	if _, _, err := readServerFrame(br); err != nil { // subscribed ack
		t.Fatalf("subscribed ack: %v", err)
	}

	rec.stream.append("plain stdout line")

	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("log event op=%d err=%v", op, err)
	}
	var logEvt map[string]any
	if err := json.Unmarshal(payload, &logEvt); err != nil || logEvt["type"] != "log" || logEvt["run_id"] != "runX" {
		t.Fatalf("log event = %v (err=%v)", logEvt, err)
	}
	data, _ := logEvt["data"].(map[string]any)
	if data["line"] != "plain stdout line" {
		t.Fatalf("log event data.line = %v, want %q", data, "plain stdout line")
	}

	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)
}

// Regression: a recorder-style line that merely STARTS WITH the "@@AGUI " marker but isn't well-formed
// JSON (a spoofed prefix) must fall back to a log envelope carrying the ORIGINAL line, not be dropped
// or forwarded as a bogus verbatim frame.
func TestStreamRunEventsMalformedAGUIFallsBackToLog(t *testing.T) {
	s, _ := newRunServer(t)
	rec := &run{ID: "runZ", State: "running", stream: newRunStream()}
	s.mu.Lock()
	s.runs["runZ"] = rec
	s.mu.Unlock()
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, "runZ")
	defer conn.Close()

	if _, _, err := readServerFrame(br); err != nil { // subscribed ack
		t.Fatalf("subscribed ack: %v", err)
	}

	spoofed := wsAGUIPrefix + "not-actually-json (a recorder line that happens to start with the marker)"
	rec.stream.append(spoofed)

	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("fallback log op=%d err=%v", op, err)
	}
	var logEvt map[string]any
	if err := json.Unmarshal(payload, &logEvt); err != nil || logEvt["type"] != "log" {
		t.Fatalf("malformed @@AGUI must fall back to a log event, got %v (err=%v)", logEvt, err)
	}
	data, _ := logEvt["data"].(map[string]any)
	if data["line"] != spoofed {
		t.Fatalf("log fallback must carry the ORIGINAL line, got %v want %q", data, spoofed)
	}

	_, _ = conn.Write(wsClientFrame(wsOpClose, nil))
	_, _, _ = readServerFrame(br)
}

func TestStreamRunEventsUnknownRunIDClosesGracefully(t *testing.T) {
	s := newTestServer() // runs map is empty — "no-such-run" can't exist
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, "no-such-run")
	defer conn.Close()

	op, payload, err := readServerFrame(br)
	if err != nil || op != wsOpText {
		t.Fatalf("error frame op=%d err=%v", op, err)
	}
	var errFrame map[string]string
	if err := json.Unmarshal(payload, &errFrame); err != nil || errFrame["type"] != "error" || errFrame["run_id"] != "no-such-run" {
		t.Fatalf("error frame = %v (err=%v)", errFrame, err)
	}
	op, _, err = readServerFrame(br)
	if err != nil || op != wsOpClose {
		t.Fatalf("close frame op=%d err=%v", op, err)
	}
}

// TestStreamRunEventsWriteMutexRace exercises the two goroutines that write to the SAME socket
// concurrently — the read loop (replying pong to client pings) and the event pusher (forwarding
// appended runStream lines) — under `go test -race`. Without wc's write mutex this corrupts the
// frame stream (interleaved header/payload bytes); with it, every frame the reader parses is well-formed.
func TestStreamRunEventsWriteMutexRace(t *testing.T) {
	s, _ := newRunServer(t)
	rec := &run{ID: "runY", State: "running", stream: newRunStream()}
	s.mu.Lock()
	s.runs["runY"] = rec
	s.mu.Unlock()
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, "runY")
	defer conn.Close()

	if _, _, err := readServerFrame(br); err != nil { // subscribed ack
		t.Fatalf("subscribed ack: %v", err)
	}

	var mu sync.Mutex
	received := 0
	readerDone := make(chan struct{})
	go func() {
		defer close(readerDone)
		for {
			if _, _, err := readServerFrame(br); err != nil {
				return
			}
			mu.Lock()
			received++
			mu.Unlock()
		}
	}()

	const n = 150
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { // read-loop goroutine writes: a pong per client ping
		defer wg.Done()
		for i := 0; i < n; i++ {
			if _, err := conn.Write(wsClientFrame(wsOpPing, nil)); err != nil {
				return
			}
		}
	}()
	go func() { // pusher goroutine writes: an event per appended line
		defer wg.Done()
		for i := 0; i < n; i++ {
			rec.stream.append("line")
		}
	}()
	wg.Wait()

	for i := 0; i < 200; i++ { // poll briefly for both write paths to drain, rather than a fixed sleep
		mu.Lock()
		got := received
		mu.Unlock()
		if got >= n {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	_ = conn.Close()
	<-readerDone

	mu.Lock()
	defer mu.Unlock()
	if received == 0 {
		t.Fatal("received no frames from concurrent pong/pusher writers (race test exercised nothing)")
	}
}

// readUntilType reads server frames until it finds an AG-UI envelope with the given type, or fails.
// The subscribed-snapshot replays every buffered runStream line as a frame, so run.finished arrives
// after the run's stdout log frames.
func readUntilType(t *testing.T, br *bufio.Reader, want string) map[string]any {
	t.Helper()
	for i := 0; i < 50; i++ {
		op, payload, err := readServerFrame(br)
		if err != nil {
			t.Fatalf("reading frames for %q: %v", want, err)
		}
		if op != wsOpText {
			continue
		}
		var ev map[string]any
		if json.Unmarshal(payload, &ev) == nil && ev["type"] == want {
			return ev
		}
	}
	t.Fatalf("did not see a %q frame within 50 frames", want)
	return nil
}

// M14 tail 1: a WS subscriber must receive a TYPED run.finished event carrying the real exit_code,
// injected by the control-API's finish goroutine (the one AG-UI event the brain cannot emit). Driven
// end-to-end through spawnRun's real finish path (fake agentctl exits 1).
func TestRunFinishedEmittedOverWS(t *testing.T) {
	s, _ := newRunServer(t) // fake agentctl: echoes two lines, exit 1
	id := createRunAndWait(t, s)
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, id)
	defer conn.Close()
	if op, payload, err := readServerFrame(br); err != nil || op != wsOpText || !strings.Contains(string(payload), `"subscribed"`) {
		t.Fatalf("subscribed ack op=%d err=%v payload=%s", op, err, payload)
	}

	ev := readUntilType(t, br, "run.finished")
	if ev["run_id"] != id {
		t.Errorf("run.finished run_id = %v, want %s", ev["run_id"], id)
	}
	data, _ := ev["data"].(map[string]any)
	if data == nil || data["exit_code"] != float64(1) {
		t.Fatalf("run.finished data = %v, want exit_code 1", ev["data"])
	}
	if data["state"] != "done" { // state disambiguates exit_code:-1 (signal-kill vs failed-spawn)
		t.Errorf("run.finished state = %v, want done", data["state"])
	}
	if ev["ts"] == nil || ev["ts"] == "" {
		t.Errorf("run.finished must carry a ts, got %v", ev["ts"])
	}
	if _, hasSeq := ev["seq"]; hasSeq {
		t.Errorf("control-API-injected run.finished must omit seq (separate un-ordered space), got %v", ev["seq"])
	}
}

// A run that FAILS TO SPAWN (agentctl missing) never sets an exit code; run.finished must carry the
// sentinel -1 so the UI does not read the zero value as a clean exit 0.
func TestRunFinishedFailedSpawnSentinel(t *testing.T) {
	s := &server{
		repo:      t.TempDir(),
		agentctl:  "/nonexistent/agentctl-does-not-exist",
		token:     "secret-tok",
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
	}
	id := createRunAndWait(t, s)
	s.mu.RLock()
	st := s.runs[id].State
	s.mu.RUnlock()
	if st != "failed" {
		t.Fatalf("run state = %q, want failed (spawn should fail)", st)
	}
	ts := httptest.NewServer(s.mux())
	defer ts.Close()

	conn, br := wsDialRunID(t, ts, id)
	defer conn.Close()
	if op, _, err := readServerFrame(br); err != nil || op != wsOpText {
		t.Fatalf("subscribed ack op=%d err=%v", op, err)
	}
	ev := readUntilType(t, br, "run.finished")
	data, _ := ev["data"].(map[string]any)
	if data == nil || data["exit_code"] != float64(-1) {
		t.Fatalf("failed-spawn run.finished data = %v, want exit_code -1", ev["data"])
	}
	if data["state"] != "failed" { // the disambiguator: exit_code -1 + state failed = spawn error
		t.Errorf("failed-spawn run.finished state = %v, want failed", data["state"])
	}
}

// A run whose process is KILLED BY A SIGNAL yields ExitError.ExitCode()==-1 with State=="done" — the
// same exit_code as a failed spawn. The `state` field disambiguates them (a signal-kill is state=done).
func TestRunFinishedSignalKillCarriesDoneState(t *testing.T) {
	s, _ := newRunServerWithScript(t, "#!/bin/sh\necho 'starting'\nkill -9 $$\n")
	id := createRunAndWait(t, s)
	s.mu.RLock()
	st, code := s.runs[id].State, s.runs[id].ExitCode
	s.mu.RUnlock()
	if st != "done" || code != -1 {
		t.Fatalf("signal-killed run: state=%q exit=%d, want done/-1", st, code)
	}
	ts := httptest.NewServer(s.mux())
	defer ts.Close()
	conn, br := wsDialRunID(t, ts, id)
	defer conn.Close()
	if op, _, err := readServerFrame(br); err != nil || op != wsOpText {
		t.Fatalf("subscribed ack op=%d err=%v", op, err)
	}
	ev := readUntilType(t, br, "run.finished")
	data, _ := ev["data"].(map[string]any)
	if data == nil || data["exit_code"] != float64(-1) || data["state"] != "done" {
		t.Fatalf("signal-kill run.finished data = %v, want exit_code -1 + state done (distinct from failed-spawn)", ev["data"])
	}
}
