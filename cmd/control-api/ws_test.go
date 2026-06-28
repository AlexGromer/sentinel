package main

import (
	"bufio"
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
	"testing"
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
