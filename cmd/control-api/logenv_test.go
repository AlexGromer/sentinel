package main

// Gates for the persisted logging section (ADR-065 follow-on). The claims: a saved level reaches the
// run, a typo is refused with its path rather than silently ignored, and the ADR-063 precedence is
// unchanged by the new layer.

import (
	"encoding/json"
	"strings"
	"testing"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
)

// eventCategoriesForTest reads the vocabulary through the same accessor production uses, so this test
// cannot pass against a stale hand-copied list.
func eventCategoriesForTest() []string { return eventcatalog.Categories() }

func loggingEnv(t *testing.T, doc string) map[string]string {
	t.Helper()
	var cfg map[string]any
	if err := json.Unmarshal([]byte(doc), &cfg); err != nil {
		t.Fatalf("test doc is not JSON: %v", err)
	}
	return persistedLoggingEnv(cfg)
}

func TestPersistedLoggingEnv(t *testing.T) {
	for _, tc := range []struct {
		name, doc string
		want      map[string]string
	}{
		{"absent section", `{"llm":{"backend":"openai"}}`, nil},
		{"global level only", `{"logging":{"level":"info"}}`,
			map[string]string{"SENTINEL_LOG_LEVEL": "info"}},
		{"level is case-insensitive", `{"logging":{"level":"WARN"}}`,
			map[string]string{"SENTINEL_LOG_LEVEL": "warn"}},
		{"per-category", `{"logging":{"levels":{"heal":"error","llm":"debug"}}}`,
			map[string]string{"SENTINEL_LOG_LEVELS": "heal=error,llm=debug"}},
		{"both", `{"logging":{"level":"info","levels":{"heal":"error"}}}`,
			map[string]string{"SENTINEL_LOG_LEVEL": "info", "SENTINEL_LOG_LEVELS": "heal=error"}},
		// The spawn path must never fail a run over a bad stored value — it drops it. The PUT path is
		// where a typo gets reported (see TestValidateLoggingSection).
		{"unknown level dropped", `{"logging":{"level":"loud"}}`, nil},
		{"unknown category dropped", `{"logging":{"levels":{"telepathy":"info"}}}`, nil},
		{"wrong type dropped", `{"logging":{"level":42,"levels":"nope"}}`, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := loggingEnv(t, tc.doc)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v want %v", got, tc.want)
			}
			for k, v := range tc.want {
				if got[k] != v {
					t.Fatalf("%s = %q want %q (full: %v)", k, got[k], v, got)
				}
			}
		})
	}
}

// The env must be byte-identical for the same document across spawns, or a captured environment
// cannot be compared between two runs. Map iteration order makes that a real hazard.
func TestPersistedLoggingEnvIsDeterministic(t *testing.T) {
	doc := `{"logging":{"levels":{"llm":"debug","heal":"error","run":"info","browser":"warn"}}}`
	first := loggingEnv(t, doc)["SENTINEL_LOG_LEVELS"]
	if first == "" {
		t.Fatal("expected a per-category value")
	}
	for i := 0; i < 20; i++ {
		if got := loggingEnv(t, doc)["SENTINEL_LOG_LEVELS"]; got != first {
			t.Fatalf("unstable ordering: %q then %q", first, got)
		}
	}
	if !strings.HasPrefix(first, "browser=") {
		t.Fatalf("expected sorted pairs, got %q", first)
	}
}

// A typo must be refused at write time with the path that was wrong. A level that looks saved but
// never applies is the same silent-degradation shape this milestone exists to close.
func TestValidateLoggingSection(t *testing.T) {
	for _, tc := range []struct {
		name, doc, wantPath string
	}{
		{"absent is fine", `{"llm":{"backend":"openai"}}`, ""},
		{"valid", `{"logging":{"level":"info","levels":{"heal":"error"}}}`, ""},
		{"not an object", `{"logging":"debug"}`, "logging"},
		{"unknown member", `{"logging":{"lvl":"info"}}`, "logging.lvl"},
		{"bad level", `{"logging":{"level":"loud"}}`, "logging.level"},
		{"level wrong type", `{"logging":{"level":42}}`, "logging.level"},
		{"levels not an object", `{"logging":{"levels":"heal=info"}}`, "logging.levels"},
		{"unknown category", `{"logging":{"levels":{"telepathy":"info"}}}`, "logging.levels.telepathy"},
		{"bad per-category level", `{"logging":{"levels":{"heal":"loud"}}}`, "logging.levels.heal"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateLoggingSection([]byte(tc.doc))
			if tc.wantPath == "" {
				if err != nil {
					t.Fatalf("want accepted, got %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("want %s refused, got accepted", tc.wantPath)
			}
			if !strings.Contains(err.Error(), tc.wantPath) {
				t.Fatalf("error must name the offending path %q, got %q", tc.wantPath, err.Error())
			}
		})
	}
}

// Categories are validated against the CATALOGUE, not a hand-kept list, so the two cannot drift.
func TestLoggingCategoriesComeFromCatalogue(t *testing.T) {
	cats := eventCategoriesForTest()
	if len(cats) == 0 {
		t.Fatal("the catalogue exposes no categories — config validation would refuse everything")
	}
	for _, c := range cats {
		if !knownLogCategory(c) {
			t.Fatalf("catalogue category %q is refused by config validation", c)
		}
	}
	if knownLogCategory("definitely-not-a-category") {
		t.Fatal("validation accepts a category the catalogue never defined")
	}
}

// Process env still wins over a persisted level, and per-category still reaches the run — the ADR-063
// precedence must be unchanged by adding this layer.
func TestLoggingEnvPrecedence(t *testing.T) {
	persisted := map[string]string{"SENTINEL_LOG_LEVEL": "info", "SENTINEL_LOG_LEVELS": "heal=error"}

	env := resolveRunEnv([]string{"SENTINEL_LOG_LEVEL=debug"}, nil, persisted)
	if got := envValue(env, "SENTINEL_LOG_LEVEL"); got != "debug" {
		t.Fatalf("process env must win: got %q want debug", got)
	}
	if got := envValue(env, "SENTINEL_LOG_LEVELS"); got != "heal=error" {
		t.Fatalf("a persisted value with no process-env counterpart must apply: got %q", got)
	}

	// With nothing in the process env, the persisted level applies.
	env = resolveRunEnv([]string{"PATH=/usr/bin"}, nil, persisted)
	if got := envValue(env, "SENTINEL_LOG_LEVEL"); got != "info" {
		t.Fatalf("persisted level must reach the run: got %q want info", got)
	}
}
