package main

// The two `go/path-injection` findings CodeQL reports as HIGH, settled by EXPERIMENT.
//
//	cmd/control-api/main.go        os.Open(filepath.Join(rec.ArtifactDir, name))
//	cmd/control-api/import_handler.go  os.WriteFile(filepath.Join(specDir, base), …)
//
// WHY THIS FILE EXISTS RATHER THAN A DISMISSAL WITH A COMMENT. Reading the guards says they hold.
// So does the comment above each of them. Both are the same claim in two voices, and a scanner
// disagreeing with them is not resolved by a third voice agreeing — it is resolved by making the
// attack and looking at the filesystem. That is what this does: it drives the REAL handlers, over
// the REAL mux, with the payloads an attacker would send, and then asks the disk what happened.
//
// WHAT WAS ACTUALLY UNCOVERED BEFORE THIS. `validImportName` had a unit test with traversal names —
// but on the FUNCTION, never through the route, so nothing proved the handler calls it for every
// file it writes. The artifact route had no traversal test at all: main_test.go only checks that the
// whitelist is complete, which is a statement about the map, not about what the handler will open.
// Those are two instances of one habit this project keeps paying for — covering the function and
// leaving the call site to inference.
//
// ⚠ THE DECODING MATTERS, which is why this goes through the mux. net/http percent-decodes a query
// value before a handler sees it, so `%2e%2e%2f` arrives as `../`. A guard tested by calling it with
// a literal string never meets that; a guard tested through the route does.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// traversalNames are the shapes a path-injection attempt actually takes. Kept as one list because
// both handlers must refuse all of them, and a per-handler list would drift.
var traversalNames = []string{
	"../../etc/passwd",
	"../secret.txt",
	"../../secret.txt",
	"..%2f..%2fsecret.txt",         // percent-encoded separators
	"%2e%2e%2f%2e%2e%2fsecret.txt", // percent-encoded dots AND separators
	"....//secret.txt",             // the shape that survives a naive single "../" strip
	"..\\..\\secret.txt",           // Windows separators
	"/etc/passwd",                  // absolute
	"//etc/passwd",                 // absolute, double slash
	"frames/../../secret.txt",      // starts like the one allowed subdirectory
	"frames/frame-0001.png/../../secret.txt",
	"frames/frame-0001.png\x00../../secret.txt", // NUL truncation
	"./../../secret.txt",
	"secret.txt", // plain, but not whitelisted — must still be refused
	"plan.json/../../secret.txt",
	"scenario.json\x00.png",
}

// TestArtifactRouteCannotBeMadeToOpenAFileOutsideTheRun drives GET /v1/runs/{id}/artifact.
//
// Kills: dropping the whitelist, dropping the separator/`..` rejection, widening frameNamePattern,
// or taking the directory from the request instead of the run record.
func TestArtifactRouteCannotBeMadeToOpenAFileOutsideTheRun(t *testing.T) {
	s := newTestServer()

	// The run's own directory, and a secret one level ABOVE it — the thing a traversal would reach.
	artifactDir := filepath.Join(s.repo, "runs", "control-probe", "artifacts")
	if err := os.MkdirAll(artifactDir, 0o750); err != nil {
		t.Fatal(err)
	}
	const secret = "THE-FILE-OUTSIDE-THE-RUN"
	secretPath := filepath.Join(s.repo, "runs", "control-probe", "secret.txt")
	if err := os.WriteFile(secretPath, []byte(secret), 0o600); err != nil {
		t.Fatal(err)
	}
	// A legitimate artifact, so the route is known to WORK — otherwise every refusal below would be
	// satisfied by a handler that refuses everything, including what it should serve.
	if err := os.WriteFile(filepath.Join(artifactDir, "plan.json"), []byte(`{"ok":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	// A file INSIDE the run directory that is NOT whitelisted. This is what makes the whitelist
	// load-bearing rather than decorative: measured by mutation, every traversal payload is already
	// refused by the separator/`..` checks, so removing the whitelist changed nothing observable and
	// the mutation SURVIVED. The whitelist's real job is this one — a run directory holds more than
	// the artifacts a caller may read (stored session state with live cookies is the case that
	// matters), and nothing about a plain in-directory name trips a traversal check.
	const inDirSecret = "THE-FILE-INSIDE-THE-RUN-THAT-IS-NOT-PUBLIC"
	if err := os.WriteFile(filepath.Join(artifactDir, "storage-state.json"), []byte(inDirSecret), 0o600); err != nil {
		t.Fatal(err)
	}
	s.mu.Lock()
	s.runs["probe"] = &run{ID: "probe", ArtifactDir: artifactDir, State: "done"}
	s.mu.Unlock()

	mux := s.mux()
	get := func(name string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, "/v1/runs/probe/artifact?name="+url.QueryEscape(name), nil)
		req.Header.Set("Authorization", "Bearer "+s.token)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}

	// NON-VACUITY FIRST. Every refusal below is trivially satisfied by a broken route, so the route
	// must be shown to serve a legitimate artifact before its refusals mean anything.
	if rec := get("plan.json"); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Fatalf("the route cannot serve a legitimate artifact (%d) — every check below would be vacuous", rec.Code)
	}

	for _, name := range traversalNames {
		rec := get(name)
		if strings.Contains(rec.Body.String(), secret) {
			t.Errorf("PATH INJECTION IS REAL: %q returned the file outside the run directory", name)
			continue
		}
		if rec.Code == http.StatusOK {
			t.Errorf("%q was served (200) — it names no whitelisted artifact and no frame", name)
		}
	}

	// The other half of the boundary, and the one no traversal payload can reach: a file that IS in
	// the run directory and is NOT on the whitelist must still be refused.
	if rec := get("storage-state.json"); rec.Code == http.StatusOK || strings.Contains(rec.Body.String(), inDirSecret) {
		t.Errorf("a non-whitelisted file inside the run directory was served (%d) — the whitelist is "+
			"what bounds WHICH file, and the traversal checks cannot do its job", rec.Code)
	}
}

// TestImportRouteWritesNothingOutsideItsTempDirectory drives POST /v1/import.
//
// Kills: skipping validImportName for any file in the batch, accepting a raw name that Base() would
// have tamed, or writing before the name is checked.
func TestImportRouteWritesNothingOutsideItsTempDirectory(t *testing.T) {
	s := newTestServer()
	// The handler shells out to agentctl; pointing it at a path that does not exist makes the spawn
	// fail AFTER the files are written, which is precisely the window this test inspects. The check
	// is what landed on disk, not what the route answered.
	s.agentctl = filepath.Join(s.repo, "no-such-agentctl")

	canary := filepath.Join(s.repo, "canary.spec.ts")
	mux := s.mux()

	for _, name := range traversalNames {
		body, _ := json.Marshal(map[string]any{
			"files": []map[string]string{
				// A legitimate file FIRST, so a handler that refuses the batch on the second entry has
				// still been made to write one — the case where an early write escapes a late check.
				{"name": "ok.spec.ts", "content": "// legitimate"},
				{"name": name, "content": "// " + name},
			},
		})
		req := httptest.NewRequest(http.MethodPost, "/v1/import", strings.NewReader(string(body)))
		req.Header.Set("Authorization", "Bearer "+s.token)
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if _, err := os.Stat(canary); err == nil {
			t.Fatalf("PATH INJECTION IS REAL: %q wrote outside the import temp dir (%s)", name, canary)
		}
		// The route must REFUSE the name, not merely fail to escape with it. Measured by mutation:
		// `filepath.Base` alone tames every payload here — a traversal name collapses to its last
		// segment and is written happily INSIDE the temp dir — so a check that only looks for an
		// escape says nothing about whether the name was validated at all, and that mutation
		// SURVIVED. What validImportName actually buys is that a foreign name never becomes a file,
		// and that is observable as a 400.
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%q was not refused (HTTP %d): a name that is not a plain spec file must be "+
				"rejected by name, not tamed into one", name, rec.Code)
		}
	}

	// And nothing may have been written anywhere under the repo root except the temp dirs the handler
	// creates and removes itself. Asserted as "no .spec.ts survives", because the handler's own
	// `defer os.RemoveAll(outDir)` cleans what it legitimately made.
	var strays []string
	_ = filepath.Walk(s.repo, func(p string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() {
			return nil //nolint:nilerr // an unreadable entry is not what this walk is about
		}
		if strings.HasSuffix(p, ".spec.ts") {
			strays = append(strays, p)
		}
		return nil
	})
	if len(strays) > 0 {
		t.Errorf("%d spec file(s) survived under the repo root, so the import channel leaks files: %v",
			len(strays), strays)
	}
}

// TestTheGuardsRefuseTheSameShapesWhenCalledDirectly is the unit half, kept because it names WHICH
// guard refused — the route tests above prove the outcome, this one localises a regression.
func TestTheGuardsRefuseTheSameShapesWhenCalledDirectly(t *testing.T) {
	for _, name := range traversalNames {
		if isFrameName(name) {
			t.Errorf("frameNamePattern accepts %q — it must match only frames/frame-NNNN.png", name)
		}
		if _, ok := validImportName(name); ok {
			t.Errorf("validImportName accepts %q", name)
		}
		if artifactWhitelist[name] {
			t.Errorf("artifactWhitelist contains %q", name)
		}
	}
	// The positive control for the frame pattern: it must still accept what it exists for, or the
	// checks above are satisfied by a pattern that matches nothing at all.
	for _, good := range []string{"frames/frame-0000.png", "frames/frame-9999.png"} {
		if !isFrameName(good) {
			t.Errorf("frameNamePattern rejects %q, which it exists to allow", good)
		}
	}
	for i, good := range []string{"a.spec.ts", "b.spec.js", "c.cy.ts", "d.cy.js"} {
		if _, ok := validImportName(good); !ok {
			t.Errorf("validImportName rejects legitimate name %d (%q)", i, good)
		}
	}
	if len(artifactWhitelist) == 0 {
		t.Error("artifactWhitelist is empty, so the membership checks above assert nothing")
	}
}
