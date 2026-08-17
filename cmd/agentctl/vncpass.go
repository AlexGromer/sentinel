package main

// VNC password lifecycle (W3 [LIVE-VNC]) — the SINGLE producer of the secret x11vnc checks.
//
// Modelled line by line on cmd/control-api/token.go::resolveToken, deliberately: that case table was
// bought by measured failures — a truncated write that still parsed, an operator's file clobbered, a
// token that silently changed on every restart — and none of them are about bearer tokens. They are
// about "a Go process owns a secret file under state/".
//
//	env       SENTINEL_VNC_PASSWORD set     → use it verbatim, never touch the file
//	file      state/vnc.password usable     → reuse it, so a password already pasted into a viewer
//	                                          survives a restart
//	generated otherwise                     → 8 chars from a 56-symbol alphabet, persisted 0600
//
// WHY HERE AND NOT IN control-api. The consumer is x11vnc inside the `browser-vnc` container, and
// start ORDER decides who may produce: control-api waits on `browser` with `condition:
// service_healthy` (docker-compose.yml), so it starts LAST — a password written by it would arrive
// after it was needed. A container that produces its own secret as the first step of its own
// entrypoint removes the ordering question instead of answering it. `agentctl` is already in the
// image, in the .deb and in the six-platform release matrix: this is a NEW VERB, not the fifth
// binary that ADR-119 deleted a component for.
//
// WHY NOT SHELL IN THE ENTRYPOINT. The never-clobber rules below ARE the value of the file; writing
// them again in shell would be a SECOND copy of the rule, which is the failure internal/configguard
// exists to prevent (see its package comment). Shell keeps exactly the job it is for: handing the
// plaintext to x11vnc.
//
// ⚠ THERE IS NO `disabled` SOURCE, and that absence is the feature. resolveToken has one because a
// token-less control-api is a meaningful, safe, read-only mode. A password-less VNC is not: it is a
// desktop that accepts input. No value of any environment variable yields an empty password AND a
// running x11vnc — `vncPassUnavailable` exists only to return exit code 2 so `set -e` in
// scripts/vnc-entrypoint.sh stops before the server starts.

import (
	"crypto/rand"
	"flag"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"strings"

	"github.com/AlexGromer/sentinel/internal/eventlog"
	"github.com/AlexGromer/sentinel/internal/svclog"
)

const (
	// ⚠ NAMED `vnc.password`, not `vnc.pass`, and the difference is machine-checked rather than
	// cosmetic: configguard.Secretish("vnc_password") is TRUE (substring `password`), while
	// Secretish("vnc_pass") is FALSE — the bare word `pass` is not in secretNameParts, and hasWord
	// only knows `token`/`key`. Every redaction path in internal/redact keys off Secretish, so the
	// shorter name would have produced a field nobody redacts. Asserted by
	// TestEveryNameThatCanCarryTheVNCPasswordIsSecretish.
	vncPassFileName = "vnc.password"

	// ⚠ EIGHT, and this number is MEASURED, not chosen (2026-08-17, x11vnc 0.9.16 in bookworm).
	// Classic RFB "VNC Authentication" builds its DES key from the FIRST EIGHT BYTES of the password
	// and discards the rest. Proven in both directions against a live server holding the 16-character
	// password `ABCDEFGH12345678`: a client sending `ABCDEFGH` was ACCEPTED, a client sending
	// `ABCDEFGX` was REFUSED. Corroborated by `x11vnc -storepasswd`, which writes an 8-byte file for a
	// 4-, 8- or 16-character password alike, byte-identical for the first two.
	//
	// Generating 64 hex characters would therefore PRINT a long secret and CHECK a short one — the
	// worst kind of security theatre, because the reassuring part is the number in the log.
	vncPassChars = 8

	vncPassMinLen = 8   // below the protocol's effective width there is nothing left to protect
	vncPassMaxLen = 512 // same sanity ceiling as tokenMaxLen: an operator's file is not a payload
)

// vncPassAlphabet is what we GENERATE from; usableVNCPass (what we ACCEPT) stays exactly as
// permissive as usableToken. Same split as token.go — accept broadly, generate narrowly (there, hex).
//
// The value travels through a YAML file, a shell entrypoint, argv and a human's copy/paste; every
// character that needs quoting in even one of those will one day be lost in one of those. Lookalike
// glyphs (0/O, 1/l/I) are excluded because this password is read off a terminal by eye and typed into
// a viewer by hand.
//
// 56 symbols ** 8 positions = 9.7e13 ≈ 46.5 bits — and that is the ONLY honest number, because only
// eight positions are ever checked (see vncPassChars).
const vncPassAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

// vncPassSource labels where the live password came from (for the startup line, the journal, tests).
type vncPassSource string

const (
	vncPassFromEnv       vncPassSource = "env"
	vncPassFromFile      vncPassSource = "file"
	vncPassGenerated     vncPassSource = "generated"
	vncPassGeneratedMem  vncPassSource = "generated (in-memory)"
	vncPassGeneratedOnly vncPassSource = "generated (not persisted)"
	vncPassUnavailable   vncPassSource = "unavailable"
)

// vncPassFilePath: SENTINEL_VNC_PASSWORD_FILE overrides <repo>/state/vnc.password — the same shape
// and the same reason as tokenFilePath.
func vncPassFilePath(repo string) string {
	if p := strings.TrimSpace(os.Getenv("SENTINEL_VNC_PASSWORD_FILE")); p != "" {
		return p
	}
	return filepath.Join(repo, "state", vncPassFileName)
}

// usableVNCPass: one line, no inner whitespace, printable ASCII, bounded length.
//
// The character rule matches usableToken byte for byte and for the same reason: '!'..'~' excludes
// space, tab, CR/LF and EVERY non-ASCII rune. For VNC that is not cosmetic — RFB puts the password
// bytes into the DES key as-is, so a non-ASCII rune would contribute a different number of bytes
// under a different terminal encoding, i.e. a password that "sometimes works".
func usableVNCPass(s string) bool {
	if len(s) < vncPassMinLen || len(s) > vncPassMaxLen {
		return false
	}
	for _, r := range s {
		if r < '!' || r > '~' {
			return false
		}
	}
	return true
}

func newVNCPass() (string, error) {
	b := make([]byte, vncPassChars)
	n := big.NewInt(int64(len(vncPassAlphabet)))
	for i := range b {
		// crypto/rand.Int rather than rand.Read + `% len(alphabet)`: 256 is not a multiple of 56, so
		// the modulo form is biased toward the first 32 letters. Int does rejection sampling.
		// token.go has no such concern because hex.EncodeToString maps a byte to two characters with
		// no remainder.
		k, err := rand.Int(rand.Reader, n)
		if err != nil {
			return "", err
		}
		b[i] = vncPassAlphabet[k.Int64()]
	}
	return string(b), nil
}

// writeVNCPassFile is a byte-for-byte twin of writeTokenFile with a different file name. Atomic: a
// crash mid-write must never leave a truncated-but-plausible password behind (a short prefix would
// pass usableVNCPass on the next start). os.CreateTemp already creates with 0600, so there is no
// window in which the file is group/world-readable.
//
// ⚠ The trailing "\n" is kept, exactly as token.go writes it, because it was MEASURED to be safe:
// x11vnc's `-passwdfile` takes the FIRST LINE of the file, and a live server started from a file
// holding `sekret12\n` accepted the password `sekret12` (2026-08-17). Had it not, this would be the
// one place where the twin had to diverge — and the divergence would have needed saying out loud.
func writeVNCPassFile(path, pass string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, "."+vncPassFileName+"-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op once the rename succeeded
	if _, err := f.WriteString(pass + "\n"); err != nil {
		_ = f.Close()
		return err
	}
	// ⚠ RECORDED EQUIVALENT MUTATION (2026-08-17): deleting this Chmod leaves every test green,
	// because os.CreateTemp already creates with 0600 and TestResolveVNCPassGeneratesPersistsAndReuses
	// therefore still sees 0600. It is kept anyway, and the survival is written down rather than
	// hidden: the line defends against a change in that documented stdlib behaviour, and the only test
	// that could kill the mutation would have to assert on the temp file mid-write — a test of an
	// implementation detail that would break on any future rewrite of this function.
	if err := f.Chmod(0o600); err != nil { // explicit: CreateTemp's mode is documented, don't rely on it
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// resolveVNCPass returns the live password, its source, the file path and any warnings. It NEVER
// returns an error: whether to start is cmdVNCPassword's decision, and every diagnostic travels
// through warnings — exactly as resolveToken does.
func resolveVNCPass(repo string) (pass string, src vncPassSource, path string, warnings []string) {
	path = vncPassFilePath(repo)

	if v := strings.TrimSpace(os.Getenv("SENTINEL_VNC_PASSWORD")); v != "" {
		if !usableVNCPass(v) {
			// ⚠ A DELIBERATE DIVERGENCE FROM token.go, not an oversight. resolveToken takes
			// CONTROL_API_TOKEN verbatim because a malformed bearer token simply fails to
			// authenticate — the operator sees 403 and knows. A malformed VNC password does not fail
			// visibly: RFB pads or TRUNCATES it into eight bytes, so a password with a space in it,
			// or a 3-character one, produces a server that starts, looks healthy, and accepts a
			// credential the operator did not think they set. Refusing at the door is the only place
			// where the difference is still visible.
			return "", vncPassUnavailable, path, []string{
				fmt.Sprintf("SENTINEL_VNC_PASSWORD is set but unusable (need %d-%d printable non-space "+
					"ASCII characters; RFB checks only the first %d bytes) — refusing to start a VNC "+
					"server with a credential nobody can predict", vncPassMinLen, vncPassMaxLen, vncPassChars),
			}
		}
		return v, vncPassFromEnv, path, nil
	}

	// Reuse a previously persisted password so a value already typed into a viewer keeps working.
	existing, readErr := os.ReadFile(path)
	trimmed := strings.TrimSpace(string(existing))
	switch {
	case readErr == nil && usableVNCPass(trimmed):
		return trimmed, vncPassFromFile, path, nil
	case readErr == nil && trimmed != "":
		// Non-empty but unusable: this may be operator data (a wrong file pointed at by
		// SENTINEL_VNC_PASSWORD_FILE). Never clobber it — run with an in-memory password instead.
		gen, err := newVNCPass()
		if err != nil {
			return "", vncPassUnavailable, path, []string{fmt.Sprintf("cannot generate a password: %v", err)}
		}
		return gen, vncPassGeneratedOnly, path, []string{
			fmt.Sprintf("%s holds unusable content — left untouched; using a throwaway password that "+
				"changes on every restart", path),
		}
	case readErr != nil && !os.IsNotExist(readErr):
		// Unreadable (permissions, a directory, …). Same rule: do not overwrite what we cannot read.
		gen, err := newVNCPass()
		if err != nil {
			return "", vncPassUnavailable, path, []string{fmt.Sprintf("cannot generate a password: %v", err)}
		}
		return gen, vncPassGeneratedOnly, path, []string{
			fmt.Sprintf("%s unreadable: %v — left untouched; using a throwaway password that changes "+
				"on every restart", path, readErr),
		}
	}

	// Missing, or present-but-blank (a truncated earlier write) — safe to (re)create.
	gen, err := newVNCPass()
	if err != nil {
		return "", vncPassUnavailable, path, []string{fmt.Sprintf("cannot generate a password: %v", err)}
	}
	if err := writeVNCPassFile(path, gen); err != nil {
		return gen, vncPassGeneratedMem, path, []string{
			fmt.Sprintf("cannot persist the password to %s: %v (a new one is generated on every "+
				"restart, so a viewer session does not survive one)", path, err),
		}
	}
	return gen, vncPassGenerated, path, nil
}

// cmdVNCPassword is the verb. Without --print it makes the file exist and says WHERE, never WHAT;
// with --print it writes the password to stdout and nothing else, so the entrypoint can capture it
// without the surrounding prose ending up in a variable.
func cmdVNCPassword(repo string, args []string) int {
	fs := flag.NewFlagSet("vnc-password", flag.ExitOnError)
	print := fs.Bool("print", false, "write the password itself to stdout (for a consumer that needs the value)")
	_ = fs.Parse(args)

	pass, src, path, warnings := resolveVNCPass(repo)
	for _, w := range warnings {
		fmt.Fprintf(os.Stderr, "agentctl vnc-password: WARNING — %s\n", w)
	}
	if src == vncPassUnavailable || pass == "" {
		// Exit 2, and `set -e` in the entrypoint turns that into "x11vnc never starts". This is what
		// makes "neither path yielded a password" impossible to survive rather than merely unlikely.
		fmt.Fprintln(os.Stderr, "agentctl vnc-password: no usable password by either path — refusing to continue")
		return 2
	}

	// The service journal gets the SOURCE and the PATH, never the value. The sentence comes from the
	// catalogue's template rather than from concatenation here (ADR-117), and the code LITERAL stays
	// at the call site, which is where the catalogue gate's emitter scan looks for it.
	detail := ""
	if src == vncPassFromEnv {
		detail = " (the file was not touched)"
	}
	msg, ok := eventlog.Render("service.vnc_password_source", map[string]string{
		"source": string(src),
		"path":   path,
		"detail": detail,
	})
	if !ok {
		msg = "eventlog.uncatalogued: service.vnc_password_source is not in the catalogue"
	}
	w := svclog.Open(filepath.Join(repo, "state"), "agentctl")
	w.Log(svclog.Record{Lvl: "info", Cat: "service", Code: "service.vnc_password_source", Msg: msg})
	w.Close()

	if *print {
		fmt.Println(pass)
		return 0
	}
	// Deliberately not the value: whoever runs this without --print wants the file to exist, and a
	// password echoed into a terminal scrollback (or a container log) is a copy nobody chose to make.
	fmt.Fprintf(os.Stderr, "agentctl vnc-password: source=%s file=%s\n", src, path)
	return 0
}
