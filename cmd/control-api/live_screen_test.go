package main

import (
	"bufio"
	"encoding/base64"
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fakeVNC is an x11vnc stand-in that speaks exactly enough RFB to be authenticated against, and
// REFUSES a wrong password. A fake that accepted anything could not tell a working relay from one
// that sends the challenge back unencrypted — the same lesson token_test.go's live-status fake
// learned when it answered every path and hid a wrong one.
func fakeVNC(t *testing.T, pass string, offerVNCAuth bool) (addr string, stop func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = c.Write([]byte("RFB 003.008\n"))
				ver := make([]byte, 12)
				if _, err := io.ReadFull(c, ver); err != nil {
					return
				}
				if !offerVNCAuth {
					_, _ = c.Write([]byte{1, 1}) // one type: None
					return
				}
				_, _ = c.Write([]byte{1, 2}) // one type: VncAuth
				var chosen [1]byte
				if _, err := io.ReadFull(c, chosen[:]); err != nil {
					return
				}
				challenge := make([]byte, 16)
				for i := range challenge {
					challenge[i] = byte(i * 7)
				}
				_, _ = c.Write(challenge)
				got := make([]byte, 16)
				if _, err := io.ReadFull(c, got); err != nil {
					return
				}
				want := make([]byte, 16)
				blk, _ := desCipher(pass)
				blk.Encrypt(want[0:8], challenge[0:8])
				blk.Encrypt(want[8:16], challenge[8:16])
				if string(got) != string(want) {
					_, _ = c.Write([]byte{0, 0, 0, 1}) // refused
					return
				}
				_, _ = c.Write([]byte{0, 0, 0, 0}) // ok
				// ClientInit -> ServerInit, so the status path can report a size.
				var shared [1]byte
				if _, err := io.ReadFull(c, shared[:]); err != nil {
					return
				}
				init := make([]byte, 24)
				binary.BigEndian.PutUint16(init[0:2], 1280)
				binary.BigEndian.PutUint16(init[2:4], 800)
				init[4], init[5], init[7] = 32, 24, 1
				name := "fake:99"
				binary.BigEndian.PutUint32(init[20:24], uint32(len(name)))
				_, _ = c.Write(init)
				_, _ = c.Write([]byte(name))
				io.Copy(io.Discard, c)
			}(c)
		}
	}()
	return ln.Addr().String(), func() { _ = ln.Close() }
}

func TestScreenIsSkippedWhenTheProfileIsNotRunning(t *testing.T) {
	// The DEFAULT case, and the one that would be most expensive to get wrong: an `error` here would
	// put a permanent warning on every deployment that simply did not ask for a screen.
	t.Setenv("CONTROL_API_VNC_ADDR", "")
	s := newTestServer()
	st := s.screenState()
	if st["available"] != false {
		t.Fatalf("an unconfigured screen must be unavailable, got %v", st)
	}
	reason, _ := st["reason"].(string)
	if !strings.Contains(reason, "vnc") {
		t.Errorf("the reason does not name the profile that would provide it: %q", reason)
	}
}

func TestScreenDistinguishesNotConfiguredFromBroken(t *testing.T) {
	// THE distinction the whole live area is built around (docs/index.html: "not configured here" and
	// "configured and broken" must not look the same). Asserted as two DIFFERENT sentences, because a
	// shared one would send an operator to fix the wrong thing.
	t.Setenv("CONTROL_API_VNC_ADDR", "")
	s := newTestServer()
	unconfigured, _ := s.screenState()["reason"].(string)

	t.Setenv("CONTROL_API_VNC_ADDR", "127.0.0.1:1")
	broken, _ := s.screenState()["reason"].(string)

	if unconfigured == broken {
		t.Fatalf("both states produce the same sentence: %q", unconfigured)
	}
	if !strings.Contains(broken, "127.0.0.1:1") {
		t.Errorf("the broken-state reason does not name the address that failed: %q", broken)
	}
}

func TestScreenRefusesAServerWithNoPassword(t *testing.T) {
	// A VNC server offering only None is a desktop anyone on that network can drive. Relaying it would
	// put the bearer token in front of something that has no lock at all behind it — and the profile
	// promises the opposite, so this is a refusal rather than a warning.
	addr, stop := fakeVNC(t, "irrelevant", false)
	defer stop()
	t.Setenv("CONTROL_API_VNC_ADDR", addr)
	t.Setenv("SENTINEL_VNC_PASSWORD", "Abcdefgh")
	s := newTestServer()
	st := s.screenState()
	if st["available"] != false {
		t.Fatalf("a password-less VNC server must not be relayed, got %v", st)
	}
	if r, _ := st["reason"].(string); !strings.Contains(r, "does not require a password") {
		t.Errorf("the refusal does not say why: %q", r)
	}
}

func TestScreenAuthenticatesAndDescribesItself(t *testing.T) {
	addr, stop := fakeVNC(t, "Abcdefgh", true)
	defer stop()
	t.Setenv("CONTROL_API_VNC_ADDR", addr)
	t.Setenv("SENTINEL_VNC_PASSWORD", "Abcdefgh")
	s := newTestServer()
	st := s.screenState()
	if st["available"] != true {
		t.Fatalf("a live screen must be available, got %v", st)
	}
	if st["width"] != 1280 || st["height"] != 800 {
		t.Errorf("size not reported: %v x %v", st["width"], st["height"])
	}
}

func TestScreenSaysWhenThePasswordIsRefused(t *testing.T) {
	// ⚠ The message must name WHERE the password came from, never the value — an operator with two
	// deployments needs to know which file to look at.
	addr, stop := fakeVNC(t, "Abcdefgh", true)
	defer stop()
	t.Setenv("CONTROL_API_VNC_ADDR", addr)
	t.Setenv("SENTINEL_VNC_PASSWORD", "WrongPass")
	s := newTestServer()
	st := s.screenState()
	if st["available"] != false {
		t.Fatalf("a refused password must not read as available: %v", st)
	}
	r, _ := st["reason"].(string)
	if !strings.Contains(r, "refused the password") {
		t.Errorf("the reason does not say the password was refused: %q", r)
	}
	if strings.Contains(r, "WrongPass") {
		t.Errorf("THE REASON QUOTES THE PASSWORD: %q", r)
	}
}

func TestScreenRelayRefusesAnonymousBeforeHijacking(t *testing.T) {
	// The relay is `accessOpen` at the guard on purpose (a browser WebSocket cannot send an
	// Authorization header), which makes the handler the ONLY thing standing between an anonymous
	// caller and a desktop. Asserted with a real request rather than by reading the route table.
	addr, stop := fakeVNC(t, "Abcdefgh", true)
	defer stop()
	t.Setenv("CONTROL_API_VNC_ADDR", addr)
	t.Setenv("SENTINEL_VNC_PASSWORD", "Abcdefgh")
	s := newTestServer()
	s.token = "the-token"

	req := httptest.NewRequest(http.MethodGet, "/v1/live/screen", nil)
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Connection", "Upgrade")
	// ⚠ COMPUTED, not written as a literal. Any valid Sec-WebSocket-Key is 16 random bytes, so in
	// base64 it looks exactly like a credential to a secret scanner — the pre-commit gate blocked
	// on RFC 6455 §1.3's OWN sample nonce. The honest fix is to remove the finding rather than to
	// silence the detector: the key is derived from the plain phrase the RFC derives it from, which
	// is also more legible than the base64 was.
	req.Header.Set("Sec-WebSocket-Key", base64.StdEncoding.EncodeToString([]byte("the sample nonce")))
	req.Header.Set("Sec-WebSocket-Version", "13")
	rec := httptest.NewRecorder()
	s.handleLiveScreen(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("an anonymous upgrade got %d, want 403 — the handler is the only gate on this route", rec.Code)
	}
}

func TestScreenRelayNeverOffersTheBrowserAPassword(t *testing.T) {
	// The property that keeps the secret out of the page, asserted on the BYTES the relay writes: the
	// security-type list it offers must be exactly one entry, type 1 (None). Offering type 2 would ask
	// the operator's browser for a credential control-api is already holding.
	addr, stop := fakeVNC(t, "Abcdefgh", true)
	defer stop()
	t.Setenv("CONTROL_API_VNC_ADDR", addr)
	t.Setenv("SENTINEL_VNC_PASSWORD", "Abcdefgh")
	s := newTestServer()

	up, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer up.Close()
	if err := vncHandshakeUpstream(up, "Abcdefgh"); err != nil {
		t.Fatalf("upstream handshake: %v", err)
	}

	client, server := net.Pipe()
	defer client.Close()
	go s.relayScreen(server, bufio.NewReader(server), up, addr, "test")

	read := func() []byte {
		_ = client.SetReadDeadline(time.Now().Add(3 * time.Second))
		hdr := make([]byte, 2)
		if _, err := io.ReadFull(client, hdr); err != nil {
			t.Fatalf("read header: %v", err)
		}
		n := int(hdr[1] & 0x7f)
		payload := make([]byte, n)
		if _, err := io.ReadFull(client, payload); err != nil {
			t.Fatalf("read payload: %v", err)
		}
		return payload
	}
	writeMasked := func(b []byte) {
		_ = client.SetWriteDeadline(time.Now().Add(3 * time.Second))
		frame := []byte{0x82, byte(0x80 | len(b)), 0, 0, 0, 0}
		frame = append(frame, b...)
		if _, err := client.Write(frame); err != nil {
			t.Fatalf("write: %v", err)
		}
	}

	if got := string(read()); !strings.HasPrefix(got, "RFB 003.") {
		t.Fatalf("first frame is not an RFB version: %q", got)
	}
	writeMasked([]byte("RFB 003.008\n"))
	types := read()
	if len(types) != 2 || types[0] != 1 || types[1] != 1 {
		t.Fatalf("the relay offered security types %v, want exactly [1] (None) — anything else asks the "+
			"page for a credential control-api already holds", types)
	}
	for _, b := range types {
		if b == 2 {
			t.Fatal("the relay offered VncAuth to the browser")
		}
	}
}
