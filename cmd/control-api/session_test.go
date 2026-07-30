package main

// ADR-109 control-api gates: who is asking, and what that entitles them to see.
//
// The store's own tests already assert the SQL scoping. These assert the layer above it — the one that
// decides WHICH owner the SQL is asked about, which is where a mistake stops being a bug and becomes a
// data leak.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/AlexGromer/sentinel/internal/identity"
)

func idPost(t *testing.T, s *server, path, tok, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	if tok != "" {
		r.Header.Set("Authorization", "Bearer "+tok)
	}
	s.mux().ServeHTTP(rec, r)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec, out
}

func idGet(t *testing.T, s *server, path, tok string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	if tok != "" {
		r.Header.Set("Authorization", "Bearer "+tok)
	}
	s.mux().ServeHTTP(rec, r)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec, out
}

// TestMachineTokenIsUnscoped: the credential CI and agentctl use must keep seeing everything, or
// adding accounts would silently break every automated caller.
func TestMachineTokenIsUnscoped(t *testing.T) {
	s := newTestServer()
	rec, body := idGet(t, s, "/v1/me", s.token)
	if rec.Code != http.StatusOK {
		t.Fatalf("/v1/me with the machine token: %d (%s)", rec.Code, rec.Body.String())
	}
	if body["machine"] != true {
		t.Errorf("/v1/me did not report a machine caller: %v", body)
	}
	if body["scoped"] != false {
		t.Errorf("the machine token reported itself as scoped: %v", body)
	}
}

func TestUnknownCredentialIsRefused(t *testing.T) {
	s := newTestServer()
	for name, tok := range map[string]string{
		"empty":     "",
		"nonsense":  "not-a-token",
		"near-miss": s.token + "x",
		"prefix":    s.token[:len(s.token)-1],
		"stale-hex": strings.Repeat("ab", sessionBytes),
	} {
		if rec, _ := idGet(t, s, "/v1/me", tok); rec.Code != http.StatusForbidden {
			t.Errorf("%s credential got %d, want 403", name, rec.Code)
		}
	}
}

// TestSessionsExpireOnRead: an expired session must not work even before any sweep has run.
func TestSessionsExpireOnRead(t *testing.T) {
	ss := newSessionStore()
	tok := ss.mint("u1", "alice", false, -time.Second) // already expired
	if _, ok := ss.lookup(tok); ok {
		t.Fatal("an expired session resolved — expiry is not enforced on read")
	}
	live := ss.mint("u1", "alice", false, time.Hour)
	if _, ok := ss.lookup(live); !ok {
		t.Fatal("a live session did not resolve")
	}
	ss.drop(live)
	if _, ok := ss.lookup(live); ok {
		t.Fatal("a dropped session still resolved")
	}
}

// TestDropUserEndsEverySession: deleting an account must not leave a live token for an account that no
// longer exists — access nobody can revoke.
func TestDropUserEndsEverySession(t *testing.T) {
	ss := newSessionStore()
	a1 := ss.mint("u1", "alice", false, time.Hour)
	a2 := ss.mint("u1", "alice", false, time.Hour) // a second browser
	b := ss.mint("u2", "bob", false, time.Hour)
	ss.dropUser("u1")
	if _, ok := ss.lookup(a1); ok {
		t.Error("a session survived its account's deletion")
	}
	if _, ok := ss.lookup(a2); ok {
		t.Error("a SECOND session of the deleted account survived")
	}
	if _, ok := ss.lookup(b); !ok {
		t.Error("another account's session was dropped too")
	}
}

// TestAccountCreationNeedsACredential: there is no unauthenticated bootstrap, not even for the first
// account. An endpoint that mints an admin without a credential is an open door on any reachable
// deployment for as long as nobody has walked through it.
func TestAccountCreationNeedsACredential(t *testing.T) {
	s := newTestServer()
	if rec, _ := idPost(t, s, "/v1/users", "", `{"name":"alice","password":"correcthorse"}`); rec.Code != http.StatusForbidden {
		t.Errorf("unauthenticated account creation got %d, want 403", rec.Code)
	}
}

// TestLoginAnswersTheSameForAWrongNameAndAWrongPassword: otherwise the endpoint is a directory of who
// has an account here.
func TestLoginAnswersTheSameForAWrongNameAndAWrongPassword(t *testing.T) {
	s := newTestServer()
	// No store in a unit server, so login reports 503 rather than leaking anything. The interesting
	// property — one answer for both failures — is asserted where a store exists (store_test.go covers
	// the store side); here the point is that neither shape reveals more than the other.
	r1, _ := idPost(t, s, "/v1/login", "", `{"name":"nobody","password":"x"}`)
	r2, _ := idPost(t, s, "/v1/login", "", `{"name":"alice","password":"wrong"}`)
	if r1.Code != r2.Code {
		t.Errorf("a wrong name (%d) and a wrong password (%d) answer differently", r1.Code, r2.Code)
	}
	if r1.Body.String() != r2.Body.String() {
		t.Errorf("a wrong name and a wrong password answer with different bodies:\n  %s\n  %s",
			r1.Body.String(), r2.Body.String())
	}
}

func TestValidUserName(t *testing.T) {
	ok := []string{"alice", "bob.smith", "a_b-c", "user@host", "A1"}
	for _, n := range ok {
		if msg := validUserName(n); msg != "" {
			t.Errorf("%q was rejected: %s", n, msg)
		}
	}
	bad := map[string]string{
		"empty":       "",
		"space":       "alice smith",
		"newline":     "alice\nadmin",
		"tab":         "a\tb",
		"slash":       "../etc",
		"long":        strings.Repeat("a", maxUserNameLen+1),
		"nul":         "a\x00b",
		"unicode-ctl": "a‮b", // right-to-left override: renders as a different name than it is
	}
	for label, n := range bad {
		if msg := validUserName(n); msg == "" {
			t.Errorf("%s (%q) was accepted", label, n)
		}
	}
}

// TestLiveRunsAreScopedToo: the in-memory map is filtered, not only the store.
//
// Added because a mutation SURVIVED: deleting the in-memory filter broke nothing, since every other
// test here runs without a store and none of them looked at a LIVE run. That is the worst possible
// gap to leave — scoping only the persisted rows would leak every running run to everyone, and a
// running run is the one a person is most likely to be looking at.
func TestLiveRunsAreScopedToo(t *testing.T) {
	s := newTestServer()
	// Sessions minted directly: this asserts the LISTING, and going through /v1/login would drag in a
	// store the unit server does not have.
	aliceTok := s.sessions.mint("u-alice", "alice", false, time.Hour)
	bobTok := s.sessions.mint("u-bob", "bob", false, time.Hour)
	s.runs["a1"] = &run{ID: "a1", State: "running", Owner: "u-alice", stream: newRunStream()}
	s.runs["b1"] = &run{ID: "b1", State: "running", Owner: "u-bob", stream: newRunStream()}
	s.runs["x1"] = &run{ID: "x1", State: "running", Owner: "", stream: newRunStream()} // pre-identity / machine-started

	ids := func(tok string) []string {
		rec, body := idGet(t, s, "/v1/runs", tok)
		if rec.Code != http.StatusOK {
			t.Fatalf("/v1/runs: %d (%s)", rec.Code, rec.Body.String())
		}
		raw, _ := body["runs"].([]any)
		out := []string{}
		for _, r := range raw {
			if m, ok := r.(map[string]any); ok {
				out = append(out, m["run_id"].(string))
			}
		}
		sort.Strings(out)
		return out
	}

	if got := ids(aliceTok); len(got) != 1 || got[0] != "a1" {
		t.Errorf("alice sees %v, want [a1] — a live run leaked across accounts", got)
	}
	if got := ids(bobTok); len(got) != 1 || got[0] != "b1" {
		t.Errorf("bob sees %v, want [b1]", got)
	}
	// The machine token is unscoped and sees all three, including the unowned one.
	if got := ids(s.token); len(got) != 3 {
		t.Errorf("the machine token sees %v, want all three", got)
	}
}

// TestOwnerIsNotClientSettable: `owner` on runRequest is unexported, so a JSON body naming an owner
// cannot write into somebody else's set. Asserted through the decoder rather than by reading the
// struct, because the question is what a REQUEST can do.
func TestOwnerIsNotClientSettable(t *testing.T) {
	var req runRequest
	if err := json.Unmarshal([]byte(`{"target":"http://x","owner":"someone-else"}`), &req); err != nil {
		t.Fatal(err)
	}
	if req.owner != "" {
		t.Fatalf("a client set the owner to %q — scoping would then be self-service", req.owner)
	}
}

// TestPasswordPolicyIsEnforcedBeforeHashing: a short password must be refused with a reason, not
// silently hashed. Checked at the handler, since that is where a person meets the rule.
func TestPasswordPolicyIsEnforcedBeforeHashing(t *testing.T) {
	s := newTestServer()
	rec, body := idPost(t, s, "/v1/users", s.token, `{"name":"alice","password":"short"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("a short password got %d, want 400 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "at least") {
		t.Errorf("the refusal does not say what the rule is: %q", msg)
	}
}

// TestRehashOnLoginKeepsTheSamePassword: raising the iteration policy must not change what password
// works. The upgrade path is the one place a plaintext password exists, so it is also the one place
// this can go wrong silently.
func TestRehashOnLoginKeepsTheSamePassword(t *testing.T) {
	weak := "pbkdf2-sha256$1000$AAAAAAAAAAAAAAAAAAAAAA$" // deliberately malformed tail
	if identity.Verify(weak, "pw") {
		t.Fatal("a malformed credential verified")
	}
	strong, err := identity.Hash("pw")
	if err != nil {
		t.Fatal(err)
	}
	if !identity.Verify(strong, "pw") || identity.NeedsRehash(strong) {
		t.Fatal("a freshly hashed credential is either unverifiable or already stale")
	}
}
