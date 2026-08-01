package main

// Alex's directive, extending ADR-109:
//
//	"Everything the working person owns — artefacts, settings, objects — belongs to them. Only the
//	 global settings and the tool belong to the master user (the admin), who configures the tool for
//	 everyone."
//
// The config domain was the last place that ignored it: one document under a bare key, writable by
// anyone holding any credential. Splitting it needs an answer to "which settings are the TOOL's?",
// and the answer is not a taste — the document's own sections already divide along it:
//
//	llm, settings, logging  — the tool. Which model backend this deployment talks to, how long its
//	                          artefacts live on ITS disk, which gates fail a run, what it logs. Every
//	                          one is an environment variable the server process reads; changing one
//	                          changes what the installation does for everybody.
//	run, auth               — the person. The budget I run with, my storage_state path, my planner,
//	                          my coverage target. Nobody else's run is affected by mine.
//
// A section with no declared scope is REFUSED, not defaulted. Defaulting to global would let a new
// section quietly become admin-only; defaulting to personal would let it quietly become per-account.
// Both are decisions, and a decision nobody made is the one that gets made wrong.
//
// WHO gets a personal document: whoever the caller is. A machine caller and a deployment with no
// accounts both have owner "" and therefore write the GLOBAL document — the same one rule as
// everywhere else in ADR-109 ("no subject, no scoping"), which is also what keeps the setup wizard
// working unchanged: it authenticates with the machine token, so the run/auth defaults it saves are
// the tool's defaults, exactly as they were before this split existed.
//
// Note on the spawn path: `llm`, `settings` and `logging` are what control-api materialises into a
// run's environment, and all three are global — so mergedPersistedEnv keeps reading the global
// document and needs no notion of ownership. The personal sections are form defaults the interface
// fills in, not server-side environment; a personal `run.max_steps` reaches a run as a field on
// POST /v1/runs, where it already travelled.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

type configScope string

const (
	scopeGlobal configScope = "global"
	scopeUser   configScope = "user"
)

// configSectionScope is the single source of truth for the split. GET /v1/config-schema publishes it
// verbatim, so the UI disables what a caller may not change instead of guessing — and instead of
// letting them fill in a form whose save is going to be refused.
var configSectionScope = map[string]configScope{
	"llm":      scopeGlobal,
	"settings": scopeGlobal,
	"logging":  scopeGlobal,
	"run":      scopeUser,
	"auth":     scopeUser,
}

// configSections returns the declared section names, sorted — for error messages and the schema, both
// of which are read by people and must not reorder between calls.
func configSections() []string {
	out := make([]string, 0, len(configSectionScope))
	for k := range configSectionScope {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// splitConfigDoc divides an incoming document by declared scope.
//
// It returns the two halves as raw sections rather than re-marshalling the whole document, so the
// bytes a caller sent are the bytes that get stored: a round trip through map[string]any would
// reorder members and rewrite numbers (1 becoming 1e+00), turning "save my config" into "save
// something equivalent to my config".
func splitConfigDoc(body []byte) (global, personal map[string]json.RawMessage, err error) {
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, nil, fmt.Errorf("config must be a JSON object: %w", err)
	}
	global, personal = map[string]json.RawMessage{}, map[string]json.RawMessage{}
	var unknown []string
	for name, raw := range doc {
		switch configSectionScope[name] {
		case scopeGlobal:
			global[name] = raw
		case scopeUser:
			personal[name] = raw
		default:
			unknown = append(unknown, name)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, nil, fmt.Errorf("unknown configuration section(s) %s: every section must declare whether it "+
			"belongs to the tool (admin) or to the person using it, and this one declares neither — known sections are %s",
			strings.Join(unknown, ", "), strings.Join(configSections(), ", "))
	}
	return global, personal, nil
}

// mergeConfigDocs overlays a personal document on the global one and reports where each section came
// from. The overlay is per SECTION, not per field: a person who set `run` owns the whole answer to
// "how do my runs start", and half-merging two `run` blocks would produce a configuration neither
// party wrote — with no way for either to see what the other contributed.
func mergeConfigDocs(global, personal map[string]json.RawMessage) (map[string]json.RawMessage, map[string]string) {
	out := map[string]json.RawMessage{}
	sources := map[string]string{}
	for k, v := range global {
		out[k], sources[k] = v, string(scopeGlobal)
	}
	for k, v := range personal {
		out[k], sources[k] = v, string(scopeUser)
	}
	return out, sources
}

// mayWriteGlobal reports whether this caller may change the tool's own configuration.
func mayWriteGlobal(c caller) bool { return c.machine || c.admin }

// globalSectionsIn names the global sections present in a document, for the refusal message. A 403
// that does not say WHICH member was refused leaves the caller to bisect their own document.
func globalSectionsIn(global map[string]json.RawMessage) string {
	names := make([]string, 0, len(global))
	for k := range global {
		names = append(names, k)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

// marshalConfigDoc re-serialises a section map with sorted keys, so the same document always produces
// the same bytes — a stored config that differs only in member order would show as a change in every
// diff an operator takes of it.
func marshalConfigDoc(doc map[string]json.RawMessage) ([]byte, error) {
	keys := make([]string, 0, len(doc))
	for k := range doc {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		key, err := json.Marshal(k)
		if err != nil {
			return nil, err
		}
		b.Write(key)
		b.WriteByte(':')
		b.Write(doc[k])
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}
