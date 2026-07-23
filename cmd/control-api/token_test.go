package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// clearTokenEnv isolates a test from whatever the developer/CI has exported. t.Setenv restores the
// previous value at the end of the test (and forbids t.Parallel, which is why none of these run parallel).
func clearTokenEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"CONTROL_API_TOKEN", "CONTROL_API_AUTOTOKEN", "CONTROL_API_TOKEN_FILE", "CONTROL_API_PRINT_TOKEN"} {
		t.Setenv(k, "")
	}
}

// The env value wins and — crucially — nothing is written to disk: an operator who manages the secret
// externally must not find the control-api scattering a copy of it into ./state.
func TestResolveTokenEnvWinsAndWritesNothing(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	t.Setenv("CONTROL_API_TOKEN", "externally-managed-secret")

	tok, src, path, warn := resolveToken(repo)
	if tok != "externally-managed-secret" || src != tokenFromEnv {
		t.Fatalf("got (%q, %q), want the env value with source %q", tok, src, tokenFromEnv)
	}
	if len(warn) != 0 {
		t.Errorf("unexpected warnings: %v", warn)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("env-supplied token must not be persisted, but %s exists (stat err=%v)", path, err)
	}
}

// A blank/whitespace CONTROL_API_TOKEN is "unset", not "the empty token" — same present-but-empty
// treatment ADR-063 established for the LLM_* vars.
func TestResolveTokenBlankEnvIsUnset(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	t.Setenv("CONTROL_API_TOKEN", "   \t ")

	tok, src, _, _ := resolveToken(repo)
	if src == tokenFromEnv {
		t.Fatalf("blank CONTROL_API_TOKEN was treated as a real token (%q)", tok)
	}
	if src != tokenGenerated {
		t.Fatalf("source = %q, want %q", src, tokenGenerated)
	}
}

func TestResolveTokenGeneratesPersistsAndReuses(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()

	tok1, src1, path, warn := resolveToken(repo)
	if src1 != tokenGenerated {
		t.Fatalf("source = %q, want %q (warnings: %v)", src1, tokenGenerated, warn)
	}
	if len(tok1) != 2*tokenBytes {
		t.Errorf("generated token is %d chars, want %d hex chars", len(tok1), 2*tokenBytes)
	}
	if want := filepath.Join(repo, "state", tokenFileName); path != want {
		t.Errorf("path = %q, want %q", path, want)
	}

	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("token file not written: %v", err)
	}
	if runtime.GOOS != "windows" { // Windows maps Go's POSIX bits onto ACLs; 0600 is not observable there
		if got := fi.Mode().Perm(); got != 0o600 {
			t.Errorf("token file mode = %04o, want 0600", got)
		}
	}

	// Second start must REUSE it — a token already pasted into the UI has to survive a restart.
	tok2, src2, _, _ := resolveToken(repo)
	if tok2 != tok1 {
		t.Errorf("token changed across restarts: %q → %q", tok1, tok2)
	}
	if src2 != tokenFromFile {
		t.Errorf("source = %q, want %q", src2, tokenFromFile)
	}
}

// CONTROL_API_AUTOTOKEN=0 is the ONLY way to get the pre-ADR-064 tokenless (read-only) instance back.
func TestResolveTokenAutotokenDisabled(t *testing.T) {
	clearTokenEnv(t)
	for _, v := range []string{"0", "false", "NO", " off "} {
		repo := t.TempDir()
		t.Setenv("CONTROL_API_AUTOTOKEN", v)
		tok, src, path, _ := resolveToken(repo)
		if tok != "" || src != tokenDisabled {
			t.Errorf("AUTOTOKEN=%q → (%q, %q), want an empty token with source %q", v, tok, src, tokenDisabled)
		}
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("AUTOTOKEN=%q must not create %s", v, path)
		}
	}
	// Anything else leaves auto-generation ON — the default must be the friendly path.
	for _, v := range []string{"", "1", "true", "yes", "please"} {
		repo := t.TempDir()
		t.Setenv("CONTROL_API_AUTOTOKEN", v)
		if _, src, _, _ := resolveToken(repo); src != tokenGenerated {
			t.Errorf("AUTOTOKEN=%q → source %q, want %q (auto-generation stays on)", v, src, tokenGenerated)
		}
	}
}

// A blank file is the fingerprint of a truncated earlier write — safe to replace.
func TestResolveTokenBlankFileIsRegenerated(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	path := filepath.Join(repo, "state", tokenFileName)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("  \n"), 0o600); err != nil {
		t.Fatal(err)
	}

	tok, src, _, _ := resolveToken(repo)
	if src != tokenGenerated || len(tok) != 2*tokenBytes {
		t.Fatalf("got (%q, %q), want a freshly generated token", tok, src)
	}
	stored, err := os.ReadFile(path)
	if err != nil || strings.TrimSpace(string(stored)) != tok {
		t.Errorf("blank file was not replaced with the new token (content=%q, err=%v)", stored, err)
	}
}

// Non-blank but unusable content may be the operator's own data behind CONTROL_API_TOKEN_FILE.
// Destroying it would be worse than running with a throwaway token, so we must NOT overwrite.
func TestResolveTokenUnusableFileIsNeverClobbered(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	custom := filepath.Join(repo, "operator-data.txt")
	const payload = "short" // < tokenMinLen → unusable, but clearly somebody's content
	if err := os.WriteFile(custom, []byte(payload), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CONTROL_API_TOKEN_FILE", custom)

	tok, src, path, warn := resolveToken(repo)
	if path != custom {
		t.Errorf("path = %q, want the CONTROL_API_TOKEN_FILE override %q", path, custom)
	}
	if src != tokenGeneratedOnly || len(tok) != 2*tokenBytes {
		t.Fatalf("got (%q, %q), want an in-memory token with source %q", tok, src, tokenGeneratedOnly)
	}
	if len(warn) == 0 {
		t.Error("silently ignoring the operator's file — a warning is required")
	}
	if got, _ := os.ReadFile(custom); string(got) != payload {
		t.Errorf("operator file was clobbered: %q → %q", payload, got)
	}
}

// An unreadable path (here: a directory where a file is expected) follows the same never-clobber rule.
func TestResolveTokenUnreadablePathIsNeverClobbered(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	asDir := filepath.Join(repo, "not-a-file")
	if err := os.MkdirAll(asDir, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CONTROL_API_TOKEN_FILE", asDir)

	tok, src, _, warn := resolveToken(repo)
	if src != tokenGeneratedOnly || len(tok) != 2*tokenBytes {
		t.Fatalf("got (%q, %q), want an in-memory token with source %q", tok, src, tokenGeneratedOnly)
	}
	if len(warn) == 0 {
		t.Error("expected a warning about the unreadable token path")
	}
	if fi, err := os.Stat(asDir); err != nil || !fi.IsDir() {
		t.Errorf("the directory at the token path was replaced (err=%v)", err)
	}
}

// An operator-supplied secret in the file is honoured verbatim — we only reject shapes we cannot use.
func TestResolveTokenAcceptsOperatorSuppliedFile(t *testing.T) {
	clearTokenEnv(t)
	repo := t.TempDir()
	custom := filepath.Join(repo, "my.token")
	const secret = "s3cret-but-long-enough"
	if err := os.WriteFile(custom, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CONTROL_API_TOKEN_FILE", custom)

	tok, src, _, _ := resolveToken(repo)
	if tok != secret || src != tokenFromFile {
		t.Fatalf("got (%q, %q), want (%q, %q)", tok, src, secret, tokenFromFile)
	}
}

func TestUsableToken(t *testing.T) {
	long := strings.Repeat("a", tokenMaxLen+1)
	cases := []struct {
		in   string
		want bool
	}{
		{strings.Repeat("a", tokenMinLen), true},
		{"0123456789abcdef0123456789abcdef", true},
		{"", false},
		{"short", false},
		{long, false},
		{"has space inside!!!!!!!", false},
		{"has\ttab\tinside!!!!!!", false},
		{"пароль-достаточной-длины", false}, // non-ASCII cannot survive an HTTP header round-trip
	}
	for _, c := range cases {
		if got := usableToken(c.in); got != c.want {
			t.Errorf("usableToken(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

// A token that cannot be persisted must still work for the running process (warn, don't die).
func TestResolveTokenUnwritableDirStillYieldsAToken(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root — an unwritable directory cannot be simulated")
	}
	clearTokenEnv(t)
	repo := t.TempDir()
	locked := filepath.Join(repo, "locked")
	if err := os.MkdirAll(locked, 0o500); err != nil { // r-x: cannot create the state/ subdir inside
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o700) })
	t.Setenv("CONTROL_API_TOKEN_FILE", filepath.Join(locked, "sub", tokenFileName))

	tok, src, _, warn := resolveToken(repo)
	if len(tok) != 2*tokenBytes {
		t.Fatalf("token = %q, want a usable generated token despite the write failure", tok)
	}
	if src != tokenGeneratedMem {
		t.Errorf("source = %q, want %q", src, tokenGeneratedMem)
	}
	if len(warn) == 0 {
		t.Error("a non-persisted token must be announced — it changes on every restart")
	}
}
