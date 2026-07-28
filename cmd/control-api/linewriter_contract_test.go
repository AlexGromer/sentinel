package main

import (
	"strings"
	"testing"
)

// SEC-RUNS-ERROR-UNGUARDED. runs.error is clean only because it never receives the output of the
// application under test — and that rests entirely on one invariant: lineWriter.Write always returns
// (len(p), nil). os/exec's copy goroutine surfaces a writer error as cmd.Wait()'s error, and that
// error string flows straight into rec.Error and then into the runs.error column. So the day
// lineWriter.Write returns a real error, a stdout/stderr copy failure — which carries the copied AUT
// bytes — could land in the database, and the ADR-100 inventory's "runs.error is clean" would quietly
// become false.
//
// The invariant was a property of the implementation, guarded by nothing and pinned by no test. This
// pins it: the contract is now explicit, and a change that breaks it fails here rather than in
// production.
//
// Kills: returning an error from Write on any input.
// Kills: returning a byte count other than len(p) (a short write also makes os/exec's copy fail).
func TestLineWriterWriteContract(t *testing.T) {
	inputs := [][]byte{
		nil,
		[]byte(""),
		[]byte("no newline"),
		[]byte("one line\n"),
		[]byte("two\nlines\n"),
		[]byte("trailing partial\nand more"),
		[]byte("\r\nwindows\r\n"),
		[]byte{0xff, 0xfe, 0x00, '\n'},                 // invalid UTF-8 + a NUL, still must be consumed whole
		[]byte(strings.Repeat("x", 1<<16) + "\n"),      // large
		[]byte("secret-shaped ghp_0123456789ABCDEF\n"), // gitleaks:allow — the writer must not choke on it either
	}
	// A sink over a directory that cannot be created is nil-safe by design; exercise that path too, so
	// the contract holds even when the log files were never opened.
	for _, sink := range []*logSink{newLogSink(t.TempDir()), newLogSink("/proc/nonexistent/cannot")} {
		w := &lineWriter{rs: newRunStream(), sink: sink}
		for _, in := range inputs {
			n, err := w.Write(in)
			if err != nil {
				t.Fatalf("Write(%q) returned error %v — the copy goroutine would surface this into runs.error", in, err)
			}
			if n != len(in) {
				t.Fatalf("Write(%q) returned n=%d, want %d — a short write also fails os/exec's copy", in, n, len(in))
			}
		}
	}
}
