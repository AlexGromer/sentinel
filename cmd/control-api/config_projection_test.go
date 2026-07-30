package main

// ADR-107 completeness gates: the configuration model has ONE definition and three projections
// (HTTP, CLI, UI). These tests assert the HTTP projection is complete by WALKING the schema, never by
// listing the keys they expect to find.
//
// Why walking is the whole point. Before ADR-107 the hub rendered inputs for nine values the submit
// handler never read — budgets and the auth block — because `runRequest` had nowhere to put them. Any
// test written as "check these fields exist" would have been written from the same understanding that
// produced the gap, and would have agreed with it. A test that enumerates the schema cannot: a field
// added to the schema without a home on the request fails it the moment it is added.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
)

// requestJSONTags returns the set of json tag names settable by a client on runRequest.
// Unexported fields are deliberately excluded: `plan` and `llm` are server-resolved and must never
// become client-settable, so they are not part of the projection.
func requestJSONTags(t *testing.T) map[string]bool {
	t.Helper()
	out := map[string]bool{}
	rt := reflect.TypeOf(runRequest{})
	for i := 0; i < rt.NumField(); i++ {
		f := rt.Field(i)
		if f.PkgPath != "" { // unexported
			continue
		}
		tag := strings.Split(f.Tag.Get("json"), ",")[0]
		if tag == "" || tag == "-" {
			t.Fatalf("runRequest.%s has no json tag — an exported field with no tag is settable under its "+
				"Go name, which no schema describes", f.Name)
		}
		out[tag] = true
	}
	return out
}

func schemaFields(t *testing.T) map[string]map[string]any {
	t.Helper()
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema: got %d want 200", rec.Code)
	}
	var doc struct {
		Fields map[string]map[string]any `json:"fields"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	if len(doc.Fields) == 0 {
		t.Fatal("config-schema served no `fields` — this gate would pass vacuously")
	}
	return doc.Fields
}

// TestRunRequestCoversEverySchemaField: every per-run field the schema advertises is settable on
// POST /v1/runs. This is the gate that would have caught the ADR-107 gap.
func TestRunRequestCoversEverySchemaField(t *testing.T) {
	tags := requestJSONTags(t)
	for name := range schemaFields(t) {
		if !tags[name] {
			t.Errorf("schema advertises field %q but runRequest has no json:%q — a UI rendering the schema "+
				"would offer a control whose value the API silently drops", name, name)
		}
	}
}

// TestEverySchemaFieldDeclaresItsGroup: a field with no group cannot be laid out by a UI that renders
// from the schema, so it lands wherever the renderer's `else` puts it — which is how a budget input
// ends up next to a target URL.
func TestEverySchemaFieldDeclaresItsGroup(t *testing.T) {
	for name, spec := range schemaFields(t) {
		g, _ := spec["group"].(string)
		if g == "" {
			t.Errorf("schema field %q declares no `group` — a schema-driven form has nowhere to put it", name)
		}
		if _, ok := spec["type"].(string); !ok {
			t.Errorf("schema field %q declares no `type` — a renderer cannot choose an input for it", name)
		}
	}
}

// TestBudgetAndAuthFieldsReachAgentctl: the fields with NO agentctl flag (budgets, the auth block) must
// travel as a RunConfig file, and the ones WITH a flag must appear on the argv.
//
// This asserts the wiring end to end at the argv/file level rather than trusting that a struct field
// with a matching name is plumbed: the defect ADR-107 fixes was exactly a value that existed as an
// input, had a name, and reached nothing.
func TestBudgetAndAuthFieldsReachAgentctl(t *testing.T) {
	dir := t.TempDir()
	req := runRequest{
		Target: "http://example.test", PlanBudget: "111", HealBudget: "222", TotalBudget: "333",
		StorageState: "/tmp/state.json", LoginPlan: "/tmp/login.json", PWNoTrace: true,
	}
	path, err := writeRunConfig(dir, &req)
	if err != nil {
		t.Fatalf("writeRunConfig: %v", err)
	}
	if path == "" {
		t.Fatal("writeRunConfig wrote nothing for a request carrying budgets and auth")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	body := string(raw)
	for _, want := range []string{
		"plan_budget: 111", "heal_budget: 222", "total_budget: 333",
		"auth:", `storage_state: "/tmp/state.json"`, `login_plan: "/tmp/login.json"`, "pw_no_trace: true",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("run.yaml is missing %q\n--- file ---\n%s", want, body)
		}
	}

	// The keys the RunConfig loader accepts (brain/runconfig.py _KEY_ENV / _AUTH_ENV). A key we emit
	// under a name the loader ignores is silently dropped — the file would exist and change nothing.
	for _, emitted := range []string{"plan_budget", "heal_budget", "total_budget"} {
		if !strings.Contains(body, emitted+": ") {
			t.Errorf("budget %q is not emitted at the top level where the loader reads it", emitted)
		}
	}
}

// TestWriteRunConfigWritesNothingWhenThereIsNothingToWrite: a run carrying no budgets and no auth must
// spawn the same command it spawned before ADR-107, so `--run-config` has to stay off the argv.
func TestWriteRunConfigWritesNothingWhenThereIsNothingToWrite(t *testing.T) {
	path, err := writeRunConfig(t.TempDir(), &runRequest{Target: "http://example.test", Goal: "log in"})
	if err != nil {
		t.Fatalf("writeRunConfig: %v", err)
	}
	if path != "" {
		t.Errorf("writeRunConfig produced %q for a request with no budgets and no auth", path)
	}
}

// TestAppendRunFlagsPassesEveryFlaggedField: the fields that DO have an agentctl flag reach the argv.
// Driven from a table of (setter, expected flag) so a new flagged field is a one-line addition rather
// than a new test nobody writes.
func TestAppendRunFlagsPassesEveryFlaggedField(t *testing.T) {
	cases := []struct {
		name string
		set  func(*runRequest)
		want []string
	}{
		{"scenario", func(r *runRequest) { r.Scenario = "checkout" }, []string{"--scenario", "checkout"}},
		{"aut_version", func(r *runRequest) { r.AutVersion = "deadbeef" }, []string{"--aut-version", "deadbeef"}},
		{"ci", func(r *runRequest) { r.CI = true }, []string{"--ci"}},
		{"force_replay", func(r *runRequest) { r.ForceReplay = true }, []string{"--force-replay"}},
		{"heal_llm", func(r *runRequest) { r.HealLLM = true }, []string{"--heal-llm"}},
	}
	for _, c := range cases {
		var req runRequest
		c.set(&req)
		got := strings.Join(appendRunFlags([]string{"run"}, &req, ""), " ")
		if !strings.Contains(got, strings.Join(c.want, " ")) {
			t.Errorf("%s: argv %q does not carry %q", c.name, got, strings.Join(c.want, " "))
		}
		// And the flag must NOT appear when the field is unset, or the argv would carry it always and the
		// positive assertion above would hold for a constant.
		var empty runRequest
		if bare := strings.Join(appendRunFlags([]string{"run"}, &empty, ""), " "); strings.Contains(bare, c.want[0]) {
			t.Errorf("%s: argv %q carries %s for an empty request", c.name, bare, c.want[0])
		}
	}
	// The RunConfig path is a flag too, and its absence must keep the flag off.
	if got := strings.Join(appendRunFlags(nil, &runRequest{}, "/tmp/run.yaml"), " "); !strings.Contains(got, "--run-config /tmp/run.yaml") {
		t.Errorf("argv %q does not carry --run-config", got)
	}
	if got := strings.Join(appendRunFlags(nil, &runRequest{}, ""), " "); strings.Contains(got, "--run-config") {
		t.Errorf("argv %q carries --run-config with no config written", got)
	}
}

// TestCIAndForceReplayRejectedTogether: the pair is refused with a 400 rather than spawning a run that
// agentctl kills at startup.
func TestCIAndForceReplayRejectedTogether(t *testing.T) {
	s := newTestServer()
	body, _ := json.Marshal(runRequest{Target: "http://example.test", CI: true, ForceReplay: true})
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(string(body)))
	r.Header.Set("Authorization", "Bearer "+s.token)
	s.mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("ci+force_replay: got %d want 400 (body: %s)", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "mutually exclusive") {
		t.Errorf("the 400 does not say why: %s", rec.Body.String())
	}
}
