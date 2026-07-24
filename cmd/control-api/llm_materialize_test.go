package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// newEnvCapturingServer backs the server with a fake agentctl that dumps its full environment to
// <repo>/env.txt (instead of argv). This proves what LLM_* the spawn env actually carries (ADR-063).
func newEnvCapturingServer(t *testing.T) (s *server, repo, envPath string) {
	t.Helper()
	repo = t.TempDir()
	envPath = filepath.Join(repo, "env.txt")
	script := filepath.Join(repo, "fake-agentctl-env.sh")
	body := "#!/bin/sh\nenv > '" + envPath + "'\nexit 0\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("write env-capturing agentctl: %v", err)
	}
	return &server{repo: repo, agentctl: script, token: "secret-tok", corsAllow: map[string]bool{}, runs: map[string]*run{}}, repo, envPath
}

func readEnvFile(t *testing.T, path string) map[string]string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read env dump: %v", err)
	}
	out := map[string]string{}
	for _, line := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if i := strings.IndexByte(line, '='); i >= 0 {
			out[line[:i]] = line[i+1:]
		}
	}
	return out
}

func runBodyJSON(t *testing.T, m map[string]any) string {
	t.Helper()
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// TestPerRunLLMMaterialized: a per-run `llm` body reaches the agentctl spawn env as LLM_* (openai path
// gets the noauth placeholder key).
func TestPerRunLLMMaterialized(t *testing.T) {
	for _, k := range []string{"LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL_PLANNER", "LLM_MODEL_HEAL", "LLM_VISION", "LLM_API_KEY"} {
		os.Unsetenv(k) // isolate from any ambient LLM_* so per-run is the source
	}
	s, _, envPath := newEnvCapturingServer(t)
	body := runBodyJSON(t, map[string]any{
		"target": "file:///x.html", "mode": "goal", "goal": "do it",
		"llm": map[string]any{"backend": "openai", "base_url": "http://ollama:11434/v1",
			"model_planner": "qwen3:14b", "model_heal": "qwen2.5vl:7b", "vision": true},
	})
	postRunAndWait(t, s, body)
	env := readEnvFile(t, envPath)
	want := map[string]string{
		"LLM_BACKEND": "openai", "LLM_BASE_URL": "http://ollama:11434/v1",
		"LLM_MODEL_PLANNER": "qwen3:14b", "LLM_MODEL_HEAL": "qwen2.5vl:7b",
		"LLM_VISION": "1", "LLM_API_KEY": "noauth",
	}
	for k, v := range want {
		if env[k] != v {
			t.Errorf("spawn env[%s] = %q, want %q", k, env[k], v)
		}
	}
}

// TestProcessEnvWinsOverPerRun: the control-api process env is authoritative; a per-run override never
// changes a variable it already sets.
func TestProcessEnvWinsOverPerRun(t *testing.T) {
	t.Setenv("LLM_BACKEND", "anthropic")
	s, _, envPath := newEnvCapturingServer(t)
	body := runBodyJSON(t, map[string]any{"target": "file:///x.html", "mode": "goal", "goal": "g",
		"llm": map[string]any{"backend": "openai"}})
	postRunAndWait(t, s, body)
	if got := readEnvFile(t, envPath)["LLM_BACKEND"]; got != "anthropic" {
		t.Errorf("LLM_BACKEND = %q, want anthropic (process env wins over per-run)", got)
	}
}

// TestPersistedLLMMaterialized: with a store-gateway, the persisted /v1/config `llm` reaches the spawn env
// when neither the process env nor a per-run body sets it.
func TestPersistedLLMMaterialized(t *testing.T) {
	for _, k := range []string{"LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL_PLANNER"} {
		os.Unsetenv(k) // a present (even empty) process var would block the persisted layer by design
	}
	sc, err := newStoreClient(startTestGateway(t, ""), "") // no-auth gateway
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s, _, envPath := newEnvCapturingServer(t)
	s.store = sc

	cfg := []byte(`{"llm":{"backend":"openai","base_url":"http://persisted:11434/v1","model":{"planner":"qwen3:8b"}}}`)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", cfg, "secret-tok"); rec.Code != http.StatusOK {
		t.Fatalf("PUT /v1/config = %d (%s)", rec.Code, rec.Body.String())
	}
	body := runBodyJSON(t, map[string]any{"target": "file:///x.html", "mode": "goal", "goal": "g"}) // no per-run llm
	postRunAndWait(t, s, body)
	env := readEnvFile(t, envPath)
	if env["LLM_BACKEND"] != "openai" || env["LLM_BASE_URL"] != "http://persisted:11434/v1" || env["LLM_MODEL_PLANNER"] != "qwen3:8b" {
		t.Errorf("persisted llm not materialized: backend=%q base=%q planner=%q", env["LLM_BACKEND"], env["LLM_BASE_URL"], env["LLM_MODEL_PLANNER"])
	}
}

// TestPerRunLLMValidation400: bad per-run llm is a 400, not a broken spawn.
func TestPerRunLLMValidation400(t *testing.T) {
	s, _, _ := newEnvCapturingServer(t)
	cases := map[string]any{
		"userinfo base_url": map[string]any{"base_url": "http://u:p@h/v1"},
		"secret api_key":    map[string]any{"backend": "openai", "api_key": "sk-x"},
		"bad backend":       map[string]any{"backend": "bogus"},
	}
	for name, llm := range cases {
		body := runBodyJSON(t, map[string]any{"target": "file:///x.html", "mode": "goal", "goal": "g", "llm": llm})
		if rec := postRun(t, s, body); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d want 400 (%s)", name, rec.Code, rec.Body.String())
		}
	}
}
