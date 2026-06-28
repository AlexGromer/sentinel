package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestMkArtifactDirPerms (#26): the default run dir and the runs/ root are owner-only (0700) so
// other local users can't read trace.zip (AUT DOM/screenshots, possible PII).
func TestMkArtifactDirPerms(t *testing.T) {
	repo := t.TempDir()
	dir := mkArtifactDir(repo, "run1", "")

	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o700 {
		t.Fatalf("run dir perm = %o, want 0700", perm)
	}
	rinfo, err := os.Stat(filepath.Join(repo, "runs"))
	if err != nil {
		t.Fatal(err)
	}
	if perm := rinfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("runs/ perm = %o, want 0700", perm)
	}
}

// writeTrace creates runsRoot/<name>/trace.zip with the given mtime and returns its path.
func writeTrace(t *testing.T, runsRoot, name string, mod time.Time) string {
	t.Helper()
	d := filepath.Join(runsRoot, name)
	if err := os.MkdirAll(d, 0o700); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(d, "trace.zip")
	if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(p, mod, mod); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestSweepTracesKeepsNewest (#26): with KEEP=2, only the two newest trace.zip survive; the older
// runs keep their directory + non-trace artifacts (only trace.zip is pruned).
func TestSweepTracesKeepsNewest(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "2")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	now := time.Now()
	for i, name := range []string{"runA", "runB", "runC", "runD"} {
		writeTrace(t, runsRoot, name, now.Add(time.Duration(i)*time.Hour)) // A oldest .. D newest
	}

	sweepTraces(runsRoot)

	for name, want := range map[string]bool{"runA": false, "runB": false, "runC": true, "runD": true} {
		_, err := os.Stat(filepath.Join(runsRoot, name, "trace.zip"))
		if got := err == nil; got != want {
			t.Errorf("%s/trace.zip present=%v, want %v", name, got, want)
		}
		if _, err := os.Stat(filepath.Join(runsRoot, name)); err != nil {
			t.Errorf("%s dir must remain (only trace.zip is pruned): %v", name, err)
		}
	}
}

// TestSweepTracesKeepZero (#34 pt3 / GAP doc): KEEP=0 is NOT the same as KEEP=-1. -1 disables
// count-pruning; 0 keeps zero newest, so the sweep deletes EVERY trace.zip each run (dirs + other
// artifacts remain). Guards the documented OUTPUTS.md semantics for the boundary value.
func TestSweepTracesKeepZero(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "0")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	now := time.Now()
	for i, name := range []string{"runA", "runB", "runC"} {
		writeTrace(t, runsRoot, name, now.Add(time.Duration(i)*time.Hour))
	}

	sweepTraces(runsRoot)

	for _, name := range []string{"runA", "runB", "runC"} {
		if _, err := os.Stat(filepath.Join(runsRoot, name, "trace.zip")); err == nil {
			t.Errorf("%s/trace.zip must be pruned with KEEP=0 (keep zero newest)", name)
		}
		if _, err := os.Stat(filepath.Join(runsRoot, name)); err != nil {
			t.Errorf("%s dir must remain (only trace.zip is pruned): %v", name, err)
		}
	}
}

// TestSweepTracesTTL (#26): with count-pruning disabled (KEEP=-1) a trace older than the TTL is
// removed while a fresh one is kept.
func TestSweepTracesTTL(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "-1")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "24")
	runsRoot := filepath.Join(t.TempDir(), "runs")
	now := time.Now()
	old := writeTrace(t, runsRoot, "old", now.Add(-48*time.Hour))
	fresh := writeTrace(t, runsRoot, "fresh", now.Add(-1*time.Hour))

	sweepTraces(runsRoot)

	if _, err := os.Stat(old); err == nil {
		t.Error("old trace should be pruned by TTL")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Error("fresh trace should be kept")
	}
}
