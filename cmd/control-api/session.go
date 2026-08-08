package main

// ADR-109 local accounts, the control-api half: who is asking, and what that entitles them to see.
//
// Two kinds of caller, and the difference is the whole design:
//
//	MACHINE — the CONTROL_API_TOKEN. Unscoped: it sees every row. CI, agentctl and the air-gapped
//	          bundle authenticate this way, and none of them is a person with data of their own. It is
//	          also the only credential that exists on a fresh install, so it is what creates the first
//	          account.
//	SESSION — a local account, minted by POST /v1/login. Scoped to its own rows.
//
// Identity stays OPT-IN (see internal/store: an empty owner means unowned). With no accounts created
// nothing is scoped and the deployment behaves exactly as it did — the single-team install open-core
// exists to serve must not be broken by adding a feature it never asked for.
//
// Sessions live in memory, deliberately. A restart logging everyone out is the correct behaviour for a
// tool whose token is already per-process: persisting them would mean a second credential store to
// protect, expire and purge, for the sake of not retyping a password after a restart.

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/AlexGromer/sentinel/internal/identity"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

const (
	sessionBytes      = 32
	sessionDefaultTTL = 12 * time.Hour
	// A name is a login handle, not prose: bounded and charset-restricted so it cannot carry control
	// characters into a log line or a UI, and cannot be confused for another name by whitespace alone.
	maxUserNameLen = 64
	minPasswordLen = 8
)

type session struct {
	userID  string
	name    string
	admin   bool
	expires time.Time
}

type sessionStore struct {
	mu   sync.Mutex
	byID map[string]session
}

func newSessionStore() *sessionStore { return &sessionStore{byID: map[string]session{}} }

// mint returns a new opaque session token. "" on an entropy failure — fail closed, because a
// predictable session token is a login for everyone.
func (ss *sessionStore) mint(userID, name string, admin bool, ttl time.Duration) string {
	b := make([]byte, sessionBytes)
	if _, err := rand.Read(b); err != nil {
		return ""
	}
	tok := hex.EncodeToString(b)
	ss.mu.Lock()
	defer ss.mu.Unlock()
	ss.sweepLocked()
	ss.byID[tok] = session{userID: userID, name: name, admin: admin, expires: time.Now().Add(ttl)}
	return tok
}

// lookup resolves a token, dropping it if expired. Expiry is enforced on READ rather than by a timer:
// a sweep that has not run yet must never make an expired session usable.
func (ss *sessionStore) lookup(tok string) (session, bool) {
	if tok == "" {
		return session{}, false
	}
	ss.mu.Lock()
	defer ss.mu.Unlock()
	s, ok := ss.byID[tok]
	if !ok {
		return session{}, false
	}
	if time.Now().After(s.expires) {
		delete(ss.byID, tok)
		return session{}, false
	}
	return s, true
}

func (ss *sessionStore) drop(tok string) {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	delete(ss.byID, tok)
}

// dropUser ends every session an account holds. Called when the account is deleted, so removing
// someone also removes their access rather than leaving a live token for an account that is gone.
func (ss *sessionStore) dropUser(userID string) {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	for tok, s := range ss.byID {
		if s.userID == userID {
			delete(ss.byID, tok)
		}
	}
}

func (ss *sessionStore) sweepLocked() {
	now := time.Now()
	for tok, s := range ss.byID {
		if now.After(s.expires) {
			delete(ss.byID, tok)
		}
	}
}

// ---------------------------------------------------------------- callers

// caller is who is asking. `machine` and `userID` are mutually exclusive by construction.
type caller struct {
	machine bool
	userID  string
	name    string
	admin   bool
}

// owner is the value to scope store reads by: "" means unscoped.
//
// A machine caller returns "" and therefore sees everything, which is the same "" that a deployment
// with no accounts uses. That coincidence is intentional: it is the one rule, "no subject means no
// scoping", rather than two rules that happen to agree today.
func (c caller) owner() string { return c.userID }

func bearerOf(r *http.Request) string {
	return strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
}

// callerOf identifies the requester, or reports false when the credential is neither the machine token
// nor a live session.
func (s *server) callerOf(r *http.Request) (caller, bool) {
	tok := bearerOf(r)
	if tok == "" {
		return caller{}, false
	}
	// The machine token is checked FIRST and in constant time, exactly as authed() does. Checking the
	// session map first would let a timing difference distinguish "a session that does not exist" from
	// "the machine token", which is a hint about the shape of the secret.
	if s.token != "" && subtle.ConstantTimeCompare([]byte(tok), []byte(s.token)) == 1 {
		return caller{machine: true}, true
	}
	if sess, ok := s.sessions.lookup(tok); ok {
		return caller{userID: sess.userID, name: sess.name, admin: sess.admin}, true
	}
	return caller{}, false
}

// requireCaller writes the 403 and reports false, so a handler is one line.
func (s *server) requireCaller(w http.ResponseWriter, r *http.Request) (caller, bool) {
	c, ok := s.callerOf(r)
	if !ok {
		writeJSON(w, http.StatusForbidden, map[string]string{
			"error": "missing/invalid credential: send the machine token (CONTROL_API_TOKEN) or a session from POST /v1/login"})
		return caller{}, false
	}
	return c, true
}

// ---------------------------------------------------------------- handlers

type loginReq struct {
	Name     string `json:"name"`
	Password string `json:"password"`
}

func (s *server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var req loginReq
	if json.NewDecoder(r.Body).Decode(&req) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}
	if s.store == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "local accounts need a store-gateway: this deployment has none, so there is nowhere for an account to live"})
		return
	}
	u, ok := s.store.getUser(&storepb.UserRef{Name: strings.TrimSpace(req.Name)})
	// One answer for "no such name" and "wrong password", and the KDF runs either way. A reply that
	// distinguished them would turn this endpoint into a list of who has an account here, and a reply
	// that skipped the KDF on a missing name would leak the same thing through timing.
	valid := false
	if ok && u.Found {
		valid = identity.Verify(u.PwHash, req.Password)
	} else {
		identity.Verify("pbkdf2-sha256$"+strconv.Itoa(identity.DefaultIterations)+"$AAAAAAAAAAAAAAAAAAAAAA$"+
			"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", req.Password)
	}
	if !valid {
		// HEALTH-005: recorded at `warn`, with the name that was TRIED and never the password. The
		// reply still cannot distinguish "no such name" from "wrong password" — that is a property of
		// the ANSWER, and writing to our own journal does not weaken it.
		// ⚠ `reason` is still English PROSE, and deliberately so for now. [JOURNAL-VALUE-I18N] wants it
		// to be a token the catalogue expands («bad_credentials»), and naming the fields — which this
		// change does — is what makes that possible at all. But a token without a resolver is worse
		// than the prose it replaces: the wire rule keeps Russian off the wire, so the hub would
		// receive `bad_credentials` and, having nothing to expand it with, would show that word to
		// BOTH readers. The resolver is a catalogue table plus a hub change, and the hub is not this
		// branch's file. Tokenising here and finishing there would leave main in the worse state in
		// between, so the prose stays until the resolver lands with it.
		s.journalEvent("service.login_failed", "warn", map[string]string{
			"actor": strings.TrimSpace(req.Name), "reason": "invalid name or password",
		}, nil)
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid name or password"})
		return
	}
	tok := s.sessions.mint(u.UserId, u.Name, u.IsAdmin, sessionTTL())
	if tok == "" {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "could not mint a session"})
		return
	}
	// A successful login is the only moment a plaintext password exists here, so it is also the only
	// moment a hash made under weaker parameters can be upgraded.
	if identity.NeedsRehash(u.PwHash) {
		if h, err := identity.Hash(req.Password); err == nil {
			u.PwHash = h
			s.store.upsertUser(u)
		}
	}
	// The subject is the account that just signed in, not the (anonymous) caller: without it this is
	// the one record an account most needs and cannot see.
	s.journalSubject("service.login_ok", "info", map[string]string{"actor": u.Name}, nil, u.UserId)
	writeJSON(w, http.StatusOK, map[string]any{
		"session": tok, "user": map[string]any{"user_id": u.UserId, "name": u.Name, "is_admin": u.IsAdmin},
		"expires_in_seconds": int(sessionTTL().Seconds()),
	})
}

func (s *server) handleLogout(w http.ResponseWriter, r *http.Request) {
	// Journalled BEFORE the drop: afterwards the session is gone and actorOf has nobody to name, so
	// every sign-out would be recorded as "anonymous".
	actor, _ := s.actorOf(r)
	s.journalEvent("service.logout", "info", map[string]string{"actor": actor}, r)
	s.sessions.drop(bearerOf(r))
	// 200 whether or not the token was live: "you are logged out" is true either way, and reporting
	// otherwise would tell an unauthenticated caller whether a token they hold is real.
	writeJSON(w, http.StatusOK, map[string]string{"status": "logged out"})
}

func (s *server) handleMe(w http.ResponseWriter, r *http.Request) {
	c, _ := s.callerOf(r)
	if c.machine {
		writeJSON(w, http.StatusOK, map[string]any{
			"machine": true, "scoped": false,
			"note": "the machine token is unscoped: it sees every row, which is what CI and agentctl need"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"machine": false, "scoped": true,
		"user": map[string]any{"user_id": c.userID, "name": c.name, "is_admin": c.admin},
	})
}

type createUserReq struct {
	Name     string `json:"name"`
	Password string `json:"password"`
	IsAdmin  bool   `json:"is_admin"`
}

// handleCreateUser: the machine token or an admin session may create an account.
//
// There is deliberately NO unauthenticated bootstrap path, not even for the first account. control-api
// already prints and persists a machine token on every start, so an operator always has one — and an
// endpoint that creates an admin without a credential would be an open door on any reachable
// deployment for exactly as long as nobody had used it.
func (s *server) handleCreateUser(w http.ResponseWriter, r *http.Request) {
	// guard() has already required the machine token or an admin session (access.go: accessAdmin).
	// The REQUEST is judged before the deployment is. A 503 about a missing store, sent in answer to a
	// body that was invalid anyway, hides the thing the caller can actually fix.
	var req createUserReq
	if json.NewDecoder(r.Body).Decode(&req) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}
	name := strings.TrimSpace(req.Name)
	if msg := validUserName(name); msg != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": msg})
		return
	}
	if len(req.Password) < minPasswordLen {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "password must be at least " + strconv.Itoa(minPasswordLen) + " characters"})
		return
	}
	if s.store == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error": "local accounts need a store-gateway: this deployment has none"})
		return
	}
	if existing, ok := s.store.getUser(&storepb.UserRef{Name: name}); ok && existing.Found {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "an account named " + name + " already exists"})
		return
	}
	hash, err := identity.Hash(req.Password)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "could not hash the password"})
		return
	}
	u := &storepb.User{UserId: newRunID(), Name: name, PwHash: hash, IsAdmin: req.IsAdmin}
	if !s.store.upsertUser(u) {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "the store did not accept the account"})
		return
	}
	// The FIRST account is the moment identity starts to mean something: from here the pre-identity
	// open reads require a credential (access.go, legacyOpen). Dropping the memo makes that true on the
	// very next request rather than up to accountsMemoTTL later — a window in which a just-created
	// account's rows would still be readable by an anonymous caller.
	s.forgetAccounts()
	creator, _ := s.actorOf(r)
	s.journalSubject("service.account_created", "info", map[string]string{
		"actor": creator, "account": u.Name, "admin": strconv.FormatBool(u.IsAdmin),
	}, r, u.UserId)
	writeJSON(w, http.StatusCreated, map[string]any{
		"user_id": u.UserId, "name": u.Name, "is_admin": u.IsAdmin})
}

func (s *server) handleListUsers(w http.ResponseWriter, r *http.Request) {
	_, _ = s.callerOf(r) // guard(): machine token or admin session only
	if s.store == nil {
		writeJSON(w, http.StatusOK, map[string]any{"users": []any{}, "total": 0,
			"store": false, "store_reason": storeAbsentReason})
		return
	}
	list, ok := s.store.listUsers()
	if !ok {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "the store did not answer"})
		return
	}
	out := make([]map[string]any, 0, len(list.Users))
	for _, u := range list.Users {
		// pw_hash is not selected by the store's ListUsers, and is not assembled here either.
		out = append(out, map[string]any{"user_id": u.UserId, "name": u.Name, "is_admin": u.IsAdmin,
			"created_at": u.CreatedAt})
	}
	writeJSON(w, http.StatusOK, map[string]any{"users": out, "total": list.Total, "store": true})
}

func (s *server) handleDeleteUser(w http.ResponseWriter, r *http.Request) {
	c, _ := s.callerOf(r) // guard(): machine token or admin session only
	id := r.PathValue("id")
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "no user id"})
		return
	}
	if s.store == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "local accounts need a store-gateway"})
		return
	}
	// Removing your own account while logged in would end the session mid-request and leave the caller
	// unable to see the result. Refused rather than half-done.
	if !c.machine && c.userID == id {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "an account cannot remove itself — ask another admin, or use the machine token"})
		return
	}
	// Journalled at `warn` and BEFORE the delete, while the name still exists to be written down: an
	// account id alone answers "which row" and this journal answers "who", and after the row is gone
	// nothing can turn the id back into a name.
	name := id
	if u, ok := s.store.getUser(&storepb.UserRef{UserId: id}); ok && u.Found && u.Name != "" {
		name = u.Name
	}
	remover, _ := s.actorOf(r)
	s.journalSubject("service.account_deleted", "warn", map[string]string{
		"actor": remover, "account": name,
	}, r, id, "user_id: "+id)
	s.store.deleteUser(&storepb.UserRef{UserId: id})
	// The rows the account owned are LEFT (internal/store: unowned, not deleted). Its sessions are not:
	// a live token for an account that no longer exists is access nobody can revoke.
	s.sessions.dropUser(id)
	s.forgetAccounts() // removing the LAST account re-opens the legacyOpen reads; see handleCreateUser
	writeJSON(w, http.StatusOK, map[string]string{"status": "removed", "user_id": id})
}

// validUserName returns "" when the name is acceptable, or the reason it is not.
func validUserName(name string) string {
	if name == "" {
		return "name is required"
	}
	if len(name) > maxUserNameLen {
		return "name must be at most " + strconv.Itoa(maxUserNameLen) + " characters"
	}
	for _, r := range name {
		ok := r == '-' || r == '_' || r == '.' || r == '@' ||
			(r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')
		if !ok {
			return "name may contain only letters, digits and - _ . @ (no spaces or control characters)"
		}
	}
	return ""
}

// storeAbsentReason is the same sentence the run/scenario listings use, so a deployment with no
// gateway explains itself identically wherever a caller meets it. The comment above said "the same
// sentence" while main.go::storeMarker held a hand-copied duplicate of it; the two are one constant
// now, which is what the comment always claimed.
//
// The remedy changed on 2026-08-03: the store-gateway is part of the default compose stack and
// control-api is pointed at it by default, so `--profile store` no longer names anything.
const storeAbsentReason = "this deployment has no store-gateway, so nothing is persisted — in the " +
	"compose stack `docker compose up` starts one and control-api is already pointed at it; " +
	"elsewhere run store-gateway and set CONTROL_API_STORE_ADDR"

func sessionTTL() time.Duration {
	if v := strings.TrimSpace(os.Getenv("CONTROL_API_SESSION_TTL")); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return sessionDefaultTTL
}
