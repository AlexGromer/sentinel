package main

// The negative control for ADR-100: a normal run must delete NOTHING from the store.
//
// This is the gate the "explicitly invoked, never automatic" requirement lives or dies by, and it is
// deliberately built around the real function rather than a re-creation of it: mkArtifactDir is what
// every single run calls, and it is where the automatic retention sweeps (sweepTraces / sweepLogs /
// sweepRuns) actually fire. Asserting against a copy of that block would prove nothing about the
// block that ships.
//
// The sibling failure that makes this worth a test: SEC-RETENTION-DOWNLOAD was found because a
// proposed "delete on serve" retention mode would have destroyed a run at the moment a human merely
// OPENED it. Automatic deletion reaches further than its author expects, every time.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/AlexGromer/sentinel/internal/store"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// TestANormalRunPurgesNothing.
// Kills: purgeStore(...) wired into the sweep block in mkArtifactDir alongside sweepTraces/
// sweepLogs/sweepRuns — the single mistake the whole "explicit invocation" posture exists to prevent.
func TestANormalRunPurgesNothing(t *testing.T) {
	repo := t.TempDir()
	dbPath := filepath.Join(repo, "state", "locators.db")
	if err := os.MkdirAll(filepath.Dir(dbPath), 0o700); err != nil {
		t.Fatal(err)
	}

	const canary = "CANARY-NOTAUTOMATIC-Confirm-payment-0123"
	s, err := store.New(dbPath)
	if err != nil {
		t.Fatalf("store.New: %v", err)
	}
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		if _, err := s.AppendAudit(ctx, &storepb.AuditRow{
			RunId: "seed", Step: int64(i), Original: canary, Outcome: "flagged",
		}); err != nil {
			t.Fatalf("AppendAudit: %v", err)
		}
	}
	if _, err := s.UpsertRun(ctx, &storepb.RunRecord{
		RunId: "seed-run", Target: "https://shop.example.com/" + canary, State: "done",
	}); err != nil {
		t.Fatalf("UpsertRun: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	before, err := os.ReadFile(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(before), canary) {
		t.Fatal("fixture is vacuous: the canary never reached the database file")
	}

	// Make the sweeps as eager as they can possibly be. If any purge had been folded in among them,
	// this configuration is the one that would fire it.
	t.Setenv("SENTINEL_TRACE_KEEP", "0")
	t.Setenv("SENTINEL_LOG_KEEP", "1")
	t.Setenv("SENTINEL_RUN_KEEP", "1")

	// The real call every run makes — this is where sweepTraces/sweepLogs/sweepRuns run.
	for i := 0; i < 3; i++ {
		_ = mkArtifactDir(repo, "run-"+string(rune('a'+i)), "")
	}

	after, err := os.ReadFile(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	// Byte-for-byte: not merely "the rows are still queryable" but "nothing touched this file". A
	// purge that deleted and re-vacuumed would leave the rows gone; one that deleted without
	// vacuuming would leave a same-length file with different bytes. Both fail here.
	if string(after) != string(before) {
		t.Fatal("a normal run modified the store database — purge must never be automatic")
	}

	// And prove it through the API too, so a future change that keeps the file identical by accident
	// (e.g. writing to a different path) cannot pass this by luck.
	s2, err := store.New(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	rows, err := s2.AuditRows(ctx, &storepb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows.Rows) != 5 {
		t.Fatalf("healing_audit has %d rows after three runs, want 5 — something purged them", len(rows.Rows))
	}
	run, err := s2.GetRun(ctx, &storepb.RunId{RunId: "seed-run"})
	if err != nil {
		t.Fatal(err)
	}
	if !run.Found {
		t.Fatal("the seeded run disappeared during a normal run")
	}
}
