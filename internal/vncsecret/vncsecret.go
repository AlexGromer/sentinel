// Package vncsecret states, in ONE place, where the VNC password comes from and what counts as one.
//
// WHY IT EXISTS NOW AND NOT EARLIER. `cmd/agentctl/vncpass.go` deliberately kept this logic in
// `package main` and wrote down the trigger for moving it: "when a second consumer appears". ADR-127's
// relay is that consumer — control-api has to READ the password to spend it on the operator's behalf,
// so the browser never sees it. Moving the shared half now means the ORDER (env wins, then the file)
// is declared once instead of twice; leaving it would have been the second copy of a rule, which is
// the failure `internal/configguard` exists to prevent.
//
// WHAT IS AND IS NOT HERE. The reading half only. Generation, the atomic write and the never-clobber
// rules stay in agentctl, because they belong to the single PRODUCER: a reader that could create the
// file would be a second producer, and two producers of one secret is how a password silently changes
// under a running server.
package vncsecret

import (
	"os"
	"path/filepath"
	"strings"
)

const (
	// FileName is the on-disk name. ⚠ `vnc.password`, never `vnc.pass`: configguard.Secretish is true
	// for the former (substring `password`) and FALSE for the latter — the bare word `pass` is not in
	// its dictionary — and every redaction path keys off exactly that predicate.
	FileName = "vnc.password"

	// MinLen is the protocol's effective width. Measured against x11vnc 0.9.16: classic VNC auth builds
	// its DES key from the FIRST EIGHT BYTES and discards the rest — a server holding
	// `ABCDEFGH12345678` accepts `ABCDEFGH` and refuses `ABCDEFGX`. Below eight there is nothing left
	// to protect.
	MinLen = 8
	MaxLen = 512
)

// FilePath resolves the password file: SENTINEL_VNC_PASSWORD_FILE overrides <repo>/state/vnc.password.
func FilePath(repo string) string {
	if p := strings.TrimSpace(os.Getenv("SENTINEL_VNC_PASSWORD_FILE")); p != "" {
		return p
	}
	return filepath.Join(repo, "state", FileName)
}

// Usable reports whether a string can serve as a VNC password: one line, no inner whitespace,
// printable ASCII, bounded length.
//
// The character rule matches usableToken in cmd/control-api/token.go byte for byte, and for VNC it is
// not cosmetic: RFB puts the password bytes into the DES key as-is, so a non-ASCII rune would
// contribute a different number of bytes under a different terminal encoding — a password that
// "sometimes works".
func Usable(s string) bool {
	if len(s) < MinLen || len(s) > MaxLen {
		return false
	}
	for _, r := range s {
		if r < '!' || r > '~' {
			return false
		}
	}
	return true
}

// Source labels where a password came from, for logs and tests. Never the value.
type Source string

const (
	FromEnv  Source = "env"
	FromFile Source = "file"
	Absent   Source = "absent"
)

// Read returns the live password WITHOUT creating anything. Order matches the producer's: the
// environment wins, then the file.
//
// Returns Absent with an empty string when neither path yields a usable value — the caller decides
// what that means. For the relay it means "there is a VNC server configured that we cannot
// authenticate to", which is a REASON to show a person, not a crash.
func Read(repo string) (string, Source) {
	if v := strings.TrimSpace(os.Getenv("SENTINEL_VNC_PASSWORD")); v != "" && Usable(v) {
		return v, FromEnv
	}
	b, err := os.ReadFile(FilePath(repo))
	if err != nil {
		return "", Absent
	}
	if v := strings.TrimSpace(string(b)); Usable(v) {
		return v, FromFile
	}
	return "", Absent
}
