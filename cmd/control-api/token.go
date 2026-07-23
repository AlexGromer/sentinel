package main

// Bearer-token lifecycle (M-UI-MODES, ADR-064).
//
// Before ADR-064 the token came from CONTROL_API_TOKEN and nowhere else: an operator who did not
// invent and export a secret BEFORE the first start got a silently read-only API (every mutation 403).
// That was the single most common first-run stumble — neither docker-compose.yml (${CONTROL_API_TOKEN:-})
// nor install.sh produced one.
//
// resolveToken keeps the wire contract identical (mutations still require a bearer token; ADR-032) and
// only changes WHERE the value comes from, in this order:
//
//	env       CONTROL_API_TOKEN is set        → use it verbatim, never touch the file (pre-ADR-064 behaviour)
//	disabled  CONTROL_API_AUTOTOKEN=0         → "" → mutations 403 (fail-closed read-only demo/public instance)
//	file      state/control-api.token exists  → reuse it, so a token pasted into the UI survives a restart
//	generated otherwise                       → 32 random bytes → hex, persisted 0600 (atomically)
//
// Invariant kept from ADR-032: possessing the token is what authorises a mutation. Auto-generation gives
// the SERVER a token; it does not hand that token to anyone who can reach the port. Mode 3 (ui.go) hands
// it to the served page only via a single-use, TTL-bounded bootstrap nonce printed on the operator's
// terminal, so port reachability alone still buys nothing.

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const (
	tokenFileName = "control-api.token"
	tokenBytes    = 32 // → 64 hex chars
	tokenMinLen   = 16 // an operator-supplied token file may hold their own secret; keep a sanity floor
	tokenMaxLen   = 512
)

// tokenSource labels where the live token came from (for the startup log and for tests).
type tokenSource string

const (
	tokenFromEnv       tokenSource = "env"
	tokenDisabled      tokenSource = "disabled"
	tokenFromFile      tokenSource = "file"
	tokenGenerated     tokenSource = "generated"
	tokenGeneratedMem  tokenSource = "generated (in-memory)"
	tokenGeneratedOnly tokenSource = "generated (not persisted)"
)

// envDisabled reports whether an env var carries an explicit "off" value. Anything else — including
// unset and empty — leaves the feature enabled, so the default path stays the friendly one.
func envDisabled(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "0", "false", "no", "off":
		return true
	}
	return false
}

// tokenFilePath resolves where the persisted token lives. CONTROL_API_TOKEN_FILE overrides the default
// <repo>/state/control-api.token (the same ./state directory agentctl and the orchestrator already use).
func tokenFilePath(repo string) string {
	if p := strings.TrimSpace(os.Getenv("CONTROL_API_TOKEN_FILE")); p != "" {
		return p
	}
	return filepath.Join(repo, "state", tokenFileName)
}

// usableToken reports whether stored bytes look like a token we may hand out. Deliberately permissive
// about the alphabet (an operator may point CONTROL_API_TOKEN_FILE at their own secret) but strict about
// shape: one line, no whitespace inside, printable ASCII, bounded length.
func usableToken(s string) bool {
	if len(s) < tokenMinLen || len(s) > tokenMaxLen {
		return false
	}
	for _, r := range s {
		if r < '!' || r > '~' { // excludes space, tab, CR/LF and every non-ASCII rune
			return false
		}
	}
	return true
}

func newToken() (string, error) {
	b := make([]byte, tokenBytes)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// writeTokenFile persists tok at path with 0600, atomically: a crash mid-write must never leave a
// truncated-but-plausible token behind (a short hex prefix would pass usableToken on the next start).
// os.CreateTemp already creates with 0600, so there is no window where the file is group/world-readable.
func writeTokenFile(path, tok string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, "."+tokenFileName+"-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op once the rename succeeded
	if _, err := f.WriteString(tok + "\n"); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Chmod(0o600); err != nil { // explicit: CreateTemp's mode is documented, but don't rely on it
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// resolveToken returns the live bearer token, where it came from, and the file path it is (or would be)
// persisted at. It never returns an error: a token that cannot be persisted still works for this process,
// and a token that is deliberately disabled is the caller's fail-closed read-only mode. Diagnostics go to
// stderr via the returned warnings so main() owns all logging.
func resolveToken(repo string) (tok string, src tokenSource, path string, warnings []string) {
	path = tokenFilePath(repo)

	if v := strings.TrimSpace(os.Getenv("CONTROL_API_TOKEN")); v != "" {
		return v, tokenFromEnv, path, nil
	}
	if envDisabled("CONTROL_API_AUTOTOKEN") {
		return "", tokenDisabled, path, nil
	}

	// Reuse a previously persisted token so a value already pasted into the UI keeps working.
	existing, readErr := os.ReadFile(path)
	trimmed := strings.TrimSpace(string(existing))
	switch {
	case readErr == nil && usableToken(trimmed):
		return trimmed, tokenFromFile, path, nil
	case readErr == nil && trimmed != "":
		// Non-empty but unusable: this may be operator data (a wrong file pointed at by
		// CONTROL_API_TOKEN_FILE). Never clobber it — run with an in-memory token instead.
		gen, err := newToken()
		if err != nil {
			return "", tokenDisabled, path, []string{fmt.Sprintf("cannot generate a token: %v (mutations will 403)", err)}
		}
		return gen, tokenGeneratedOnly, path, []string{
			fmt.Sprintf("%s holds unusable content — left untouched; using a throwaway in-memory token", path),
		}
	case readErr != nil && !os.IsNotExist(readErr):
		// Unreadable (permissions, a directory, …). Same rule: do not overwrite what we cannot read.
		gen, err := newToken()
		if err != nil {
			return "", tokenDisabled, path, []string{fmt.Sprintf("cannot generate a token: %v (mutations will 403)", err)}
		}
		return gen, tokenGeneratedOnly, path, []string{
			fmt.Sprintf("%s unreadable: %v — left untouched; using a throwaway in-memory token", path, readErr),
		}
	}

	// Missing, or present-but-blank (a truncated earlier write) — safe to (re)create.
	gen, err := newToken()
	if err != nil {
		return "", tokenDisabled, path, []string{fmt.Sprintf("cannot generate a token: %v (mutations will 403)", err)}
	}
	if err := writeTokenFile(path, gen); err != nil {
		return gen, tokenGeneratedMem, path, []string{
			fmt.Sprintf("cannot persist the token to %s: %v (a new one is generated on every restart)", path, err),
		}
	}
	return gen, tokenGenerated, path, nil
}
