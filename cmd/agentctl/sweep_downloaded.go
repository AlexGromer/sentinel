package main

// agentctl sweep-downloaded (SEC-RETENTION-DOWNLOAD-CONSUMER) — the explicit half of the
// until_downloaded policy.
//
// ADR-103 made a real download leave a downloaded.json marker in the run's directory and deliberately
// deleted nothing: a download must never be the thing that erases the evidence. This is the consumer
// Alex chose to keep SEPARATE and EXPLICIT — the operator decides, by name, that runs the human has
// already taken a copy of may now be removed. It is neither automatic (it is absent from the
// start-of-run sweep block, where sweepTraces/sweepLogs/sweepRuns live) nor part of `purge-store`
// (that deletes DATABASE rows over gRPC; this deletes run DIRECTORIES on the filesystem — two
// different mechanisms that should not be conflated).
//
// It deletes the WHOLE run directory, which is the literal reading of the mode: the human has the
// copy, so the run is done. That is destructive and irreversible, so it refuses to act without --yes,
// and --dry-run reports what it would take without touching anything. Counts, never paths or content
// — the report is as shareable as the run log.

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// runsWithDownloadMarker returns the run directories under runsRoot that carry a downloaded.json
// marker — the runs a human has taken a copy of (ADR-103). Sorted for a deterministic report. It is
// pure and side-effect-free so the decision ("which runs would go") is testable apart from the
// deletion.
func runsWithDownloadMarker(runsRoot string) []string {
	entries, err := os.ReadDir(runsRoot)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, err := os.Stat(filepath.Join(runsRoot, e.Name(), "downloaded.json")); err == nil {
			out = append(out, filepath.Join(runsRoot, e.Name()))
		}
	}
	sort.Strings(out)
	return out
}

func cmdSweepDownloaded(repo string, args []string) int {
	fs := flag.NewFlagSet("sweep-downloaded", flag.ExitOnError)
	yes := fs.Bool("yes", false, "confirm: delete the run directories a human has downloaded (cannot be undone)")
	dryRun := fs.Bool("dry-run", false, "report what would be deleted, delete nothing")
	_ = fs.Parse(args)

	runsRoot := filepath.Join(repo, "runs")
	targets := runsWithDownloadMarker(runsRoot)

	if len(targets) == 0 {
		fmt.Println("sweep-downloaded: no runs are marked downloaded — nothing to do")
		return 0
	}

	if *dryRun {
		// Counts, never paths: a dry run must not become a way to list run ids either.
		fmt.Printf("sweep-downloaded: %d run(s) marked downloaded would be deleted (dry-run, nothing removed)\n",
			len(targets))
		return 0
	}
	if !*yes {
		fmt.Fprintf(os.Stderr, "refusing to delete %d downloaded run(s) without --yes: this cannot be undone "+
			"(use --dry-run to preview)\n", len(targets))
		return 2
	}

	removed := 0
	for _, dir := range targets {
		if err := os.RemoveAll(dir); err != nil {
			fmt.Fprintf(os.Stderr, "sweep-downloaded: could not remove a run dir: %v\n", err)
			continue // best-effort: report the count we actually achieved, never claim more
		}
		removed++
	}
	fmt.Printf("sweep-downloaded: removed %d of %d downloaded run(s)\n", removed, len(targets))
	if removed < len(targets) {
		return 1 // some could not be removed — a non-zero exit so a script notices
	}
	return 0
}
