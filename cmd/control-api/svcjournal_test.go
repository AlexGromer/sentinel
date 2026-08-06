package main

// HEALTH-005 — the gate on the service journal.
//
// What is asserted is that the operations which were TRACELESS now leave a trace, and that the
// journal stays readable while doing it. Measured before this change: `session.go` and
// `configfile.go` had zero logging lines of any kind, so creating an account, deleting one, changing
// the global config and failing to sign in were all invisible; everything else went to stderr, which
// is the container's journal — not a file, not catalogued, not filterable, not in the UI.
//
// The coverage check is a WALK OF THE ROUTES TABLE, not a list of endpoints. A list would go stale on
// the day somebody adds a route, and the whole reason the hook lives in `guard` is that a route
// cannot be served without passing through it.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/AlexGromer/sentinel/internal/svclog"
)

// readJournal returns every record the server wrote, decoded.
func readJournal(t *testing.T, repo string) []svclog.Record {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(repo, "state", "logs", svclog.FileName))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		t.Fatal(err)
	}
	var out []svclog.Record
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if line == "" {
			continue
		}
		var r svclog.Record
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatalf("journal line is not valid JSON: %s", line)
		}
		out = append(out, r)
	}
	return out
}

// journalServer is a test server whose journal is open at `debug`, so reads are recorded too and the
// level rule can be asserted in both directions from one fixture.
func journalServer(t *testing.T, level string) *server {
	t.Helper()
	s := newTestServer()
	t.Setenv("SENTINEL_SERVICE_LOG_LEVEL", level)
	s.journal = svclog.Open(filepath.Join(s.repo, "state"), "control-api")
	if s.journal == nil {
		t.Fatal("could not open the journal")
	}
	t.Cleanup(s.journal.Close)
	return s
}

func TestEveryRouteIsJournalledBecauseTheHookIsInTheGuard(t *testing.T) {
	// The property, walked rather than listed: hit every route the table declares and require a record
	// for each. A route added tomorrow is covered by being in the table — which is the entire reason
	// the hook is in guard() and not in forty-two handlers.
	s := journalServer(t, "debug")
	mux := s.mux()
	var hit, missing []string
	for _, sp := range s.routes() {
		method, path, ok := strings.Cut(sp.pattern, " ")
		if !ok {
			t.Fatalf("route pattern has no method: %q", sp.pattern)
		}
		// A concrete id for the {id} routes; the journal records the PATTERN either way.
		concrete := strings.ReplaceAll(path, "{id}", "no-such-id-for-the-gate")
		before := len(readJournal(t, s.repo))
		req := httptest.NewRequest(method, concrete, strings.NewReader("{}"))
		req.Header.Set("Authorization", "Bearer "+s.token)
		mux.ServeHTTP(httptest.NewRecorder(), req)
		if len(readJournal(t, s.repo)) > before {
			hit = append(hit, sp.pattern)
		} else {
			missing = append(missing, sp.pattern)
		}
	}
	if len(missing) > 0 {
		t.Errorf("%d route(s) left no journal record: %s", len(missing), strings.Join(missing, ", "))
	}
	// A floor, so a change that made the walk find nothing cannot pass by asserting over an empty set —
	// the vacuous-pass shape this project keeps meeting in its own gates.
	if len(hit) < 30 {
		t.Errorf("only %d routes were exercised — the walk is not finding the table", len(hit))
	}
}

func TestTheDefaultLevelHidesReadsAndKeepsMutations(t *testing.T) {
	// Levels are how "record everything" survives contact with the hub, which polls /v1/runs/{id} every
	// 2s — ~300 records per ten-minute run, all of them a read nobody will come looking for.
	s := journalServer(t, svclog.DefaultLevel)
	mux := s.mux()

	get := httptest.NewRequest(http.MethodGet, "/v1/runs", nil)
	get.Header.Set("Authorization", "Bearer "+s.token)
	mux.ServeHTTP(httptest.NewRecorder(), get)
	if n := len(readJournal(t, s.repo)); n != 0 {
		t.Errorf("a read was journalled at the default level: %+v", readJournal(t, s.repo))
	}

	del := httptest.NewRequest(http.MethodDelete, "/v1/scenarios/gate-id", nil)
	del.Header.Set("Authorization", "Bearer "+s.token)
	mux.ServeHTTP(httptest.NewRecorder(), del)
	recs := readJournal(t, s.repo)
	if len(recs) == 0 {
		t.Fatal("a mutation was NOT journalled at the default level — the journal would be silent about " +
			"exactly what it exists for")
	}
	last := recs[len(recs)-1]
	if last.Method != http.MethodDelete || last.Route != "/v1/scenarios/{id}" {
		t.Errorf("want the route PATTERN on the record, got %s %s", last.Method, last.Route)
	}
	if last.Svc != "control-api" {
		t.Errorf("the record does not name its writer: %+v", last)
	}
	if last.Status == 0 || last.Actor == "" {
		t.Errorf("status/actor missing — a journal that records every call as 200 by nobody looks complete "+
			"and says nothing: %+v", last)
	}

	// A NON-200, because the check above cannot fail. `journalCall` defaults an unstamped status to
	// 200, so asserting "a status is present" is satisfied by the fallback rather than by the capture —
	// measured: a mutation that removed the capture entirely walked straight through it. A refused call
	// is the case where the recorded number has to come from the handler.
	unauth := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader("{}"))
	mux.ServeHTTP(httptest.NewRecorder(), unauth)
	recs = readJournal(t, s.repo)
	last = recs[len(recs)-1]
	if last.Status != http.StatusForbidden {
		t.Errorf("a refused call was journalled as %d — the status is being defaulted, not captured, "+
			"so every refusal in this journal reads as a success: %+v", last.Status, last)
	}
	if last.Code != "service.api_refused" || last.Lvl != "warn" {
		t.Errorf("a refusal must be its own code at `warn` — it is what somebody comes looking for: %+v", last)
	}
}

func TestASignInFailureIsRecordedAndThePasswordIsNot(t *testing.T) {
	s := journalServer(t, svclog.DefaultLevel)
	// A store, because handleLogin answers 503 before it ever looks at a credential when there is none
	// — so without one this would assert against a deployment that cannot have accounts at all, and
	// pass or fail for a reason that has nothing to do with journalling.
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	s.forgetAccounts()

	body := `{"name":"someone","password":"hunter2-not-in-the-journal"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/login", strings.NewReader(body))
	s.mux().ServeHTTP(httptest.NewRecorder(), req)

	recs := readJournal(t, s.repo)
	var found bool
	for _, r := range recs {
		if r.Code == "service.login_failed" {
			found = true
			if !strings.Contains(r.Msg, "someone") {
				t.Errorf("the failed sign-in does not say WHO was tried: %q", r.Msg)
			}
		}
		if strings.Contains(r.Msg, "hunter2-not-in-the-journal") {
			t.Fatalf("the password reached the journal: %q", r.Msg)
		}
	}
	if !found {
		t.Errorf("a failed sign-in left no service.login_failed record — the one line somebody comes "+
			"looking for. Records: %+v", recs)
	}
}

func TestTheJournalSurvivesNotBeingOpenable(t *testing.T) {
	// A nil journal must be safe everywhere: a service that refused to start over its own log file
	// would turn a logging problem into an outage, and one that started silently without a log would
	// be the silent degradation HEALTH-002 forbids. svclog.Open says so once; the callers carry on.
	s := newTestServer()
	s.journal = nil
	req := httptest.NewRequest(http.MethodGet, "/v1/runs", nil)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("a nil journal changed the response: %d", rec.Code)
	}
	s.journalEvent("service.login_ok", "info", "no journal here", nil) // must not panic
}

func TestTheRecorderForwardsEveryOptionalWriterInterface(t *testing.T) {
	// Asserted as a SET, not one member. The first version of this test checked http.Flusher alone, the
	// wrapper forwarded Flush alone, and TestStreamHandshakeAndIngest went red on the WebSocket
	// handshake with a 500 — because /v1/stream hijacks the connection and http.Hijacker had been
	// silently removed. Both failures are invisible in a status code, and the next wrapper will forget
	// a different interface, so the property is what is checked.
	var w http.ResponseWriter = &statusRecorder{ResponseWriter: httptest.NewRecorder()}
	for _, tc := range []struct {
		name string
		ok   bool
		why  string
	}{
		{"http.Flusher", func() bool { _, ok := w.(http.Flusher); return ok }(),
			"/v1/stream and /v1/live/mjpeg would arrive all at once at the end instead of streaming"},
		{"http.Hijacker", func() bool { _, ok := w.(http.Hijacker); return ok }(),
			"the WebSocket upgrade on /v1/stream answers 500"},
	} {
		if !tc.ok {
			t.Errorf("statusRecorder does not forward %s — %s", tc.name, tc.why)
		}
	}
}
