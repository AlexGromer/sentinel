package main

// The Help strings of `apiVerbs` describe capabilities that live in ANOTHER process. That is the
// whole hazard: a sentence written here about control-api's insides is a copy, and a copy is only
// correct on the day it is typed.
//
// WHAT WAS MEASURED (2026-08-22). `health` read "readiness: store, LLM endpoint, config" — the three
// checks /readyz had when the verb was written. cmd/control-api/readyz.go now fills SIX, having gained
// browser (ADR-124), orchestrator (ADR-126) and vnc (ADR-127) since. The help had been wrong for three
// arcs, silently, and wrong in the direction that costs a person something: it named FEWER components
// than the deployment has, so a reader concludes the one they care about is not covered and goes
// looking somewhere else.
//
// This gate does not check that the Help is CORRECT — a sentence cannot be checked for truth. It
// checks that no Help holds a partial copy of that list at all, which is the property that made the
// sentence go stale. The response already carries the component names, one per key; the CLI's job is
// to say what the answer is about.

import (
	"encoding/json"
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// readyzComponents reads the component names /readyz actually reports, from the one place that
// decides them. Deliberately the real file rather than a list retyped here: a gate against copied
// lists that keeps its own copy would be the defect wearing a test's clothes.
func readyzComponents(t *testing.T) []string {
	t.Helper()
	b, err := os.ReadFile("../control-api/readyz.go")
	if err != nil {
		t.Fatalf("cannot read the readiness source: %v — if it moved, follow it; do not delete this gate, "+
			"it is the only thing holding these Help strings to the deployment they describe", err)
	}
	seen := map[string]bool{}
	var out []string
	for _, m := range regexp.MustCompile(`checks\["([a-z_]+)"\]`).FindAllStringSubmatch(string(b), -1) {
		if !seen[m[1]] {
			seen[m[1]] = true
			out = append(out, m[1])
		}
	}
	// The floor. Everything below iterates over this set, so an extraction that finds nothing would
	// report a clean bill of health over an empty list. Measured 2026-08-22: 6 — store, config, llm,
	// browser, orchestrator, vnc.
	const minComponents = 5
	if len(out) < minComponents {
		t.Fatalf("found %d readiness components (%v), below the floor of %d — the extraction has stopped "+
			"seeing readyz.go and every assertion built on it is now vacuous", len(out), out, minComponents)
	}
	return out
}

func TestNoAPIHelpCopiesTheReadinessComponentList(t *testing.T) {
	comps := readyzComponents(t)
	t.Logf("readiness components as readyz.go reports them: %v", comps)

	// One component name in a Help is a subject ("the persisted config document"). TWO or more is a
	// LIST, and a list of another process's components is the thing that rotted. The threshold is
	// where it is because `config get` and `config set` legitimately say "config" and must stay
	// legal — a gate that forces them to be reworded would be traded away within the month.
	for _, v := range apiVerbs {
		low := strings.ToLower(v.Help)
		var named []string
		for _, c := range comps {
			if regexp.MustCompile(`(^|[^a-z])` + regexp.QuoteMeta(c) + `($|[^a-z])`).MatchString(low) {
				named = append(named, c)
			}
		}
		if len(named) >= 2 {
			t.Errorf("`agentctl %s` describes itself with a list of control-api's components %v. That "+
				"list lives in cmd/control-api/readyz.go and changes there; measured 2026-08-22, this "+
				"Help said three when there were six, and had for three arcs. Say what the answer is "+
				"ABOUT and let the response carry the names.", v.Verb, named)
		}
	}
}

// --- the same class, two more derived sets (W6 follow-up) --------------------------------------
//
// ⚠ WHY THIS SECTION EXISTS. The gate above was written for ONE set — the readiness components — and
// two mutations walked straight past it with the whole repository green: restoring `service-log`'s
// stale Help (it named four kinds of record when brain/events.json declares twenty `service.*` codes)
// and restoring `events-catalog`'s "every event the BRAIN can emit" (three processes emit; naming one
// tells a reader the other two are not in the answer). Both are the same defect as `health`'s — a
// Help holding a partial copy of a set that lives elsewhere — and the first gate could not see them
// because it knew only one set. A gate aimed at one instance of a class is a gate that will be walked
// past by the next instance.
//
// Both sets below are DERIVED from brain/events.json, which is where they change.

// serviceCodesAndEmitters reads the `service.*` codes and the distinct emitters out of the catalogue.
func serviceCodesAndEmitters(t *testing.T) (codes []string, emitters []string) {
	t.Helper()
	b, err := os.ReadFile("../../brain/events.json")
	if err != nil {
		t.Fatalf("cannot read the event catalogue: %v — if it moved, follow it", err)
	}
	var doc struct {
		Events map[string]struct {
			Emitter string `json:"emitter"`
		} `json:"events"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatalf("the event catalogue is not JSON: %v", err)
	}
	seenEm := map[string]bool{}
	for code, e := range doc.Events {
		if strings.HasPrefix(code, "service.") {
			codes = append(codes, strings.TrimPrefix(code, "service."))
		}
		if e.Emitter != "" && !seenEm[e.Emitter] {
			seenEm[e.Emitter] = true
			emitters = append(emitters, e.Emitter)
		}
	}
	sort.Strings(codes)
	sort.Strings(emitters)
	// Floors, because both loops below iterate over these and an empty set passes perfectly.
	// Measured 2026-08-22: 20 service codes, 3 emitters (pw-executor, control-api, agentctl).
	if len(codes) < 15 {
		t.Fatalf("found %d service.* codes, below the floor — the catalogue stopped parsing and every "+
			"assertion built on it is vacuous", len(codes))
	}
	if len(emitters) < 2 {
		t.Fatalf("found %d emitter(s); with fewer than two, naming one is not a defect and this gate "+
			"asserts nothing", len(emitters))
	}
	return codes, emitters
}

func TestNoAPIHelpCopiesTheServiceRecordKinds(t *testing.T) {
	codes, _ := serviceCodesAndEmitters(t)
	t.Logf("service.* record kinds as the catalogue declares them: %d", len(codes))
	for _, v := range apiVerbs {
		low := strings.ToLower(v.Help)
		var named []string
		for _, c := range codes {
			// The bare noun, as a Help would write it: `api_call` reads as "api call" in prose.
			word := strings.ReplaceAll(c, "_", " ")
			if regexp.MustCompile(`(^|[^a-z])` + regexp.QuoteMeta(word) + `($|[^a-z])`).MatchString(low) {
				named = append(named, c)
			}
		}
		// Two or more is a LIST — the same threshold and the same reason as the components gate above.
		if len(named) >= 2 {
			t.Errorf("`agentctl %s` describes itself by listing service record kinds %v. There are %d of "+
				"them and they change in brain/events.json; measured 2026-08-22, this Help named four when "+
				"there were twenty. Say what the answer is ABOUT and let the response carry the codes.",
				v.Verb, named, len(codes))
		}
	}
}

func TestNoAPIHelpClaimsOneProcessEmitsEverything(t *testing.T) {
	_, emitters := serviceCodesAndEmitters(t)
	t.Logf("emitters as the catalogue declares them: %v", emitters)
	for _, v := range apiVerbs {
		low := strings.ToLower(v.Help)
		if !strings.Contains(low, "emit") {
			continue
		}
		var named []string
		for _, e := range emitters {
			if strings.Contains(low, strings.ToLower(e)) {
				named = append(named, e)
			}
		}
		// The word "brain" is how the surviving mutation phrased it — the process's colloquial name,
		// which no `emitter` field spells. Checked separately, because a Help that says "the brain"
		// makes exactly the claim this test refuses and matches none of the emitter strings.
		if strings.Contains(low, "the brain") {
			named = append(named, "the brain")
		}
		if len(named) == 1 && len(emitters) > 1 {
			t.Errorf("`agentctl %s` says only %v emits, while %d processes do (%v). A reader concludes "+
				"the others are not in the answer — and they are. Name the whole, or name none.",
				v.Verb, named, len(emitters), emitters)
		}
	}
}
