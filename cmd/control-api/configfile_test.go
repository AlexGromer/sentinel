package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

// fileTierServer is a control-API with NO store and NO store address: the true standalone tier, where
// ADR-075 says the config document is a file. newTestServer already gives every server its own throwaway
// repo (see TestMain), so the name exists to say which tier is under test, not to change the wiring.
func fileTierServer(t *testing.T) *server {
	t.Helper()
	return newTestServer()
}

const goodConfig = `{"llm":{"backend":"openai","base_url":"http://ollama.lan:11434/v1",` +
	`"model":{"planner":"qwen3:14b","heal":"qwen2.5vl:7b"}},"run":{"mode":"explore","planner":"heuristic"}}`

// The defect this closes: PUT answered 501 "this deployment keeps its config in a file (standalone
// tier)" while writing no file anywhere, so the wizard's save button was decorative. The round trip is
// the whole claim — written, then read back as the same document, through the HTTP surface the wizard
// actually uses.
func TestConfigFileTierRoundTrip(t *testing.T) {
	s := fileTierServer(t)

	rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(goodConfig), "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT /v1/config = %d (%s)", rec.Code, rec.Body.String())
	}
	if body["tier"] != string(tierFile) {
		t.Fatalf("PUT tier = %v want %q — an operator must be told which medium answered", body["tier"], tierFile)
	}

	// The file exists, is non-empty, and is not world-readable. Non-emptiness first: a zero-byte file
	// would satisfy "exists" and satisfy nothing else.
	path := s.configFilePath()
	st, err := os.Stat(path)
	if err != nil {
		t.Fatalf("no config file at %s: %v", path, err)
	}
	if st.Size() == 0 {
		t.Fatal("config file is empty")
	}
	if perm := st.Mode().Perm(); perm != 0o600 {
		t.Fatalf("config file mode = %v want 0600 (it names internal hosts and models)", perm)
	}

	rec, body = doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /v1/config = %d (%s)", rec.Code, rec.Body.String())
	}
	got, ok := body["config"].(map[string]any)
	if !ok || len(got) == 0 {
		t.Fatalf("GET returned no config document: %v", body)
	}
	var want map[string]any
	if err := json.Unmarshal([]byte(goodConfig), &want); err != nil {
		t.Fatal(err)
	}
	gotB, _ := json.Marshal(got)
	wantB, _ := json.Marshal(want)
	if string(gotB) != string(wantB) {
		t.Fatalf("round trip changed the document:\n got  %s\n want %s", gotB, wantB)
	}
	if body["key"] != setupConfigKey {
		t.Fatalf("GET key = %v want %q", body["key"], setupConfigKey)
	}
	if ts, _ := body["updated_at"].(string); ts == "" {
		t.Fatal("GET carries no updated_at — the wizard shows it, and a blank one reads as never saved")
	}
}

// The old handler returned 501 BEFORE reading the body, so a malformed document and a valid one were
// refused identically. Now that the file tier accepts writes, the guard has to run FIRST — otherwise the
// standalone tier would happily persist a document the service tier rejects, and the two tiers would be
// configuring different products.
func TestConfigFileTierValidatesBeforeWriting(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		wantCode   int
	}{
		{"secret member", `{"llm":{"api_key":"sk-real-secret"}}`, http.StatusBadRequest},
		{"not JSON", `{"llm":`, http.StatusBadRequest},
		{"unknown log level", `{"logging":{"level":"screaming"}}`, http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := fileTierServer(t)
			rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(tc.body), "secret-tok")
			if rec.Code != tc.wantCode {
				t.Fatalf("PUT %s = %d want %d (%s)", tc.name, rec.Code, tc.wantCode, rec.Body.String())
			}
			if _, err := os.Stat(s.configFilePath()); !os.IsNotExist(err) {
				t.Fatalf("a refused document left a file behind at %s (err=%v)", s.configFilePath(), err)
			}
		})
	}
}

// A missing file is "nothing saved yet" (404). A file that exists but cannot be parsed is NOT that —
// telling the operator "no config" would send them to re-run the wizard over a document they might
// rather fix, and would quietly discard settings that are still on disk.
func TestConfigFileMissingAndCorruptAreDifferentAnswers(t *testing.T) {
	s := fileTierServer(t)
	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET with no file = %d want 404 (%s)", rec.Code, rec.Body.String())
	}
	if body["error"] != "no config stored" {
		t.Fatalf("missing-file error = %v want %q", body["error"], "no config stored")
	}

	if err := os.MkdirAll(filepath.Dir(s.configFilePath()), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(s.configFilePath(), []byte("{not json at all"), 0o600); err != nil {
		t.Fatal(err)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code == http.StatusNotFound {
		t.Fatal("a corrupt config file answered 404 — indistinguishable from never having saved")
	}
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("GET with corrupt file = %d want 500 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); msg == "" || msg == "no config stored" {
		t.Fatalf("corrupt-file error = %q, must name the real problem", msg)
	}
}

// `s.store == nil` has two causes and they must not share an answer. With CONTROL_API_STORE_ADDR set but
// the gateway down at boot (main.go is fail-open and never re-dials), writing a file would be a silent
// degradation: the gateway comes back, the store wins the next read, and the operator's saved settings
// disappear with no message. So that case refuses — and leaves no file to be shadowed later.
func TestStoreConfiguredButDownRefusesInsteadOfWritingAFile(t *testing.T) {
	s := fileTierServer(t)
	s.storeAddr = "unix:/nonexistent/store.sock" // configured; newStoreClient failed, so s.store stayed nil

	if got := s.configTier(); got != tierUnavailable {
		t.Fatalf("configTier = %q want %q", got, tierUnavailable)
	}
	rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(goodConfig), "secret-tok")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("PUT with a dead configured store = %d want 503 (%s)", rec.Code, rec.Body.String())
	}
	if body["tier"] != string(tierUnavailable) {
		t.Fatalf("PUT tier = %v want %q", body["tier"], tierUnavailable)
	}
	if _, err := os.Stat(s.configFilePath()); !os.IsNotExist(err) {
		t.Fatal("a configured-but-down store fell back to a file — the gateway would shadow it on the next read")
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("GET with a dead configured store = %d want 503", rec.Code)
	}
}

// ADR-063 promises the persisted config is layer 3 of a run's LLM env. In the standalone tier that
// promise was vacuous — getPersistedLLM returned nil whenever there was no store, so a wizard-saved
// backend could never reach a run. The claim is about the RUN, so the assertion is about run env.
func TestPersistedLLMReachesTheRunFromTheConfigFile(t *testing.T) {
	s := fileTierServer(t)
	if env := s.getPersistedLLM(); len(env) != 0 {
		t.Fatalf("with nothing saved, getPersistedLLM = %v want empty", env)
	}
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(goodConfig), "secret-tok"); rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d (%s)", rec.Code, rec.Body.String())
	}

	env := s.getPersistedLLM()
	if len(env) == 0 {
		t.Fatal("a saved config produced no LLM env — ADR-063 layer 3 is still dead in the standalone tier")
	}
	for k, want := range map[string]string{
		"LLM_BACKEND":       "openai",
		"LLM_BASE_URL":      "http://ollama.lan:11434/v1",
		"LLM_MODEL_PLANNER": "qwen3:14b",
		"LLM_MODEL_HEAL":    "qwen2.5vl:7b",
	} {
		if env[k] != want {
			t.Fatalf("run env %s = %q want %q (full env %v)", k, env[k], want, env)
		}
	}
}

// The readiness probe must read the same file the wizard writes, and must keep an UNCONFIGURED
// standalone deployment ready: running purely from the process env is a legitimate way to deploy, and a
// 503 would call it broken. A corrupt file is the one case that is genuinely not ready.
func TestReadyzReflectsTheConfigFileTier(t *testing.T) {
	s := fileTierServer(t)

	code, status, checks := readyBody(t, s, "secret-tok")
	if code != http.StatusOK || status != "ready" {
		t.Fatalf("standalone with no config must stay ready: HTTP %d %q %+v", code, status, checks)
	}
	if checks["config"].Status != "skipped" {
		t.Fatalf("config check with no file = %q want skipped", checks["config"].Status)
	}

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(goodConfig), "secret-tok"); rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d (%s)", rec.Code, rec.Body.String())
	}
	s.invalidateReadiness()
	_, _, checks = readyBody(t, s, "secret-tok")
	if checks["config"].Status != "ok" {
		t.Fatalf("config check after saving = %q want ok (%+v)", checks["config"].Status, checks["config"])
	}
	if checks["config"].Detail == "" {
		t.Fatal("config check carries no detail — an authenticated operator has no way to see WHICH file answered")
	}

	if err := os.WriteFile(s.configFilePath(), []byte("{broken"), 0o600); err != nil {
		t.Fatal(err)
	}
	s.invalidateReadiness()
	code, status, checks = readyBody(t, s, "secret-tok")
	if checks["config"].Status != "error" {
		t.Fatalf("config check with a corrupt file = %q want error", checks["config"].Status)
	}
	if code != http.StatusServiceUnavailable || status != "not_ready" {
		t.Fatalf("a corrupt config must not report ready: HTTP %d %q", code, status)
	}
}

// Re-saving REPLACES the document rather than overlaying it, and the temp file the atomic write goes
// through never survives beside it. The replacement half is the one that bites: a shorter second
// document written over a longer first one leaves the tail of the first behind unless the write
// truncates, and the reader would then call a perfectly good save "corrupt".
func TestConfigFileResaveReplacesTheDocument(t *testing.T) {
	s := fileTierServer(t)
	long := `{"llm":{"backend":"openai","base_url":"http://a-deliberately-long-host.example.lan:11434/v1"},` +
		`"run":{"mode":"explore","planner":"heuristic","max_steps":40}}`
	short := `{"run":{"mode":"goal"}}`

	for _, doc := range []string{long, short} {
		if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(doc), "secret-tok"); rec.Code != http.StatusOK {
			t.Fatalf("PUT = %d (%s)", rec.Code, rec.Body.String())
		}
	}

	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET after re-save = %d (%s)", rec.Code, rec.Body.String())
	}
	got, ok := body["config"].(map[string]any)
	if !ok || len(got) == 0 {
		t.Fatalf("GET returned no document after re-save: %v", body)
	}
	if _, stale := got["llm"]; stale {
		t.Fatalf("the first document's llm block survived the second save: %v", got)
	}
	runBlk, ok := got["run"].(map[string]any)
	if !ok || runBlk["mode"] != "goal" {
		t.Fatalf("second document not stored: run = %v", got["run"])
	}

	entries, err := os.ReadDir(filepath.Dir(s.configFilePath()))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) == 0 {
		t.Fatal("state dir is empty after two saves")
	}
	for _, e := range entries {
		if e.Name() != configFileName {
			t.Fatalf("stray file %q left beside the config", e.Name())
		}
	}
}
