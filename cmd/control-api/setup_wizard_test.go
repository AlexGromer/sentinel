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

// TestSetupWizardSchemaSnapshotMatchesHandler: the wizard's offline fallback schema still describes the
// surface the live handler serves — same top-level keys, same enums, same fields (incl. default/required)
// and same llm descriptor env names. The wizard renders from these, so drift silently misconfigures users.
func TestSetupWizardSchemaSnapshotMatchesHandler(t *testing.T) {
	snap := extractSnapshot(t, readWizard(t), "SCHEMA", "FALLBACK_SCHEMA")
	live := liveConfigSchema(t)

	if !reflect.DeepEqual(sortedKeys(snap), sortedKeys(live)) {
		t.Fatalf("top-level keys drifted:\n  wizard: %v\n  handler: %v", sortedKeys(snap), sortedKeys(live))
	}
	for _, k := range []string{"modes", "planner", "backends", "roles"} {
		if !reflect.DeepEqual(snap[k], live[k]) {
			t.Fatalf("%s drifted:\n  wizard: %v\n  handler: %v", k, snap[k], live[k])
		}
	}

	sf, lf := sub(t, snap, "fields", "wizard"), sub(t, live, "fields", "handler")
	if !reflect.DeepEqual(sortedKeys(sf), sortedKeys(lf)) {
		t.Fatalf("fields drifted:\n  wizard: %v\n  handler: %v", sortedKeys(sf), sortedKeys(lf))
	}
	for _, k := range sortedKeys(lf) {
		sd, ld := sub(t, sf, k, "wizard fields"), sub(t, lf, k, "handler fields")
		for _, attr := range []string{"type", "default", "required"} {
			if !reflect.DeepEqual(sd[attr], ld[attr]) {
				t.Fatalf("fields.%s.%s drifted: wizard=%v handler=%v", k, attr, sd[attr], ld[attr])
			}
		}
	}

	sl, ll := sub(t, snap, "llm", "wizard"), sub(t, live, "llm", "handler")
	if !reflect.DeepEqual(sortedKeys(sl), sortedKeys(ll)) {
		t.Fatalf("llm descriptors drifted:\n  wizard: %v\n  handler: %v", sortedKeys(sl), sortedKeys(ll))
	}
	for _, k := range sortedKeys(ll) {
		sd, ld := sub(t, sl, k, "wizard llm"), sub(t, ll, k, "handler llm")
		if sd["env"] != ld["env"] {
			t.Fatalf("llm.%s.env drifted: wizard=%v handler=%v", k, sd["env"], ld["env"])
		}
		// the wizard must not carry a value for a secret descriptor, and must keep the secret flag
		if k == "api_key" {
			if sd["secret"] != true {
				t.Fatalf("llm.api_key.secret must stay true in the wizard snapshot, got %v", sd["secret"])
			}
			if _, leaked := sd["default"]; leaked {
				t.Fatalf("llm.api_key must never carry a default/value in the wizard snapshot")
			}
		}
	}
	if !reflect.DeepEqual(sl["backend"].(map[string]any)["enum"], live["backends"]) {
		t.Fatalf("wizard llm.backend.enum != handler backends: %v vs %v",
			sl["backend"].(map[string]any)["enum"], live["backends"])
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
