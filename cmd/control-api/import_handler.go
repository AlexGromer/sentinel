package main

// POST /v1/import (PROD-IMPORT channel 2, ADR-105) — the UI-upload / HTTP channel over the same
// transpiler the filesystem channel drives. A team's first contact is usually "here are my test
// files"; this accepts them and returns the rewrite report (the honest diagnosis), so the hub can
// show it before anyone commits to adopting the tool.
//
// Synchronous by design: import is a fast, deterministic, browser-less transform (unlike a run), so
// there is nothing to stream or track. It spawns the SAME `agentctl import` the CLI uses rather than
// reimplementing the parser in Go — one transpiler, one behaviour, no drift.

import (
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type importFile struct {
	Name    string `json:"name"`
	Content string `json:"content"`
}

type importRequest struct {
	Files []importFile `json:"files"`
}

// import limits — an upload is a convenience, not a bulk pipeline (the CLI/git channels handle a whole
// suite). Bounded so a stray or hostile upload cannot exhaust disk or time.
const (
	maxImportFiles   = 200
	maxImportBytes   = 8 << 20 // 8 MiB total
	importReportName = "import-report.json"
)

func (s *server) handleImport(w http.ResponseWriter, r *http.Request) {
	if !s.authed(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "missing/invalid bearer token (set CONTROL_API_TOKEN)"})
		return
	}
	var req importRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxImportBytes+(1<<20))).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad JSON: " + err.Error()})
		return
	}
	if len(req.Files) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "no files: send {\"files\":[{\"name\":\"x.spec.ts\",\"content\":\"...\"}]}"})
		return
	}
	if len(req.Files) > maxImportFiles {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "too many files (max 200) — use `agentctl import --from` for a whole suite"})
		return
	}

	specDir, err := os.MkdirTemp("", "sentinel-import-spec-")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "temp dir: " + err.Error()})
		return
	}
	defer os.RemoveAll(specDir) // Go os.RemoveAll, cleaned up after the synchronous run
	outDir, err := os.MkdirTemp("", "sentinel-import-out-")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "temp dir: " + err.Error()})
		return
	}
	defer os.RemoveAll(outDir)

	total := 0
	wrote := 0
	for _, f := range req.Files {
		// Name is reduced to a base name and must be a .spec.ts — a traversal-shaped or foreign name is
		// refused, never written outside specDir. (Cypress/Selenium dialects arrive as their own
		// extensions when those parsers land; today only .spec.ts is transpiled.)
		base, ok := validImportName(f.Name)
		if !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "each file name must be a plain *.spec.ts (got " + f.Name + ")"})
			return
		}
		total += len(f.Content)
		if total > maxImportBytes {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "upload exceeds 8 MiB — use `agentctl import --from` for a whole suite"})
			return
		}
		if err := os.WriteFile(filepath.Join(specDir, base), []byte(f.Content), 0o600); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "write: " + err.Error()})
			return
		}
		wrote++
	}

	// Spawn the same agentctl import the CLI uses; --artifact-dir points the report at our temp dir.
	cmd := exec.Command(s.agentctl, "import", "--from", specDir, "--artifact-dir", outDir)
	cmd.Dir = s.repo
	cmd.Env = os.Environ()
	if out, err := cmd.CombinedOutput(); err != nil {
		// counts, never the uploaded content, in the error surfaced to the client.
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{
			"error": "import failed", "detail": lastLine(string(out))})
		return
	}
	report, err := os.ReadFile(filepath.Join(outDir, importReportName))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "no report produced: " + err.Error()})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = w.Write(report) // the import-report.json IS the response — the diagnosis the caller asked for
}

// validImportName reduces an uploaded file name to a safe base name, or refuses it. It must be a
// plain *.spec.ts with no path separators and no traversal — a name is the one piece of the upload
// that becomes a filesystem path, so a foreign or traversal-shaped name is rejected before anything
// is written. Returns (base, true) when safe.
func validImportName(name string) (string, bool) {
	base := filepath.Base(strings.TrimSpace(name))
	if base == "" || base == "." || base == ".." {
		return "", false
	}
	if strings.ContainsAny(name, `/\`) || strings.Contains(name, "..") {
		return "", false // reject the RAW name if it tried to traverse, even though Base() would tame it
	}
	if !strings.HasSuffix(base, ".spec.ts") {
		return "", false
	}
	return base, true
}

// lastLine returns the final non-empty line of s (the agentctl error summary), bounded, so a failure
// detail never becomes a channel for echoing the whole uploaded suite back.
func lastLine(s string) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	last := lines[len(lines)-1]
	if len(last) > 300 {
		last = last[:300]
	}
	return last
}
