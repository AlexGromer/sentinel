// Package eventcatalog exposes the event catalogue (brain/events.json) to the Go side.
//
// The file lives in brain/ because that is where it is authored and where the Python emitter reads
// it from, and go:embed patterns cannot reach outside the containing file's directory — hence a Go
// file inside a Python package directory. Same arrangement, and same reason, as docs/embed.go
// (ADR-064). The alternative was a second copy under internal/ guarded by a byte-identity test;
// one file with no copy to drift from is strictly better.
//
// The catalogue is the source of truth for BOTH sides of the boundary. Python renders the English
// text into the wire line (see brain/eventlog.py); control-api parses that line back and enriches it
// from here — module, phase, and the `degrades` flag that has to reach the verdict. Serving the same
// bytes to the browser (GET /v1/events-catalog) is what lets the UI show Russian without the server
// doing any i18n.
package eventcatalog

import (
	_ "embed"
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

//go:embed events.json
var raw []byte

// Raw returns the catalogue bytes verbatim, for serving to the browser.
func Raw() []byte { return raw }

// Event is one diagnostic entry.
//
// ⚠ ADR-117 REVERSES half of a decision recorded here. The comment used to read "the bilingual text
// is the browser's business, and decoding it here would invite using it server-side", and the
// caution was right about `ru` and wrong about `en`. Measured cost of the old arrangement: Go
// assembled its journal sentences by concatenation while the catalogue held a template for the same
// code — two statements of one format, with nothing comparing them. Six codes drifted, and the hub,
// which extracts values from the EN sentence by the EN template to re-render them in Russian, fell
// back to English one line at a time. A screenshot found it; no gate could.
//
// So `En` is decoded and `Ru` is NOT. The wire rule is unchanged and is the reason: no Russian text
// travels on the wire (brain/eventlog.py says the same), the browser owns translation, and the
// server owns exactly one sentence — the one it renders from the template it was given.
type Event struct {
	// En is the English template, with `{field}` placeholders. Rendered by Render, never concatenated.
	En       string   `json:"en"`
	Lvl      string   `json:"lvl"`
	Cat      string   `json:"cat"`
	Phase    string   `json:"phase"`
	Modules  []string `json:"modules"`
	Degrades bool     `json:"degrades"`
	Exit     *int     `json:"exit"`
	// Fault is the HEALTH-004 axis: whose problem this outcome is (none/app/tool/test/config). Only
	// codes that END a run carry it — the catalogue gate ties it to `exit` so the two sets cannot
	// diverge. It is what lets a refusal to start (our model is down) stop reading as `integrity`,
	// which sent the reader to check a plan_hash that was never involved.
	Fault string `json:"fault"`
	// Src overrides the source the category implies. Empty for almost every code — the axis is derived
	// (cat -> sources -> audiences) so the two cannot disagree, and an override weakens that on purpose
	// only where the emitter and the subject differ. The drift codes are the case: the healer emits
	// them and they are statements about the APPLICATION. The catalogue gate refuses an override that
	// does not justify itself in `src_why`.
	Src string `json:"src"`
}

// ExitInfo is one row of the `exit_codes` table: the bilingual, severity-tagged meaning of a process
// exit code. The table has been in the catalogue since ADR-087 and had NO product consumer — Go
// hardcoded four words in verdictEnum and the hub hardcoded a third copy, so both lost exit 4 (the
// tool crashed) and exit -1 (killed / never spawned) into a generic word. Reading it here is what
// collapses the three tables into one.
type ExitInfo struct {
	Icon     string `json:"icon"`
	Severity string `json:"severity"`
	Fault    string `json:"fault"`
	// Verdict is the WORD this exit code means — ADR-141. It is read from the catalogue rather than
	// switched on here because the switch was the defect: verdictEnum named four codes and sent every
	// other one to `problem` through a `default`, so a run that brain had already called
	// `tool_failure_salvaged` was recorded as `problem`. Measured 2026-08-31: exits 4, 5 and -1 all
	// lost their word crossing this boundary. `Verdict` says WHAT happened; `Fault` says WHOSE it is.
	Verdict string `json:"verdict"`
	RU      string `json:"ru"`
	EN      string `json:"en"`
	RUHint  string `json:"ru_hint"`
	ENHint  string `json:"en_hint"`
}

// Foreign is one boundary-classification rule for output we do not instrument (Playwright, Node,
// Chromium). Order matters: the first match wins, and the last rule is a catch-all, so no line is
// ever left without a level and a category.
type Foreign struct {
	Code             string `json:"code"`
	Match            string `json:"match"`
	Lvl              string `json:"lvl"`
	Cat              string `json:"cat"`
	CollapseStack    bool   `json:"collapse_stack"`
	AttachToPrevious bool   `json:"attach_to_previous"`

	re *regexp.Regexp
}

// Matches reports whether the line matches this rule. A rule whose regexp failed to compile never
// matches, so a malformed catalogue degrades to "falls through to the catch-all" rather than
// panicking a running control-API — the CI gate is what keeps the regexps valid.
func (f *Foreign) Matches(line string) bool { return f.re != nil && f.re.MatchString(line) }

type catalogue struct {
	Events     map[string]Event    `json:"events"`
	Foreign    []*Foreign          `json:"foreign_patterns"`
	Categories []string            `json:"categories"`
	Levels     []string            `json:"levels"`
	Sources    map[string]srcEntry `json:"sources"`
	ExitCodes  map[string]ExitInfo `json:"exit_codes"`
	Faults     map[string]struct{} `json:"faults"`
	Audiences  map[string]audEntry `json:"audiences"`
}

type audEntry struct {
	Sources []string `json:"sources"`
}

type srcEntry struct {
	Cats []string `json:"cats"`
}

var (
	once   sync.Once
	parsed catalogue
	byMod  map[string]string // code -> the single emitting module, when unambiguous
	bySrc  map[string]string // category -> source ("tool" / "application" / "testing")
)

func load() {
	once.Do(func() {
		if err := json.Unmarshal(raw, &parsed); err != nil {
			// A broken embedded catalogue must not take the control-API down: an empty catalogue
			// degrades every line to the unclassified path, which is visible, rather than fatal.
			parsed = catalogue{Events: map[string]Event{}}
			return
		}
		for _, f := range parsed.Foreign {
			if re, err := regexp.Compile(f.Match); err == nil {
				f.re = re
			}
		}
		// The SOURCE axis is derived from the category, so a record never carries it redundantly and the
		// two can never disagree. The catalogue gate proves the partition is total and non-overlapping.
		bySrc = map[string]string{}
		for src, meta := range parsed.Sources {
			for _, c := range meta.Cats {
				bySrc[c] = src
			}
		}
		byMod = make(map[string]string, len(parsed.Events))
		for code, e := range parsed.Events {
			// A code emitted from exactly one module gets a `mod` on its records. Two modules mean
			// the line itself cannot say which one it came from, so the field is left empty rather
			// than guessed — an unfilterable field beats a wrong one.
			if len(e.Modules) == 1 {
				// `__main__` reads as `brain.main`: trimming only the prefix left `brain.main__`.
				byMod[code] = "brain." + strings.Trim(e.Modules[0], "_")
			}
		}
	})
}

// Lookup returns the catalogue entry for a code.
func Lookup(code string) (Event, bool) {
	load()
	e, ok := parsed.Events[code]
	return e, ok
}

// Module returns the emitting module for a code, or "" when the code has no single home.
func Module(code string) string {
	load()
	return byMod[code]
}

// Categories returns the closed category vocabulary. Config validation reads it from here so a
// category added to the catalogue is immediately accepted, and one that was never defined is refused.
func Categories() []string {
	load()
	return parsed.Categories
}

// Levels returns the closed level vocabulary, ordered from least to most severe.
func Levels() []string {
	load()
	return parsed.Levels
}

// SourceOf maps a category to its source — whose log this is: the tool, the application under test, or
// the testing itself. A tester asks that before asking which subsystem.
func SourceOf(cat string) string {
	load()
	if s, ok := bySrc[cat]; ok {
		return s
	}
	return "tool"
}

// SourceOfCode answers whose log a RECORD is, for a code whose subject differs from its emitter.
// Falls through to the category rule for everything else, which is all but three codes today.
//
// Asked per code rather than per category because the exception is genuinely per code: `heal` is the
// tool's own category and belongs there, while `heal.drift_*` report that the application changed.
// Moving the whole category would take the healer's real diagnostics with it; moving the drift codes
// to another category would hide them from a `heal` filter, where a reader looking at self-healing
// expects to find them.
func SourceOfCode(code, cat string) string {
	load()
	if s := parsed.Events[code].Src; s != "" {
		return s
	}
	return SourceOf(cat)
}

// SourcesOf resolves a filter word to the set of SOURCES it stands for. An audience name expands to
// its members; anything else stands for itself.
//
// That is what makes `src=application` and `src=business` the same syntax — one names a source, the
// other a set of them, and a caller does not have to know which the reader meant. `agentctl` has
// promised exactly this since ADR-068 ("--src takes a source OR an audience name"), and the server
// matched the string exactly, so `src=business` answered with an EMPTY LIST. Empty reads as "there
// are no business-side records", not as "that word was not understood" — the vacuous answer this
// codebase keeps hunting. Measured 2026-08-04 against a live run whose every step had failed.
func SourcesOf(name string) []string {
	load()
	if a, ok := parsed.Audiences[name]; ok && len(a.Sources) > 0 {
		return a.Sources
	}
	return []string{name}
}

// FaultOf returns the fault domain a code declares, or "" when it declares none. Only terminal codes
// (those with an `exit`) declare one, so "" is the normal answer for the vast majority and means
// "this record did not end the run", NOT "nobody is at fault" — that answer is spelled `none`.
func FaultOf(code string) string {
	load()
	return parsed.Events[code].Fault
}

// ExitInfoOf returns the catalogue's meaning for a process exit code. `ok` is false for a code the
// catalogue never declared, which a caller must render as "unknown exit N" rather than inventing a
// meaning — an exit code we cannot explain is itself a fact worth showing.
func ExitInfoOf(code int) (ExitInfo, bool) {
	load()
	e, ok := parsed.ExitCodes[strconv.Itoa(code)]
	return e, ok
}

// IsFault reports whether name is a member of the closed fault vocabulary. Used at the boundary so a
// value from an older or newer brain cannot smuggle an unrenderable domain into the store.
func IsFault(name string) bool {
	load()
	_, ok := parsed.Faults[name]
	return ok
}

// ForeignRules returns the ordered classification rules for third-party output.
func ForeignRules() []*Foreign {
	load()
	return parsed.Foreign
}
