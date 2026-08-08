// Package eventlog renders a journal sentence from the event catalogue instead of assembling it.
//
// It is the Go half of brain/eventlog.py::log, and it exists because the alternative is TWO
// statements of one format with nothing comparing them. Measured cost of that arrangement: Go built
// its service-journal sentences by concatenation while brain/events.json held a template for the
// same code; six codes drifted, and the hub — which extracts values out of the EN sentence BY the EN
// template in order to re-render them in Russian — fell back to English one line at a time. A
// screenshot found it. No gate could, and the gate written afterwards
// (cmd/control-api/svcjournal_wording_test.go) could only catch a drift that had already happened.
// Rendering from the template makes the drift impossible rather than detectable (ADR-117).
//
// WHY A PACKAGE OF ITS OWN, and not a function on eventcatalog: internal/redact already imports
// eventcatalog (it asks whether a name is one of our event codes), so the catalogue package cannot
// import the redactor back. This package imports both.
//
// ⚠ It deliberately contains NO event-code literals. tests/test_event_catalog_offline.py maps
// emitters to PATHS and finds codes by regexing `'service.x'` literals inside them, so a code that
// moved here would become a phantom the catalogue gate can no longer see. Callers keep their code
// literals; only the rendering moved.
package eventlog

import (
	"strings"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
	"github.com/AlexGromer/sentinel/internal/redact"
)

// Render builds the sentence for `code` from the catalogue's own EN template, substituting `fields`.
//
// ⚠ THE LEVEL IS NOT TAKEN FROM THE CATALOGUE, and this is the one place Python's design does not
// transfer. eventlog.py reads `lvl` from the entry because a brain code means one thing; the Go side
// picks it per CALL — `service.api_call` is `debug` for a read and `info` for a mutation,
// `service.api_refused` is `warn` for 401/403 and `error` for 5xx, while the catalogue declares one
// level per code. Reading `lvl` from the entry here would silently downgrade every 5xx to `warn` and
// every mutation to `debug`; and because svclog filters at WRITE time against a default of `info`,
// mutations would stop being written AT ALL in a default deployment. So Render returns text and
// nothing else — the caller keeps the level, deliberately.
//
// Returns ok=false for an uncatalogued code, or one whose entry carries no English template. The
// caller must not invent a sentence in that case: a code the catalogue does not know is a code the
// reader's browser cannot render either, and the honest outcome is the `eventlog.uncatalogued`
// signal, exactly as on the Python side.
func Render(code string, fields map[string]string) (string, bool) {
	e, ok := eventcatalog.Lookup(code)
	if !ok || e.En == "" {
		return "", false
	}
	return renderTemplate(e.En, fields), true
}

// renderTemplate substitutes `{key}` placeholders, redacting each VALUE as it goes.
//
// Redaction is per-field and happens BEFORE substitution, never afterwards over the assembled
// sentence. The reason is not tidiness: redact.Line scans for `name=value` shapes because that is
// all an assembled sentence offers, and run over a catalogue template it would be scanning OUR OWN
// prose — a template ending in `token: ` would blank the sentence after it. That is not
// hypothetical; the wording of one existing message was already steered away from that shape, in a
// comment that says so. With the name and the value apart, the NAME is authoritative (redact.Value).
//
// A field the caller did not supply is left visible as `{name}`, the same contract as `_Lenient` in
// eventlog.py: a blank where a value belongs reads as a rendering bug nobody can name, while
// `{name}` says exactly which value was missing.
//
// An unbalanced brace yields the template verbatim rather than a half-substituted sentence — again
// matching eventlog.py::_render. A logger may not raise, and a mangled sentence is harder to
// diagnose than an unsubstituted one.
func renderTemplate(tmpl string, fields map[string]string) string {
	var b strings.Builder
	b.Grow(len(tmpl) + 32)
	for i := 0; i < len(tmpl); {
		if tmpl[i] != '{' {
			b.WriteByte(tmpl[i])
			i++
			continue
		}
		j := strings.IndexByte(tmpl[i:], '}')
		if j < 0 {
			return tmpl
		}
		key := tmpl[i+1 : i+j]
		if v, present := fields[key]; present {
			b.WriteString(redact.Value(key, v))
		} else {
			b.WriteString(tmpl[i : i+j+1])
		}
		i += j + 1
	}
	return b.String()
}
