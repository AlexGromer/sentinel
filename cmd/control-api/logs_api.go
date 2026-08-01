package main

// Log-reading surface for the UI (M9-LIVE):
//
//	GET /v1/runs/{id}/logs   the structured diagnostics of one run, paged and filterable
//	GET /v1/events-catalog   the catalogue itself, so the browser can render Russian
//
// Reading from run.jsonl rather than from the in-memory ring buffer is deliberate: the ring holds
// only the last 1000 lines and dies with the process, so a run from a previous control-API lifetime
// would otherwise have no logs at all — one of the things that made the milestone's failures hard to
// discuss after the fact.

import (
	"bufio"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
)

// A page is bounded so one request cannot pull a multi-megabyte run into a browser tab.
const (
	logsDefaultLimit = 500
	logsMaxLimit     = 5000
)

// handleRunLogs serves a run's structured diagnostics.
//
// Filters (all optional, AND-combined): lvl (minimum level), src (tool/application/testing), cat, mod,
// code, step, q (substring of the message), after (seq exclusive), limit. Server-side filtering exists so a long run stays usable on
// a slow link; the UI still filters client-side for instant feedback within a loaded page.
func (s *server) handleRunLogs(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validRunID(id) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id must be a bare run id"})
		return
	}

	// The artifact dir is derived the same way spawnRun builds it, never taken from the request, so
	// this cannot be walked outside runs/.
	dir := filepath.Join(s.repo, "runs", "control-"+id, "logs")
	path := filepath.Join(dir, "run.jsonl")
	f, err := os.Open(path)
	if err != nil {
		// A run that predates this feature, or one still starting, has no file yet. That is not an
		// error the operator can act on, so it answers 200 with an explicit marker instead of 404 —
		// the UI needs to distinguish "no logs recorded" from "no logs matched", which is exactly the
		// distinction the empty-200 list endpoints got wrong earlier in this milestone.
		writeJSON(w, http.StatusOK, map[string]any{
			"run_id": id, "records": []any{}, "recorded": false,
			"reason": "this run has no log file (it predates structured logging, or has not started writing)",
		})
		return
	}
	defer f.Close()

	q := r.URL.Query()
	minRank := logRank(q.Get("lvl"))
	wantCat, wantMod, wantCode := q.Get("cat"), q.Get("mod"), q.Get("code")
	// ADR-067: `src` is the coarse axis a tester reaches for first (is it my app or the tool?), `step`
	// narrows to one step of the run — together they answer "what went wrong, and where".
	wantSrc, wantStep := q.Get("src"), q.Get("step")
	needle := strings.ToLower(q.Get("q"))
	after, _ := strconv.Atoi(q.Get("after"))
	limit := logsDefaultLimit
	if n, err := strconv.Atoi(q.Get("limit")); err == nil && n > 0 {
		limit = min(n, logsMaxLimit)
	}

	records := make([]json.RawMessage, 0, 64)
	var prev logRecord // the last record appended, for collapsing an identical run of them
	var scanned, matched int
	var degradations []string
	seenDeg := map[string]bool{}

	sc := bufio.NewScanner(f)
	// A single record can exceed bufio's default 64 KiB — an unparseable-reply diagnostic carries a
	// 300-char model excerpt, and a collapsed stack trace carries every frame.
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		var rec logRecord
		if err := json.Unmarshal(line, &rec); err != nil {
			continue // a torn last line during a live run is not a reason to fail the request
		}
		scanned++
		// Degradations are collected across the WHOLE file, never only the returned page: they are
		// what the verdict badge reads, and a paged-out degradation would make a run look clean.
		if rec.Degrades && !seenDeg[rec.Code] {
			seenDeg[rec.Code] = true
			degradations = append(degradations, rec.Code)
		}
		if rec.Seq <= after ||
			logRank(rec.Lvl) < minRank ||
			(wantCat != "" && rec.Cat != wantCat) ||
			(wantMod != "" && rec.Mod != wantMod) ||
			(wantCode != "" && rec.Code != wantCode) ||
			(wantSrc != "" && rec.Src != wantSrc) ||
			(wantStep != "" && strconv.Itoa(rec.Step) != wantStep) ||
			(needle != "" && !strings.Contains(strings.ToLower(rec.Msg), needle)) {
			continue
		}
		matched++
		if len(records) >= limit {
			continue
		}
		// Collapse consecutive identical records into one carrying a count. This is a PRESENTATION
		// concern, which is why it lives here and not in the sink: holding a record back on the write
		// side to count its repeats kept a stuck run out of its own log file for as long as the loop
		// lasted, and real repeats arrive seconds apart, so no write-side deadline could both stay
		// live and still collapse. On the read side both properties hold at once.
		if n := len(records); n > 0 && sameRecord(&prev, &rec) {
			prev.N++
			b, err := json.Marshal(&prev)
			if err == nil {
				records[n-1] = json.RawMessage(b)
			}
			continue
		}
		prev = rec
		prev.N = 1
		records = append(records, json.RawMessage(append([]byte(nil), line...)))
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"run_id": id, "recorded": true, "records": records,
		"scanned": scanned, "matched": matched, "truncated": matched > len(records),
		"degradations": degradations,
	})
}

// sameRecord reports whether two records are the same event repeated. Seq/TS are excluded by
// definition, and Parent is compared so a frame never merges into the error it hangs off.
func sameRecord(a, b *logRecord) bool {
	return a.Lvl == b.Lvl && a.Cat == b.Cat && a.Code == b.Code && a.Msg == b.Msg &&
		a.Mod == b.Mod && a.Parent == b.Parent
}

// logRank maps a level name to its ordering. An empty or unknown name yields 0, so an absent `lvl`
// filter admits everything rather than silently excluding records.
func logRank(lvl string) int {
	switch strings.ToLower(strings.TrimSpace(lvl)) {
	case "debug":
		return 10
	case "info":
		return 20
	case "warn":
		return 30
	case "error":
		return 40
	}
	return 0
}

// handleEventsCatalog serves the catalogue verbatim so the browser can render the reader's language
// without the server doing any i18n.
//
// Unauthenticated on purpose, like /healthz: it is the shipped bilingual message list, carries no
// run data and no secrets, and the Logs view needs it before a token is in play (the wizard renders
// before the bootstrap exchange). Cacheable — it only changes when the binary does.
func (s *server) handleEventsCatalog(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(eventcatalog.Raw())
}
