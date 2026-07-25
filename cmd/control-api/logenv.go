package main

// Persisted logging configuration (M9-LIVE, ADR-065 follow-on).
//
// There are TWO log levels in this product and they are not the same setting:
//
//	CAPTURE — what the brain writes at all. Server-side, persisted here, materialized into the run's
//	          env, and per CATEGORY so `heal` can be quiet while `llm` is verbose. Changing it only
//	          affects future runs, because a run's file cannot be rewritten after the fact.
//	VIEW    — what the Logs tab shows out of what was captured. Client-side and instant; changing
//	          your mind costs nothing, so it does not belong in a stored document at all.
//
// Capture therefore defaults to `debug` (see brain/eventlog.py): a run is short, its log is small, and
// a detail not captured costs a repeat run. This section exists for the cases where that default is
// wrong — a long soak, or a category known to be noisy.
//
// Materialization follows ADR-063 exactly: process env > per-run > persisted, and the layering is
// already generic, so this only has to produce a map of SENTINEL_LOG_* vars. The `SENTINEL_` prefix
// passes agentctl's env allowlist (cmd/agentctl/main.go), so nothing needs plumbing.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
)

// Vocabularies come from the catalogue, so a level or category added there needs no change here — and
// a value the UI offers can never be one this refuses.
func knownLogLevel(s string) bool {
	switch s {
	case "debug", "info", "warn", "error":
		return true
	}
	return false
}

// persistedLoggingEnv turns the config document's `logging` section into SENTINEL_LOG_* vars. Unknown
// or malformed members are dropped rather than surfaced: this path runs when SPAWNING a run, where
// failing would cost the operator a test result. The PUT path validates and reports instead, which is
// where a bad value should be caught.
func persistedLoggingEnv(cfg map[string]any) map[string]string {
	sec, ok := cfg["logging"].(map[string]any)
	if !ok {
		return nil
	}
	out := map[string]string{}
	if v, _ := sec["level"].(string); knownLogLevel(strings.ToLower(v)) {
		out["SENTINEL_LOG_LEVEL"] = strings.ToLower(v)
	}
	if per, ok := sec["levels"].(map[string]any); ok {
		pairs := make([]string, 0, len(per))
		for cat, raw := range per {
			lvl, _ := raw.(string)
			lvl = strings.ToLower(lvl)
			if !knownLogLevel(lvl) || !knownLogCategory(cat) {
				continue
			}
			pairs = append(pairs, cat+"="+lvl)
		}
		// Sorted so the same document always yields the same env — otherwise a run's environment
		// differs between spawns for no reason, which makes a captured env impossible to compare.
		sort.Strings(pairs)
		if len(pairs) > 0 {
			out["SENTINEL_LOG_LEVELS"] = strings.Join(pairs, ",")
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// knownLogCategory reports whether a category exists in the catalogue. Checked against the catalogue
// rather than a hand-kept list so the two cannot drift.
func knownLogCategory(cat string) bool {
	for _, e := range eventcatalog.Categories() {
		if e == cat {
			return true
		}
	}
	return false
}

// validateLoggingSection reports the first bad member by path, so a PUT tells the operator exactly
// which field was refused rather than rejecting the whole document anonymously.
func validateLoggingSection(body []byte) error {
	var doc map[string]any
	if json.Unmarshal(body, &doc) != nil {
		return nil // not our shape to judge; handlePutConfig reports malformed JSON on its own
	}
	raw, present := doc["logging"]
	if !present {
		return nil
	}
	sec, ok := raw.(map[string]any)
	if !ok {
		return fmt.Errorf("logging: must be an object")
	}
	for k := range sec {
		if k != "level" && k != "levels" {
			return fmt.Errorf("logging.%s: unknown member (expected level, levels)", k)
		}
	}
	if v, present := sec["level"]; present {
		s, ok := v.(string)
		if !ok || !knownLogLevel(strings.ToLower(s)) {
			return fmt.Errorf("logging.level: %v is not one of debug, info, warn, error", v)
		}
	}
	if v, present := sec["levels"]; present {
		per, ok := v.(map[string]any)
		if !ok {
			return fmt.Errorf("logging.levels: must be an object of category -> level")
		}
		for cat, lvl := range per {
			if !knownLogCategory(cat) {
				return fmt.Errorf("logging.levels.%s: unknown category (expected one of %s)",
					cat, strings.Join(eventcatalog.Categories(), ", "))
			}
			s, ok := lvl.(string)
			if !ok || !knownLogLevel(strings.ToLower(s)) {
				return fmt.Errorf("logging.levels.%s: %v is not one of debug, info, warn, error", cat, lvl)
			}
		}
	}
	return nil
}

// getPersistedLogging reads the stored config's `logging` section as env vars — the lowest-precedence
// layer, alongside getPersistedLLM. Fail-open for the same reason: a run must never fail because the
// stored config is unreachable.
func (s *server) getPersistedLogging() map[string]string {
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
	return persistedLoggingEnv(doc)
}

// mergedPersistedEnv is the single lowest-precedence layer handed to resolveRunEnv: the LLM connection
// (ADR-063) plus the logging levels. Both are plain env vars with identical precedence, so they share
// one map and resolveRunEnv needs no knowledge of either.
func (s *server) mergedPersistedEnv() map[string]string {
	llm, logging := s.getPersistedLLM(), s.getPersistedLogging()
	if len(logging) == 0 {
		return llm
	}
	out := make(map[string]string, len(llm)+len(logging))
	for k, v := range llm {
		out[k] = v
	}
	for k, v := range logging {
		out[k] = v
	}
	return out
}
