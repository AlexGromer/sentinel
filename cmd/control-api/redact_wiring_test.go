package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ADR-098: this one stayed behind when the redactor moved to internal/redact. It asserts the WIRING —
// that everything the sink writes descends from write() and is therefore redacted — which is a fact
// about control-api, not about the scanner. The pure-function checks went with the function; a test
// that follows its subject keeps saying something when the subject moves.
// The choke point: everything the sink writes descends from write(), so a secret must not reach ANY of
// the three files. Asserting through the sink rather than the pure function is what proves the wiring.
func TestSinkRedactsEveryFileItWrites(t *testing.T) {
	dir := t.TempDir()
	s := newLogSink(dir)
	if s == nil {
		t.Fatal("sink not created")
	}
	s.write(`[error|app] app.console_error: checkout failed for token=LEAKED-SESSION-VALUE`)
	s.write(`@@AGUI {"type":"tool.call","data":{"args_summary":"navigate https://x/?access_token=LEAKED-IN-FRAME"}}`)
	s.close()

	found := map[string]bool{}
	for _, name := range []string{"run.log", "run.jsonl", "events.jsonl"} {
		b, err := os.ReadFile(filepath.Join(dir, "logs", name))
		if err != nil {
			t.Fatalf("reading %s: %v", name, err)
		}
		body := string(b)
		found[name] = len(strings.TrimSpace(body)) > 0
		for _, secret := range []string{"LEAKED-SESSION-VALUE", "LEAKED-IN-FRAME"} {
			if strings.Contains(body, secret) {
				t.Fatalf("%s contains %s:\n%s", name, secret, body)
			}
		}
	}
	// Non-emptiness asserted AFTER the fact and BEFORE trusting the absence above: three empty files
	// would satisfy every "does not contain" check while proving nothing at all.
	if !found["run.log"] || !found["run.jsonl"] || !found["events.jsonl"] {
		t.Fatalf("a log file was empty, so the absence checks above are vacuous: %v", found)
	}
}

// A credential that both rules catch must read as one redaction, not two. Cosmetic, but a line reading
// `[REDACTED] [REDACTED]` invites the reader to wonder whether the redactor is broken — and a security
// control that looks broken gets turned off.
