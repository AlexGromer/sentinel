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

// TestMkArtifactDirControlAPIPath (#34 pt3): control-api drives agentctl with --artifact-dir =
// repo/runs/control-<id>, a subdir of runs/. That override path must STILL chmod the runs/ root and
// run sweepTraces — otherwise trace.zip (AUT DOM/screenshots) accumulates unbounded in a control-api
// deployment. Before the fix, override != "" skipped both.
func TestMkArtifactDirControlAPIPath(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "1")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	repo := t.TempDir()
	runsRoot := filepath.Join(repo, "runs")
	now := time.Now()
	writeTrace(t, runsRoot, "control-old1", now.Add(-2*time.Hour)) // oldest → should be pruned
	writeTrace(t, runsRoot, "control-old2", now.Add(-1*time.Hour)) // newest existing → kept (KEEP=1)

	override := filepath.Join(runsRoot, "control-new") // the just-started run, no trace.zip yet
	if dir := mkArtifactDir(repo, "ignored", override); dir != override {
		t.Fatalf("dir = %q, want override %q", dir, override)
	}

	if _, err := os.Stat(filepath.Join(runsRoot, "control-old1", "trace.zip")); err == nil {
		t.Error("control-api --artifact-dir under runs/ must trigger sweepTraces; oldest trace not pruned")
	}
	if _, err := os.Stat(filepath.Join(runsRoot, "control-old2", "trace.zip")); err != nil {
		t.Error("newest existing trace should be kept with KEEP=1")
	}
	rinfo, err := os.Stat(runsRoot)
	if err != nil {
		t.Fatal(err)
	}
	if perm := rinfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("runs/ perm = %o, want 0700 on the control-api override path", perm)
	}
}

// TestMkArtifactDirExternalOverrideUntouched (#34 pt3): a user-supplied --artifact-dir OUTSIDE
// repo/runs is a tree we don't own — mkArtifactDir must not sweep repo/runs for it, even with KEEP=0
// (which would otherwise prune every trace). Guards against the fix over-reaching.
func TestMkArtifactDirExternalOverrideUntouched(t *testing.T) {
	t.Setenv("SENTINEL_TRACE_KEEP", "0")
	t.Setenv("SENTINEL_TRACE_TTL_HOURS", "0")
	repo := t.TempDir()
	keep := writeTrace(t, filepath.Join(repo, "runs"), "runA", time.Now())

	external := filepath.Join(t.TempDir(), "external-out")
	mkArtifactDir(repo, "ignored", external)

	if _, err := os.Stat(keep); err != nil {
		t.Error("external --artifact-dir must NOT sweep repo/runs (not the managed tree for this run)")
	}
}

// TestIsUnder covers the path-containment gate: root itself and nested paths match; a sibling sharing
// the name prefix (runs-evil next to runs) does not.
func TestIsUnder(t *testing.T) {
	root := filepath.Join(t.TempDir(), "runs")
	cases := []struct {
		path string
		want bool
	}{
		{root, true},
		{filepath.Join(root, "control-x"), true},
		{filepath.Join(root, "a", "b"), true},
		{root + "-evil", false}, // sibling sharing the prefix, not nested
		{filepath.Join(filepath.Dir(root), "other"), false},
	}
	for _, c := range cases {
		if got := isUnder(c.path, root); got != c.want {
			t.Errorf("isUnder(%q, %q) = %v, want %v", c.path, root, got, c.want)
		}
	}
}

// GAP-SEC-005 / ADR-081. Logs retention mirrors traces, with one deliberate difference: both knobs
// default to OFF. A trace is a bulky by-product; the logs ARE the diagnosis, and an upgrade that
// silently deleted a user's evidence would be a worse failure than the gap it closed.
func TestSweepLogsIsOffByDefault(t *testing.T) {
	root := t.TempDir()
	for _, id := range []string{"a", "b", "c"} {
		if err := os.MkdirAll(filepath.Join(root, id, "logs"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, id, "logs", "run.jsonl"), []byte("{}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("SENTINEL_LOG_KEEP", "")
	t.Setenv("SENTINEL_LOG_TTL_HOURS", "")
	sweepLogs(root)
	for _, id := range []string{"a", "b", "c"} {
		if _, err := os.Stat(filepath.Join(root, id, "logs", "run.jsonl")); err != nil {
			t.Fatalf("logs deleted with both knobs unset: %s (%v)", id, err)
		}
	}
}

func TestSweepLogsKeepsNewestAndSpearsTheAuditTrail(t *testing.T) {
	root := t.TempDir()
	ids := []string{"oldest", "middle", "newest"}
	for i, id := range ids {
		lg := filepath.Join(root, id, "logs")
		if err := os.MkdirAll(lg, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(lg, "run.jsonl"), []byte("{}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		// Artefacts that must SURVIVE the sweep: a swept run stays auditable and replayable.
		for _, keepMe := range []string{"plan.json", "heal-report.json", "executed-plan.json"} {
			if err := os.WriteFile(filepath.Join(root, id, keepMe), []byte("{}"), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		mod := time.Now().Add(time.Duration(i-len(ids)) * time.Hour)
		if err := os.Chtimes(lg, mod, mod); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("SENTINEL_LOG_KEEP", "1")
	t.Setenv("SENTINEL_LOG_TTL_HOURS", "0")
	sweepLogs(root)

	if _, err := os.Stat(filepath.Join(root, "newest", "logs", "run.jsonl")); err != nil {
		t.Fatalf("the newest logs were swept: %v", err)
	}
	for _, gone := range []string{"oldest", "middle"} {
		if _, err := os.Stat(filepath.Join(root, gone, "logs")); !os.IsNotExist(err) {
			t.Fatalf("%s logs survived a keep=1 sweep (err=%v)", gone, err)
		}
	}
	// The control that makes the sweep safe to enable: only logs/ goes.
	for _, id := range ids {
		for _, keepMe := range []string{"plan.json", "heal-report.json", "executed-plan.json"} {
			if _, err := os.Stat(filepath.Join(root, id, keepMe)); err != nil {
				t.Fatalf("the sweep removed %s from %s — a swept run must stay auditable: %v", keepMe, id, err)
			}
		}
	}
}
