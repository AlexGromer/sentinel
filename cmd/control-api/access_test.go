package main

// Gates for ADR-109's second half (cmd/control-api/access.go).
//
// These walk the route DECLARATION TABLE rather than naming routes. A gate that listed the routes it
// knew about would agree with a new one that scoped nothing — which is how the first half of this
// milestone shipped thirteen unscoped routes past a full test suite: every test that existed passed,
// because no test asked the question of routes nobody had thought about.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// requestFor builds a request from a route declaration, substituting id for the {id} wildcard. Every
// route in the table can be exercised this way, which is what lets the gates below be exhaustive
// without a per-route fixture.
func requestFor(sp routeSpec, id string) *http.Request {
	path := strings.ReplaceAll(routePath(sp.pattern), "{id}", id)
	var body *strings.Reader
	method := routeMethod(sp.pattern)
	if method == http.MethodPost || method == http.MethodPut {
		body = strings.NewReader(`{}`)
	}
	if body != nil {
		req := httptest.NewRequest(method, path, body)
		req.Header.Set("Content-Type", "application/json")
		return req
	}
	return httptest.NewRequest(method, path, nil)
}

// TestEveryRouteDeclaresItsAccess is the structural half: a declaration that is not a decision (an
// open route with no reason, a domain nobody resolves) fails here rather than in production.
func TestEveryRouteDeclaresItsAccess(t *testing.T) {
	known := map[ownedDomain]bool{
		domainRun: true, domainScenario: true, domainTest: true, domainChat: true, domainResult: true,
	}
	seen := map[string]bool{}
	for _, sp := range newTestServer().routes() {
		if seen[sp.pattern] {
			t.Errorf("%s is declared twice — one of the two is dead, and which one depends on map order", sp.pattern)
		}
		seen[sp.pattern] = true
		switch sp.access {
		case accessOpen:
			if strings.TrimSpace(sp.why) == "" {
				t.Errorf("%s is open with no stated reason: an open route in a product that has accounts is a "+
					"decision, and an undocumented one gets copied", sp.pattern)
			}
			if sp.domain != "" {
				t.Errorf("%s is open AND names an owned row (%s) — an anonymous caller has no owner to compare against",
					sp.pattern, sp.domain)
			}
		case accessAuthed, accessAdmin:
			if sp.why != "" {
				t.Errorf("%s carries a `why` but is not open; the field exists to justify openness", sp.pattern)
			}
		default:
			t.Errorf("%s declares access %q, which the guard does not implement", sp.pattern, sp.access)
		}
		if sp.domain != "" && !known[sp.domain] {
			t.Errorf("%s scopes by domain %q, which ownerOfRow cannot resolve — it would pass everything",
				sp.pattern, sp.domain)
		}
		if sp.legacyOpen && sp.access != accessAuthed {
			t.Errorf("%s is legacyOpen but not accessAuthed: the flag only relaxes a credential requirement", sp.pattern)
		}
		if sp.h == nil {
			t.Errorf("%s has no handler", sp.pattern)
		}
	}
	if len(seen) < 30 {
		t.Fatalf("only %d routes declared — the table has shrunk unexpectedly, and these gates would pass by "+
			"walking almost nothing", len(seen))
	}
}

// TestEveryCredentialledRouteRefusesAnonymous walks the table and sends each route NO credential.
// A new route inherits this the moment it is declared.
func TestEveryCredentialledRouteRefusesAnonymous(t *testing.T) {
	s := newTestServer()
	checked := 0
	for _, sp := range s.routes() {
		if sp.access == accessOpen || sp.legacyOpen {
			continue // open by declaration; legacyOpen is covered by its own gate below
		}
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, requestFor(sp, "any-id"))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s without a credential: got %d, want 403", sp.pattern, rec.Code)
		}
		checked++
	}
	if checked < 20 {
		t.Fatalf("only %d routes were checked — the filter above is excluding too much", checked)
	}
}

// TestOpenRoutesStayOpenWhenAccountsExist pins the other side of the same decision: probes and the
// login endpoint must NOT start refusing because somebody created an account. A readiness probe that
// began answering 403 would take a deployment down at the moment identity was switched on.
func TestOpenRoutesStayOpenWhenAccountsExist(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	sc.upsertUser(&storepb.User{UserId: "u1", Name: "alex", PwHash: "x"})
	s.forgetAccounts()
	if !s.accountsExist() {
		t.Fatal("accountsExist() is false right after an account was created")
	}
	for _, sp := range s.routes() {
		if sp.access != accessOpen {
			continue
		}
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, requestFor(sp, "any-id"))
		if rec.Code == http.StatusForbidden {
			t.Errorf("%s answered 403 with accounts present, but is declared open: %s", sp.pattern, sp.why)
		}
	}
}

// TestLegacyOpenReadsTightenOnTheFirstAccount is the behaviour Alex chose: the pre-identity open reads
// keep answering anonymously until an account exists, and stop the moment one does.
func TestLegacyOpenReadsTightenOnTheFirstAccount(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc

	legacy := 0
	for _, sp := range s.routes() {
		if !sp.legacyOpen {
			continue
		}
		legacy++
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, requestFor(sp, "no-such-run"))
		if rec.Code == http.StatusForbidden {
			t.Errorf("%s refused an anonymous caller BEFORE any account existed — that breaks every "+
				"pre-identity status poll at upgrade time", sp.pattern)
		}
	}
	if legacy == 0 {
		t.Fatal("no legacyOpen routes in the table — this gate is measuring nothing")
	}

	sc.upsertUser(&storepb.User{UserId: "u1", Name: "alex", PwHash: "x"})
	s.forgetAccounts()
	for _, sp := range s.routes() {
		if !sp.legacyOpen {
			continue
		}
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, requestFor(sp, "no-such-run"))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s still answered anonymously (%d) after an account existed — an unauthenticated "+
				"caller is unscoped, so this is every account's rows", sp.pattern, rec.Code)
		}
	}
}

// rawStore dials the gateway directly, for the domains control-api itself never writes (chats are
// projected by the BRAIN, so storeClient has no upsert for them).
func rawStore(t *testing.T, addr string) storepb.StoreServiceClient {
	t.Helper()
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return storepb.NewStoreServiceClient(conn)
}

// seedRow creates one row of `domain` owned by `owner` and returns the id that names it.
func seedRow(t *testing.T, s *server, raw storepb.StoreServiceClient, domain ownedDomain, owner string) string {
	t.Helper()
	id := "row-" + owner + "-" + string(domain)
	switch domain {
	case domainRun:
		dir := filepath.Join(s.repo, "runs", "control-"+id)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		rec := &run{ID: id, Owner: owner, State: "done", ArtifactDir: dir, stream: newRunStream()}
		rec.stream.finish() // no live process: let the SSE handler drain and return instead of blocking
		s.mu.Lock()
		s.runs[id] = rec
		s.mu.Unlock()
		s.store.upsertRun(rec)
	case domainScenario:
		s.store.saveScenario(&storepb.Scenario{ScenarioId: id, Name: id, Target: "http://x", Owner: owner})
	case domainTest:
		// A test only exists by promotion, so the scenario it is frozen from is seeded first. The test
		// inherits the PROMOTER, not the scenario (ADR-109) — which is why owner travels on PromoteReq.
		s.store.saveScenario(&storepb.Scenario{ScenarioId: id + "-src", Name: id, Target: "http://x", Owner: owner})
		tr, ok := s.store.promoteTest(&storepb.PromoteReq{ScenarioId: id + "-src", Name: id, Owner: owner})
		if !ok || !tr.Found {
			t.Fatalf("promote for %s owner=%s failed", domain, owner)
		}
		return tr.TestId
	case domainChat:
		if _, err := raw.UpsertChat(context.Background(), &storepb.ChatProjection{
			ConversationId: id, LastTarget: "http://x", TurnCount: 1, Owner: owner}); err != nil {
			t.Fatal(err)
		}
	case domainResult:
		s.store.saveResult(&storepb.ResultRecord{RunId: id, Verdict: "passed", Owner: owner})
	default:
		t.Fatalf("seedRow does not know how to create a %s", domain)
	}
	return id
}

// TestScopedRoutesRefuseAnotherAccountsRow is the gate this whole change exists for.
//
// For every route that names an owned row, it seeds that row for account A and asks for it as account
// B. B must be answered as though the row did not exist. Crucially it ALSO asks as A and requires a
// different answer: a server that replied 404 to everyone would satisfy the first half alone, and
// "nobody can read anything" is not the property being asserted.
func TestScopedRoutesRefuseAnotherAccountsRow(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	raw := rawStore(t, addr)

	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x"})
	sc.upsertUser(&storepb.User{UserId: "ub", Name: "bob", PwHash: "x"})
	s.forgetAccounts()
	tokA := s.sessions.mint("ua", "alice", false, sessionTTL())
	tokB := s.sessions.mint("ub", "bob", false, sessionTTL())

	// One row per domain, all owned by A. Seeded once and reused across every route of that domain.
	owned := map[ownedDomain]string{}
	scoped := 0
	for _, sp := range s.routes() {
		if sp.domain == "" {
			continue
		}
		scoped++
		id, ok := owned[sp.domain]
		if !ok {
			id = seedRow(t, s, raw, sp.domain, "ua")
			owned[sp.domain] = id
		}

		reqB := requestFor(sp, id)
		reqB.Header.Set("Authorization", "Bearer "+tokB)
		recB := httptest.NewRecorder()
		s.mux().ServeHTTP(recB, reqB)

		wantB := http.StatusNotFound
		if routeMethod(sp.pattern) == http.MethodDelete {
			wantB = http.StatusOK // idempotent, indistinguishable from deleting a row that is not there
		}
		if recB.Code != wantB {
			t.Errorf("%s as another account: got %d want %d (body %s)",
				sp.pattern, recB.Code, wantB, strings.TrimSpace(recB.Body.String()))
		}
		// For a DELETE the status code is deliberately indistinguishable from success, so the code is not
		// evidence — the row is. Checked HERE, before the owner's control request, because that request
		// will legitimately delete it.
		if routeMethod(sp.pattern) == http.MethodDelete {
			if owner, found := s.ownerOfRow(sp.domain, id); !found || owner != "ua" {
				t.Errorf("%s: another account's DELETE actually removed the row (found=%v owner=%q)",
					sp.pattern, found, owner)
			}
		}

		reqA := requestFor(sp, id)
		reqA.Header.Set("Authorization", "Bearer "+tokA)
		recA := httptest.NewRecorder()
		s.mux().ServeHTTP(recA, reqA)
		if recA.Code == http.StatusNotFound {
			t.Errorf("%s as the OWNER: got 404 — the route refuses everyone, so the refusal above proves "+
				"nothing about scoping (body %s)", sp.pattern, strings.TrimSpace(recA.Body.String()))
		}
		if routeMethod(sp.pattern) == http.MethodDelete {
			delete(owned, sp.domain) // the owner's control request consumed it; the next route reseeds
		}
	}
	if scoped < 10 {
		t.Fatalf("only %d scoped routes walked — the table lost its domains and this gate stopped measuring", scoped)
	}
}

// deadStore is a store client pointed at a socket nobody is listening on. grpc.NewClient is lazy, so
// it constructs fine and fails on every call — which is what a gateway that died after boot looks
// like from in here.
func deadStore(t *testing.T) *storeClient {
	t.Helper()
	conn, err := grpc.NewClient("unix:"+filepath.Join(t.TempDir(), "nobody.sock"),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return &storeClient{cl: storepb.NewStoreServiceClient(conn), conn: conn}
}

// TestAnUnreachableGatewayDoesNotReopenTheLegacyReads — found by a SURVIVING mutation.
//
// accountsExist() asks the store "are there accounts?", and the honest-looking answer to a gateway
// that does not respond is "no". That answer re-opens every legacyOpen route to anonymous callers for
// as long as the store is down: a transport failure would silently become an access-control decision,
// and the failure mode is invisible — the routes answer 200 exactly as they did before accounts.
func TestAnUnreachableGatewayDoesNotReopenTheLegacyReads(t *testing.T) {
	s := newTestServer()
	s.store = deadStore(t)

	if !s.accountsExist() {
		t.Fatal("an unreachable gateway was read as 'no accounts exist' — the legacy reads just re-opened")
	}
	legacy := 0
	for _, sp := range s.routes() {
		if !sp.legacyOpen {
			continue
		}
		legacy++
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, requestFor(sp, "any-id"))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s answered %d anonymously while the gateway was unreachable", sp.pattern, rec.Code)
		}
	}
	if legacy == 0 {
		t.Fatal("no legacyOpen routes — this gate is measuring nothing")
	}

	// A gateway that answered EARLIER leaves a known answer, and that answer is what a later hiccup must
	// fall back to — otherwise a deployment with genuinely no accounts would start refusing anonymous
	// polls the first time the store stuttered, which is the opposite mistake.
	s.accounts.mu.Lock()
	s.accounts.known, s.accounts.exists = true, false
	s.accounts.mu.Unlock()
	if s.accountsExist() {
		t.Error("a store hiccup overrode the last known answer (no accounts) — anonymous polls would start failing")
	}
}

// TestALiveRunIsScopedBeforeItReachesTheStore — found by a SURVIVING mutation.
//
// ownerOfRow consults the in-memory map first because a run that is RUNNING may not be in the store
// yet (and in a deployment with no gateway, never will be). Dropping that lookup leaves the guard
// unable to resolve an owner, and an unresolvable owner is treated as "not found" — which passes.
// So every live run would be readable by every account: the run a person is most likely to be
// looking at is the one that leaks. This is the same shape as the surviving mutation in the first
// half of ADR-109, where scoping only the persisted rows handed out every running run.
func TestALiveRunIsScopedBeforeItReachesTheStore(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x"})
	sc.upsertUser(&storepb.User{UserId: "ub", Name: "bob", PwHash: "x"})
	s.forgetAccounts()

	// In memory only — NOT persisted. This is what a run looks like between spawn and its first upsert,
	// and permanently in a deployment with no gateway.
	rec := &run{ID: "live-1", Owner: "ua", State: "running", stream: newRunStream()}
	rec.stream.finish()
	s.mu.Lock()
	s.runs["live-1"] = rec
	s.mu.Unlock()

	if _, found := sc.getRun("live-1"); found {
		t.Fatal("fixture is wrong: the run must NOT be in the store, or this proves nothing")
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/runs/live-1", nil)
	req.Header.Set("Authorization", "Bearer "+s.sessions.mint("ub", "bob", false, sessionTTL()))
	w := httptest.NewRecorder()
	s.mux().ServeHTTP(w, req)
	if w.Code != http.StatusNotFound {
		t.Errorf("another account read a LIVE run that is not yet persisted: got %d want 404", w.Code)
	}

	req = httptest.NewRequest(http.MethodGet, "/v1/runs/live-1", nil)
	req.Header.Set("Authorization", "Bearer "+s.sessions.mint("ua", "alice", false, sessionTTL()))
	w = httptest.NewRecorder()
	s.mux().ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("the OWNER could not read their own live run: got %d want 200", w.Code)
	}
}

// TestSpawnHandsItsOwnStoreToTheRun is the control-api half of "one store, not two".
//
// agentctl starts its own gateway over repo/state/locators.db when it inherits no address. So a run
// launched from here persisted into a database this process never reads — the brain wrote the `chats`
// projection there, and GET /v1/chats answered 0 about a conversation that plainly existed. Both
// halves are needed and each is invisible without the other, so each is pinned where it lives.
func TestSpawnHandsItsOwnStoreToTheRun(t *testing.T) {
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s, _, envPath := newEnvCapturingServer(t)
	s.store, s.storeAddr = sc, addr

	postRunAndWait(t, s, runBodyJSON(t, map[string]any{"target": "file:///x.html", "mode": "goal", "goal": "g"}))
	if got := readEnvFile(t, envPath)["STORE_ADDR"]; got != addr {
		t.Errorf("spawn env STORE_ADDR = %q, want %q — without it the run opens a SECOND database and the "+
			"chats projection lands where this process never reads", got, addr)
	}
}

// TestSpawnWithoutAStoreHandsDownNothing: the address is passed only when the gateway actually
// answered at boot. Handing down one that did not dial would replace agentctl's working local
// fallback with a dead address, turning "no persistence" into "a run that cannot reach its store".
func TestSpawnWithoutAStoreHandsDownNothing(t *testing.T) {
	s, _, envPath := newEnvCapturingServer(t)
	s.store, s.storeAddr = nil, "unix:/tmp/never-dialled.sock" // configured, did not answer

	postRunAndWait(t, s, runBodyJSON(t, map[string]any{"target": "file:///x.html", "mode": "goal", "goal": "g"}))
	if got := readEnvFile(t, envPath)["STORE_ADDR"]; got != "" {
		t.Errorf("spawn env STORE_ADDR = %q, want empty: the gateway never answered, so the run must keep "+
			"its own local fallback", got)
	}
}

// TestMachineTokenStaysUnscoped: CI and agentctl authenticate as the machine and must keep seeing
// every row, exactly as before. This is the rule that lets the scoping above be safe to add.
func TestMachineTokenStaysUnscoped(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	raw := rawStore(t, addr)
	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x"})
	s.forgetAccounts()
	id := seedRow(t, s, raw, domainScenario, "ua")

	req := requestFor(routeSpec{pattern: "GET /v1/scenarios/{id}"}, id)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("machine token reading another account's scenario: got %d want 200", rec.Code)
	}
}

// TestUnownedRowsStayVisible pins the opt-in rule: rows written before accounts existed belong to
// nobody and must not disappear from the person who has since logged in.
func TestUnownedRowsStayVisible(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	raw := rawStore(t, addr)
	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x"})
	s.forgetAccounts()
	id := seedRow(t, s, raw, domainScenario, "") // pre-identity row

	req := requestFor(routeSpec{pattern: "GET /v1/scenarios/{id}"}, id)
	req.Header.Set("Authorization", "Bearer "+s.sessions.mint("ua", "alice", false, sessionTTL()))
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("a logged-in account reading an UNOWNED row: got %d want 200 — identity is opt-in, and a "+
			"row written before it existed belongs to nobody", rec.Code)
	}
}

// TestArtifactOfAHistoricalRunIsReachable: the artifact route used to consult only the in-memory map,
// so every artifact of every run became unreachable when control-api restarted — the hub could list
// the run and then answer "no such run" for the report it had just named.
func TestArtifactOfAHistoricalRunIsReachable(t *testing.T) {
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc

	dir := filepath.Join(s.repo, "runs", "control-hist")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "report.json"), []byte(`{"verdict":"passed"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	// Persisted only — NOT in s.runs, which is what a run from a previous process looks like.
	sc.upsertRun(&run{ID: "hist", State: "done", ArtifactDir: dir})

	req := httptest.NewRequest(http.MethodGet, "/v1/runs/hist/artifact?name=report.json", nil)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("artifact of a persisted-but-not-live run: got %d want 200 (%s)", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["verdict"] != "passed" {
		t.Fatalf("artifact body: %v (err=%v)", body, err)
	}
}

// TestLiveFramesAreServedAndBounded — ADR-108d.
//
// Frames live in a SUBDIRECTORY, one per step, so the flat whitelist cannot enumerate them and they
// are matched by shape instead. The shape has to be narrow enough that it can only ever name a file
// this run wrote — which is why the refusals below matter as much as the acceptance.
func TestLiveFramesAreServedAndBounded(t *testing.T) {
	s := newTestServer()
	dir := filepath.Join(s.repo, "runs", "control-fr")
	if err := os.MkdirAll(filepath.Join(dir, "frames"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "frames", "frame-0007.png"), []byte("PNGDATA"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.runs["fr"] = &run{ID: "fr", State: "done", ArtifactDir: dir, stream: newRunStream()}

	get := func(name string) int {
		req := httptest.NewRequest(http.MethodGet, "/v1/runs/fr/artifact?name="+name, nil)
		req.Header.Set("Authorization", "Bearer "+s.token)
		rec := httptest.NewRecorder()
		s.mux().ServeHTTP(rec, req)
		return rec.Code
	}
	if code := get("frames%2Fframe-0007.png"); code != http.StatusOK {
		t.Errorf("a real frame: got %d want 200 — the live view would show nothing", code)
	}
	// Everything that is not exactly a frame of this run's own making.
	for _, bad := range []string{
		"frames%2F..%2F..%2Fetc%2Fpasswd",
		"frames%2Fframe-7.png",       // not four digits
		"frames%2Fframe-0007.png.sh", // suffix smuggling
		"frames%2Fother.png",
		"frames%2F",
	} {
		if code := get(bad); code == http.StatusOK {
			t.Errorf("%s was served; the frame pattern is too wide", bad)
		}
	}
}
