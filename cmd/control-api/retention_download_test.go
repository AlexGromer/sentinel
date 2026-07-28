package main

// SEC-RETENTION-DOWNLOAD. The hub reads plan.json / scenario.json / heal-report.json on every run
// open just to draw the run, so a retention rule of "delete once served" would erase a run the moment
// a human looked at it. The fix is a distinction — `?download=1`, set by the hub only on a real
// download — recorded as a downloaded.json marker and NOTHING else (deletion is a later explicit
// policy, never a side effect of serving).
//
// The load-bearing case is the NEGATIVE CONTROL: a view must leave no marker. Without it, every
// design that consumes "downloaded" runs would consume viewed ones too — the exact bug this closes.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func getArtifact(t *testing.T, s *server, id, query string) int {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact"+query, nil)
	r.Header.Set("Authorization", "Bearer secret-tok")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	return rec.Code
}

func newArtifactRun(t *testing.T) (*server, string, string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "plan.json"), []byte(`{"ok":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: "aaaaaaaaaaaaaaaa", State: "done", ArtifactDir: dir}
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{rec.ID: rec}}
	return s, rec.ID, dir
}

func markerPath(dir string) string { return filepath.Join(dir, "downloaded.json") }

// TestViewingArtifactWritesNoMarker — THE negative control.
// Kills: marking a run downloaded on a plain (view) fetch, which would make opening a run in the hub
// indistinguishable from taking a copy of it.
func TestViewingArtifactWritesNoMarker(t *testing.T) {
	s, id, dir := newArtifactRun(t)

	// A view — no download flag, and the two false-signal shapes a naive check might accept.
	for _, q := range []string{"?name=plan.json", "?name=plan.json&download=0", "?name=plan.json&download=true"} {
		if code := getArtifact(t, s, id, q); code != http.StatusOK {
			t.Fatalf("view %q: code=%d, want 200", q, code)
		}
		if _, err := os.Stat(markerPath(dir)); err == nil {
			t.Fatalf("view %q wrote a downloaded.json marker — a view must never read as a download", q)
		}
	}
}

// TestDownloadWritesTheMarker.
// Kills: dropping the marker write, which would leave a real download unrecorded and a
// download-consuming policy with nothing to act on.
func TestDownloadWritesTheMarker(t *testing.T) {
	s, id, dir := newArtifactRun(t)

	if code := getArtifact(t, s, id, "?name=plan.json&download=1"); code != http.StatusOK {
		t.Fatalf("download: code=%d, want 200", code)
	}
	b, err := os.ReadFile(markerPath(dir))
	if err != nil {
		t.Fatalf("a genuine download left no downloaded.json marker: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("marker is not valid JSON: %v", err)
	}
	if m["downloaded"] != "plan.json" {
		t.Fatalf("marker does not name what was taken: %v", m)
	}
	if m["at"] == nil || m["at"] == "" {
		t.Fatalf("marker has no timestamp: %v", m)
	}
}

// TestDownloadDeletesNothing — the whole point of Alex's decision: a download marks, never erases.
// Kills: any deletion folded into the download path (delete-on-serve, the bug the split warned of).
func TestDownloadDeletesNothing(t *testing.T) {
	s, id, dir := newArtifactRun(t)

	if code := getArtifact(t, s, id, "?name=plan.json&download=1"); code != http.StatusOK {
		t.Fatalf("download: code=%d, want 200", code)
	}
	if _, err := os.Stat(filepath.Join(dir, "plan.json")); err != nil {
		t.Fatalf("the downloaded artifact was deleted — download must mark, never erase: %v", err)
	}
	// and the served bytes are still the real artifact on a second fetch
	if code := getArtifact(t, s, id, "?name=plan.json"); code != http.StatusOK {
		t.Fatalf("the run became unreadable after a download: code=%d", code)
	}
}
