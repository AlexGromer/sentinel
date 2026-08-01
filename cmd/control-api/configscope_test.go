package main

// Gates for the config split (configscope.go) — Alex's directive extending ADR-109: the tool belongs
// to the master user, everything the working person configures belongs to them.
//
// The property being asserted is NOT "these five sections are classified". A gate that listed them
// would agree with a sixth section that nobody classified — which is exactly the failure mode the
// fail-closed refusal exists to prevent. So the gates ask instead: can an unclassified section reach
// the store at all, can a non-admin change the tool, and does one account's document ever touch
// another's.

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// storeBackedServer is a control-API with a real gateway and two accounts, one admin.
func storeBackedServer(t *testing.T) (*server, string, string, string) {
	t.Helper()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sc.close)
	s := newTestServer()
	s.store, s.storeAddr = sc, addr
	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x", IsAdmin: true})
	sc.upsertUser(&storepb.User{UserId: "ub", Name: "bob", PwHash: "x"})
	sc.upsertUser(&storepb.User{UserId: "uc", Name: "carol", PwHash: "x"})
	s.forgetAccounts()
	return s, s.sessions.mint("ua", "alice", true, sessionTTL()),
		s.sessions.mint("ub", "bob", false, sessionTTL()),
		s.sessions.mint("uc", "carol", false, sessionTTL())
}

func getConfigAs(t *testing.T, s *server, token string) (int, map[string]any) {
	t.Helper()
	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, token)
	return rec.Code, body
}

// TestUnclassifiedSectionIsRefusedAndNothingIsWritten is the fail-closed rule, and the second half
// matters as much as the first: a refusal that had already written the sections it recognised would
// leave a half-applied configuration behind an error message.
func TestUnclassifiedSectionIsRefusedAndNothingIsWritten(t *testing.T) {
	s, admin, _, _ := storeBackedServer(t)

	rec, body := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"telepathy":{"enabled":true}}`), admin)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("PUT with an unclassified section = %d, want 400 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "telepathy") {
		t.Errorf("the refusal does not name the offending section: %q", msg)
	}
	if code, _ := getConfigAs(t, s, admin); code != http.StatusNotFound {
		t.Errorf("a refused PUT still wrote something: GET /v1/config = %d, want 404", code)
	}
}

// TestNonAdminCannotChangeTheTool: the whole point of the split. And it is refused BY NAME — silently
// dropping the tool's sections would answer "saved" to someone whose change never happened.
func TestNonAdminCannotChangeTheTool(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"settings":{"log_keep":5}}`), admin); rec.Code != http.StatusOK {
		t.Fatalf("admin PUT of the tool's config = %d", rec.Code)
	}

	rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{"settings":{"log_keep":999}}`), bob)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("non-admin writing a global section = %d, want 403 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "settings") {
		t.Errorf("the refusal does not name the refused section: %q", msg)
	}
	// The document is the evidence, not the status code: a 403 that had written anyway would look
	// identical from here.
	_, got := getConfigAs(t, s, admin)
	cfg, _ := got["config"].(map[string]any)
	settings, _ := cfg["settings"].(map[string]any)
	if settings["log_keep"] != float64(5) {
		t.Errorf("a refused write changed the tool's configuration: log_keep = %v, want 5", settings["log_keep"])
	}
}

// TestPersonalSectionsAreEachAccountsOwn: bob's run settings are bob's, carol's are carol's, and
// neither is the deployment's.
func TestPersonalSectionsAreEachAccountsOwn(t *testing.T) {
	s, admin, bob, carol := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"run":{"max_steps":40}}`), admin); rec.Code != http.StatusOK {
		t.Fatalf("admin seeding config: %d", rec.Code)
	}
	for tok, steps := range map[string]int{bob: 10, carol: 99} {
		if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
			[]byte(`{"run":{"max_steps":`+strconv.Itoa(steps)+`}}`), tok); rec.Code != http.StatusOK {
			t.Fatalf("personal PUT (%d steps) = %d", steps, rec.Code)
		}
	}
	for tok, want := range map[string]float64{bob: 10, carol: 99} {
		_, got := getConfigAs(t, s, tok)
		cfg, _ := got["config"].(map[string]any)
		run, _ := cfg["run"].(map[string]any)
		if run["max_steps"] != want {
			t.Errorf("account read max_steps=%v, want %v — the personal layers are sharing a document", run["max_steps"], want)
		}
		// The tool's section still reaches them: a personal document layers OVER the global one, it does
		// not replace it. Without this a person who saved one preference would lose the deployment's LLM.
		if _, ok := cfg["llm"]; !ok {
			t.Error("the global llm section vanished for an account with a personal document")
		}
		// And `sources` says which layer each section came from — not derivable from the merged document,
		// and what an interface needs to show what a reset would restore.
		sources, _ := got["sources"].(map[string]any)
		if sources["run"] != "user" || sources["llm"] != "global" {
			t.Errorf("sources = %v, want run:user llm:global", sources)
		}
	}
	// The admin's own view is unaffected by either of them.
	_, adminGot := getConfigAs(t, s, admin)
	acfg, _ := adminGot["config"].(map[string]any)
	arun, _ := acfg["run"].(map[string]any)
	if arun["max_steps"] != float64(40) {
		t.Errorf("another account's personal document leaked into the global one: %v", arun["max_steps"])
	}
}

// TestMachineTokenWritesTheGlobalDocument pins the rule that keeps the setup wizard working unchanged:
// a caller with no subject writes the tool's document for BOTH halves. Before the split the wizard
// saved run/auth defaults for the deployment; it authenticates as the machine, and it still does.
func TestMachineTokenWritesTheGlobalDocument(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"run":{"max_steps":40},"auth":{"storage_state":"/s.json"}}`),
		s.token); rec.Code != http.StatusOK {
		t.Fatalf("machine PUT = %d", rec.Code)
	}
	// An account that has saved nothing of its own sees the deployment's defaults, run/auth included.
	for _, tok := range []string{admin, bob} {
		_, got := getConfigAs(t, s, tok)
		cfg, _ := got["config"].(map[string]any)
		run, _ := cfg["run"].(map[string]any)
		if run["max_steps"] != float64(40) {
			t.Errorf("the wizard's run defaults did not reach an account: %v", run["max_steps"])
		}
		sources, _ := got["sources"].(map[string]any)
		if sources["run"] != "global" {
			t.Errorf("sources[run] = %v, want global — nobody saved a personal one", sources["run"])
		}
	}
}

// TestMayWriteGlobalTravelsWithTheDocument: the interface has to disable what a caller cannot change,
// and it cannot infer that from the document.
func TestMayWriteGlobalTravelsWithTheDocument(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{"llm":{"backend":"anthropic"}}`), admin); rec.Code != http.StatusOK {
		t.Fatal(rec.Code)
	}
	for tok, want := range map[string]bool{admin: true, bob: false, s.token: true} {
		_, got := getConfigAs(t, s, tok)
		if got["may_write_global"] != want {
			t.Errorf("may_write_global = %v, want %v", got["may_write_global"], want)
		}
	}
}

// TestSchemaPublishesTheClassification: the map that ENFORCES the split is the map the UI reads, so a
// section cannot be admin-only in one and personal in the other.
func TestSchemaPublishesTheClassification(t *testing.T) {
	rec, body := doJSON(t, newTestServer(), http.MethodGet, "/v1/config-schema", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema = %d", rec.Code)
	}
	published, _ := body["config_sections"].(map[string]any)
	if len(published) != len(configSectionScope) {
		t.Fatalf("schema publishes %d sections, the enforcer knows %d", len(published), len(configSectionScope))
	}
	for name, scope := range configSectionScope {
		if published[name] != string(scope) {
			t.Errorf("section %q: schema says %v, the enforcer says %q", name, published[name], scope)
		}
	}
}

// TestTheWizardOnlyWritesClassifiedSections reads the section names out of the setup wizard's own
// document builder. The wizard is the main writer of this document and lives in another language, so
// a section added there without a classification would not fail until a person pressed save and got a
// 400 — in production, on their configuration, with no CI signal at all.
func TestTheWizardOnlyWritesClassifiedSections(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "setup", "index.html"))
	if err != nil {
		t.Fatal(err)
	}
	// buildConfigDoc ends with `return { llm: llm, run: run, auth: {...} };`
	body := regexp.MustCompile(`(?s)function buildConfigDoc\s*\(\)\s*\{.*?\n\}`).Find(raw)
	if body == nil {
		t.Fatal("buildConfigDoc not found in docs/setup/index.html — this gate would pass by reading nothing")
	}
	ret := regexp.MustCompile(`(?s)return \{(.*?)\n  \};`).FindSubmatch(body)
	if ret == nil {
		t.Fatalf("the return literal of buildConfigDoc did not parse — the gate must fail loudly rather "+
			"than silently stop checking:\n%s", body)
	}
	names := regexp.MustCompile(`(?m)^\s{4}(\w+):`).FindAllSubmatch(ret[1], -1)
	if len(names) == 0 {
		t.Fatalf("no section names found in the return literal:\n%s", ret[1])
	}
	for _, m := range names {
		name := string(m[1])
		if _, ok := configSectionScope[name]; !ok {
			t.Errorf("the setup wizard writes a %q section that configscope.go does not classify — saving "+
				"from the wizard would 400 in production", name)
		}
	}
}

// TestSplitPreservesTheCallersBytes: the stored document must be the document that was sent. Round
// tripping through map[string]any would rewrite numbers (40 becoming 4e+01) and reorder members, so
// "save my configuration" would quietly save something merely equivalent to it.
func TestSplitPreservesTheCallersBytes(t *testing.T) {
	s, admin, _, _ := storeBackedServer(t)
	const doc = `{"settings":{"log_keep":40,"heal_auto":0.85},"run":{"max_steps":40}}`
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(doc), admin); rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d", rec.Code)
	}
	rec, err := s.store.getConfig(setupConfigKey, "", storeCallTimeout)
	if err != nil || rec == nil {
		t.Fatalf("reading back: %v", err)
	}
	if !strings.Contains(rec.ValueJson, `"log_keep":40`) || !strings.Contains(rec.ValueJson, `"heal_auto":0.85`) {
		t.Errorf("the stored bytes were rewritten: %s", rec.ValueJson)
	}
	// Sections are stored in a stable order, so an operator diffing the stored document sees changes,
	// not member shuffling.
	var first, second map[string]json.RawMessage
	_ = json.Unmarshal([]byte(rec.ValueJson), &first)
	again, _ := marshalConfigDoc(first)
	_ = json.Unmarshal(again, &second)
	if reMarshalled, _ := marshalConfigDoc(second); string(reMarshalled) != string(again) {
		t.Error("re-serialising the same document produced different bytes")
	}
}
