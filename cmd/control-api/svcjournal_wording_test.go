package main

// HEALTH-005 PR-B — every service message must MATCH its catalogue template.
//
// WHY THIS GATE EXISTS. The catalogue is a second statement of each message's shape, and until now
// nothing compared it to the first. That is not cosmetic: the hub renders a record in the reader's
// language by taking the catalogue's EN template, extracting the field values out of the message the
// server actually sent, and substituting them into the RU template. When the two disagree the
// extraction fails and the row falls back to the raw English — silently, one row at a time.
//
// Six codes were doing exactly that, and every gate in this repository was green. `service.logout`
// emitted "Signed out" against a template of "Signed out: {actor}". `service.config_changed` emitted
// a list of sections against a template about keys and tiers. `service.api_refused` emitted the
// api_call sentence. It was found by LOOKING AT A SCREENSHOT of the view this PR adds — a Russian
// page with four English lines in it — which is the fourth time in two sessions that a screenshot has
// found what the gates could not.
//
// The property is checked against REAL emitted records: the server is driven through the paths that
// produce them, and each record's own `msg` is matched against the template for its own `code`. The
// four messages emitted from main() are unreachable from a test, so they are built by the same
// functions main() calls (svcjournal.go) rather than by copies of those expressions.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
	"github.com/AlexGromer/sentinel/internal/svclog"
)

// templateRE turns a catalogue template into the matcher the hub effectively applies: literals are
// literal, `{field}` is any run of characters. Anchored, because a template that matched a PREFIX
// would accept exactly the drift this gate exists to catch.
func templateRE(t *testing.T, tpl string) *regexp.Regexp {
	t.Helper()
	var b strings.Builder
	b.WriteString("^")
	for len(tpl) > 0 {
		i := strings.IndexByte(tpl, '{')
		if i < 0 {
			b.WriteString(regexp.QuoteMeta(tpl))
			break
		}
		j := strings.IndexByte(tpl[i:], '}')
		if j < 0 {
			b.WriteString(regexp.QuoteMeta(tpl))
			break
		}
		b.WriteString(regexp.QuoteMeta(tpl[:i]))
		b.WriteString("(.*?)")
		tpl = tpl[i+j+1:]
	}
	b.WriteString("$")
	re, err := regexp.Compile(b.String())
	if err != nil {
		t.Fatalf("template %q does not compile: %v", tpl, err)
	}
	return re
}

// catalogueEN reads the shipped catalogue — not a copy of it — for one code's English template.
func catalogueEN(t *testing.T, code string) string {
	t.Helper()
	var doc struct {
		Events map[string]struct {
			EN string `json:"en"`
			RU string `json:"ru"`
		} `json:"events"`
	}
	if err := json.Unmarshal(eventcatalog.Raw(), &doc); err != nil {
		t.Fatal(err)
	}
	e, ok := doc.Events[code]
	if !ok {
		t.Fatalf("%s is emitted but not catalogued — it would reach the UI through the "+
			"system.unclassified catch-all, at the wrong level and in English only", code)
	}
	if e.RU == "" {
		t.Fatalf("%s has no Russian phrasing", code)
	}
	return e.EN
}

// requireMatch is the whole property, in one place.
func requireMatch(t *testing.T, code, msg string) {
	t.Helper()
	tpl := catalogueEN(t, code)
	if !templateRE(t, tpl).MatchString(msg) {
		t.Errorf("%s: the message and its catalogue template disagree, so the hub cannot render this "+
			"row in Russian and will silently show the English\n  message:  %q\n  template: %q", code, msg, tpl)
	}
}

func TestEveryServiceMessageMatchesItsCatalogueTemplate(t *testing.T) {
	s := journalServer(t, "debug") // debug, so reads are recorded and api_call is covered too
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	s.forgetAccounts()
	mux := s.mux()

	call := func(method, path, body, bearer string) {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		mux.ServeHTTP(httptest.NewRecorder(), req)
	}

	// --- drive the paths that emit ------------------------------------------------------------
	call(http.MethodGet, "/v1/runs", "", s.token)                                                 // api_call
	call(http.MethodPost, "/v1/runs", "{}", "")                                                   // api_refused (403)
	call(http.MethodGet, "/readyz", "", "")                                                       // a probe, whatever it answers
	call(http.MethodPost, "/v1/users", `{"name":"alice","password":"pw-long-enough-1"}`, s.token) // account_created
	call(http.MethodPost, "/v1/login", `{"name":"nobody","password":"wrong"}`, "")                // login_failed
	call(http.MethodPost, "/v1/login", `{"name":"alice","password":"pw-long-enough-1"}`, "")      // login_ok
	call(http.MethodPost, "/v1/logout", "", s.sessions.mint("u-x", "alice", false, sessionTTL())) // logout
	call(http.MethodPut, "/v1/config", `{"run":{"planner":"heuristic"}}`, s.token)                // config_changed

	// A foreign-row reach needs two accounts and a row belonging to one of them.
	s.mu.Lock()
	s.runs["ownedbyalice"] = &run{ID: "ownedbyalice", Owner: "u-alice", State: "finished"}
	s.mu.Unlock()
	call(http.MethodGet, "/v1/runs/ownedbyalice", "", s.sessions.mint("u-bob", "bob", false, sessionTTL()))

	// The account goes LAST: deleting it is the event, and it must not remove rows the checks above
	// still need. Its id comes from the creation response rather than from a list, so a DELETE that
	// silently matched nothing could not pass as coverage.
	mk := httptest.NewRequest(http.MethodPost, "/v1/users",
		strings.NewReader(`{"name":"doomed","password":"pw-long-enough-2"}`))
	mk.Header.Set("Authorization", "Bearer "+s.token)
	mkRec := httptest.NewRecorder()
	mux.ServeHTTP(mkRec, mk)
	var made struct {
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(mkRec.Body.Bytes(), &made); err != nil || made.UserID == "" {
		t.Fatalf("could not create the account to delete: %s", mkRec.Body.String())
	}
	call(http.MethodDelete, "/v1/users/"+made.UserID, "", s.token)

	// --- and the four that only main() emits, through the functions main() calls -----------------
	direct := map[string]string{
		"service.started":           startedMsg("dev", "manual", 4242, " — addr: 127.0.0.1:8090"),
		"service.stopped":           stoppedMsg("signal terminated"),
		"service.token_source":      tokenSourceMsg("env", 0),
		"service.store_unreachable": storeUnreachableMsg("unix:/tmp/x.sock", "connection refused"),
		// The purge is agentctl's, and its message is asserted in cmd/agentctl. Named here so the
		// coverage check below cannot be satisfied by forgetting it.
	}
	for code, msg := range direct {
		requireMatch(t, code, msg)
	}

	// --- assert over what was actually written --------------------------------------------------
	seen := map[string]bool{}
	for _, rec := range readJournal(t, s.repo) {
		if !strings.HasPrefix(rec.Code, "service.") {
			continue
		}
		seen[rec.Code] = true
		requireMatch(t, rec.Code, rec.Msg)
	}
	for c := range direct {
		seen[c] = true
	}

	// A FLOOR, so a walk that emitted nothing cannot pass by asserting over an empty set — the
	// vacuous-pass shape this project keeps meeting in its own gates.
	want := []string{
		"service.api_call", "service.api_refused", "service.login_ok", "service.login_failed",
		"service.logout", "service.account_created", "service.config_changed", "service.foreign_row",
		"service.account_deleted",
		"service.started", "service.stopped", "service.token_source", "service.store_unreachable",
	}
	var missing []string
	for _, c := range want {
		if !seen[c] {
			missing = append(missing, c)
		}
	}
	if len(missing) > 0 {
		t.Errorf("%d service code(s) were never produced, so their wording is unchecked: %s",
			len(missing), strings.Join(missing, ", "))
	}
}

func TestAJournalLineNeverCarriesALabelWithNoValue(t *testing.T) {
	// Seen in the same screenshot: "…, global: run settings, personal: " — a label with nothing after
	// it. It reads as a value that failed to render rather than as a layer that was not written, which
	// is a worse answer than not mentioning the layer at all. The catalogue template cannot catch this
	// ({detail} absorbs anything), so it is asserted on the record.
	s := journalServer(t, "debug")
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	s.forgetAccounts()

	// The machine token has no subject, so it writes the GLOBAL layer and no personal one — the case
	// that produced the dangling label.
	req := httptest.NewRequest(http.MethodPut, "/v1/config", strings.NewReader(`{"run":{"planner":"heuristic"}}`))
	req.Header.Set("Authorization", "Bearer "+s.token)
	s.mux().ServeHTTP(httptest.NewRecorder(), req)

	var found bool
	for _, rec := range readJournal(t, s.repo) {
		if rec.Code != "service.config_changed" {
			continue
		}
		found = true
		if strings.HasSuffix(rec.Msg, ": ") || strings.HasSuffix(rec.Msg, ":") ||
			strings.Contains(rec.Msg, ": ,") {
			t.Errorf("a journal line ends in a label with no value — it reads as a failed render, "+
				"not as an absent layer: %q", rec.Msg)
		}
	}
	if !found {
		t.Fatal("no service.config_changed record was written, so this check asserted nothing")
	}
}

func TestAProbeAnsweringNotReadyIsNotFiledAsAFailure(t *testing.T) {
	// /readyz answers 503 for as long as a dependency is unconfigured — which for a standalone
	// deployment is forever, and is correct. Filed as `service.api_refused` at `error`, an orchestrator
	// polling readiness writes a red record every few seconds and buries the journal under the health
	// of a healthy service. Measured on a live deployment, in a screenshot of the journal view.
	s := journalServer(t, "debug")
	// A DECLARED but unreachable store, so /readyz genuinely answers 503. Without this the test server
	// has nothing configured, every dependency is `skipped`, readiness is 200 — and the check skipped
	// itself. Measured: with it skipping, BOTH mutations of the probe rule survived, including the one
	// that exempts every route from being recorded as a refusal.
	s.storeAddr = "unix:/nonexistent/sentinel-probe-gate.sock"

	mux := s.mux()
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code < 500 {
		t.Fatalf("/readyz answered %d with a declared-but-unreachable store — this check needs a real "+
			"5xx to mean anything, and skipping instead is how it passed while broken", rec.Code)
	}
	recs := readJournal(t, s.repo)
	if len(recs) == 0 {
		t.Fatal("the probe was not journalled at all")
	}
	last := recs[len(recs)-1]
	if last.Code == "service.api_refused" || last.Lvl == "error" {
		t.Errorf("a readiness probe answering %d was filed as %s/%s — its non-2xx is its contract, "+
			"not a fault", last.Status, last.Code, last.Lvl)
	}

	// The control: a route that is NOT a probe must still be filed as a failure, or this would be a
	// blanket amnesty rather than a declaration about two routes.
	mux.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader("{}")))
	recs = readJournal(t, s.repo)
	last = recs[len(recs)-1]
	if last.Code != "service.api_refused" {
		t.Errorf("a genuine refusal is no longer recorded as one (%s) — the probe exemption is "+
			"applying to everything", last.Code)
	}
}

// A compile-time reminder that the level vocabulary is shared, not copied.
var _ = svclog.Rank
