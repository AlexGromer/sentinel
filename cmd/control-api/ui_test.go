package main

import (
	"encoding/json"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	webui "github.com/AlexGromer/sentinel/docs"
)

func uiTestServer(t *testing.T, u *uiServer) *server {
	t.Helper()
	return &server{
		repo:      ".",
		agentctl:  "/nonexistent/agentctl",
		token:     "secret-tok",
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
		ui:        u,
	}
}

func enabledUI(t *testing.T) *uiServer {
	t.Helper()
	t.Setenv("CONTROL_API_UI_DIR", "")
	t.Setenv("CONTROL_API_SERVE_UI", "1")
	u := newUIServer()
	if !u.enabled || u.source != "embedded" {
		t.Fatalf("newUIServer: enabled=%v source=%q, want an enabled embedded UI", u.enabled, u.source)
	}
	return u
}

func get(t *testing.T, h http.Handler, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, target, nil))
	return rec
}

// ---------------------------------------------------------------------------------------------
// Modes 1 and 2 must be byte-for-byte the mux they were before ADR-064.
// ---------------------------------------------------------------------------------------------

func TestUIDisabledByDefault(t *testing.T) {
	t.Setenv("CONTROL_API_SERVE_UI", "")
	t.Setenv("CONTROL_API_UI_DIR", "")
	u := newUIServer()
	if u.enabled {
		t.Fatal("the UI must be strictly opt-in")
	}
	h := uiTestServer(t, u).mux()
	if rec := get(t, h, "/"); rec.Code != http.StatusNotFound {
		t.Errorf("GET / with the UI off = %d, want 404", rec.Code)
	}
	if rec := get(t, h, "/v1/ui-token?nonce=x"); rec.Code != http.StatusNotFound {
		t.Errorf("bootstrap endpoint is reachable with the UI off (%d)", rec.Code)
	}
	// A nil ui (every pre-existing test constructs one) must not panic either.
	if rec := get(t, uiTestServer(t, nil).mux(), "/healthz"); rec.Code != http.StatusOK {
		t.Errorf("healthz with a nil ui = %d, want 200", rec.Code)
	}
}

func TestUIEnabledOnlyByExplicitOptIn(t *testing.T) {
	t.Setenv("CONTROL_API_UI_DIR", "")
	for _, v := range []string{"", "0", "false", "no", "off", "maybe"} {
		t.Setenv("CONTROL_API_SERVE_UI", v)
		if newUIServer().enabled {
			t.Errorf("CONTROL_API_SERVE_UI=%q enabled the UI", v)
		}
	}
	for _, v := range []string{"1", "true", "YES", " on "} {
		t.Setenv("CONTROL_API_SERVE_UI", v)
		if !newUIServer().enabled {
			t.Errorf("CONTROL_API_SERVE_UI=%q did not enable the UI", v)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// Mode 3 serving
// ---------------------------------------------------------------------------------------------

func TestUIServesTheThreePages(t *testing.T) {
	h := uiTestServer(t, enabledUI(t)).mux()
	for _, c := range []struct{ path, needle string }{
		{"/", "<html"},
		{"/setup/", "<html"},
		{"/chat/", "<html"},
		{"/calculators/vram.html", "<html"},
		{"/prices.json", "{"},
		{"/backend-presets.json", "{"},
	} {
		rec := get(t, h, c.path)
		if rec.Code != http.StatusOK {
			t.Errorf("GET %s = %d, want 200", c.path, rec.Code)
			continue
		}
		if !strings.Contains(strings.ToLower(rec.Body.String()), c.needle) {
			t.Errorf("GET %s did not look like the expected asset (first 80 bytes: %.80q)", c.path, rec.Body.String())
		}
	}
}

// The API keeps answering, and an unknown /v1/ path must NOT fall through to index.html — a 200 with
// an HTML body would make a typo'd endpoint look like a working one.
func TestUIDoesNotSwallowTheAPI(t *testing.T) {
	h := uiTestServer(t, enabledUI(t)).mux()
	if rec := get(t, h, "/healthz"); rec.Code != http.StatusOK {
		t.Errorf("GET /healthz = %d, want 200", rec.Code)
	}
	if rec := get(t, h, "/v1/config-schema"); rec.Code != http.StatusOK {
		t.Errorf("GET /v1/config-schema = %d, want 200", rec.Code)
	}
	rec := get(t, h, "/v1/definitely-not-an-endpoint")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown /v1/ path = %d, want 404", rec.Code)
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["error"] == "" {
		t.Errorf("unknown /v1/ path returned %q, want a JSON error (err=%v)", rec.Body.String(), err)
	}
	if strings.Contains(strings.ToLower(rec.Body.String()), "<html") {
		t.Error("unknown /v1/ path was answered with the UI page")
	}
}

// A directory listing is the only way the FileServer could disclose names we do not serve.
func TestUINeverListsDirectories(t *testing.T) {
	h := uiTestServer(t, enabledUI(t)).mux()
	for _, p := range []string{"/calculators/", "/calculators"} {
		rec := get(t, h, p)
		if rec.Code == http.StatusOK && strings.Contains(rec.Body.String(), "<a href=") {
			t.Errorf("GET %s produced a directory listing", p)
		}
	}
}

func TestUIRejectsTraversal(t *testing.T) {
	h := uiTestServer(t, enabledUI(t)).mux()
	for _, p := range []string{
		"/setup/../../etc/passwd",
		"/etc/passwd",
		"/../go.mod",
		"/%2e%2e/go.mod",
		"/ARCHITECTURE.md",
	} {
		rec := get(t, h, p)
		if rec.Code == http.StatusOK {
			t.Errorf("GET %s = 200 — served something outside the allowlist", p)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// The leak gate: docs/ also holds INTERNAL-ONLY material. It is gitignored, so a widened embed
// pattern would look clean in CI and only bake it in on a maintainer's machine.
// ---------------------------------------------------------------------------------------------

func TestEmbeddedUIHasNoInternalDocs(t *testing.T) {
	var offenders []string
	err := fs.WalkDir(webui.FS, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		low := strings.ToLower(p)
		if strings.Contains(low, "internal") || strings.HasSuffix(low, ".md") || strings.HasSuffix(low, ".docx") {
			offenders = append(offenders, p)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking the embedded UI: %v", err)
	}
	if len(offenders) > 0 {
		t.Fatalf("the go:embed allowlist in docs/embed.go was widened and now carries %v — "+
			"internal/liability material must never ship inside the binary", offenders)
	}
}

// The disk source (CONTROL_API_UI_DIR, for live-editing the pages) points at the real docs/ tree,
// where those files physically exist — the filter, not the embed list, is what protects it there.
func TestUIDiskSourceFiltersInternalDocs(t *testing.T) {
	docs := filepath.Join("..", "..", "docs")
	if _, err := os.Stat(filepath.Join(docs, "index.html")); err != nil {
		t.Skipf("docs/ tree not present: %v", err)
	}
	t.Setenv("CONTROL_API_SERVE_UI", "")
	t.Setenv("CONTROL_API_UI_DIR", docs)
	u := newUIServer()
	if !u.enabled || u.source != docs {
		t.Fatalf("newUIServer with CONTROL_API_UI_DIR: enabled=%v source=%q", u.enabled, u.source)
	}
	h := uiTestServer(t, u).mux()

	if rec := get(t, h, "/"); rec.Code != http.StatusOK {
		t.Errorf("GET / from the disk source = %d, want 200", rec.Code)
	}
	// Present-or-not on this machine, these must never be reachable.
	for _, p := range []string{
		"/COMPETITIVE_ANALYSIS.internal.md",
		"/COMPETITIVE_ANALYSIS.raw.internal.json",
		"/DOC_BACKLOG.internal.md",
		"/ARCHITECTURE.md",
		"/embed.go",
		"/_config.yml",
	} {
		if rec := get(t, h, p); rec.Code == http.StatusOK {
			t.Errorf("GET %s = 200 from the disk source — the allowlist filter is not applied", p)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// Bootstrap nonce
// ---------------------------------------------------------------------------------------------

func TestBootstrapExchangesTheTokenExactlyOnce(t *testing.T) {
	s := uiTestServer(t, enabledUI(t))
	nonce := s.ui.arm(time.Minute)
	if len(nonce) != 2*uiNonceBytes {
		t.Fatalf("nonce is %d chars, want %d", len(nonce), 2*uiNonceBytes)
	}
	h := s.mux()

	rec := get(t, h, "/v1/ui-token?nonce="+nonce)
	if rec.Code != http.StatusOK {
		t.Fatalf("first exchange = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["token"] != s.token {
		t.Fatalf("first exchange returned %q, want the bearer token (err=%v)", rec.Body.String(), err)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store — the token must never be cached", cc)
	}

	if rec := get(t, h, "/v1/ui-token?nonce="+nonce); rec.Code != http.StatusForbidden {
		t.Errorf("replayed nonce = %d, want 403 — the nonce must be single-use", rec.Code)
	}
}

func TestBootstrapRejects(t *testing.T) {
	t.Run("wrong nonce", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		s.ui.arm(time.Minute)
		if rec := get(t, s.mux(), "/v1/ui-token?nonce=deadbeef"); rec.Code != http.StatusForbidden {
			t.Errorf("got %d, want 403", rec.Code)
		}
	})

	t.Run("missing nonce", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		s.ui.arm(time.Minute)
		if rec := get(t, s.mux(), "/v1/ui-token"); rec.Code != http.StatusForbidden {
			t.Errorf("got %d, want 403", rec.Code)
		}
	})

	t.Run("expired", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		nonce := s.ui.arm(-time.Second) // already in the past
		if rec := get(t, s.mux(), "/v1/ui-token?nonce="+nonce); rec.Code != http.StatusForbidden {
			t.Errorf("got %d, want 403", rec.Code)
		}
	})

	t.Run("never armed", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		if rec := get(t, s.mux(), "/v1/ui-token?nonce=whatever"); rec.Code != http.StatusForbidden {
			t.Errorf("got %d, want 403", rec.Code)
		}
	})

	t.Run("burned after repeated wrong guesses", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		nonce := s.ui.arm(time.Minute)
		h := s.mux()
		for i := 0; i < uiBootstrapMaxTries; i++ {
			get(t, h, "/v1/ui-token?nonce=wrong")
		}
		if rec := get(t, h, "/v1/ui-token?nonce="+nonce); rec.Code != http.StatusForbidden {
			t.Errorf("the correct nonce still worked after %d wrong guesses (%d)", uiBootstrapMaxTries, rec.Code)
		}
	})

	t.Run("cross-origin", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		nonce := s.ui.arm(time.Minute)
		h := s.mux()
		for _, hdr := range []map[string]string{
			{"Origin": "https://evil.example"},
			{"Sec-Fetch-Site": "cross-site"},
			{"Sec-Fetch-Site": "same-site"},
		} {
			req := httptest.NewRequest(http.MethodGet, "/v1/ui-token?nonce="+nonce, nil)
			for k, v := range hdr {
				req.Header.Set(k, v)
			}
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != http.StatusForbidden {
				t.Errorf("%v → %d, want 403", hdr, rec.Code)
			}
			if strings.Contains(rec.Body.String(), s.token) {
				t.Errorf("%v leaked the token", hdr)
			}
		}
		// …and a refused cross-origin attempt must not have burned the nonce for the real page.
		if rec := get(t, h, "/v1/ui-token?nonce="+nonce); rec.Code != http.StatusOK {
			t.Errorf("the legitimate exchange after cross-origin probes = %d, want 200", rec.Code)
		}
	})

	t.Run("same-origin fetch is accepted", func(t *testing.T) {
		s := uiTestServer(t, enabledUI(t))
		nonce := s.ui.arm(time.Minute)
		req := httptest.NewRequest(http.MethodGet, "/v1/ui-token?nonce="+nonce, nil)
		req.Header.Set("Origin", "http://"+req.Host)
		req.Header.Set("Sec-Fetch-Site", "same-origin")
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Errorf("same-origin exchange = %d, want 200 (%s)", rec.Code, rec.Body.String())
		}
	})
}

// With no token there is nothing to hand out, so the endpoint must not exist at all.
func TestBootstrapAbsentWithoutAToken(t *testing.T) {
	s := uiTestServer(t, enabledUI(t))
	s.token = ""
	if n := s.ui.arm(time.Minute); n == "" {
		t.Fatal("arm() should still mint a nonce; registration is what gates the endpoint")
	}
	if rec := get(t, s.mux(), "/v1/ui-token?nonce=x"); rec.Code != http.StatusNotFound {
		t.Errorf("got %d, want 404 — a tokenless instance must not expose the bootstrap", rec.Code)
	}
}

func TestBootstrapTTL(t *testing.T) {
	for _, c := range []struct {
		env  string
		want time.Duration
	}{
		{"", uiBootstrapDefaultTTL},
		{"90s", 90 * time.Second},
		{"garbage", uiBootstrapDefaultTTL},
		{"0", 0},
	} {
		t.Setenv("CONTROL_API_UI_BOOTSTRAP_TTL", c.env)
		if got := bootstrapTTL(); got != c.want {
			t.Errorf("CONTROL_API_UI_BOOTSTRAP_TTL=%q → %v, want %v", c.env, got, c.want)
		}
	}
	// A non-positive TTL disables the bootstrap outright — no nonce is minted, nothing is printed.
	u := enabledUI(t)
	if n := u.arm(0); n != "" {
		t.Errorf("arm(0) minted %q, want the bootstrap disabled", n)
	}
}

func TestDisplayAddr(t *testing.T) {
	for in, want := range map[string]string{
		"0.0.0.0:8090":   "127.0.0.1:8090",
		"[::]:8090":      "127.0.0.1:8090",
		":8090":          "127.0.0.1:8090",
		"127.0.0.1:8090": "127.0.0.1:8090",
		"10.0.0.5:8090":  "10.0.0.5:8090",
		"nonsense":       "nonsense",
	} {
		if got := displayAddr(in); got != want {
			t.Errorf("displayAddr(%q) = %q, want %q", in, got, want)
		}
	}
}
