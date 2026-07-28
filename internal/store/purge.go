package store

// Purge (ADR-100): the explicitly-invoked half of the foreign-text policy.
//
// ADR-098 redacts the trace on write and ADR-099 gives a run directory a lifetime. Both act at write
// time, and write-time redaction is powerless over what was written BEFORE it existed. This file is
// what reaches that: an operator-invoked purge of the rows in which foreign text — the text of the
// site under test, and the operator's own typed phrases — has already accumulated.
//
// WHY DELETE ROWS AND NOT BLANK FIELDS. docs/DB_FOREIGN_TEXT.md classifies every column of both
// schemas, and the finding that shapes this code is that nearly all foreign text here is INHERENT:
// `healed_locators.value` is literally `{"role":"button","name":"Pay now"}`, i.e. the page's own text
// IS the locator. Blanking it leaves "any button" and breaks healing, so it protects nothing and
// costs the feature. Deleting the row is the only honest operation, and it is destructive enough
// that the caller must ask for it by name.
//
// WHY THIS IS NEVER AUTOMATIC. It is deliberately absent from agentctl's start-of-run sweep block
// (sweepTraces/sweepLogs/sweepRuns, cmd/agentctl/main.go). A swept trace is reproducible by running
// again; healing history and run results are not. An automatic purge would also make "the tool
// tidied up" indistinguishable from "evidence was erased" after the fact, and nothing recovers that
// distinction once it is lost.

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// timeKind says how a table stores its age. The two schemas genuinely disagree — the heal/trust
// tables (ADR-007 era) keep REAL epoch seconds, the M13 domains (ADR-050) keep RFC3339 TEXT — and a
// single comparison operator cannot serve both. Getting this wrong is silent in the worst way: a
// float compared against "2026-07-28T09:00:00Z" matches either nothing or everything.
type timeKind int

const (
	epochReal timeKind = iota // REAL seconds since the epoch
	rfc3339Text
)

// purgeTable is one purgeable table: where its age lives, and what the operator gives up by
// emptying it. The capability is carried here rather than in the caller so it cannot drift from the
// table list — a new entry without an answer to "what breaks?" does not compile.
type purgeTable struct {
	timeCol    string
	kind       timeKind
	capability string
}

// purgeable enumerates every table that can hold foreign text and therefore can be purged.
//
// Two deliberate omissions, both of which the empty-scope refusal below turns into a clear error
// rather than a silent no-op:
//   - `config` holds live configuration, not accumulated history. Deleting it breaks a running
//     deployment. Its foreign text (run.target / run.goal) is a reason to stop putting those in
//     config, not a reason to let a cleanup tool empty the service's own settings.
//   - `step_failures` carries no foreign text at all (measured: step_key is a hash, last5 is a pair
//     of counters), and ClearQuarantine already owns emptying it.
var purgeable = map[string]purgeTable{
	// heal/trust schema — REAL epoch
	"healed_locators": {"created_at", epochReal,
		"the heal cache resets: healing falls back to re-learning locators from scratch"},
	"healing_audit": {"ts", epochReal,
		"`agentctl calibrate` loses its input — heal precision and the confidence histogram are computed from this table"},
	"golden_snapshots": {"created_at", epochReal,
		"visual/a11y baselines are gone: the next baseline run re-establishes them, and until it does there is nothing to compare against"},
	// M13 domains — RFC3339 TEXT, except metrics which is REAL
	"runs":    {"started_at", rfc3339Text, "run history disappears from the hub"},
	"results": {"created_at", rfc3339Text, "per-step verdicts and regressions disappear from the hub"},
	"metrics": {"ts", epochReal, "metric series and trends lose their history"},
	"scenarios": {"created_at", rfc3339Text,
		"SAVED SCENARIOS ARE DESTROYED — these are authored assets, not derived data, and nothing regenerates them"},
	"tests": {"created_at", rfc3339Text,
		"PROMOTED TESTS ARE DESTROYED — authored assets, not derived data"},
	"chats": {"updated_at", rfc3339Text, "conversation projections are gone"},
}

// PurgeStore deletes rows from the named tables, optionally older than a cutoff, and optionally
// scrubs the freed bytes. It refuses an empty or unknown scope instead of guessing.
func (s *Server) PurgeStore(_ context.Context, r *pb.PurgeReq) (*pb.PurgeReport, error) {
	// An empty scope is REFUSED, never read as "everything". The dangerous default here is not a
	// hypothetical: this call is unrecoverable, and `agentctl purge-store` with a forgotten flag
	// must fail loudly rather than empty the store.
	if len(r.Tables) == 0 {
		return nil, status.Error(codes.InvalidArgument,
			"purge: at least one table is required — an empty scope is refused, not treated as \"all tables\"")
	}
	seen := map[string]bool{}
	names := make([]string, 0, len(r.Tables))
	for _, t := range r.Tables {
		if _, ok := purgeable[t]; !ok {
			return nil, status.Errorf(codes.InvalidArgument,
				"purge: %q is not a purgeable table (purgeable: %s)", t, strings.Join(purgeableNames(), ", "))
		}
		if !seen[t] { // a repeated name must not double-count in the report
			seen[t] = true
			names = append(names, t)
		}
	}
	sort.Strings(names) // deterministic report order regardless of how the caller listed them

	s.mu.Lock()
	defer s.mu.Unlock()

	rep := &pb.PurgeReport{}
	for _, name := range names {
		pt := purgeable[name]
		n, err := s.purgeOne(name, pt, r.OlderThanEpoch)
		if err != nil {
			return nil, fmt.Errorf("purge %s: %w", name, err)
		}
		rep.Counts = append(rep.Counts, &pb.PurgeTableCount{Table: name, Rows: n})
		if n > 0 {
			rep.CapabilitiesLost = append(rep.CapabilitiesLost, name+": "+pt.capability)
		}
	}

	if r.Vacuum {
		// VACUUM alone is NOT enough to make "the bytes are gone" true: with WAL on (server.go) the
		// deleted content can still be sitting in the -wal file, so a scrub that skipped the
		// checkpoint would report success over a file that still greps positive. Checkpoint first,
		// rewrite second, checkpoint again so the rewritten pages are the ones on disk.
		if _, err := s.db.Exec(`PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
			rep.VacuumSkipped = "wal_checkpoint before VACUUM failed: " + err.Error()
		} else if _, err := s.db.Exec(`VACUUM`); err != nil {
			// The rows ARE deleted at this point. Failing the whole call would misreport that, so
			// report the partial truth instead: deleted, not scrubbed. VACUUM needs free space of
			// roughly the database size, so "no space left" is the expected reason here.
			rep.VacuumSkipped = "VACUUM failed (it rewrites the whole file and needs free space " +
				"of about the database size): " + err.Error()
		} else if _, err := s.db.Exec(`PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
			rep.VacuumSkipped = "wal_checkpoint after VACUUM failed: " + err.Error()
		} else {
			rep.Vacuumed = true
		}
	}
	return rep, nil
}

// purgeOne deletes from one table, honouring that table's own way of storing time. Table and column
// names come from the `purgeable` map — never from the request — so the formatted SQL below cannot
// carry caller-controlled identifiers. The cutoff is always a bound parameter.
func (s *Server) purgeOne(name string, pt purgeTable, olderThan float64) (int64, error) {
	var (
		res interface{ RowsAffected() (int64, error) }
		err error
	)
	switch {
	case olderThan <= 0:
		// No age filter: every row of the named table. This is also the only way to reach rows whose
		// timestamp is missing or unparseable (see the rfc3339Text branch below).
		res, err = s.db.Exec(`DELETE FROM ` + name)
	case pt.kind == epochReal:
		res, err = s.db.Exec(`DELETE FROM `+name+` WHERE `+pt.timeCol+` < ?`, olderThan)
	default:
		// RFC3339 TEXT. strftime('%s', ...) parses the stored string into epoch seconds rather than
		// comparing strings: a lexicographic compare is only correct while every value is UTC with a
		// 'Z' suffix, which is today's invariant (nowRFC3339 writes UTC) but is an invariant of the
		// WRITERS, not of the column. A row whose timestamp is NULL or unparseable yields NULL here
		// and is therefore NOT deleted — deliberately, since a row that cannot be dated cannot be
		// shown to be old. A no-filter purge still removes it.
		res, err = s.db.Exec(
			`DELETE FROM `+name+` WHERE CAST(strftime('%s', `+pt.timeCol+`) AS REAL) < ?`, olderThan)
	}
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

func purgeableNames() []string {
	out := make([]string, 0, len(purgeable))
	for k := range purgeable {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
