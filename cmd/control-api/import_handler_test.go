package main

import "testing"

// PROD-IMPORT channel 2 (ADR-105). The file name is the one piece of an upload that becomes a
// filesystem path, so validImportName is the security boundary of the HTTP import channel. The full
// endpoint is verified live (it spawns the real agentctl import); this pins the boundary that must
// hold regardless.
func TestValidImportName(t *testing.T) {
	ok := []string{"login.spec.ts", "  billing.spec.ts  ", "a-b_c.spec.ts"}
	for _, n := range ok {
		if base, good := validImportName(n); !good || base == "" {
			t.Errorf("validImportName(%q) rejected a legitimate spec name", n)
		}
	}
	// Kills: accepting a traversal-shaped name, a path separator, or a non-spec extension — each of
	// which would let an upload write outside the temp spec dir or smuggle a non-test file.
	bad := []string{
		"../../etc/passwd", "../evil.spec.ts", "a/b.spec.ts", `a\b.spec.ts`,
		"..", ".", "", "plain.txt", "config.json", "x.spec.ts.bak", "spec.ts",
	}
	for _, n := range bad {
		if _, good := validImportName(n); good {
			t.Errorf("validImportName(%q) accepted an unsafe/foreign name", n)
		}
	}
}

// lastLine must not echo more than its bound — an import failure detail must never become a channel
// for reflecting the whole uploaded suite back to the caller.
func TestLastLineBounded(t *testing.T) {
	big := ""
	for i := 0; i < 1000; i++ {
		big += "x"
	}
	if got := lastLine("first\n" + big); len(got) > 300 {
		t.Fatalf("lastLine returned %d chars, want <= 300", len(got))
	}
	if got := lastLine("a\nb\nlast line"); got != "last line" {
		t.Fatalf("lastLine = %q, want the final line", got)
	}
}
