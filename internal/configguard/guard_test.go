package configguard

import (
	"encoding/json"
	"testing"
)

func TestSecretishWordBoundaries(t *testing.T) {
	secret := []string{
		"api_key", "apiKey", "LLM_API_KEY", "ANTHROPIC_API_KEY", "key", "llm_key",
		"token", "bearer_token", "token_value", "auth_bearer", "password", "passwd",
		"client_secret", "private_key", "credentials", "Credential",
	}
	for _, n := range secret {
		if !Secretish(n) {
			t.Errorf("Secretish(%q) = false, want true", n)
		}
	}
	// counters and ordinary config names that merely contain a secret-ish substring
	clean := []string{
		"max_tokens", "total_tokens", "tokens", "plan_budget", "base_url", "backend",
		"structured", "vision", "storage_state", "pw_no_trace", "monkey", "keyboard",
	}
	for _, n := range clean {
		if Secretish(n) {
			t.Errorf("Secretish(%q) = true, want false", n)
		}
	}
}

func TestValidate(t *testing.T) {
	bad := map[string]string{
		"not json":            `{`,
		"bare string":         `"sk-live-1"`,
		"array document":      `[{"a":1}]`,
		"number document":     `42`,
		"null document":       `null`,
		"top-level secret":    `{"api_key":"x"}`,
		"nested secret":       `{"llm":{"api_key":"x"}}`,
		"secret in an array":  `{"backends":[{"ok":1},{"apikey":"x"}]}`,
		"deeply nested":       `{"a":{"b":{"c":{"private_key":"x"}}}}`,
		"uppercase env style": `{"LLM_API_KEY":"x"}`,
	}
	for name, doc := range bad {
		if err := Validate(doc); err == nil {
			t.Errorf("%s: Validate(%q) = nil, want an error", name, doc)
		}
	}

	good := []string{
		`{}`,
		`{"llm":{"backend":"openai","base_url":"http://ollama:11434/v1","max_tokens":4096}}`,
		`{"run":{"mode":"explore","max_steps":40,"plan_budget":50000}}`,
		`{"auth":{"storage_state":"state/auth.json","pw_no_trace":true}}`,
	}
	for _, doc := range good {
		if err := Validate(doc); err != nil {
			t.Errorf("Validate(%q) = %v, want nil", doc, err)
		}
	}
}

// The reported path must point at the offending member so an operator can fix the document.
func TestFindSecretKeyReportsPath(t *testing.T) {
	cases := map[string]string{
		`{"api_key":"x"}`:                     "api_key",
		`{"llm":{"api_key":"x"}}`:             "llm.api_key",
		`{"a":{"b":{"private_key":"x"}}}`:     "a.b.private_key",
		`{"backends":[{"ok":1},{"key":"x"}]}`: "backends[1].key",
	}
	for doc, want := range cases {
		var v any
		if err := json.Unmarshal([]byte(doc), &v); err != nil {
			t.Fatal(err)
		}
		if got := FindSecretKey(v, ""); got != want {
			t.Errorf("FindSecretKey(%s) = %q, want %q", doc, got, want)
		}
	}
	var clean any
	if err := json.Unmarshal([]byte(`{"llm":{"backend":"openai","max_tokens":10}}`), &clean); err != nil {
		t.Fatal(err)
	}
	if got := FindSecretKey(clean, ""); got != "" {
		t.Errorf("clean document reported %q", got)
	}
}
