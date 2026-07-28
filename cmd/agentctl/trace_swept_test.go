package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// SEC-TRACE-SWEPT-SILENTLY. sweepTraces deleted trace.zip with a bare `_ = os.Remove` — no event, no
// log, no mark — and it is ON by default, so a removed trace was indistinguishable from a run that
// never had one. ADR-099 made the trace a downloadable artifact, which turned that silence into an
// apparent loss. Every removal now leaves a trace-removed.json marker in the swept run's own
// directory; the run log line is the audible half (asserted by the caller's stderr, not here).
//
// These gates run the REAL sweepTraces over a real directory tree, never a re-implementation.

// TestSweepTracesLeavesAMarkerNamingTheReason.
// Kills: reverting to a bare os.Remove with no marker (the silent delete).
// Kills: marking a run whose trace was NOT removed (a false "swept" on a surviving run).
func TestSweepTracesLeavesAMarkerNamingTheReason(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "1")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	now := time.Now()
	writeTrace(t, runsRoot, "old", now.Add(-2*time.Hour)) // removed (over keep=1)
	writeTrace(t, runsRoot, "new", now)                   // survives (newest)

	sweepTraces(runsRoot)

	// the swept run is marked, and the marker names WHY
	mkPath := filepath.Join(runsRoot, "old", "trace-removed.json")
	b, err := os.ReadFile(mkPath)
	if err != nil {
		t.Fatalf("swept run has no trace-removed marker: %v — the deletion is still silent", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("marker is not valid JSON: %v", err)
	}
	if m["removed_by"] != "retention" || m["reason"] != "count" {
		t.Fatalf("marker does not name the reason: %v", m)
	}
	// the SURVIVING run is NOT marked — a marker there would be a lie
	if _, err := os.Stat(filepath.Join(runsRoot, "new", "trace-removed.json")); err == nil {
		t.Fatal("the surviving run was marked as swept — a marker must mean the trace is actually gone")
	}
	// and its trace is genuinely still there
	if _, err := os.Stat(filepath.Join(runsRoot, "new", "trace.zip")); err != nil {
		t.Fatalf("the newest trace was removed: %v", err)
	}
}

// TestSweepTracesMarkerReasonIsTTLWhenOnlyTooOld.
// Kills: hard-coding reason="count" regardless of why the trace went.
func TestSweepTracesMarkerReasonIsTTLWhenOnlyTooOld(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "-1") // count-pruning OFF, so the only reason to delete is TTL
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "1")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	writeTrace(t, runsRoot, "stale", time.Now().Add(-3*time.Hour)) // past the 1h TTL

	sweepTraces(runsRoot)

	b, err := os.ReadFile(filepath.Join(runsRoot, "stale", "trace-removed.json"))
	if err != nil {
		t.Fatalf("a TTL-swept trace left no marker: %v", err)
	}
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	if m["reason"] != "ttl" {
		t.Fatalf("reason = %v, want ttl (the trace was deleted for age, not count)", m["reason"])
	}
}

// TestSweepTracesNoMarkerWhenNothingRemoved.
// Kills: writing a marker unconditionally (which would make every run look swept).
func TestSweepTracesNoMarkerWhenNothingRemoved(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "10")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	writeTrace(t, runsRoot, "kept", time.Now())

	sweepTraces(runsRoot)

	if _, err := os.Stat(filepath.Join(runsRoot, "kept", "trace-removed.json")); err == nil {
		t.Fatal("a run under the keep limit was marked swept — nothing was removed")
	}
	if _, err := os.Stat(filepath.Join(runsRoot, "kept", "trace.zip")); err != nil {
		t.Fatalf("the trace under the keep limit was removed: %v", err)
	}
}
