package main

import (
	"encoding/json"
	"testing"
)

func TestValidateLLMBase(t *testing.T) {
	ok := []string{
		"",
		"http://localhost:11434/v1",
		"https://host.docker.internal:11434/v1",
		"http://192.168.1.5:11434/v1", // RFC1918 homelab — allowed
		"http://10.0.0.2:8000/v1/",
	}
	for _, b := range ok {
		if err := validateLLMBase(b); err != nil {
			t.Errorf("validateLLMBase(%q) = %v, want nil", b, err)
		}
	}
	bad := []string{
		"ftp://host/v1",                  // wrong scheme
		"not-a-url",                      // no scheme/host
		"http://user:pass@host:11434/v1", // embedded credentials
		"http://169.254.169.254/latest",  // cloud-metadata link-local
	}
	for _, b := range bad {
		if err := validateLLMBase(b); err == nil {
			t.Errorf("validateLLMBase(%q) = nil, want error", b)
		}
	}
}

func TestParseRunLLM(t *testing.T) {
	// nil / null → no config, no error
	for _, raw := range []json.RawMessage{nil, json.RawMessage("null"), json.RawMessage("")} {
		if c, err := parseRunLLM(raw); c != nil || err != nil {
			t.Errorf("parseRunLLM(%q) = (%v,%v), want (nil,nil)", raw, c, err)
		}
	}
	// valid
	c, err := parseRunLLM(json.RawMessage(`{"backend":"openai","base_url":"http://h:11434/v1","model_planner":"qwen3:14b","model_heal":"qwen2.5-vl:7b","vision":true}`))
	if err != nil || c == nil {
		t.Fatalf("valid parseRunLLM err=%v c=%v", err, c)
	}
	if c.Backend != "openai" || c.BaseURL != "http://h:11434/v1" || c.ModelPlanner != "qwen3:14b" || c.Vision == nil || !*c.Vision {
		t.Errorf("parsed config wrong: %+v", c)
	}
	// rejections
	bad := map[string]string{
		"unknown backend": `{"backend":"bogus"}`,
		"bad base_url":    `{"base_url":"http://u:p@h/v1"}`,
		"secret api_key":  `{"backend":"openai","api_key":"sk-live-123"}`,
		"secret llm_key":  `{"llm_api_key":"x"}`,
		"non-object":      `"astring"`,
		"array":           `[1,2]`,
	}
	for name, raw := range bad {
		if _, err := parseRunLLM(json.RawMessage(raw)); err == nil {
			t.Errorf("parseRunLLM(%s) = nil error, want rejection", name)
		}
	}
}

func TestResolveRunEnvPrecedence(t *testing.T) {
	// process env sets BACKEND; per-run sets base_url+planner; persisted sets heal.
	base := []string{"PATH=/x", "LLM_BACKEND=anthropic"}
	perRun := &llmRunConfig{Backend: "openai", BaseURL: "http://h:11434/v1", ModelPlanner: "qwen3:14b"}
	persisted := map[string]string{"LLM_BACKEND": "openai", "LLM_MODEL_HEAL": "qwen2.5-vl:7b"}
	env := resolveRunEnv(base, perRun, persisted)

	if got := envValue(env, "LLM_BACKEND"); got != "anthropic" {
		t.Errorf("LLM_BACKEND = %q, want anthropic (process env wins over per-run and persisted)", got)
	}
	if got := envValue(env, "LLM_BASE_URL"); got != "http://h:11434/v1" {
		t.Errorf("LLM_BASE_URL = %q, want per-run value", got)
	}
	if got := envValue(env, "LLM_MODEL_PLANNER"); got != "qwen3:14b" {
		t.Errorf("LLM_MODEL_PLANNER = %q, want per-run value", got)
	}
	if got := envValue(env, "LLM_MODEL_HEAL"); got != "qwen2.5-vl:7b" {
		t.Errorf("LLM_MODEL_HEAL = %q, want persisted value", got)
	}
	// effective backend is anthropic (env), so no noauth default is added
	if got := envValue(env, "LLM_API_KEY"); got != "" {
		t.Errorf("LLM_API_KEY = %q, want empty (backend anthropic)", got)
	}
}

func TestResolveRunEnvNoauthDefault(t *testing.T) {
	// openai backend via per-run, no key anywhere → placeholder noauth
	env := resolveRunEnv([]string{"PATH=/x"}, &llmRunConfig{Backend: "openai", BaseURL: "http://h/v1"}, nil)
	if got := envValue(env, "LLM_API_KEY"); got != "noauth" {
		t.Errorf("LLM_API_KEY = %q, want noauth", got)
	}
	// a real key in the process env is never shadowed by the placeholder
	env2 := resolveRunEnv([]string{"LLM_API_KEY=sk-real"}, &llmRunConfig{Backend: "openai"}, nil)
	if got := envValue(env2, "LLM_API_KEY"); got != "sk-real" {
		t.Errorf("LLM_API_KEY = %q, want sk-real (process env wins)", got)
	}
}

func TestPersistedLLMEnv(t *testing.T) {
	var cfg map[string]any
	_ = json.Unmarshal([]byte(`{"llm":{"backend":"openai","base_url":"http://h:11434/v1","model":{"planner":"qwen3:14b","heal":"qwen2.5-vl:7b"},"vision":true}}`), &cfg)
	got := persistedLLMEnv(cfg)
	want := map[string]string{
		"LLM_BACKEND": "openai", "LLM_BASE_URL": "http://h:11434/v1",
		"LLM_MODEL_PLANNER": "qwen3:14b", "LLM_MODEL_HEAL": "qwen2.5-vl:7b", "LLM_VISION": "1",
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("persistedLLMEnv[%s] = %q, want %q", k, got[k], v)
		}
	}
	// a bad stored base_url is dropped, not fatal; the rest still apply
	var cfg2 map[string]any
	_ = json.Unmarshal([]byte(`{"llm":{"backend":"openai","base_url":"http://u:p@h/v1"}}`), &cfg2)
	got2 := persistedLLMEnv(cfg2)
	if _, ok := got2["LLM_BASE_URL"]; ok {
		t.Errorf("persistedLLMEnv kept an invalid base_url: %v", got2)
	}
	if got2["LLM_BACKEND"] != "openai" {
		t.Errorf("persistedLLMEnv dropped valid backend alongside invalid base_url: %v", got2)
	}
	// no llm → nil
	if persistedLLMEnv(map[string]any{"run": map[string]any{}}) != nil {
		t.Errorf("persistedLLMEnv with no llm should be nil")
	}
}
