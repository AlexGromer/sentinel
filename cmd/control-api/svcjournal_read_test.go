package main

// HEALTH-005 PR-B — the gate on READING the service journal.
//
// Everything here goes through `s.mux()` rather than calling the reader directly. The capability is
// "an operator can read the journal over HTTP, and sees exactly what is theirs" — a test that called
// journalScope() would be measuring a copy of the boundary instead of the boundary, which is how
// ADR-092 shipped a false degradation and how the PR-1b gate measured a copy of the thing it guarded.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/AlexGromer/sentinel/internal/svclog"
)

// writeJournalFixture writes records verbatim to one of the journal's two generations. The reader is
// what is under test, so the records are placed rather than produced — that is what lets one fixture
// hold an admin's event, two accounts' events and an unowned one at the same time.
func writeJournalFixture(t *testing.T, repo, name string, recs []svclog.Record) {
	t.Helper()
	dir := filepath.Join(repo, "state", "logs")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	for _, r := range recs {
		line, err := json.Marshal(&r)
		if err != nil {
			t.Fatal(err)
		}
		b.Write(line)
		b.WriteByte('\n')
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte(b.String()), 0o640); err != nil {
		t.Fatal(err)
	}
}

// getServiceLog calls the real route with the given bearer and decodes the answer.
func getServiceLog(t *testing.T, s *server, bearer, query string) map[string]any {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/service-log"+query, nil)
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /v1/service-log%s -> %d: %s", query, rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("answer is not JSON: %v (%s)", err, rec.Body.String())
	}
	return out
}

// codesIn lists the `code` of every returned record, in the order they came back.
func codesIn(t *testing.T, body map[string]any) []string {
	t.Helper()
	raw, ok := body["records"].([]any)
	if !ok {
		t.Fatalf("no records array in %v", body)
	}
	var out []string
	for _, r := range raw {
		m, ok := r.(map[string]any)
		if !ok {
			t.Fatalf("a record is not an object: %v", r)
		}
		out = append(out, fmt.Sprint(m["code"]))
	}
	return out
}

// scopeFixture: one journal holding an unowned deployment event and one event each for two accounts.
// Seq is stamped as a real writer stamps it, starting at 1.
func scopeFixture(t *testing.T, s *server) {
	t.Helper()
	writeJournalFixture(t, s.repo, svclog.FileName, []svclog.Record{
		{Seq: 1, Lvl: "info", Cat: "service", Code: "service.started", Msg: "Service control-api started", Svc: "control-api"},
		{Seq: 2, Lvl: "info", Cat: "service", Code: "service.login_ok", Msg: "alice signed in", Actor: "alice", Owner: "u-alice", Svc: "control-api"},
		{Seq: 3, Lvl: "info", Cat: "service", Code: "service.config_changed", Msg: "bob changed the configuration", Actor: "bob", Owner: "u-bob", Svc: "control-api"},
		{Seq: 4, Lvl: "warn", Cat: "service", Code: "service.foreign_row", Msg: "bob reached for someone else's row", Actor: "bob", Owner: "u-bob", Svc: "control-api", Foreign: true},
	})
}

func TestTheMachineTokenAndAnAdminReadTheWholeJournal(t *testing.T) {
	s := newTestServer()
	scopeFixture(t, s)

	for _, tc := range []struct {
		who    string
		bearer string
	}{
		{"the machine token", s.token},
		{"an admin account", s.sessions.mint("u-root", "root", true, time.Hour)},
	} {
		body := getServiceLog(t, s, tc.bearer, "")
		if got := len(codesIn(t, body)); got != 4 {
			t.Errorf("%s got %d records, want all 4: %v", tc.who, got, codesIn(t, body))
		}
		if body["scoped"] != false {
			t.Errorf("%s was reported as scoped — it sees everything, and saying otherwise would be a "+
				"warning about a limit that is not there", tc.who)
		}
		if _, has := body["scope_reason"]; has {
			t.Errorf("%s was given a scope_reason for an unscoped view", tc.who)
		}
	}
}

func TestAnAccountSeesItsOwnEventsAndNeitherAnothersNorTheDeploymentsr(t *testing.T) {
	s := newTestServer()
	scopeFixture(t, s)

	body := getServiceLog(t, s, s.sessions.mint("u-alice", "alice", false, time.Hour), "")
	got := codesIn(t, body)
	if len(got) != 1 || got[0] != "service.login_ok" {
		t.Fatalf("alice saw %v — she owns exactly one of the four records", got)
	}
	// The two halves that matter separately: another ACCOUNT's rows, and the DEPLOYMENT's own history.
	// The second is the inversion of ADR-109's unowned-is-public rule, and it is the half a reviewer
	// would expect to be wrong.
	for _, forbidden := range []string{"service.config_changed", "service.foreign_row", "service.started"} {
		for _, c := range got {
			if c == forbidden {
				t.Errorf("alice was shown %s", forbidden)
			}
		}
	}
	if body["scoped"] != true {
		t.Error("a partial view did not say it was partial")
	}
	if r, _ := body["scope_reason"].(string); !strings.Contains(r, "admin") {
		t.Errorf("the scope reason does not name who CAN see the rest: %q", r)
	}
}

func TestTheCountsDoNotLeakWhatTheScopeHid(t *testing.T) {
	// `matched` is the number a caller would use to decide "there is more, page for it". If the scope
	// predicate ran after the count, that number would publish exactly the volume the scope exists to
	// withhold — an oracle for "how busy is this deployment" available to the weakest credential in it.
	s := newTestServer()
	scopeFixture(t, s)

	body := getServiceLog(t, s, s.sessions.mint("u-alice", "alice", false, time.Hour), "")
	matched, _ := body["matched"].(float64)
	if int(matched) != 1 {
		t.Errorf("matched=%v for a caller who may see 1 record — the count is taken before the scope", matched)
	}
	if body["truncated"] != false {
		t.Error("a complete page was reported as truncated")
	}
	// The control: `scanned` is deliberately NOT scoped — it counts lines read, is the same for every
	// caller, and a test that expected it scoped would be asserting the wrong property.
	if scanned, _ := body["scanned"].(float64); int(scanned) != 4 {
		t.Errorf("scanned=%v, want 4 — the reader did not read the whole file", scanned)
	}
}

func TestAJournalIsReadFromItsEndAndAcrossTheRotatedGeneration(t *testing.T) {
	// Two properties in one fixture because they are one behaviour: the answer to "what happened
	// recently" spans the rotation boundary, and comes from the END of the stream.
	s := newTestServer()
	writeJournalFixture(t, s.repo, svclog.Rotated, []svclog.Record{
		{Seq: 1, Lvl: "info", Cat: "service", Code: "service.started", Msg: "old generation", Svc: "control-api"},
	})
	var recent []svclog.Record
	for i := 1; i <= 5; i++ {
		recent = append(recent, svclog.Record{
			Seq: i, Lvl: "info", Cat: "service", Code: "service.config_changed",
			Msg: fmt.Sprintf("change %d", i), Svc: "control-api",
		})
	}
	writeJournalFixture(t, s.repo, svclog.FileName, recent)

	// Unlimited: the rotated generation is present, and it comes FIRST — chronological order across the
	// boundary, not "whatever the OS listed".
	all := getServiceLog(t, s, s.token, "")
	if n := len(codesIn(t, all)); n != 6 {
		t.Fatalf("got %d records over both generations, want 6: %v", n, codesIn(t, all))
	}
	if first := firstMsg(t, all); first != "old generation" {
		t.Errorf("the rotated generation is not first (%q) — the two halves are out of order", first)
	}

	// Limited: the TAIL, not the head. A head-first page of a journal is a page of its oldest records,
	// which is the one thing nobody opens a journal to see.
	page := getServiceLog(t, s, s.token, "?limit=2")
	msgs := msgsIn(t, page)
	if len(msgs) != 2 || msgs[0] != "change 4" || msgs[1] != "change 5" {
		t.Errorf("a limited page gave %v — want the last two records", msgs)
	}
	if page["truncated"] != true {
		t.Error("a page that dropped records did not say so")
	}
}

func TestAnUnstampedRecordIsStillShown(t *testing.T) {
	// A writer that does not stamp a sequence (the browser service writes from Node) must not vanish.
	// Written as an unconditional `Seq <= after`, the paging filter drops every such record with after
	// at its zero value — silently, and only for the stream that never advertised paging.
	s := newTestServer()
	writeJournalFixture(t, s.repo, svclog.FileName, []svclog.Record{
		{Lvl: "info", Cat: "service", Code: "service.started", Msg: "browser came up", Svc: "browser"},
	})
	if got := codesIn(t, getServiceLog(t, s, s.token, "")); len(got) != 1 {
		t.Errorf("an unstamped record was dropped: %v", got)
	}
}

func TestFiltersNarrowTheJournalByWriterAndActor(t *testing.T) {
	s := newTestServer()
	writeJournalFixture(t, s.repo, svclog.FileName, []svclog.Record{
		{Seq: 1, Lvl: "debug", Cat: "service", Code: "service.api_call", Msg: "a read", Actor: "alice", Svc: "control-api"},
		{Seq: 2, Lvl: "warn", Cat: "service", Code: "service.api_refused", Msg: "a refusal", Actor: "bob", Svc: "control-api"},
		{Seq: 3, Lvl: "info", Cat: "service", Code: "service.started", Msg: "browser came up", Svc: "browser"},
	})
	for _, tc := range []struct{ query, want string }{
		{"?svc=browser", "service.started"},
		{"?actor=bob", "service.api_refused"},
		{"?lvl=warn", "service.api_refused"},
		{"?code=service.api_call", "service.api_call"},
		{"?q=refusal", "service.api_refused"},
	} {
		got := codesIn(t, getServiceLog(t, s, s.token, tc.query))
		if len(got) != 1 || got[0] != tc.want {
			t.Errorf("%s selected %v, want exactly [%s]", tc.query, got, tc.want)
		}
	}
}

func TestAnAbsentJournalSaysSoRatherThanAnsweringAnEmptyPage(t *testing.T) {
	// "Nothing was recorded" and "nothing matched" send an operator to different places, and an empty
	// list alone cannot tell them apart — the distinction the empty-200 list endpoints got wrong.
	s := newTestServer()
	body := getServiceLog(t, s, s.token, "")
	if body["recorded"] != false {
		t.Error("an absent journal reported itself as recorded")
	}
	if r, _ := body["reason"].(string); !strings.Contains(r, svclog.FileName) {
		t.Errorf("the reason does not name the file that is missing: %q", r)
	}
	if p, _ := body["path"].(string); !strings.HasSuffix(p, filepath.Join("logs", svclog.FileName)) {
		t.Errorf("the answer does not say WHERE the journal would be: %q", p)
	}
}

func TestTheReaderReadsWhatTheWriterWrote(t *testing.T) {
	// The two halves of the capability, joined. Each is tested apart from the other above and in
	// svcjournal_test.go, and both can pass while the pair disagrees about the path, the file name or
	// the record shape — which is precisely what a written-but-unreadable journal looks like.
	s := journalServer(t, svclog.DefaultLevel)
	mux := s.mux()

	del := httptest.NewRequest(http.MethodDelete, "/v1/scenarios/gate-id", nil)
	del.Header.Set("Authorization", "Bearer "+s.token)
	mux.ServeHTTP(httptest.NewRecorder(), del)

	body := getServiceLog(t, s, s.token, "")
	if body["recorded"] != true {
		t.Fatalf("the journal the server just wrote reads as absent: %v", body)
	}
	var found bool
	for _, m := range msgsIn(t, body) {
		if strings.Contains(m, "/v1/scenarios/{id}") {
			found = true
		}
	}
	if !found {
		t.Errorf("the mutation this server journalled is not in what the reader returns: %v", msgsIn(t, body))
	}
}

func TestAnAccountCanReadTheRecordOfItsOwnSignIn(t *testing.T) {
	// The reason the read route is `authed` and not `admin`, asserted end to end against records the
	// PRODUCT wrote — not against a fixture whose owners this test set itself.
	//
	// That distinction is the whole point here. The scoping tests above place records with an Owner and
	// then check the scope honours it, and they passed while this was broken: a successful sign-in is
	// journalled BEFORE the session exists, so actorOf saw an anonymous caller and the record carried no
	// owner — which made it admin-only. An account could not see the one line about itself that it has
	// the clearest right to. Found on a live deployment, by asking as the account.
	s := journalServer(t, svclog.DefaultLevel)
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	s.forgetAccounts()
	mux := s.mux()

	mk := httptest.NewRequest(http.MethodPost, "/v1/users",
		strings.NewReader(`{"name":"alice","password":"correct-horse-battery"}`))
	mk.Header.Set("Authorization", "Bearer "+s.token)
	mkRec := httptest.NewRecorder()
	mux.ServeHTTP(mkRec, mk)
	var made struct {
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(mkRec.Body.Bytes(), &made); err != nil || made.UserID == "" {
		t.Fatalf("could not create the account: %s", mkRec.Body.String())
	}

	li := httptest.NewRequest(http.MethodPost, "/v1/login",
		strings.NewReader(`{"name":"alice","password":"correct-horse-battery"}`))
	liRec := httptest.NewRecorder()
	mux.ServeHTTP(liRec, li)
	var sess struct {
		Session string `json:"session"`
	}
	if err := json.Unmarshal(liRec.Body.Bytes(), &sess); err != nil || sess.Session == "" {
		t.Fatalf("alice could not sign in: %s", liRec.Body.String())
	}

	got := codesIn(t, getServiceLog(t, s, sess.Session, ""))
	var sawLogin, sawCreated bool
	for _, c := range got {
		switch c {
		case "service.login_ok":
			sawLogin = true
		case "service.account_created":
			sawCreated = true
		}
	}
	if !sawLogin {
		t.Errorf("alice cannot see the record of her own sign-in: %v — which is the reason this route "+
			"is `authed` rather than `admin`", got)
	}
	if !sawCreated {
		t.Errorf("alice cannot see that her account was created: %v — the record is ABOUT her, and the "+
			"admin who made it is the actor, not the subject", got)
	}
	// The control: she still must not see the deployment's own events, or this would be a test of
	// scoping being switched off.
	for _, c := range got {
		if c == "service.started" || c == "service.token_source" {
			t.Errorf("alice was shown the deployment event %s", c)
		}
	}
}

func TestARunLogIsStillReadFromItsStart(t *testing.T) {
	// Non-degradation. The two readers are one function with a flag, so a mistake in the flag would
	// silently turn the run log — which is read forward, from the first line of a finite file — into a
	// tail. Nothing about a run log's response shape reveals that.
	s := newTestServer()
	dir := filepath.Join(s.repo, "runs", "control-abc123", "logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	for i := 1; i <= 5; i++ {
		b.WriteString(fmt.Sprintf(`{"seq":%d,"ts":"t","lvl":"info","cat":"run","code":"run.step","msg":"line %d"}`+"\n", i, i))
	}
	if err := os.WriteFile(filepath.Join(dir, "run.jsonl"), []byte(b.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/abc123/logs?limit=2", nil)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	msgs := msgsIn(t, body)
	if len(msgs) != 2 || msgs[0] != "line 1" || msgs[1] != "line 2" {
		t.Errorf("a run's first page gave %v — want the FIRST two lines", msgs)
	}
}

func TestCollapsingRepeatsIsNotReportedAsTruncation(t *testing.T) {
	// `truncated` is what tells a caller "there is more, ask for it". Computed as `matched > len(records)`
	// it also fires when five identical records collapsed into one carrying a count — nothing was
	// withheld, and the caller is sent paging for records that do not exist.
	s := newTestServer()
	dir := filepath.Join(s.repo, "runs", "control-abc123", "logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	for i := 1; i <= 5; i++ {
		b.WriteString(fmt.Sprintf(`{"seq":%d,"ts":"t","lvl":"info","cat":"run","code":"run.retry","msg":"same"}`+"\n", i))
	}
	if err := os.WriteFile(filepath.Join(dir, "run.jsonl"), []byte(b.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/abc123/logs", nil)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if len(msgsIn(t, body)) != 1 {
		t.Fatalf("the five identical records did not collapse: %v", msgsIn(t, body))
	}
	if body["truncated"] != false {
		t.Error("collapsing was reported as truncation — a caller would page for records nothing withheld")
	}
}

func firstMsg(t *testing.T, body map[string]any) string {
	t.Helper()
	m := msgsIn(t, body)
	if len(m) == 0 {
		t.Fatal("no records")
	}
	return m[0]
}

func msgsIn(t *testing.T, body map[string]any) []string {
	t.Helper()
	raw, ok := body["records"].([]any)
	if !ok {
		t.Fatalf("no records array in %v", body)
	}
	var out []string
	for _, r := range raw {
		m, ok := r.(map[string]any)
		if !ok {
			t.Fatalf("a record is not an object: %v", r)
		}
		out = append(out, fmt.Sprint(m["msg"]))
	}
	return out
}
