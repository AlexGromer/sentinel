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
	"strings"
	"sync"
)

//go:embed events.json
var raw []byte

// Raw returns the catalogue bytes verbatim, for serving to the browser.
func Raw() []byte { return raw }

// Event is one diagnostic entry. Only the fields the Go boundary actually uses are decoded; the
// bilingual text is the browser's business, and decoding it here would invite using it server-side.
type Event struct {
	Lvl      string   `json:"lvl"`
	Cat      string   `json:"cat"`
	Phase    string   `json:"phase"`
	Modules  []string `json:"modules"`
	Degrades bool     `json:"degrades"`
	Exit     *int     `json:"exit"`
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
	Events     map[string]Event `json:"events"`
	Foreign    []*Foreign       `json:"foreign_patterns"`
	Categories []string            `json:"categories"`
	Levels     []string            `json:"levels"`
	Sources    map[string]srcEntry `json:"sources"`
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

// ForeignRules returns the ordered classification rules for third-party output.
func ForeignRules() []*Foreign {
	load()
	return parsed.Foreign
}
