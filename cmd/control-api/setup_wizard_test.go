package main

// M11.5 PR-4 (ADR-061) — anti-drift gates for the setup wizard's embedded snapshots.
//
// docs/setup/index.html must render with no control-API reachable (GitHub Pages, file://, air-gapped
// bundle), so it embeds a snapshot of GET /v1/config-schema and of docs/backend-presets.json and renders
// from it until a live source overrides it. These tests are what keep that snapshot honest: they fail the
// moment the page falls behind the handler or the presets file, which is the "хардкод-дрейф" ADR-059 warns
// about. Style follows the package: flat func TestXxx + httptest, relative paths from the package dir.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// readWizard loads the wizard page. go test runs with CWD = the package dir (cmd/control-api).
func readWizard(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "setup", "index.html"))
	if err != nil {
		t.Fatalf("read docs/setup/index.html (must run from cmd/control-api): %v", err)
	}
	return string(raw)
}

// extractSnapshot pulls the `var <ident> = {...};` block between /* <name>-SNAPSHOT-BEGIN|END */ markers
// and parses it as strict JSON. The wizard must keep those literals JSON-parseable for exactly this reason.
func extractSnapshot(t *testing.T, html, name, ident string) map[string]any {
	t.Helper()
	re := regexp.MustCompile(`(?s)/\* ` + name + `-SNAPSHOT-BEGIN \*/(.*?)/\* ` + name + `-SNAPSHOT-END \*/`)
	m := re.FindStringSubmatch(html)
	if m == nil {
		t.Fatalf("docs/setup/index.html: %s-SNAPSHOT markers not found", name)
	}
	body := strings.TrimSpace(m[1])
	body = strings.TrimSpace(strings.TrimPrefix(body, "var "+ident+" ="))
	body = strings.TrimSuffix(body, ";")
	var out map[string]any
	if err := json.Unmarshal([]byte(body), &out); err != nil {
		t.Fatalf("%s snapshot is not strict JSON: %v", name, err)
	}
	return out
}

func liveConfigSchema(t *testing.T) map[string]any {
	t.Helper()
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema: got %d want 200", rec.Code)
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	return out
}

func sortedKeys(m map[string]any) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

// sub returns m[key] as a nested object, or fails.
func sub(t *testing.T, m map[string]any, key, what string) map[string]any {
	t.Helper()
	v, ok := m[key].(map[string]any)
	if !ok {
		t.Fatalf("%s: %q is not an object (got %T)", what, key, m[key])
	}
	return v
}

// prose names a key whose value is explanatory text for a human reading the payload, not a value the
// wizard renders from. The snapshot deliberately omits these; copying them in would grow the embedded
// literal to restate what the live handler already says, and none of it reaches a control.
var prose = map[string]bool{"note": true}

// schemaDiff walks both trees and returns the first divergence as a path, or "" when they agree.
//
// It WALKS rather than checking a list of blocks, and that distinction is the whole gate. The previous
// version compared the top-level key set and then descended into exactly `modes`, `planner`, `backends`,
// `roles`, `fields` and `llm`. `settings` — sixteen operator-facing knobs — was in the key set and so
// looked covered, while its contents were never compared at all. Two retention knobs drifted out of the
// snapshot underneath that, and air-gapped operators lost the ability to bound run retention with the
// gate green. A gate that enumerates what to check agrees with any implementation that shares its
// blind spot; one that walks cannot, and a block added tomorrow is covered without anyone remembering.
func schemaDiff(live, snap any, path string) string {
	at := func() string {
		if path == "" {
			return "(root)"
		}
		return path
	}
	lm, lok := live.(map[string]any)
	sm, sok := snap.(map[string]any)
	if lok != sok {
		return fmt.Sprintf("%s: shape differs — handler %T, wizard %T", at(), live, snap)
	}
	if lok {
		for _, k := range sortedKeys(lm) {
			if prose[k] {
				continue
			}
			sv, ok := sm[k]
			if !ok {
				return fmt.Sprintf("%s.%s: present in the handler, MISSING from the wizard snapshot "+
					"(handler value: %s)", at(), k, compact(lm[k]))
			}
			if d := schemaDiff(lm[k], sv, path+"."+k); d != "" {
				return d
			}
		}
		for _, k := range sortedKeys(sm) {
			if prose[k] {
				continue
			}
			if _, ok := lm[k]; !ok {
				return fmt.Sprintf("%s.%s: in the wizard snapshot but the handler no longer serves it", at(), k)
			}
		}
		return ""
	}
	if !reflect.DeepEqual(live, snap) {
		return fmt.Sprintf("%s: handler=%s wizard=%s", at(), compact(live), compact(snap))
	}
	return ""
}

func compact(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("%v", v)
	}
	if len(b) > 160 {
		return string(b[:160]) + "…"
	}
	return string(b)
}

// TestSetupWizardSchemaSnapshotMatchesHandler: the wizard's offline fallback schema still describes the
// whole surface the live handler serves. The wizard renders from it with no control-API reachable, so
// drift silently misconfigures exactly the operators who cannot check against a server.
func TestSetupWizardSchemaSnapshotMatchesHandler(t *testing.T) {
	snap := extractSnapshot(t, readWizard(t), "SCHEMA", "FALLBACK_SCHEMA")
	live := liveConfigSchema(t)

	if d := schemaDiff(live, snap, ""); d != "" {
		t.Fatalf("wizard schema snapshot drifted from GET /v1/config-schema:\n  %s", d)
	}

	// The walk proves the two trees AGREE. These two claims are different in kind and survive it.
	sl := sub(t, snap, "llm", "wizard")

	// Asymmetric, and a security claim rather than a drift one: the wizard must be STRICTER than the
	// handler about a secret. Equality alone would be satisfied by both sides carrying a key.
	ak := sub(t, sl, "api_key", "wizard llm")
	if ak["secret"] != true {
		t.Fatalf("llm.api_key.secret must stay true in the wizard snapshot, got %v", ak["secret"])
	}
	if _, leaked := ak["default"]; leaked {
		t.Fatalf("llm.api_key must never carry a default/value in the wizard snapshot")
	}

	// Cross-block identity: two places in one document name the same set, and the walk compares each
	// against its own counterpart without ever asking whether they still agree with each other.
	if !reflect.DeepEqual(sub(t, sl, "backend", "wizard llm")["enum"], live["backends"]) {
		t.Fatalf("wizard llm.backend.enum != handler backends: %v vs %v",
			sub(t, sl, "backend", "wizard llm")["enum"], live["backends"])
	}
}

// TestSetupWizardPresetsSnapshotMatchesFile: the wizard's offline preset list still matches the canonical
// docs/backend-presets.json it fetches at runtime (Pages/bundle). Same preset keys, same backend + base_url.
func TestSetupWizardPresetsSnapshotMatchesFile(t *testing.T) {
	snap := extractSnapshot(t, readWizard(t), "PRESETS", "FALLBACK_PRESETS")

	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "backend-presets.json"))
	if err != nil {
		t.Fatalf("read docs/backend-presets.json: %v", err)
	}
	var doc struct {
		Presets map[string]any `json:"presets"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("docs/backend-presets.json does not parse: %v", err)
	}

	if !reflect.DeepEqual(sortedKeys(snap), sortedKeys(doc.Presets)) {
		t.Fatalf("preset keys drifted:\n  wizard: %v\n  file: %v", sortedKeys(snap), sortedKeys(doc.Presets))
	}
	for _, k := range sortedKeys(doc.Presets) {
		fp, ok := doc.Presets[k].(map[string]any)
		if !ok {
			t.Fatalf("backend-presets.json: preset %q is not an object", k)
		}
		wp := sub(t, snap, k, "wizard presets")
		// label/note/default_model matter too: the wizard renders the label, and applyPreset() writes
		// default_model straight into LLM_MODEL_<ROLE>. Omitting them let stale model names ship offline.
		for _, attr := range []string{"backend", "base_url", "vision", "structured", "api_key", "label", "note", "default_model"} {
			if !reflect.DeepEqual(wp[attr], fp[attr]) {
				t.Fatalf("preset %s.%s drifted: wizard=%v file=%v", k, attr, wp[attr], fp[attr])
			}
		}
	}
}
