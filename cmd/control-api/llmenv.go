// Config-driven LLM for runs (ADR-063). The LLM connection a run uses is layered, highest wins:
//
//  1. control-API process env  (the operator's explicit choice — os.Environ())
//  2. per-run body `llm`        (POST /v1/runs, this request only)
//  3. persisted config          (PUT /v1/config `llm`, service tier — store-gateway only)
//
// resolveRunEnv materializes layers 2-3 into the LLM_* env of the `agentctl` subprocess WITHOUT ever
// overriding a variable the process env already sets (layer 1 wins). agentctl's filteredEnv() forwards
// the LLM_ prefix to the brain, so these reach brain/llm.py make_backend unchanged.
//
// SECRET INVARIANT: an api_key never travels through the API or UI. The per-run body is guarded by
// configguard.FindSecretKey (same rule as PUT /v1/config), the persisted document by configguard.Validate
// at write time. A real cloud key stays in the process env (ANTHROPIC_API_KEY/OPENAI_API_KEY); for a local
// OpenAI-compatible endpoint (Ollama), where the key is a non-secret placeholder, resolveRunEnv defaults
// LLM_API_KEY=noauth so the UI need not carry one.
package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"strings"

	"github.com/AlexGromer/sentinel/internal/configguard"
)

// llmBackends is the single source for the backend enum (mirrors brain/llm.py make_backend); the
// config-schema handler and per-run validation both read it so they cannot drift apart.
var llmBackends = []string{"anthropic", "openai", "sampling"}

func validBackend(b string) bool {
	for _, x := range llmBackends {
		if b == x {
			return true
		}
	}
	return false
}

// llmRunConfig is the per-run LLM override carried in the POST /v1/runs body under `llm`. It mirrors the
// hub #build fields and the config-schema `llm` descriptors. NO api_key field by design — secrets never
// travel in a run request (see the package comment); a secret-shaped member is rejected before we get here.
type llmRunConfig struct {
	Backend      string `json:"backend"`
	BaseURL      string `json:"base_url"`
	ModelPlanner string `json:"model_planner"`
	ModelHeal    string `json:"model_heal"`
	Vision       *bool  `json:"vision"`
	Structured   *bool  `json:"structured"`
}

// validateLLMBase bounds an operator-supplied OpenAI-compatible base_url. A run points the brain at this
// URL, and POST /v1/runs is only token-gated, so the same shape checks /readyz's probeLLM applies are
// enforced here: absolute http(s), no embedded credentials, and no link-local (cloud-metadata) target.
// Empty is allowed (means "not set").
func validateLLMBase(base string) error {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if base == "" {
		return nil
	}
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return fmt.Errorf("must be an absolute http(s) URL, got %q", base)
	}
	if u.User != nil {
		return fmt.Errorf("must not embed credentials (user:pass@); keys live in the process env")
	}
	if ip := net.ParseIP(u.Hostname()); ip != nil && ip.IsLinkLocalUnicast() {
		return fmt.Errorf("must not point at a link-local address (169.254.0.0/16)")
	}
	return nil
}

// parseRunLLM validates the raw `llm` member of a run request and returns the typed config. It rejects a
// secret-shaped key (defence in depth — the typed struct has no api_key field, so one is dropped anyway),
// an unknown backend, and a malformed base_url. A nil result with nil error means "no llm provided".
func parseRunLLM(raw json.RawMessage) (*llmRunConfig, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("bad JSON: %w", err)
	}
	if _, ok := doc.(map[string]any); !ok {
		return nil, fmt.Errorf("must be a JSON object")
	}
	if hit := configguard.FindSecretKey(doc, ""); hit != "" {
		return nil, fmt.Errorf("refusing secret-shaped key %q — secrets live in the process env, never in a run request", hit)
	}
	var c llmRunConfig
	if err := json.Unmarshal(raw, &c); err != nil {
		return nil, err
	}
	if c.Backend != "" && !validBackend(c.Backend) {
		return nil, fmt.Errorf("backend must be one of %s", strings.Join(llmBackends, "/"))
	}
	if err := validateLLMBase(c.BaseURL); err != nil {
		return nil, fmt.Errorf("base_url: %w", err)
	}
	return &c, nil
}

// resolveRunEnv builds the subprocess env from `base` (os.Environ()) plus the per-run and persisted LLM
// layers. Precedence is process env > per-run > persisted: a set() is a no-op when the key already exists,
// so applying per-run before persisted makes per-run win, and neither ever overrides the process env.
func resolveRunEnv(base []string, perRun *llmRunConfig, persisted map[string]string) []string {
	have := make(map[string]bool, len(base))
	for _, kv := range base {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			have[kv[:i]] = true
		}
	}
	set := func(k, v string) {
		if v == "" || have[k] {
			return
		}
		base = append(base, k+"="+v)
		have[k] = true
	}
	if perRun != nil {
		set("LLM_BACKEND", perRun.Backend)
		set("LLM_BASE_URL", perRun.BaseURL)
		set("LLM_MODEL_PLANNER", perRun.ModelPlanner)
		set("LLM_MODEL_HEAL", perRun.ModelHeal)
		if perRun.Vision != nil && *perRun.Vision {
			set("LLM_VISION", "1")
		}
		if perRun.Structured != nil && *perRun.Structured {
			set("LLM_STRUCTURED", "1")
		}
	}
	for k, v := range persisted {
		set(k, v)
	}
	// Local OpenAI-compatible endpoints (Ollama) authenticate with any non-empty key; default a
	// placeholder so a UI-configured openai run needs no key. Cloud keys stay in the process env and are
	// already present in `base` if set, so this never shadows a real ANTHROPIC_API_KEY/OPENAI_API_KEY.
	backend := envValue(base, "LLM_BACKEND")
	if backend == "openai" && !have["LLM_API_KEY"] {
		set("LLM_API_KEY", "noauth")
	}
	return base
}

// envValue returns the value of key in an os.Environ()-shaped slice ("" if absent). Last occurrence wins,
// matching exec semantics, though resolveRunEnv never writes a duplicate.
func envValue(env []string, key string) string {
	pfx := key + "="
	val := ""
	for _, kv := range env {
		if strings.HasPrefix(kv, pfx) {
			val = kv[len(pfx):]
		}
	}
	return val
}

// persistedLLMEnv maps a persisted config document's `llm` object to LLM_* env vars. It is the generalized
// form of effectiveLLMBase (which reads only base_url). A base_url that fails validateLLMBase is dropped
// (never fail a run over a stored value) — the rest still apply. Returns nil when there is no `llm`.
func persistedLLMEnv(cfg map[string]any) map[string]string {
	llm, ok := cfg["llm"].(map[string]any)
	if !ok {
		return nil
	}
	out := map[string]string{}
	if v, _ := llm["backend"].(string); v != "" && validBackend(v) {
		out["LLM_BACKEND"] = v
	}
	if v, _ := llm["base_url"].(string); v != "" && validateLLMBase(v) == nil {
		out["LLM_BASE_URL"] = v
	}
	switch m := llm["model"].(type) {
	case map[string]any: // per-role {planner, heal}
		if v, _ := m["planner"].(string); v != "" {
			out["LLM_MODEL_PLANNER"] = v
		}
		if v, _ := m["heal"].(string); v != "" {
			out["LLM_MODEL_HEAL"] = v
		}
	case string:
		if m != "" {
			out["LLM_MODEL"] = m
		}
	}
	if v, _ := llm["vision"].(bool); v {
		out["LLM_VISION"] = "1"
	}
	if v, _ := llm["structured"].(bool); v {
		out["LLM_STRUCTURED"] = "1"
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// getPersistedLLM reads the persisted config's `llm` (service tier, store-gateway only) as LLM_* env vars,
// the lowest-precedence layer for resolveRunEnv. Returns nil in the standalone tier (s.store == nil) or on
// any read/parse error — a run must never fail because the stored config is unavailable (fail-open).
func (s *server) getPersistedLLM() map[string]string {
	if s.store == nil {
		return nil
	}
	rec, err := s.store.getConfig(setupConfigKey, storeCallTimeout)
	if err != nil || rec == nil {
		return nil
	}
	var doc map[string]any
	if json.Unmarshal([]byte(rec.ValueJson), &doc) != nil {
		return nil
	}
	return persistedLLMEnv(doc)
}
