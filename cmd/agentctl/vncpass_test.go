package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/AlexGromer/sentinel/internal/configguard"
	"github.com/AlexGromer/sentinel/internal/eventlog"
)

// clearVNCEnv isolates a test from whatever the developer/CI has exported. t.Setenv restores the
// previous value at the end of the test (and forbids t.Parallel, which is why none of these run
// parallel). Same shape and same reason as clearTokenEnv in cmd/control-api/token_test.go.
func clearVNCEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"SENTINEL_VNC_PASSWORD", "SENTINEL_VNC_PASSWORD_FILE"} {
		t.Setenv(k, "")
	}
}

// The env value wins and — crucially — nothing is written to disk. An operator who keeps the secret
// in an external store must not find agentctl scattering a copy of it into ./state.
func TestResolveVNCPassEnvWinsAndWritesNothing(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	t.Setenv("SENTINEL_VNC_PASSWORD", "ExternallyManaged1")

	pass, src, path, warn := resolveVNCPass(repo)
	if pass != "ExternallyManaged1" || src != vncPassFromEnv {
		t.Fatalf("got (%q, %q), want the env value with source %q", pass, src, vncPassFromEnv)
	}
	if len(warn) != 0 {
		t.Errorf("unexpected warnings: %v", warn)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("an env-supplied password must not be persisted, but %s exists (stat err=%v)", path, err)
	}
}

// A blank/whitespace value is "unset", not "the empty password" — the same present-but-empty
// treatment ADR-063 established for the LLM_* vars.
func TestResolveVNCPassBlankEnvIsUnset(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	t.Setenv("SENTINEL_VNC_PASSWORD", "   \t ")

	pass, src, _, _ := resolveVNCPass(repo)
	if src != vncPassGenerated {
		t.Fatalf("source is %q, want %q — a blank env value must fall through to generation", src, vncPassGenerated)
	}
	if !usableVNCPass(pass) {
		t.Errorf("generated password %q is not usable by our own rule", pass)
	}
}

// ⚠ NO TWIN IN token_test.go, and the divergence is the point. resolveToken takes a malformed
// CONTROL_API_TOKEN verbatim because it simply fails to authenticate and the operator sees 403. A
// malformed VNC password fails INVISIBLY: RFB pads or truncates it into eight bytes, so the server
// starts, looks healthy, and accepts a credential nobody intended. The door is the last place where
// that difference can still be seen.
func TestResolveVNCPassUnusableEnvRefusesToStart(t *testing.T) {
	for _, bad := range []string{
		"short7c",              // 7 chars — below the protocol's effective width
		"has space",            // whitespace inside: lost by one of YAML/shell/argv/copy-paste
		"пароль12",             // non-ASCII: a different byte count under a different encoding
		strings.Repeat("x", 513), // beyond the sanity ceiling
	} {
		t.Run(bad[:min(len(bad), 12)], func(t *testing.T) {
			clearVNCEnv(t)
			repo := t.TempDir()
			t.Setenv("SENTINEL_VNC_PASSWORD", bad)

			pass, src, _, warn := resolveVNCPass(repo)
			if src != vncPassUnavailable || pass != "" {
				t.Fatalf("got (%q, %q), want an empty password with source %q", pass, src, vncPassUnavailable)
			}
			if len(warn) == 0 {
				t.Fatal("refused without saying why")
			}
			// The warning goes to stderr and from there into a container log. It must name the RULE,
			// never the value that broke it.
			for _, w := range warn {
				if strings.Contains(w, bad) {
					t.Errorf("the warning quotes the supplied value back: %q", w)
				}
			}
		})
	}
}

func TestResolveVNCPassGeneratesPersistsAndReuses(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()

	pass, src, path, warn := resolveVNCPass(repo)
	if src != vncPassGenerated || len(warn) != 0 {
		t.Fatalf("got source %q warnings %v, want %q with none", src, warn, vncPassGenerated)
	}
	if len(pass) != vncPassChars {
		t.Errorf("generated %d characters, want exactly %d — the protocol checks only that many", len(pass), vncPassChars)
	}
	for _, r := range pass {
		if !strings.ContainsRune(vncPassAlphabet, r) {
			t.Errorf("generated password %q contains %q, which is outside the alphabet", pass, r)
		}
	}
	if want := filepath.Join(repo, "state", vncPassFileName); path != want {
		t.Errorf("path is %q, want %q", path, want)
	}
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	// Windows maps POSIX bits onto ACLs, so 0600 is not observable there and the assertion would fail
	// on a correctly-restricted file. Same skip, same reason, as token_test.go.
	if runtime.GOOS != "windows" && fi.Mode().Perm() != 0o600 {
		t.Errorf("password file mode is %v, want 0600 — this is a long-lived secret in cleartext", fi.Mode().Perm())
	}

	again, src2, _, _ := resolveVNCPass(repo)
	if again != pass || src2 != vncPassFromFile {
		t.Errorf("second call gave (%q, %q), want the same password from %q — a password already typed "+
			"into a viewer has to survive a restart", again, src2, vncPassFromFile)
	}
}

// A blank file is a truncated earlier write, not operator data: safe to replace.
func TestResolveVNCPassBlankFileIsRegenerated(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	path := filepath.Join(repo, "state", vncPassFileName)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("  \n"), 0o600); err != nil {
		t.Fatal(err)
	}

	pass, src, _, _ := resolveVNCPass(repo)
	if src != vncPassGenerated {
		t.Fatalf("source is %q, want %q", src, vncPassGenerated)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(got)) != pass {
		t.Errorf("file holds %q, want the new password %q", strings.TrimSpace(string(got)), pass)
	}
}

// The rule that carries the whole file: content we cannot use may still be content somebody meant to
// keep. It is never overwritten — we run with a throwaway and SAY so.
func TestResolveVNCPassUnusableFileIsNeverClobbered(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	own := filepath.Join(t.TempDir(), "operator.txt")
	const operatorData = "this is the operator's own file, not ours"
	if err := os.WriteFile(own, []byte(operatorData), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SENTINEL_VNC_PASSWORD_FILE", own)

	pass, src, _, warn := resolveVNCPass(repo)
	if src != vncPassGeneratedOnly {
		t.Fatalf("source is %q, want %q", src, vncPassGeneratedOnly)
	}
	if !usableVNCPass(pass) {
		t.Errorf("fallback password %q is not usable", pass)
	}
	if len(warn) == 0 || !strings.Contains(warn[0], own) {
		t.Errorf("warnings %v do not name the path that was left alone", warn)
	}
	got, err := os.ReadFile(own)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != operatorData {
		t.Errorf("the operator's file was rewritten: %q", string(got))
	}
}

// Same rule on the other error path: unreadable is not the same as absent.
func TestResolveVNCPassUnreadablePathIsNeverClobbered(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	dir := filepath.Join(t.TempDir(), "a-directory")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SENTINEL_VNC_PASSWORD_FILE", dir)

	pass, src, _, warn := resolveVNCPass(repo)
	if src != vncPassGeneratedOnly {
		t.Fatalf("source is %q, want %q", src, vncPassGeneratedOnly)
	}
	if !usableVNCPass(pass) {
		t.Errorf("fallback password %q is not usable", pass)
	}
	if len(warn) == 0 {
		t.Error("said nothing about a path it could not read")
	}
	fi, err := os.Stat(dir)
	if err != nil || !fi.IsDir() {
		t.Errorf("the directory was replaced (stat err=%v)", err)
	}
}

func TestResolveVNCPassAcceptsOperatorSuppliedFile(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	own := filepath.Join(t.TempDir(), "mine.txt")
	if err := os.WriteFile(own, []byte("MyOwnPass123\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SENTINEL_VNC_PASSWORD_FILE", own)

	pass, src, _, _ := resolveVNCPass(repo)
	if pass != "MyOwnPass123" || src != vncPassFromFile {
		t.Fatalf("got (%q, %q), want the file's contents from %q", pass, src, vncPassFromFile)
	}
}

// An unwritable directory still yields a working password — the container must come up — but it says
// the password changes on every restart, which is the part an operator has to know.
func TestResolveVNCPassUnwritableDirStillYieldsAPassword(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("0500 on a directory does not prevent creation under Windows ACLs — the test would be " +
			"vacuously green rather than meaningful")
	}
	if os.Geteuid() == 0 {
		t.Skip("running as root: mode bits do not restrain uid 0")
	}
	clearVNCEnv(t)
	repo := t.TempDir()
	state := filepath.Join(repo, "state")
	if err := os.MkdirAll(state, 0o500); err != nil {
		t.Fatal(err)
	}

	pass, src, _, warn := resolveVNCPass(repo)
	if src != vncPassGeneratedMem {
		t.Fatalf("source is %q, want %q", src, vncPassGeneratedMem)
	}
	if !usableVNCPass(pass) {
		t.Errorf("password %q is not usable", pass)
	}
	if len(warn) == 0 {
		t.Error("did not warn that the password will change on every restart")
	}
}

func TestUsableVNCPass(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want bool
	}{
		{strings.Repeat("a", vncPassMinLen), true},
		{strings.Repeat("a", vncPassMaxLen), true},
		{strings.Repeat("a", vncPassMaxLen+1), false},
		{"", false},
		{"short7c", false},   // one short of the floor
		{"has spce", false},  // exactly 8 but carries a space
		{"tab\there", false}, // and a tab
		{"пароль12", false},  // non-ASCII
		{"~!@#$%^&", true},   // the '!'..'~' boundaries themselves
	} {
		if got := usableVNCPass(tc.in); got != tc.want {
			t.Errorf("usableVNCPass(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

// A floor against a constant or seeded generator: the failure mode is not "wrong characters", it is
// "the same password on every machine", which no shape check would notice.
func TestNewVNCPassIsUniformAndInAlphabet(t *testing.T) {
	seen := map[string]bool{}
	const draws = 1000
	for i := 0; i < draws; i++ {
		p, err := newVNCPass()
		if err != nil {
			t.Fatalf("draw %d: %v", i, err)
		}
		if len(p) != vncPassChars {
			t.Fatalf("draw %d has length %d, want %d", i, len(p), vncPassChars)
		}
		for _, r := range p {
			if !strings.ContainsRune(vncPassAlphabet, r) {
				t.Fatalf("draw %d contains %q, outside the alphabet", i, r)
			}
		}
		seen[p] = true
	}
	if len(seen) < 900 {
		t.Errorf("only %d distinct passwords in %d draws — the generator is not random enough to be a "+
			"credential", len(seen), draws)
	}
}

func TestWriteVNCPassFileLeavesNoTempBehind(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, vncPassFileName)
	if err := writeVNCPassFile(path, "Abcdefgh"); err != nil {
		t.Fatal(err)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(ents) != 1 || ents[0].Name() != vncPassFileName {
		var names []string
		for _, e := range ents {
			names = append(names, e.Name())
		}
		t.Errorf("directory holds %v, want only %q — a leftover temp file is a second copy of the "+
			"secret with a name nobody looks at", names, vncPassFileName)
	}
}

// The machine half of the naming decision. Every name that can CARRY the password must be
// Secretish, because every redaction path in internal/redact keys off exactly that predicate.
//
// ⚠ The last case asserts a HOLE on purpose: `vnc_pass` is NOT Secretish (the bare word `pass` is
// absent from secretNameParts, and hasWord knows only `token`/`key`). That is why the file is called
// `vnc.password`. If somebody later widens Secretish, this line goes red and the widening becomes a
// decision somebody makes rather than a side effect — the same technique internal/redact/trace.go
// uses on its own dictionary.
func TestEveryNameThatCanCarryTheVNCPasswordIsSecretish(t *testing.T) {
	for _, name := range []string{
		"SENTINEL_VNC_PASSWORD",
		"SENTINEL_VNC_PASSWORD_FILE",
		"vnc_password",
		"vncPassword",
		"vnc-password",
		"vnc.password",
		"service.vnc_password_source",
	} {
		if !configguard.Secretish(name) {
			t.Errorf("configguard.Secretish(%q) is false — a value under that name would travel "+
				"unredacted through logs, traces and the journal", name)
		}
	}
	if configguard.Secretish("vnc_pass") {
		t.Log("configguard.Secretish(\"vnc_pass\") is now TRUE — the dictionary was widened. That is " +
			"fine, but it is a blast-radius change: re-check internal/redact callers, then delete this " +
			"assertion deliberately.")
		t.Errorf("this assertion records a known hole; it must be removed on purpose, not left to rot")
	}
}

// The catalogue entry is load-bearing rather than decorative: eventlog.Render is what turns the code
// into a sentence a human reads, and its absence would print a bare identifier into the journal.
func TestVNCPasswordEventIsCatalogued(t *testing.T) {
	msg, ok := eventlog.Render("service.vnc_password_source", map[string]string{
		"source": "generated", "path": "/app/state/vnc.password", "detail": "",
	})
	if !ok {
		t.Fatal("service.vnc_password_source is not in brain/events.json — the journal would carry a bare code")
	}
	if !strings.Contains(msg, "/app/state/vnc.password") {
		t.Errorf("rendered sentence does not name the file: %q", msg)
	}
	// ⚠ The template says "файл {path}" / "file {path}" rather than "path: {path}" deliberately: a
	// placeholder immediately after a colon or an equals sign is read by
	// internal/redact.scanNamedSecrets as an assignment, and since this very code name IS Secretish,
	// the redactor would blank our own prose. The same wording reversal was already made once, for
	// service.token_source.
	if strings.Contains(msg, "path:") || strings.Contains(msg, "path=") {
		t.Errorf("the template exposes the path as an assignment (%q) — the redactor would blank it", msg)
	}
}

// THE LEAK TEST. It searches the generated password in the RAW BYTES of the journal file rather than
// in a parsed record, because the interesting failure is a copy that landed somewhere the parser does
// not look — a field added later, a message built by concatenation, an error string carrying the
// value it failed on.
func TestVNCPasswordJournalRecordCarriesNoValue(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()

	if code := cmdVNCPassword(repo, nil); code != 0 {
		t.Fatalf("verb exited %d, want 0", code)
	}
	pass, err := os.ReadFile(filepath.Join(repo, "state", vncPassFileName))
	if err != nil {
		t.Fatal(err)
	}
	secret := strings.TrimSpace(string(pass))
	if len(secret) != vncPassChars {
		t.Fatalf("read a %d-character password, want %d", len(secret), vncPassChars)
	}

	journal, err := os.ReadFile(filepath.Join(repo, "state", "logs", "service.jsonl"))
	if err != nil {
		t.Fatalf("no service journal was written — principle 7 asks a new component to bring its own "+
			"observation, and this is it: %v", err)
	}
	if !strings.Contains(string(journal), "service.vnc_password_source") {
		t.Error("the journal has no record of where the password came from")
	}
	if strings.Contains(string(journal), secret) {
		t.Errorf("THE PASSWORD IS IN state/logs/service.jsonl. The journal survives `docker compose "+
			"down`, is served over the API and is collected by scripts/collect-live-run.sh")
	}
}

// The verb refuses rather than starting something unauthenticated — and the exit CODE is what
// `set -e` in scripts/vnc-entrypoint.sh reads, so it is asserted rather than assumed.
func TestVNCPasswordVerbExitsTwoWhenNoPasswordIsPossible(t *testing.T) {
	clearVNCEnv(t)
	repo := t.TempDir()
	t.Setenv("SENTINEL_VNC_PASSWORD", "nope") // set, and unusable: the one way to reach the refusal

	if code := cmdVNCPassword(repo, nil); code != 2 {
		t.Fatalf("verb exited %d, want 2 — the entrypoint's `set -e` is what turns this into "+
			"'x11vnc never starts', and it only reads the code", code)
	}
}

// filteredEnv decides what the brain, the executor and Chromium inherit. `SENTINEL_` is an allow
// PREFIX, so without the deny set the password would reach all three and sit in /proc/<pid>/environ,
// readable by any process of the same uid. The second half of the test matters as much as the first:
// the deny must beat an explicit SENTINEL_ENV_ALLOW, or the protection is advisory.
func TestFilteredEnvNeverForwardsTheVNCPassword(t *testing.T) {
	t.Setenv("SENTINEL_ENV_ALLOWLIST", "")
	t.Setenv("SENTINEL_ENV_ALLOW", "")
	t.Setenv("SENTINEL_VNC_PASSWORD", "LeakMe12")

	for _, kv := range filteredEnv() {
		if strings.HasPrefix(kv, "SENTINEL_VNC_PASSWORD=") {
			t.Fatalf("filteredEnv forwards the VNC password: %q", kv)
		}
	}

	// Now ask for it BY NAME through the operator-facing allowlist. Deny is checked first, so this
	// still must not pass it on.
	t.Setenv("SENTINEL_ENV_ALLOW", "SENTINEL_VNC_PASSWORD")
	for _, kv := range filteredEnv() {
		if strings.HasPrefix(kv, "SENTINEL_VNC_PASSWORD=") {
			t.Fatalf("SENTINEL_ENV_ALLOW overrode the deny set: %q", kv)
		}
	}

	// A neighbouring SENTINEL_ variable must still travel — otherwise the test above would pass on a
	// filteredEnv that had simply stopped working.
	t.Setenv("SENTINEL_SVC_NAME", "browser-vnc")
	var sawNeighbour bool
	for _, kv := range filteredEnv() {
		if strings.HasPrefix(kv, "SENTINEL_SVC_NAME=") {
			sawNeighbour = true
		}
	}
	if !sawNeighbour {
		t.Error("no SENTINEL_ variable survived at all — the deny set is not what this test measured")
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
