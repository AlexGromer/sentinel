package main

// agentctl purge-service (HEALTH-005 PR-B) — the third sibling of `redact-trace` (ADR-098) and
// `purge-store` (ADR-100): the human asks for destruction by name, the tool reports counts and never
// content, and there is no automatic sweep anywhere near it.
//
// The rewrite itself is `svclog.Purge`, next to the writer that owns the file's format, its two
// generations, its permissions and its lock. This file is the CLI: flags, a refusal without --yes,
// counts on stdout — and the one thing only this layer can do, which is WRITE DOWN THAT IT HAPPENED.
//
// That record is the point. A journal whose deletion leaves no trace answers "was anything removed?"
// with silence, and silence is indistinguishable from "nothing happened" — which is exactly the state
// somebody destroying evidence would want it in. So the purge appends `service.log_purged` to the
// journal it just truncated, at `warn`, with the counts.

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/AlexGromer/sentinel/internal/eventlog"
	"github.com/AlexGromer/sentinel/internal/svclog"
)

func cmdPurgeService(repo string, args []string) int {
	fs := flag.NewFlagSet("purge-service", flag.ExitOnError)
	olderThan := fs.Duration("older-than", 0,
		"only records older than this (e.g. 720h); omit to remove every record written before now")
	yes := fs.Bool("yes", false, "confirm: this destroys audit records and cannot be undone")
	_ = fs.Parse(args)

	if !*yes {
		fmt.Fprintln(os.Stderr, "refusing to purge the service journal without --yes: these are audit records, "+
			"they are the only copy, and this cannot be undone")
		return 2
	}

	// Omitted means "everything", spelled as a cutoff of NOW rather than as a special case. It is the
	// same comparison either way, and it has the property a special case would not: a record written
	// while the purge runs is newer than the cutoff, so it survives instead of being swept up by a
	// branch that meant "all".
	cutoff := time.Now()
	if *olderThan > 0 {
		cutoff = cutoff.Add(-*olderThan)
	}

	stateDir := filepath.Join(repo, "state")
	rep, err := svclog.Purge(stateDir, cutoff)
	if err != nil {
		fmt.Fprintf(os.Stderr, "purge-service: %v\n", err)
		return 1
	}
	if len(rep.Files) == 0 {
		fmt.Println("no service journal at " + filepath.Join(stateDir, "logs") + " — nothing to purge")
		return 0
	}

	fmt.Printf("purged  %d record(s) written before %s\n", rep.Removed, cutoff.UTC().Format(time.RFC3339))
	fmt.Printf("kept    %d record(s)\n", rep.Kept)
	if rep.Undated > 0 {
		// Named every time, because it is the one way the count of what was destroyed is not the whole
		// story — and because "older than" is a question these records could not be asked.
		fmt.Printf("kept    %d of those BECAUSE their timestamp could not be read — a record we cannot place\n", rep.Undated)
		fmt.Println("        in time is not a record we may decide is old")
	}
	if !svclog.Locked {
		fmt.Println("note:   this build takes no file lock, so a record appended by a running service during")
		fmt.Println("        the rewrite may have been lost. Stop the services first to be certain.")
	}

	// Written AFTER the rewrite, so it survives it — and through the same package every other writer
	// uses, so it lands in the same file with the same shape rather than being hand-assembled here.
	w := svclog.Open(stateDir, "agentctl")
	defer w.Close()
	// The sentence comes from the catalogue's template, not from concatenation here (ADR-117): this is
	// the THIRD emitter of `service.*` codes, and a third way of building the same text is exactly how
	// six codes drifted from their templates in the first place. The code LITERAL stays here, where the
	// catalogue gate's emitter scan looks for it.
	msg, ok := eventlog.Render("service.log_purged", map[string]string{
		"removed": strconv.Itoa(rep.Removed),
		"kept":    strconv.Itoa(rep.Kept),
		"cutoff":  cutoff.UTC().Format(time.RFC3339),
	})
	if !ok {
		msg = "eventlog.uncatalogued: service.log_purged is not in the catalogue"
	}
	w.Log(svclog.Record{
		Lvl: "warn", Cat: "service", Code: "service.log_purged", Msg: msg,
	})
	return 0
}
