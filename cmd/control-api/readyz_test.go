package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// readyBody decodes /readyz into (httpStatus, overallStatus, checks).
func readyBody(t *testing.T, s *server, token string) (int, string, map[string]readyCheck) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	var body struct {
		Status string                `json:"status"`
		Checks map[string]readyCheck `json:"checks"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("/readyz body is not JSON: %v (%s)", err, rec.Body.String())
	}
	return rec.Code, body.Status, body.Checks
}

// The default standalone deployment configures nothing, and must still be READY: an unconfigured
// dependency is skipped, not failed.
func TestReadyzStandaloneIsReady(t *testing.T) {
	s := newTestServer() // store == nil, llmBaseURL == ""
	code, status, checks := readyBody(t, s, "")
	if code != http.StatusOK || status != "ready" {
		t.Fatalf("standalone must be ready: HTTP %d %q %+v", code, status, checks)
	}
	for _, name := range []string{"store", "config", "llm"} {
		if checks[name].Status != "skipped" {
			t.Errorf("check %q = %q, want skipped", name, checks[name].Status)
		}
	}
}

// /readyz is unauthenticated, so it must not hand an anonymous caller the shape of the deployment.
func TestReadyzHidesDetailFromAnonymousCallers(t *testing.T) {
	s := newTestServer()
	_, _, anon := readyBody(t, s, "")
	for name, c := range anon {
		if c.Detail != "" {
			t.Errorf("anonymous caller received detail for %q: %q", name, c.Detail)
		}
	}
	s.invalidateReadiness()
	_, _, authed := readyBody(t, s, "secret-tok")
	if authed["store"].Detail == "" {
		t.Error("an authenticated caller must receive the detail strings")
	}
}

// The central transition: a service deployment with a live gateway but no stored config is NOT ready;
// saving the config through the HTTP surface makes it ready, with no stale-cache delay.
func TestReadyz503UntilConfigThen200(t *testing.T) {
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatalf("newStoreClient: %v", err)
	}
	t.Cleanup(sc.close)
	s := storeBackedTestServer(sc)

	code, status, checks := readyBody(t, s, "secret-tok")
	if code != http.StatusServiceUnavailable || status != "not_ready" {
		t.Fatalf("no config -> want 503/not_ready, got %d/%q", code, status)
	}
	if checks["store"].Status != "ok" {
		t.Errorf("store check = %q, want ok (%s)", checks["store"].Status, checks["store"].Detail)
	}
	if checks["config"].Status != "error" {
		t.Errorf("config check = %q, want error", checks["config"].Status)
	}
	if checks["llm"].Status != "skipped" {
		t.Errorf("llm check = %q, want skipped (no base_url)", checks["llm"].Status)
	}

	rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"run":{"max_steps":40}}`), "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT /v1/config = %d: %s", rec.Code, rec.Body.String())
	}

	// No sleep: the write must invalidate the readiness memo, not wait out its TTL.
	code, status, checks = readyBody(t, s, "secret-tok")
	if code != http.StatusOK || status != "ready" {
		t.Fatalf("after saving config -> want 200/ready, got %d/%q %+v", code, status, checks)
	}
	if checks["config"].Status != "ok" {
		t.Errorf("config check = %q, want ok", checks["config"].Status)
	}
}

// The llm probe hits GET <base_url>/models and reports what it found.
func TestReadyzLLMProbe(t *testing.T) {
	var hits int
	llm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/models" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		hits++
		writeJSON(w, http.StatusOK, map[string]any{"data": []any{map[string]any{"id": "qwen3:14b"}}})
	}))
	defer llm.Close()

	s := newTestServer()
	s.llmBaseURL = llm.URL
	code, status, checks := readyBody(t, s, "secret-tok")
	if code != http.StatusOK || status != "ready" || checks["llm"].Status != "ok" {
		t.Fatalf("healthy llm -> want 200/ready/ok, got %d/%q/%q", code, status, checks["llm"].Status)
	}
	if hits != 1 {
		t.Fatalf("expected exactly one probe, got %d", hits)
	}

	// a second call inside the TTL must be served from the memo, not re-probe the endpoint
	readyBody(t, s, "secret-tok")
	if hits != 1 {
		t.Fatalf("probe was not cached: %d hits", hits)
	}

	// a dead endpoint fails readiness
	llm.Close()
	s.invalidateReadiness()
	code, status, checks = readyBody(t, s, "secret-tok")
	if code != http.StatusServiceUnavailable || status != "not_ready" || checks["llm"].Status != "error" {
		t.Fatalf("dead llm -> want 503/not_ready/error, got %d/%q/%q", code, status, checks["llm"].Status)
	}
}

// A non-2xx answer from the LLM endpoint is not readiness.
func TestReadyzLLMProbeRejectsNon2xxAndBadScheme(t *testing.T) {
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer bad.Close()
	s := newTestServer()
	s.llmBaseURL = bad.URL
	if code, _, checks := readyBody(t, s, "secret-tok"); code != http.StatusServiceUnavailable || checks["llm"].Status != "error" {
		t.Fatalf("HTTP 500 from /models must fail readiness, got %d/%q", code, checks["llm"].Status)
	}

	s2 := newTestServer()
	s2.llmBaseURL = "file:///etc/passwd"
	if code, _, checks := readyBody(t, s2, "secret-tok"); code != http.StatusServiceUnavailable || checks["llm"].Status != "error" {
		t.Fatalf("a non-http(s) base_url must fail readiness, got %d/%q", code, checks["llm"].Status)
	}
}

// The probe must not follow a redirect: /readyz would otherwise forward a request wherever the
// configured endpoint points it.
func TestReadyzLLMProbeDoesNotFollowRedirects(t *testing.T) {
	var elsewhereHit bool
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		elsewhereHit = true
		w.WriteHeader(http.StatusOK)
	}))
	defer elsewhere.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/models", http.StatusFound)
	}))
	defer redirector.Close()

	s := newTestServer()
	s.llmBaseURL = redirector.URL
	code, _, checks := readyBody(t, s, "secret-tok")
	if elsewhereHit {
		t.Fatal("the readiness probe followed a redirect to another host")
	}
	if code != http.StatusServiceUnavailable || checks["llm"].Status != "error" {
		t.Fatalf("a 302 is not a healthy /models, got %d/%q", code, checks["llm"].Status)
	}
}

// --- /v1/config ---------------------------------------------------------------------------------

func TestConfigHTTPRequiresTokenAndStore(t *testing.T) {
	// standalone: no store -> 501, whether or not a token is presented
	s := newTestServer()
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok"); rec.Code != http.StatusNotImplemented {
		t.Errorf("GET /v1/config with no store = %d, want 501", rec.Code)
	}
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{}`), "secret-tok"); rec.Code != http.StatusNotImplemented {
		t.Errorf("PUT /v1/config with no store = %d, want 501", rec.Code)
	}
	// unauthenticated -> 403 before anything else
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{}`), ""); rec.Code != http.StatusForbidden {
		t.Errorf("unauthenticated PUT = %d, want 403", rec.Code)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/config", nil, ""); rec.Code != http.StatusForbidden {
		t.Errorf("unauthenticated GET = %d, want 403", rec.Code)
	}
}

func TestConfigHTTPRoundTripAndSecretRefusal(t *testing.T) {
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatalf("newStoreClient: %v", err)
	}
	t.Cleanup(sc.close)
	s := storeBackedTestServer(sc)

	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok"); rec.Code != http.StatusNotFound {
		t.Fatalf("GET before any write = %d, want 404", rec.Code)
	}

	doc := `{"llm":{"backend":"openai","base_url":"http://ollama:11434/v1","max_tokens":4096},"run":{"max_steps":40}}`
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(doc), "secret-tok"); rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d", rec.Code)
	}
	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET = %d", rec.Code)
	}
	cfg, ok := body["config"].(map[string]any)
	if !ok {
		t.Fatalf("no config object in %v", body)
	}
	llm := cfg["llm"].(map[string]any)
	if llm["base_url"] != "http://ollama:11434/v1" {
		t.Fatalf("round-trip lost base_url: %v", llm)
	}
	if body["updated_at"] == "" {
		t.Error("updated_at must be reported")
	}

	// a secret anywhere in the document is refused with the offending path, and nothing is written
	for _, bad := range []string{
		`{"llm":{"api_key":"sk-live-1"}}`,
		`{"LLM_API_KEY":"sk-live-2"}`,
		`"sk-live-3"`,
		`{"providers":[{"name":"x"},{"bearer_token":"t"}]}`,
	} {
		rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(bad), "secret-tok")
		if rec.Code != http.StatusBadRequest {
			t.Errorf("PUT %s = %d, want 400", bad, rec.Code)
		}
		if msg, _ := body["error"].(string); msg == "" {
			t.Errorf("PUT %s: no error message", bad)
		}
	}
	// the rejected writes did not clobber the good document
	_, body = doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	cfg = body["config"].(map[string]any)
	if _, leaked := cfg["LLM_API_KEY"]; leaked {
		t.Fatal("a rejected document was persisted")
	}
	if cfg["llm"].(map[string]any)["base_url"] != "http://ollama:11434/v1" {
		t.Fatal("a rejected write mutated the stored document")
	}
}

func TestConfigHTTPRejectsOversizedBody(t *testing.T) {
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sc.close)
	s := storeBackedTestServer(sc)

	big := make([]byte, maxConfigBytes+1024)
	for i := range big {
		big[i] = 'a'
	}
	payload := append(append([]byte(`{"pad":"`), big...), []byte(`"}`)...)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", payload, "secret-tok"); rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body = %d, want 413", rec.Code)
	}
}

// The wizard PUTs cross-origin from the Pages/webui page, so the preflight must advertise PUT.
func TestCORSPreflightAllowsPUT(t *testing.T) {
	s := newTestServer()
	req := httptest.NewRequest(http.MethodOptions, "/v1/config", nil)
	req.Header.Set("Origin", "https://alexgromer.github.io")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight = %d", rec.Code)
	}
	if allow := rec.Header().Get("Access-Control-Allow-Methods"); !strings.Contains(allow, "PUT") {
		t.Fatalf("Access-Control-Allow-Methods = %q, must advertise PUT", allow)
	}
}

// readiness must not be recomputed on every request inside the TTL, and must be recomputed after it.
func TestReadinessCacheExpires(t *testing.T) {
	s := newTestServer()
	readyBody(t, s, "")
	first := s.ready.at
	readyBody(t, s, "")
	if !s.ready.at.Equal(first) {
		t.Fatal("probe re-ran inside the TTL")
	}
	s.ready.mu.Lock()
	s.ready.at = time.Now().Add(-2 * readyCacheTTL)
	s.ready.mu.Unlock()
	readyBody(t, s, "")
	if s.ready.at.Equal(first) {
		t.Fatal("probe did not re-run after the TTL expired")
	}
}
