package main

// HEALTH-005 PR-B — READING the service journal.
//
// PR-A taught the product to write one (`internal/svclog`, ADR-116) and stopped there, which left the
// journal in the one state an audit journal must never be in: written and unreadable. There was no
// route, no command and no view — only `cat` against a file that, in the deployment we actually
// recommend, lives inside a container and is 0640 to a uid the operator does not have. A record
// nobody can reach is not evidence; it is disk.
//
// The reader is NOT a second reader. `scanLog` (logs_api.go) is the same scan the run log uses,
// because `svclog.Record` is deliberately one struct for both streams — two scans over one wire
// format is exactly the drift this package was created to stop. What differs is stated as data:
//
//	tail   — a run's log is finite and read from its start; this one is unbounded, and the first 500
//	         lines of a month-old file answer no question anybody has.
//	keep   — the ownership predicate below, which the run log does not need (the guard already
//	         resolved ONE row's owner before the handler ran; a journal names thousands).
//	paths  — the rotated generation as well, so a read seconds after a rotation is not a read of
//	         almost nothing.

import (
	"net/http"
	"path/filepath"
	"strings"

	"github.com/AlexGromer/sentinel/internal/svclog"
)

// journalScope answers WHICH records this caller may read, as a predicate rather than a filtered copy.
//
// ADR-109's rule for owned ROWS is inverted here for one case, and deliberately. An unowned row is
// visible to everybody — that is what keeps identity opt-in, and nothing about it changes. But an
// unowned service EVENT is a different object: the service starting, the global configuration
// changing, the store failing, a machine-token call. Those are about the DEPLOYMENT, not about
// anyone's work, and a journal that handed them to every account would publish the topology and the
// operations schedule to whoever holds the weakest credential in it.
//
// Returns (nil, false) when no scoping applies — the machine token, an admin, or a deployment with no
// accounts, which is the same "no subject means no scoping" rule as everywhere else in ADR-109.
func (s *server) journalScope(r *http.Request) (func(*logRecord) bool, bool) {
	c, _ := s.callerOf(r)
	if c.machine || c.admin || c.owner() == "" {
		return nil, false
	}
	owner := c.owner()
	return func(rec *logRecord) bool { return rec.Owner == owner }, true
}

// scopeReason is what makes a partial view legible instead of merely partial. An account that reads
// its own journal and sees no `service.started` must be able to tell "it was not recorded" from "it
// is not mine to see" — the same distinction `recorded: false` draws for an absent file, and the one
// the empty-200 list endpoints got wrong earlier in this milestone.
const scopeReason = "this account sees only events it owns; deployment-wide events (service start/stop, " +
	"global configuration, store failures, machine-token calls) are visible to an admin account or the machine token"

// handleServiceLog serves the service journal.
//
// Filters (all optional, AND-combined): lvl (minimum level), code, svc (which binary wrote it),
// actor (who caused it), q (substring of the message), limit.
//
// Deliberately NOT accepted: src, cat, mod and step, which are run-log axes and are empty on every
// service record — a filter that can only ever match nothing is a worse answer than no filter, since
// its empty result reads as "there is none of that" rather than "that word means nothing here". Nor
// `after`: seq is per-writer and restarts with the process, so paging by it across four services and
// a restart would silently drop records.
func (s *server) handleServiceLog(w http.ResponseWriter, r *http.Request) {
	dir := filepath.Join(s.repo, "state", "logs")
	current := filepath.Join(dir, svclog.FileName)
	// The rotated generation FIRST: it is the older half, so reading it first keeps the file order
	// chronological. It usually does not exist, and scanLog skips a path it cannot open.
	paths := []string{filepath.Join(dir, svclog.Rotated), current}

	q := r.URL.Query()
	keep, scoped := s.journalScope(r)
	page := scanLog(paths, logQuery{
		minRank: logRank(q.Get("lvl")),
		code:    q.Get("code"),
		svc:     q.Get("svc"),
		actor:   q.Get("actor"),
		needle:  strings.ToLower(q.Get("q")),
		limit:   logLimitOf(q.Get("limit")),
		tail:    true,
		keep:    keep,
	})

	out := map[string]any{
		"recorded": true, "records": page.records, "path": current,
		"scanned": page.scanned, "matched": page.matched, "truncated": page.truncated,
		"scoped": scoped,
	}
	if scoped {
		out["scope_reason"] = scopeReason
	}
	// `scanned == 0` covers both "no file yet" and "an empty file", and they are the same answer to
	// the reader: nothing has been recorded here. It is reported as its own shape rather than as an
	// empty page, because "no journal" and "no matches" send an operator to different places.
	if page.scanned == 0 {
		out["recorded"] = false
		out["reason"] = "no service journal has been written yet (" + current + ")"
	}
	writeJSON(w, http.StatusOK, out)
}
