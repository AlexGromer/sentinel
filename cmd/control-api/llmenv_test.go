package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
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
		"ftp://host/v1",                       // wrong scheme
		"not-a-url",                           // no scheme/host
		"http://user:pass@host:11434/v1",      // embedded credentials
		"http://169.254.169.254/latest",       // cloud-metadata link-local
		"http://[fe80::1%25eth0]/v1",          // IPv6 link-local with a zone id — must not bypass the guard
		"http://169.254.169.254./v1",          // trailing-dot link-local
		"http://ollama:11434/v1?api_key=sk-x", // credential smuggled in the query string
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
	c, err := parseRunLLM(json.RawMessage(`{"backend":"openai","base_url":"http://h:11434/v1","model_planner":"qwen3:14b","model_heal":"qwen2.5vl:7b","vision":true}`))
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
	persisted := map[string]string{"LLM_BACKEND": "openai", "LLM_MODEL_HEAL": "qwen2.5vl:7b"}
	env := resolveRunEnv(base, perRun, persisted, nil)

	if got := envValue(env, "LLM_BACKEND"); got != "anthropic" {
		t.Errorf("LLM_BACKEND = %q, want anthropic (process env wins over per-run and persisted)", got)
	}
	if got := envValue(env, "LLM_BASE_URL"); got != "http://h:11434/v1" {
		t.Errorf("LLM_BASE_URL = %q, want per-run value", got)
	}
	if got := envValue(env, "LLM_MODEL_PLANNER"); got != "qwen3:14b" {
		t.Errorf("LLM_MODEL_PLANNER = %q, want per-run value", got)
	}
	if got := envValue(env, "LLM_MODEL_HEAL"); got != "qwen2.5vl:7b" {
		t.Errorf("LLM_MODEL_HEAL = %q, want persisted value", got)
	}
	// effective backend is anthropic (env), so no noauth default is added
	if got := envValue(env, "LLM_API_KEY"); got != "" {
		t.Errorf("LLM_API_KEY = %q, want empty (backend anthropic)", got)
	}
}

func TestResolveRunEnvNoauthDefault(t *testing.T) {
	// openai backend via per-run, no key anywhere → placeholder noauth
	env := resolveRunEnv([]string{"PATH=/x"}, &llmRunConfig{Backend: "openai", BaseURL: "http://h/v1"}, nil, nil)
	if got := envValue(env, "LLM_API_KEY"); got != "noauth" {
		t.Errorf("LLM_API_KEY = %q, want noauth", got)
	}
	// a real key in the process env is never shadowed by the placeholder
	env2 := resolveRunEnv([]string{"LLM_API_KEY=sk-real"}, &llmRunConfig{Backend: "openai"}, nil, nil)
	if got := envValue(env2, "LLM_API_KEY"); got != "sk-real" {
		t.Errorf("LLM_API_KEY = %q, want sk-real (process env wins)", got)
	}
	// a real OPENAI_API_KEY (no LLM_API_KEY) is the documented fallback — noauth must NOT shadow it
	env3 := resolveRunEnv([]string{"LLM_BACKEND=openai", "OPENAI_API_KEY=sk-cloud"}, nil, nil, nil)
	if got := envValue(env3, "LLM_API_KEY"); got != "" {
		t.Errorf("LLM_API_KEY = %q, want empty (OPENAI_API_KEY present -> no noauth placeholder)", got)
	}
}

func boolPtr(b bool) *bool { return &b }

// TestResolveRunEnvPerRunBeatsPersisted: same key set by BOTH layers, no process env -> per-run wins.
// (Guards the precedence claim the earlier tests left vacuous — disjoint keys never exercised it.)
func TestResolveRunEnvPerRunBeatsPersisted(t *testing.T) {
	env := resolveRunEnv([]string{"PATH=/x"},
		&llmRunConfig{ModelPlanner: "from-per-run"},
		map[string]string{"LLM_MODEL_PLANNER": "from-persisted"}, nil)
	if got := envValue(env, "LLM_MODEL_PLANNER"); got != "from-per-run" {
		t.Errorf("LLM_MODEL_PLANNER = %q, want from-per-run (per-run > persisted)", got)
	}
}

// TestResolveRunEnvPerRunBoolOverridesPersisted: an explicit per-run vision:false must beat persisted true.
func TestResolveRunEnvPerRunBoolOverridesPersisted(t *testing.T) {
	env := resolveRunEnv([]string{"PATH=/x"},
		&llmRunConfig{Vision: boolPtr(false)},
		map[string]string{"LLM_VISION": "1"}, nil)
	if got := envValue(env, "LLM_VISION"); got == "1" {
		t.Errorf("LLM_VISION = %q, want not \"1\" (per-run vision:false overrides persisted true)", got)
	}
}

// TestResolveRunEnvEmptyEnvIsOverridable: a present-but-empty process var (compose interpolation of an unset
// value) must NOT block a lower layer — else the brain reads "" as falsy and silently downgrades the backend.
func TestResolveRunEnvEmptyEnvIsOverridable(t *testing.T) {
	env := resolveRunEnv([]string{"LLM_BACKEND=", "PATH=/x"}, &llmRunConfig{Backend: "openai"}, nil, nil)
	if got := envValue(env, "LLM_BACKEND"); got != "openai" {
		t.Errorf("LLM_BACKEND = %q, want openai (empty env var must be overridable)", got)
	}
}

func TestPersistedLLMEnv(t *testing.T) {
	var cfg map[string]any
	_ = json.Unmarshal([]byte(`{"llm":{"backend":"openai","base_url":"http://h:11434/v1","model":{"planner":"qwen3:14b","heal":"qwen2.5vl:7b"},"vision":true}}`), &cfg)
	got := persistedLLMEnv(cfg)
	want := map[string]string{
		"LLM_BACKEND": "openai", "LLM_BASE_URL": "http://h:11434/v1",
		"LLM_MODEL_PLANNER": "qwen3:14b", "LLM_MODEL_HEAL": "qwen2.5vl:7b", "LLM_VISION": "1",
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

// TestEveryDeclaredRoleReachesARun — ОБХОД, а не перечень, и заведён он по измеренному дефекту.
//
// Роль `chat` реальна в мозге с ADR-108b, схема её публикует, мастер настройки рисует на неё поле и
// сохраняет его — а проекция сохранённого документа читала из карты моделей ровно две роли двумя
// жёсткими `if`. Значение доезжало до хранилища и не доезжало никуда дальше: интерфейс подтверждал
// сохранение, человек считал, что настроил, и не узнавал обратного.
//
// Перечень ролей здесь НЕ ПИШЕТСЯ: он берётся из `llmRoles` — того же источника, из которого его
// берут схема и обе проекции. Тест, написанный перечнем, был бы написан тем же пониманием, что
// породило дыру, и четвёртая роль повторила бы судьбу третьей молча.
//
// Утверждаются ОБА пути, потому что они независимы и разошлись поодиночке: сохранённый документ и
// пер-ранное тело. Плюс пол: обход над пустым множеством ролей прошёл бы идеально.
func TestEveryDeclaredRoleReachesARun(t *testing.T) {
	if len(llmRoles) < 3 {
		t.Fatalf("ролей объявлено %d — обход стал бы вакуумным; пол здесь не формальность", len(llmRoles))
	}

	// 1. СОХРАНЁННЫЙ документ -> окружение прогона.
	models := map[string]any{}
	for _, role := range llmRoles {
		models[role] = "model-for-" + role
	}
	cfg := map[string]any{"llm": map[string]any{"model": models}}
	got := persistedLLMEnv(cfg)
	for _, role := range llmRoles {
		key := llmModelEnv(role)
		if got[key] != "model-for-"+role {
			t.Errorf("сохранённая модель роли %q не доезжает до прогона: %s = %q, ожидалось %q. "+
				"Роль объявлена в llmRoles и публикуется схемой, значит настройка предлагается и не действует",
				role, key, got[key], "model-for-"+role)
		}
	}

	// 2. ПЕР-РАННОЕ тело -> окружение прогона, той же карте ролей.
	perRun := &llmRunConfig{Model: map[string]string{}}
	for _, role := range llmRoles {
		perRun.Model[role] = "run-" + role
	}
	env := resolveRunEnv([]string{"PATH=/x"}, perRun, nil, nil)
	seen := map[string]string{}
	for _, kv := range env {
		if i := strings.IndexByte(kv, '='); i > 0 {
			seen[kv[:i]] = kv[i+1:]
		}
	}
	for _, role := range llmRoles {
		key := llmModelEnv(role)
		if seen[key] != "run-"+role {
			t.Errorf("пер-ранная модель роли %q не доезжает: %s = %q, ожидалось %q",
				role, key, seen[key], "run-"+role)
		}
	}

	// 3. СТАРАЯ ФОРМА ЖИВА. Ломать `model_planner`/`model_heal` ради новой карты значило бы ломать
	// работающие вызовы ради формы; поэтому обе принимаются, и именованная роль побеждает — она
	// точнее, потому что называет себя.
	old := &llmRunConfig{ModelPlanner: "p-old", ModelHeal: "h-old"}
	if old.modelFor("planner") != "p-old" || old.modelFor("heal") != "h-old" {
		t.Errorf("старая форма перестала приниматься: %+v", old)
	}
	both := &llmRunConfig{ModelPlanner: "p-old", Model: map[string]string{"planner": "p-new"}}
	if both.modelFor("planner") != "p-new" {
		t.Errorf("при обеих формах победила безымянная: %q", both.modelFor("planner"))
	}
	if old.modelFor("chat") != "" {
		t.Errorf("старая форма выдумала модель для роли, которой в ней нет: %q", old.modelFor("chat"))
	}
}

// TestSchemaRolesComeFromTheSameSource — схема обязана публиковать ТУ ЖЕ тройку, что доезжает до
// прогона. До этой правки список стоял литералом в трёх местах и разошёлся в двух из трёх.
func TestSchemaRolesComeFromTheSameSource(t *testing.T) {
	s := &server{}
	rec := httptest.NewRecorder()
	s.handleConfigSchema(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	var body struct {
		Roles []string `json:"roles"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("схема не разобралась: %v", err)
	}
	if len(body.Roles) != len(llmRoles) {
		t.Fatalf("схема публикует %v, а до прогона доезжают %v", body.Roles, llmRoles)
	}
	for i, r := range llmRoles {
		if body.Roles[i] != r {
			t.Errorf("роль %d: схема говорит %q, доставка знает %q", i, body.Roles[i], r)
		}
	}
}
