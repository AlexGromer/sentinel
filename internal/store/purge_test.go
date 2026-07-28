package store

// Gates for ADR-100 (purge). Each test names the mutant it exists to kill; a test with no such
// mutant would be redundant and is not written.
//
// The load-bearing one is TestPurgeVacuumIsTheOnlyThingThatRemovesTheBytes. The naive version of
// this gate — "purge, then assert the row is gone" — passes identically under both policies and so
// proves nothing about the distinction the whole feature rests on. What separates them is only
// visible in the file's bytes.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// canary is structural: no keyword, no credential shape. internal/redact would not match it, which
// is the point — this is FOREIGN TEXT (the page's own words), not a secret, and the credential
// scanner is blind to it by design (configguard.Secretish("value") == false).
const canary = "CANARY-PURGE-Confirm-payment-01234567"

// seedAuditCanary writes the canary through the REAL write path (the AppendAudit RPC, which is what
// brain/healing.py calls), not a hand-rolled INSERT. A fixture that inserts directly would measure a
// copy of the write path rather than the write path.
func seedAuditCanary(t *testing.T, path string) *Server {
	t.Helper()
	os.Unsetenv("STORE_DSN")
	s, err := New(path)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for i := 0; i < 40; i++ { // enough rows to occupy a real page
		if _, err := s.AppendAudit(context.Background(), &pb.AuditRow{
			RunId: "r1", Step: int64(i), SemanticId: "abc123",
			PagePath: "https://shop.example.com/checkout",
			Strategy: "llm_pick", Original: canary, Healed: `{"testid":"pay"}`, Outcome: "flagged",
		}); err != nil {
			t.Fatalf("AppendAudit: %v", err)
		}
	}
	// With WAL on, freshly written content lives in the -wal file. Checkpointing first means the
	// later greps look at the main database, so a positive/negative result is about the database and
	// not about which file happened to hold the bytes.
	if _, err := s.db.Exec(`PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
		t.Fatalf("checkpoint: %v", err)
	}
	if !fileHas(t, path, canary) {
		t.Fatal("fixture is vacuous: the canary never reached the database file")
	}
	return s
}

func fileHas(t *testing.T, path, needle string) bool {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return strings.Contains(string(b), needle)
}

func auditRows(t *testing.T, s *Server) int {
	t.Helper()
	var n int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM healing_audit`).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	return n
}

// TestPurgeVacuumIsTheOnlyThingThatRemovesTheBytes proves the two policies genuinely differ, which
// is the claim the deployer's choice rests on. Measured beforehand on modernc.org/sqlite v1.53.0:
// secure_delete defaults to 0, so a deleted row stays greppable until the file is rewritten.
//
// Kills: --vacuum parsed but VACUUM never executed (the row-count assertion alone cannot see this).
// Kills: a "scrub" that skips the WAL checkpoint and leaves the bytes in -wal while reporting success.
func TestPurgeVacuumIsTheOnlyThingThatRemovesTheBytes(t *testing.T) {
	for _, tc := range []struct {
		name          string
		vacuum        bool
		wantBytesGone bool
	}{
		{"delete only leaves the bytes behind", false, false},
		{"vacuum scrubs them", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "s.db")
			s := seedAuditCanary(t, path)
			defer s.Close()

			rep, err := s.PurgeStore(context.Background(), &pb.PurgeReq{
				Tables: []string{"healing_audit"}, Vacuum: tc.vacuum,
			})
			if err != nil {
				t.Fatalf("PurgeStore: %v", err)
			}
			if rep.VacuumSkipped != "" {
				t.Fatalf("vacuum unexpectedly skipped: %s", rep.VacuumSkipped)
			}
			if rep.Vacuumed != tc.vacuum {
				t.Fatalf("Vacuumed=%v, want %v", rep.Vacuumed, tc.vacuum)
			}

			// Both policies agree here — which is exactly why this assertion alone is not enough.
			if n := auditRows(t, s); n != 0 {
				t.Fatalf("rows still queryable: %d", n)
			}
			if len(rep.Counts) != 1 || rep.Counts[0].Rows != 40 {
				t.Fatalf("counts = %+v, want healing_audit=40", rep.Counts)
			}

			// Deliberately NO checkpoint here. An earlier version of this test checkpointed before
			// grepping "so both branches are judged against the main file", and that silently did
			// the product's job for it: removing BOTH wal_checkpoint calls from the vacuum path left
			// this test green (caught by mutation). The files are now read exactly as PurgeStore
			// left them, which is the only way the checkpointing can be observed at all.
			//
			// THE ASSERTION THAT SEPARATES THE POLICIES. Note it is about CONTENT, not file size:
			// VACUUM here leaves the file the same length and only rewrites what is inside it, so a
			// size-based check would prove nothing (measured).
			gone := !fileHas(t, path, canary) && !fileHas(t, path+"-wal", canary)
			if gone != tc.wantBytesGone {
				t.Fatalf("bytes gone from disk = %v, want %v — the two purge policies are not distinct",
					gone, tc.wantBytesGone)
			}
		})
	}
}

// TestPurgeRefusesAnEmptyOrUnknownScope.
// Kills: a default that reads "no tables given" as "every table".
// Kills: a scope check that accepts an arbitrary name (which would also be an injection surface,
// since the table name is concatenated into the DELETE).
func TestPurgeRefusesAnEmptyOrUnknownScope(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		req  *pb.PurgeReq
	}{
		{"empty scope", &pb.PurgeReq{}},
		{"unknown table", &pb.PurgeReq{Tables: []string{"healing_audit", "sqlite_master"}}},
		{"config is not purgeable", &pb.PurgeReq{Tables: []string{"config"}}},
		{"injection-shaped name", &pb.PurgeReq{Tables: []string{"runs; DROP TABLE runs"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := s.PurgeStore(ctx, tc.req); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("err = %v, want InvalidArgument", err)
			}
		})
	}

	// Refusal must be total: a request naming one good and one bad table deletes NOTHING. A purge
	// that half-applied before failing would be unrecoverable and unreportable.
	if _, err := s.AppendAudit(ctx, &pb.AuditRow{RunId: "r", Original: canary}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PurgeStore(ctx, &pb.PurgeReq{Tables: []string{"healing_audit", "nope"}}); err == nil {
		t.Fatal("expected refusal")
	}
	if n := auditRows(t, s); n != 1 {
		t.Fatalf("a refused purge deleted rows anyway: %d left, want 1", n)
	}
}

// TestPurgeAgeFilterHandlesBothTimeFormats. The heal schema stores REAL epoch seconds and the M13
// domains store RFC3339 TEXT; one comparison cannot serve both.
//
// Kills: comparing an RFC3339 string against a float (matches everything or nothing — silently).
// Kills: applying the epoch comparison to the TEXT column, or vice versa.
func TestPurgeAgeFilterHandlesBothTimeFormats(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	old := time.Now().Add(-72 * time.Hour)
	recent := time.Now().Add(-1 * time.Hour)

	// runs.started_at is RFC3339 TEXT and UpsertRun honours a caller-supplied value, so both rows go
	// in through the real RPC.
	for _, r := range []struct {
		id string
		at time.Time
	}{{"old-run", old}, {"new-run", recent}} {
		if _, err := s.UpsertRun(ctx, &pb.RunRecord{
			RunId: r.id, Target: "https://shop.example.com/checkout", State: "done",
			StartedAt: r.at.UTC().Format(time.RFC3339),
		}); err != nil {
			t.Fatal(err)
		}
	}
	// healing_audit.ts is REAL and AppendAudit stamps it with now(), so the row is written through
	// the real RPC and then aged — the age, not the write, is what this test fabricates.
	for _, a := range []struct {
		id string
		at time.Time
	}{{"old-audit", old}, {"new-audit", recent}} {
		if _, err := s.AppendAudit(ctx, &pb.AuditRow{RunId: a.id, Original: canary}); err != nil {
			t.Fatal(err)
		}
		if _, err := s.db.Exec(`UPDATE healing_audit SET ts=? WHERE run_id=?`,
			float64(a.at.Unix()), a.id); err != nil {
			t.Fatal(err)
		}
	}

	cutoff := float64(time.Now().Add(-24 * time.Hour).Unix())
	rep, err := s.PurgeStore(ctx, &pb.PurgeReq{
		Tables: []string{"runs", "healing_audit"}, OlderThanEpoch: cutoff,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range rep.Counts {
		if c.Rows != 1 {
			t.Fatalf("%s: purged %d rows, want exactly 1 (the old one)", c.Table, c.Rows)
		}
	}
	// Assert WHICH row survived, not merely how many — a filter that deleted the wrong one would
	// still report 1.
	var runID string
	if err := s.db.QueryRow(`SELECT run_id FROM runs`).Scan(&runID); err != nil || runID != "new-run" {
		t.Fatalf("surviving run = %q (err %v), want new-run", runID, err)
	}
	var auditID string
	if err := s.db.QueryRow(`SELECT run_id FROM healing_audit`).Scan(&auditID); err != nil || auditID != "new-audit" {
		t.Fatalf("surviving audit = %q (err %v), want new-audit", auditID, err)
	}
}

// TestPurgeNamesTheCapabilityItTakesAway — ARCHITECTURE principle 7: nothing happens silently. The
// sibling failure is live: sweepTraces deletes traces with no event, no log line and no UI mark.
//
// Kills: dropping capabilities_lost from the report.
// Kills: reporting a loss for a table that was already empty (which would train the operator to
// ignore the line).
func TestPurgeNamesTheCapabilityItTakesAway(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.AppendAudit(ctx, &pb.AuditRow{RunId: "r", Original: canary}); err != nil {
		t.Fatal(err)
	}
	rep, err := s.PurgeStore(ctx, &pb.PurgeReq{Tables: []string{"healing_audit", "runs"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.CapabilitiesLost) != 1 {
		t.Fatalf("capabilities_lost = %v, want exactly one (runs was empty, so nothing was lost there)",
			rep.CapabilitiesLost)
	}
	// The specific capability, not just "some string": purging the audit is what blinds calibrate.
	if !strings.Contains(rep.CapabilitiesLost[0], "calibrate") {
		t.Fatalf("capabilities_lost[0] = %q, want it to name `agentctl calibrate`", rep.CapabilitiesLost[0])
	}
}

// TestPurgeReportCarriesCountsAndNeverContent — the rule redact-trace already follows: a tool that
// printed what it found would be a second copy of the leak.
//
// Kills: echoing a deleted value "for debugging".
// The count assertion is not decoration — without it the test passes vacuously by purging nothing.
func TestPurgeReportCarriesCountsAndNeverContent(t *testing.T) {
	s := newDomServer(t)
	ctx := context.Background()
	if _, err := s.AppendAudit(ctx, &pb.AuditRow{
		RunId: "r", Original: canary, PagePath: "https://shop.example.com/" + canary,
	}); err != nil {
		t.Fatal(err)
	}
	rep, err := s.PurgeStore(ctx, &pb.PurgeReq{Tables: []string{"healing_audit"}})
	if err != nil {
		t.Fatal(err)
	}
	if rep.Counts[0].Rows != 1 {
		t.Fatalf("purged %d rows — the content assertion below would be vacuous", rep.Counts[0].Rows)
	}
	if got := rep.String(); strings.Contains(got, canary) {
		t.Fatalf("the report echoes purged content: %q", got)
	}
}
