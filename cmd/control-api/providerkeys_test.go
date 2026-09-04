package main

// Gate for ADR-146 — provider keys as settings.
//
// The assertions here are deliberately written against an INDEPENDENT observation rather than against
// the implementation's own formula. The property that matters is not "handleGetProviderKeys builds a
// providerKeyStatus"; it is "the bytes this server hands a client do not contain the key". So the
// tests take the response body the handler actually produced and search it for the secret value. A
// reimplementation that changed the struct, the field names or the marshalling would still be judged
// on the only thing a leak depends on.
//
// The value used throughout is long enough to exercise the hint path and distinctive enough that a
// substring search cannot match by accident.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// canaryKey is the value that must never appear in anything a client can read.
//
// ⚠ It deliberately does NOT look like a real credential, and that is not squeamishness: the first
// version read `sk-live-…` and gitleaks flagged it as a generic-api-key, which would have blocked
// every commit touching this file and taught the next person to add an allowlist entry — a hole
// opened for a fake. The assertion here is "this exact string must not appear in the output", and
// the string's SHAPE has nothing to do with that. Long enough (>=12) to exercise the hint path,
// distinctive enough that a substring search cannot match by accident.
const canaryKey = "CANARY-NOT-A-KEY-4a7b2c9d1e6f8035"

func putProviderKeys(t *testing.T, s *server, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPut, "/v1/provider-keys", strings.NewReader(body))
	rec := httptest.NewRecorder()
	s.handlePutProviderKeys(rec, req)
	return rec
}

func getProviderKeys(t *testing.T, s *server) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/provider-keys", nil)
	rec := httptest.NewRecorder()
	s.handleGetProviderKeys(rec, req)
	return rec
}

// TestProviderKeyNeverTravelsBack is the central assertion: a stored key is absent from the bytes of
// BOTH the write response and every subsequent read. Asserted on the raw body, not on a decoded
// field, because a leak through some other member is exactly what a field-by-field check misses.
func TestProviderKeyNeverTravelsBack(t *testing.T) {
	s := &server{repo: t.TempDir()}
	t.Setenv("LLM_API_KEY", "")

	wrote := putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)
	if wrote.Code != http.StatusOK {
		t.Fatalf("PUT = %d, want 200; body %s", wrote.Code, wrote.Body.String())
	}
	if strings.Contains(wrote.Body.String(), canaryKey) {
		t.Errorf("the WRITE response echoed the key back: %s", wrote.Body.String())
	}

	read := getProviderKeys(t, s)
	if strings.Contains(read.Body.String(), canaryKey) {
		t.Errorf("the READ response carried the key: %s", read.Body.String())
	}

	// ...and it really was stored, so the assertion above is not passing vacuously over a no-op write.
	if got := s.providerKeyEnvLayer()["LLM_API_KEY"]; got != canaryKey {
		t.Fatalf("stored key not resolvable for a run: got %q", got)
	}
}

// TestProviderKeyStatusReportsSetAndHint: an administrator must be able to tell WHICH key is stored
// without being shown it. Four characters, and only for a value long enough that four characters are
// not most of it.
func TestProviderKeyStatusReportsSetAndHint(t *testing.T) {
	s := &server{repo: t.TempDir()}
	t.Setenv("LLM_API_KEY", "")
	putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)

	var got struct {
		Keys map[string]providerKeyStatus `json:"keys"`
	}
	if err := json.Unmarshal(getProviderKeys(t, s).Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	st := got.Keys["llm_api_key"]
	if !st.Set {
		t.Error("set = false after a successful write")
	}
	if st.Hint != canaryKey[len(canaryKey)-4:] {
		t.Errorf("hint = %q, want the last four characters %q", st.Hint, canaryKey[len(canaryKey)-4:])
	}
	if st.UpdatedAt == "" {
		t.Error("updated_at empty — an administrator cannot tell when the key last changed")
	}
	// A key that was never set must not claim to be.
	if got.Keys["anthropic_api_key"].Set {
		t.Error("anthropic_api_key reports set without ever being written")
	}
}

// TestProviderKeyShortValueGetsNoHint: revealing four characters of a six-character value reveals the
// value. The threshold is on raw length so a short key cannot be probed by watching a hint appear.
func TestProviderKeyShortValueGetsNoHint(t *testing.T) {
	if h := providerKeyHint("short"); h != "" {
		t.Errorf("hint for a short value = %q, want empty", h)
	}
	if h := providerKeyHint("noauth"); h != "" {
		t.Errorf("hint for %q = %q, want empty", "noauth", h)
	}
	if h := providerKeyHint("0123456789ab"); h != "89ab" {
		t.Errorf("hint at the 12-char threshold = %q, want 89ab", h)
	}
}

// TestProviderKeyFileIsOwnerOnly: 0600 is the access control for this file, not decoration.
func TestProviderKeyFileIsOwnerOnly(t *testing.T) {
	s := &server{repo: t.TempDir()}
	putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)

	fi, err := os.Stat(s.providerKeysPath())
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != 0o600 {
		t.Errorf("provider-keys file mode = %04o, want 0600", perm)
	}
	// No temp file may survive: a .provider-keys-*.tmp left behind would hold the credential at
	// whatever mode CreateTemp chose, outside the guarantee above.
	leftovers, _ := filepath.Glob(filepath.Join(s.repo, "state", ".provider-keys-*.tmp"))
	if len(leftovers) > 0 {
		t.Errorf("temp files left behind: %v", leftovers)
	}
}

// TestProviderKeyProcessEnvWins is the air-gapped/CI guarantee. A deployment passing LLM_API_KEY
// through from the host must keep using the host's key; storing one in the UI must not silently
// change which credential its runs go out with.
func TestProviderKeyProcessEnvWins(t *testing.T) {
	s := &server{repo: t.TempDir()}
	putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)

	env := resolveRunEnv([]string{"LLM_API_KEY=from-the-host"}, nil, nil, s.providerKeyEnvLayer())
	if got := envValue(env, "LLM_API_KEY"); got != "from-the-host" {
		t.Errorf("LLM_API_KEY = %q, want from-the-host (process env must beat a stored key)", got)
	}

	// The narrow half: with nothing in the process env the stored key DOES reach the run. Without
	// this, "process env wins" would also pass over an implementation that never applies layer 4.
	env2 := resolveRunEnv([]string{"PATH=/x"}, nil, nil, s.providerKeyEnvLayer())
	if got := envValue(env2, "LLM_API_KEY"); got != canaryKey {
		t.Errorf("LLM_API_KEY = %q, want the stored key when the process env has none", got)
	}
}

// TestProviderKeyBeatsNoauthPlaceholder: the openai placeholder exists so a deployment with no key
// still runs. A deployment that HAS stored one must not be downgraded to "noauth".
func TestProviderKeyBeatsNoauthPlaceholder(t *testing.T) {
	s := &server{repo: t.TempDir()}
	putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)

	env := resolveRunEnv([]string{"PATH=/x"}, &llmRunConfig{Backend: "openai"}, nil, s.providerKeyEnvLayer())
	if got := envValue(env, "LLM_API_KEY"); got != canaryKey {
		t.Errorf("LLM_API_KEY = %q, want the stored key (noauth must not shadow it)", got)
	}
}

// TestProviderKeyFromEnvIsReported: an administrator who stored a key on a deployment that also
// passes one through from the host must be told which one is in force.
func TestProviderKeyFromEnvIsReported(t *testing.T) {
	s := &server{repo: t.TempDir()}
	t.Setenv("LLM_API_KEY", "from-the-host")

	var got struct {
		Keys map[string]providerKeyStatus `json:"keys"`
	}
	if err := json.Unmarshal(getProviderKeys(t, s).Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !got.Keys["llm_api_key"].FromEnv {
		t.Error("from_env = false while LLM_API_KEY is set in the process env")
	}
	if got.Keys["llm_api_key"].Set {
		t.Error("set = true without anything having been stored — env presence is not storage")
	}
	// And the host's value is not leaked either: from_env says THAT, never WHAT.
	if strings.Contains(getProviderKeys(t, s).Body.String(), "from-the-host") {
		t.Error("the read response carried the process-env key value")
	}
}

// TestProviderKeyEmptyStringClears: rotation-to-nothing, without a second route shape.
func TestProviderKeyEmptyStringClears(t *testing.T) {
	s := &server{repo: t.TempDir()}
	t.Setenv("LLM_API_KEY", "")
	putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)
	if s.providerKeyEnvLayer()["LLM_API_KEY"] == "" {
		t.Fatal("setup failed: key not stored")
	}

	putProviderKeys(t, s, `{"name":"llm_api_key","value":""}`)
	if got := s.providerKeyEnvLayer()["LLM_API_KEY"]; got != "" {
		t.Errorf("after clearing, layer still yields %q", got)
	}
	var got struct {
		Keys map[string]providerKeyStatus `json:"keys"`
	}
	json.Unmarshal(getProviderKeys(t, s).Body.Bytes(), &got)
	if got.Keys["llm_api_key"].Set {
		t.Error("set = true after the key was cleared")
	}
}

// TestProviderKeyUnknownNameIsRefused: a name that is not stored is a key no run will ever read.
// Dropping it silently would leave an administrator certain they had configured the tool.
func TestProviderKeyUnknownNameIsRefused(t *testing.T) {
	s := &server{repo: t.TempDir()}
	rec := putProviderKeys(t, s, `{"name":"openai_key","value":"`+canaryKey+`"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("PUT unknown name = %d, want 400", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "openai_key") {
		t.Error("the refusal does not name the offending member")
	}
	// The refusal must list what IS settable — a 400 that does not is a puzzle, not an error.
	for _, n := range providerKeyNames() {
		if !strings.Contains(body, n) {
			t.Errorf("the refusal does not list the settable name %q: %s", n, body)
		}
	}
	if strings.Contains(body, canaryKey) {
		t.Error("the refusal echoed the rejected VALUE")
	}
	if _, err := os.Stat(s.providerKeysPath()); err == nil {
		t.Error("a refused write created the file anyway")
	}
}

// TestProviderKeyNonStringIsRefused: `{"llm_api_key": 123}` must not become the string "123", and must
// not be silently ignored either.
func TestProviderKeyNonStringIsRefused(t *testing.T) {
	s := &server{repo: t.TempDir()}
	if rec := putProviderKeys(t, s, `{"name":"llm_api_key","value":123}`); rec.Code != http.StatusBadRequest {
		t.Errorf("PUT non-string = %d, want 400", rec.Code)
	}
}

// TestProviderKeyCorruptFileIsNotOverwritten: an unparseable file may hold a credential nobody has a
// copy of. Refuse and name the path rather than destroying it.
func TestProviderKeyCorruptFileIsNotOverwritten(t *testing.T) {
	s := &server{repo: t.TempDir()}
	if err := os.MkdirAll(filepath.Join(s.repo, "state"), 0o755); err != nil {
		t.Fatal(err)
	}
	corrupt := []byte("{not json")
	if err := os.WriteFile(s.providerKeysPath(), corrupt, 0o600); err != nil {
		t.Fatal(err)
	}

	rec := putProviderKeys(t, s, `{"name":"llm_api_key","value":"`+canaryKey+`"}`)
	if rec.Code != http.StatusConflict {
		t.Errorf("PUT over a corrupt file = %d, want 409", rec.Code)
	}
	after, _ := os.ReadFile(s.providerKeysPath())
	if string(after) != string(corrupt) {
		t.Errorf("the corrupt file was overwritten: %q", after)
	}
	// And a READ must say so, rather than reporting "not set" — which reads as "nothing was saved".
	body := getProviderKeys(t, s).Body.String()
	if !strings.Contains(body, "\"readable\":false") {
		t.Errorf("a corrupt file reads as ordinary emptiness: %s", body)
	}
}

// TestProviderKeyRoutesAreAdminOnly: "keys live until an ADMINISTRATOR replaces them" is an access
// decision, and it is declared in the route table. Derived from the table rather than restated, so a
// route added later without accessAdmin is caught here and not by a reader noticing.
func TestProviderKeyRoutesAreAdminOnly(t *testing.T) {
	s := newTestServer()
	seen := 0
	for _, rt := range s.routes() {
		if !strings.Contains(rt.pattern, "/v1/provider-keys") {
			continue
		}
		seen++
		if rt.access != accessAdmin {
			t.Errorf("%s declares access %q, want %q — a provider key is the tool's, not an account's",
				rt.pattern, rt.access, accessAdmin)
		}
	}
	if seen < 2 {
		t.Errorf("found %d provider-keys routes, want at least the read and the write", seen)
	}
}

// TestProviderKeyEnvVarsAreDerived: every declared key must name an env var brain/llm.py actually
// honours, and the layer must be built from the same map the API accepts names from. A key settable
// through the API but absent from the layer would store fine and reach no run.
func TestProviderKeyEnvVarsAreDerived(t *testing.T) {
	s := &server{repo: t.TempDir()}
	for _, name := range providerKeyNames() {
		env := providerKeyEnv[name]
		if env == "" {
			t.Errorf("settable name %q maps to no environment variable", name)
			continue
		}
		if !strings.HasSuffix(env, "_API_KEY") {
			t.Errorf("%q maps to %q, which is not an API-key variable", name, env)
		}
	}
	// The floor: a derived list that found nothing would pass every assertion above.
	if len(providerKeyNames()) < 3 {
		t.Errorf("only %d settable keys — the backends the product ships need at least three",
			len(providerKeyNames()))
	}
	// And each one actually reaches the layer.
	for _, name := range providerKeyNames() {
		rec := putProviderKeys(t, s, `{"name":"`+name+`","value":"`+canaryKey+`"}`)
		if rec.Code != http.StatusOK {
			t.Fatalf("PUT %q = %d: %s", name, rec.Code, rec.Body.String())
		}
	}
	layer := s.providerKeyEnvLayer()
	for _, name := range providerKeyNames() {
		if layer[providerKeyEnv[name]] != canaryKey {
			t.Errorf("stored %q does not reach the run environment as %q", name, providerKeyEnv[name])
		}
	}
}
