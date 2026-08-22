package main

// USAGE-OMITS-OBSERVE-AXIS — the completeness gate for `agentctl run`'s synopsis.
//
// WHAT WAS MEASURED (2026-08-21, W5 Ф3; re-measured 2026-08-22). usage() printed SEVENTEEN of
// `run`'s twenty-one declared flags and omitted
// `--observe`: not a knob, an AXIS — the choice of what a run lets you see, offered by name in the
// hub, resolved in one place (ADR-120), and absent from the help a person reaches for when they have
// no hub. Nothing was broken; nothing was red. The synopsis was a SECOND list of the flags, written
// by hand, and the two lists were never held against each other, so the one that stopped being
// maintained was the one nobody could see.
//
// WHY THIS SHAPE, AND NOT A GREP FOR `fs.String(`. main_test.go's sibling gate parses main.go's text
// because a `case "x":` has no runtime form to ask. A flag does: newRunFlagSet() hands back the real
// *flag.FlagSet, and VisitAll enumerates what the PROGRAM accepts. That difference is not cosmetic —
// `export-git`'s `--spec` is declared with fs.Var, so a source grep for the String/Bool constructors
// would have reported it as "not a flag" and silently exempted it. Two consequences follow, and both
// are asserted below: the flag list cannot go vacuous without the floor firing, and an exemption that
// names a flag which no longer exists is itself a failure — a stale exemption is how the NEXT
// omission hides.
//
// WHAT IS DELIBERATELY NOT ASSERTED: that every flag appears with its full syntax. The synopsis is a
// synopsis; `agentctl run --help` prints the flag package's own complete listing. What must hold is
// that a person reading it cannot come away unaware that a capability exists.

import (
	"flag"
	"io"
	"os"
	"regexp"
	"strings"
	"testing"
)

// captureUsage runs the REAL usage() and returns what a person sees, rather than re-deriving the text
// from the source. os.Stderr is swapped for a pipe because usage() writes there directly (and hands
// the same os.Stderr to apiUsage, which is why the swap has to happen before the call, not inside).
// The reader runs in a goroutine: the output measured 5352 bytes on 2026-08-22 (4982 before this
// change — both taken by running the built binary with no arguments), comfortably inside a
// 64 KiB pipe buffer, but a synopsis that grows past it would deadlock this test rather than fail it,
// and a gate that hangs is worse than one that is absent.
func captureUsage(t *testing.T) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan string, 1)
	go func() {
		b, _ := io.ReadAll(r)
		done <- string(b)
	}()
	orig := os.Stderr
	os.Stderr = w
	usage()
	os.Stderr = orig
	_ = w.Close()
	out := <-done
	_ = r.Close()
	return out
}

// mentions reports whether the synopsis names --<flag> as a whole word. A plain substring test would
// let `--planner` satisfy `--plan`, which is exactly the pair this command has: the two flags mean
// different things and each has to be findable on its own.
func mentions(text, name string) bool {
	return regexp.MustCompile(`(^|[^\w-])--` + regexp.QuoteMeta(name) + `($|[^\w-])`).MatchString(text)
}

// runSynopsis returns only the `agentctl run` lines of usage() — the header lines plus their indented
// continuations, stopping at the next subcommand.
//
// MEASURED, and the reason this function exists rather than a search over the whole help: a mutation
// that deleted `[--replay --plan <p>]` from the run synopsis left the gate GREEN, because `--plan`
// still appeared on the `baseline update` and `export-spec` lines. Those lines teach a reader nothing
// about `run --replay`. "Mentioned somewhere in a five-kilobyte help" is not the property worth
// gating; "mentioned where a person is reading about this command" is.
func runSynopsis(t *testing.T, text string) string {
	t.Helper()
	// The trailing space is load-bearing, and it was MEASURED, not foreseen: without it the header
	// also matched `  agentctl runs list` (and every other `runs …` verb apiUsage prints), so the block
	// silently absorbed eleven lines belonging to a different command. A delimiter that is nearly right
	// is how a scoped assertion quietly stops being scoped.
	const head = "  agentctl run "
	var block []string
	in := false
	for _, line := range strings.Split(text, "\n") {
		switch {
		case strings.HasPrefix(line, head):
			in = true
		case strings.HasPrefix(line, "  agentctl "):
			in = false
		}
		if in {
			block = append(block, line)
		}
	}
	out := strings.Join(block, "\n")
	// The floor for the extraction itself. Delimiter-hunting is the part of a gate most likely to go
	// silently vacuous — a reformatted usage() would hand back "" and every flag below would report as
	// missing (loud, fine) or, with the polarity reversed one day, as present (silent, not fine).
	// Measured 2026-08-22: 7 lines, 695 bytes.
	if len(block) < 4 || len(out) < 200 {
		t.Fatalf("the `agentctl run` block of usage() came out as %d lines / %d bytes — the extraction "+
			"is no longer finding it, and every assertion over it is now about nothing:\n%s", len(block), len(out), out)
	}
	return out
}

func TestUsageNamesEveryRunFlag(t *testing.T) {
	fs, _ := newRunFlagSet()
	var names []string
	fs.VisitAll(func(f *flag.Flag) { names = append(names, f.Name) })

	// The floor. Every assertion below iterates over `names`; an empty or truncated set would make all
	// of them pass over nothing, which is the failure mode a completeness gate is most prone to.
	// Measured 2026-08-22: 21 flags on `run`.
	const minRunFlags = 18
	if len(names) < minRunFlags {
		t.Fatalf("newRunFlagSet() declared %d flags (%v), fewer than the floor of %d — either the "+
			"command lost half its surface, or this gate stopped seeing it and is now vacuous",
			len(names), names, minRunFlags)
	}

	text := captureUsage(t)
	// The other half of the same floor: the thing being searched has to be the real help. An empty
	// capture would make every Contains() below false-negative into a wall of failures, and a capture
	// of a few bytes would make them all trivially pass if the polarity were ever flipped.
	if len(text) < 1000 || !strings.HasPrefix(text, "usage:") {
		t.Fatalf("captureUsage() returned %d bytes starting %q — usage() is not what was captured",
			len(text), text[:min(40, len(text))])
	}

	synopsis := runSynopsis(t, text)
	declared := map[string]bool{}
	for _, n := range names {
		declared[n] = true
	}

	var missing []string
	for _, n := range names {
		if _, exempt := runFlagsNotInUsage[n]; exempt {
			continue
		}
		if !mentions(synopsis, n) {
			missing = append(missing, "--"+n)
		}
	}
	if len(missing) > 0 {
		t.Errorf("`agentctl run` accepts %v, and its own block in usage() names none of them. A flag the "+
			"help does not mention is a capability that exists only for whoever wrote it — that is how "+
			"--observe, an entire observation axis, was invisible from the terminal for a release. Add "+
			"it to usage(), or record in runFlagsNotInUsage why a person is better off not seeing it.",
			missing)
	}

	// An exemption is a decision, and a decision about a flag that no longer exists is a decision
	// about nothing — it stays in the map, keeps its slot under the cap, and quietly widens what the
	// next flag can slip through.
	for n, why := range runFlagsNotInUsage {
		if !declared[n] {
			t.Errorf("runFlagsNotInUsage exempts %q, which `agentctl run` does not accept; a stale "+
				"exemption is where the next omission hides", n)
		}
		if strings.TrimSpace(why) == "" {
			t.Errorf("runFlagsNotInUsage[%q] has no reason. The map exists to record WHY a flag is "+
				"withheld from the help; an empty string records only that someone wanted the gate quiet", n)
		}
	}

	// The ratchet, in the shape componentsWithoutProbe and apiRoutesWithoutCLI already use here: the
	// cap may only ever go DOWN. Without it, a red gate has a one-line workaround — add an entry —
	// and the synopsis shrinks one deliberate exemption at a time until it is the stale list again.
	const maxRunFlagsNotInUsage = 2 // ⚠ may only go DOWN. Today: --explore, --owner.
	if len(runFlagsNotInUsage) > maxRunFlagsNotInUsage {
		t.Errorf("runFlagsNotInUsage holds %d entries, above the cap of %d. Raising the cap is how a "+
			"completeness gate becomes a formality: print the flag in usage() instead, or make the case "+
			"to a human for why this deployment's help is better without it.",
			len(runFlagsNotInUsage), maxRunFlagsNotInUsage)
	}
}

// TestUsageNamesTheObservationAxis pins the specific omission that was measured, by NAME and by the
// values it accepts. The general gate above would go green if `--observe` were merely mentioned in
// passing; this one fails if the person cannot learn from the synopsis what to pass it. It is also
// the gate that survives a future rewrite of usage() into something derived — the axis has to be
// nameable either way.
func TestUsageNamesTheObservationAxis(t *testing.T) {
	text := runSynopsis(t, captureUsage(t))
	if !mentions(text, "observe") {
		t.Fatal("usage() does not name --observe. This is the exact regression this file was written " +
			"for: the run's observation axis reachable from the hub and unmentioned by the CLI's own help")
	}
	// The modes come from the flag's own help string, which tests/test_observation_modes_offline.py
	// already holds against the schema enum and the resolver's set — so this is not a fourth copy of
	// the list, it is a check that the synopsis quotes the one that is already gated.
	fs, _ := newRunFlagSet()
	f := fs.Lookup("observe")
	if f == nil {
		t.Fatal("`run` has no --observe flag at all — the axis is gone, not merely undocumented")
	}
	modes := regexp.MustCompile(`\b(off\|[a-z|]+)`).FindString(f.Usage)
	if modes == "" {
		t.Fatalf("could not read the mode list out of --observe's help %q; this check went vacuous", f.Usage)
	}
	if !strings.Contains(text, modes) {
		t.Errorf("usage() names --observe but not what to pass it: the flag offers %q and the synopsis "+
			"does not quote that list. A person then learns the axis exists and still cannot use it.", modes)
	}
}
