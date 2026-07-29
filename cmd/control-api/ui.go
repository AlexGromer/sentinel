package main

// Mode 3 — single-service UI (ADR-064).
//
// Modes 1 (headless) and 2 (separate `webui` service on :8088) are unchanged: with neither
// CONTROL_API_SERVE_UI nor CONTROL_API_UI_DIR set, nothing here is registered and the mux is exactly
// what it was before ADR-064.
//
// Mode 3 makes control-api serve the browser UI from its OWN port, which buys three things:
//   - one port and one process instead of two;
//   - the CORS allowlist becomes unnecessary (same-origin requests are not CORS requests at all), so
//     CONTROL_API_CORS_ORIGINS can be emptied — a strictly smaller attack surface than Mode 2;
//   - the page can be handed its bearer token without the operator copy-pasting it.
//
// That last part is where the care goes. Handing the token to whoever GETs the page would make
// "reaching the port" equivalent to "holding the token" and would gut ADR-032. Instead the token is
// exchanged exactly once, for a single-use TTL-bounded nonce that only ever appears on the operator's
// own terminal at startup. Reaching the port later buys nothing.

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"sync"
	"time"

	webui "github.com/AlexGromer/sentinel/docs"
)

const (
	uiBootstrapDefaultTTL = 5 * time.Minute
	uiBootstrapMaxTries   = 5 // a wrong nonce is either a typo or a probe; burn after a handful
	uiNonceBytes          = 32
)

// envEnabled is the positive counterpart to envDisabled (token.go): unset/empty stays OFF, so every
// Mode-3 behaviour is strictly opt-in.
func envEnabled(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// uiPathAllowed mirrors the go:embed allowlist in docs/embed.go. It matters for the DISK source
// (CONTROL_API_UI_DIR, used for live-editing the pages in dev): pointed at the repo's docs/, an
// unfiltered FileServer would happily serve COMPETITIVE_ANALYSIS.internal.md and friends. The
// embedded source cannot contain them; this keeps both sources serving the same set.
func uiPathAllowed(name string) bool {
	if name == "." || name == "" {
		return true
	}
	for _, dir := range []string{"setup", "chat", "calculators"} {
		if name == dir || strings.HasPrefix(name, dir+"/") {
			return true
		}
	}
	switch name {
	case "index.html", "prices.json", "backend-presets.json", "capabilities.json":
		return true
	}
	return false
}

// uiFS restricts any underlying FS to uiPathAllowed.
type uiFS struct{ inner fs.FS }

func (f uiFS) Open(name string) (fs.File, error) {
	if !uiPathAllowed(name) {
		return nil, &fs.PathError{Op: "open", Path: name, Err: fs.ErrNotExist}
	}
	return f.inner.Open(name)
}

// uiServer holds the Mode-3 state. The zero value is a disabled UI, which is what Modes 1 and 2 use.
type uiServer struct {
	enabled bool
	fsys    fs.FS
	source  string // "embedded" or the CONTROL_API_UI_DIR path — for the startup log

	mu      sync.Mutex
	nonce   string // "" once burned (used, expired, or too many wrong guesses)
	expires time.Time
	tries   int
}

func newUIServer() *uiServer {
	if dir := strings.TrimSpace(os.Getenv("CONTROL_API_UI_DIR")); dir != "" {
		return &uiServer{enabled: true, fsys: uiFS{os.DirFS(dir)}, source: dir}
	}
	if envEnabled("CONTROL_API_SERVE_UI") {
		return &uiServer{enabled: true, fsys: uiFS{webui.FS}, source: "embedded"}
	}
	return &uiServer{}
}

// bootstrapTTL is how long the startup nonce stays exchangeable. A non-positive value disables the
// bootstrap entirely (the operator then copies the token out of state/control-api.token by hand).
func bootstrapTTL() time.Duration {
	v := strings.TrimSpace(os.Getenv("CONTROL_API_UI_BOOTSTRAP_TTL"))
	if v == "" {
		return uiBootstrapDefaultTTL
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return uiBootstrapDefaultTTL
	}
	return d
}

// arm mints the one-time bootstrap nonce. It returns "" when the bootstrap is unavailable — the UI is
// off, the TTL is non-positive, or the entropy source failed — and the caller then prints nothing.
func (u *uiServer) arm(ttl time.Duration) string {
	if u == nil || !u.enabled || ttl <= 0 {
		return ""
	}
	b := make([]byte, uiNonceBytes)
	if _, err := rand.Read(b); err != nil {
		return ""
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.nonce = hex.EncodeToString(b)
	u.expires = time.Now().Add(ttl)
	u.tries = 0
	return u.nonce
}

// redeem exchanges got for the right to receive the token. It is single-use by construction: any
// outcome other than "wrong guess, tries left" clears the nonce.
func (u *uiServer) redeem(got string) bool {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.nonce == "" {
		return false
	}
	if time.Now().After(u.expires) {
		u.nonce = ""
		return false
	}
	if subtle.ConstantTimeCompare([]byte(got), []byte(u.nonce)) == 1 {
		u.nonce = "" // burn on success — the page keeps the token in tab memory from here on
		return true
	}
	u.tries++
	if u.tries >= uiBootstrapMaxTries {
		u.nonce = ""
	}
	return false
}

// sameOriginRequest rejects a cross-site browser caller. Only the host is compared: a TLS-terminating
// reverse proxy leaves r.TLS nil while the browser reports an https Origin, and treating that as
// cross-site would break a legitimate deployment. A genuinely cross-SITE page always carries a
// different host, which is the property this guards.
func sameOriginRequest(r *http.Request) bool {
	// Browser-set and unforgeable by page script; absent on non-browser clients.
	switch r.Header.Get("Sec-Fetch-Site") {
	case "", "same-origin", "none":
	default:
		return false
	}
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin == "" {
		return true // same-origin GETs omit Origin; so do curl/CI clients
	}
	o, err := url.Parse(origin)
	if err != nil || o.Host == "" {
		return false
	}
	return strings.EqualFold(o.Host, r.Host)
}

// handleUIToken exchanges the startup nonce for the bearer token, once.
//
// Registered ONLY when the UI is served and a token exists (see mux). The response must never be
// cached and never carries CORS headers — s.cors adds Access-Control-Allow-Origin solely for
// allowlisted origins, and Mode 3 is expected to run with an empty allowlist.
func (s *server) handleUIToken(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if !sameOriginRequest(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "cross-origin bootstrap refused"})
		return
	}
	if !s.ui.redeem(r.URL.Query().Get("nonce")) {
		writeJSON(w, http.StatusForbidden, map[string]string{
			"error": "invalid, expired or already-used bootstrap nonce — restart control-api or read the token file",
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"token": s.token})
}

// handleV1NotFound keeps unknown /v1/* paths answering JSON 404 instead of falling through to the
// catch-all UI handler, which would return index.html with a 200 and make a typo look like success.
func (s *server) handleV1NotFound(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown endpoint: " + r.URL.Path})
}

// handler serves the UI tree. Directory listings are deliberately impossible: a listing is the only
// way the underlying FS could disclose sibling names we do not intend to serve.
func (u *uiServer) handler() http.Handler {
	files := http.FileServerFS(u.fsys)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if name == "" {
			name = "."
		}
		if st, err := fs.Stat(u.fsys, name); err == nil && st.IsDir() {
			if _, err := fs.Stat(u.fsys, path.Join(name, "index.html")); err != nil {
				http.NotFound(w, r)
				return
			}
		}
		// The pages carry the bootstrap flow and read live endpoints; a stale cached copy is a
		// support burden. The payload is ~100 KB from memory, so revalidation costs nothing.
		if strings.HasSuffix(name, ".html") || strings.HasSuffix(r.URL.Path, "/") || name == "." {
			w.Header().Set("Cache-Control", "no-cache")
		}
		files.ServeHTTP(w, r)
	})
}
