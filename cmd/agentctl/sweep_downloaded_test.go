package main

import (
	"os"
	"path/filepath"
	"testing"
)

// SEC-RETENTION-DOWNLOAD-CONSUMER. The explicit half of until_downloaded: a run a human has
// downloaded (ADR-103 left a downloaded.json marker) may be removed on demand — never automatically,
// never a side effect of serving.

// seedRun makes runs/<name>/ with a plan.json, and optionally a downloaded.json marker.
func seedRun(t *testing.T, runsRoot, name string, downloaded bool) string {
	t.Helper()
	dir := filepath.Join(runsRoot, name)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "plan.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if downloaded {
		if err := os.WriteFile(filepath.Join(dir, "downloaded.json"), []byte(`{"downloaded":"plan.json"}`), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// TestRunsWithDownloadMarkerSelectsOnlyMarked — the decision, apart from the deletion.
// Kills: selecting unmarked runs (which would delete evidence nobody took a copy of).
func TestRunsWithDownloadMarkerSelectsOnlyMarked(t *testing.T) {
	runsRoot := filepath.Join(t.TempDir(), "runs")
	marked := seedRun(t, runsRoot, "taken", true)
	seedRun(t, runsRoot, "untouched", false)

	got := runsWithDownloadMarker(runsRoot)
	if len(got) != 1 || got[0] != marked {
		t.Fatalf("selected %v, want exactly [%s] — an unmarked run must never be a target", got, marked)
	}
}

// TestSweepDownloadedRequiresConfirmation — destructive, so nothing goes without --yes.
// Kills: defaulting to delete without --yes.
func TestSweepDownloadedRequiresConfirmation(t *testing.T) {
	repo := t.TempDir()
	dir := seedRun(t, filepath.Join(repo, "runs"), "taken", true)

	if code := cmdSweepDownloaded(repo, nil); code != 2 {
		t.Fatalf("no --yes: exit=%d, want 2 (refusal)", code)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("a refused sweep deleted the run anyway: %v", err)
	}
}

// TestSweepDownloadedDryRunDeletesNothing.
// Kills: --dry-run that deletes (a preview must not act).
func TestSweepDownloadedDryRunDeletesNothing(t *testing.T) {
	repo := t.TempDir()
	dir := seedRun(t, filepath.Join(repo, "runs"), "taken", true)

	if code := cmdSweepDownloaded(repo, []string{"--dry-run", "--yes"}); code != 0 {
		t.Fatalf("dry-run: exit=%d, want 0", code)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("--dry-run deleted the run: %v", err)
	}
}

// TestSweepDownloadedDeletesMarkedKeepsUnmarked — the whole behaviour, and the negative control.
// Kills: deleting an unmarked run (over-reach) or failing to delete a marked one.
func TestSweepDownloadedDeletesMarkedKeepsUnmarked(t *testing.T) {
	repo := t.TempDir()
	runsRoot := filepath.Join(repo, "runs")
	taken := seedRun(t, runsRoot, "taken", true)
	kept := seedRun(t, runsRoot, "untouched", false)

	if code := cmdSweepDownloaded(repo, []string{"--yes"}); code != 0 {
		t.Fatalf("sweep: exit=%d, want 0", code)
	}
	if _, err := os.Stat(taken); err == nil {
		t.Fatal("a downloaded run survived the explicit sweep")
	}
	// THE NEGATIVE CONTROL: a run nobody downloaded is untouched.
	if _, err := os.Stat(kept); err != nil {
		t.Fatalf("a run nobody downloaded was deleted — the sweep over-reached: %v", err)
	}
	if _, err := os.Stat(filepath.Join(kept, "plan.json")); err != nil {
		t.Fatalf("the untouched run lost its contents: %v", err)
	}
}

// TestSweepDownloadedEmptyIsANoOp — no markers, no error, no deletion.
func TestSweepDownloadedEmptyIsANoOp(t *testing.T) {
	repo := t.TempDir()
	kept := seedRun(t, filepath.Join(repo, "runs"), "untouched", false)

	if code := cmdSweepDownloaded(repo, []string{"--yes"}); code != 0 {
		t.Fatalf("empty sweep: exit=%d, want 0", code)
	}
	if _, err := os.Stat(kept); err != nil {
		t.Fatalf("a sweep with no markers deleted a run: %v", err)
	}
}
