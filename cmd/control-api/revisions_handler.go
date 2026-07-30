package main

// PROD-VERSIONING — the HTTP surface over the revision store (ADR-106 follow-on).
//
// The store has been complete since ADR-106: append-only history, step-level diff, and a rollback
// that RE-APPENDS rather than deletes, so "put it back" is itself a recorded event. What it never had
// was a way to read it back — no subcommand, no route, no screen. A revision written and unreachable
// is not history; it is a file.
//
// Like the import channel, this spawns the SAME `agentctl revisions` a person runs in a terminal
// rather than reimplementing the store in Go. One implementation, one behaviour, no drift — and the
// store stays owned by the brain, which is where its correctness is tested.

import (
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"strings"
)

// revIDPattern bounds a test id to what can safely become a path segment and a process argument. The
// brain validates a REVISION id as a 64-hex plan hash already; a TEST id is human-chosen, so it is
// bounded here, at the edge, before it is handed to anything.
func validTestID(s string) bool {
	if s == "" || len(s) > 128 {
		return false
	}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.':
		default:
			return false
		}
	}
	// "." and ".." are traversal even though every rune passed above.
	return s != "." && s != ".."
}

func (s *server) revisions(w http.ResponseWriter, r *http.Request, op string) {
	id := r.PathValue("id")
	if !validTestID(id) {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "test id must be letters, digits, - _ . (got " + id + ")"})
		return
	}
	args := []string{"revisions", op, "--test", id}
	q := r.URL.Query()
	if a := q.Get("a"); a != "" {
		args = append(args, "--rev", a)
	}
	if b := q.Get("b"); b != "" {
		args = append(args, "--rev-b", b)
	}
	if op == "rollback" {
		// The target of a rollback is never a default. Guessing "the previous one" would make an
		// irreversible-looking action depend on a reading of intent the caller never stated.
		to := q.Get("to")
		if to == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "rollback needs ?to=<revision> — the target is never assumed"})
			return
		}
		args = append(args, "--rev", to)
	}

	cmd := exec.Command(s.agentctl, args...)
	cmd.Dir = s.repo
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		// The brain distinguishes "no such revision" (3) from "bad request" (2); relay the shape
		// rather than flattening every failure into a 500 the caller cannot act on.
		code := http.StatusInternalServerError
		detail := "revisions failed"
		if ee, ok := err.(*exec.ExitError); ok {
			switch ee.ExitCode() {
			case 2:
				code, detail = http.StatusBadRequest, "bad request"
			case 3:
				code, detail = http.StatusNotFound, "no such test or revision"
			}
			if line := lastLine(string(ee.Stderr)); line != "" {
				detail = detail + ": " + line
			}
		}
		writeJSON(w, code, map[string]string{"error": detail})
		return
	}
	// agentctl prints the JSON payload; hand it through untouched so the CLI and the UI cannot drift.
	body := strings.TrimSpace(string(out))
	if !json.Valid([]byte(body)) {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "revisions produced no JSON"})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = w.Write([]byte(body))
}

func (s *server) handleRevisionsList(w http.ResponseWriter, r *http.Request) {
	s.revisions(w, r, "list")
}
func (s *server) handleRevisionsDiff(w http.ResponseWriter, r *http.Request) {
	s.revisions(w, r, "diff")
}
func (s *server) handleRevisionsShow(w http.ResponseWriter, r *http.Request) {
	s.revisions(w, r, "show")
}
func (s *server) handleRevisionsRollback(w http.ResponseWriter, r *http.Request) {
	s.revisions(w, r, "rollback")
}
