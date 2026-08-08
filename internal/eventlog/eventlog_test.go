package eventlog

import "testing"

// The rendering contract, asserted as PROPERTIES rather than against a golden sentence. A golden
// would pin today's catalogue wording, and the whole point of ADR-117 is that the wording lives in
// the catalogue and may change there without a code edit.

func TestASuppliedFieldIsSubstitutedAndAMissingOneStaysVisible(t *testing.T) {
	// The missing-field half is the one worth stating out loud: eventlog.py leaves `{key}` visible
	// (its _Lenient dict) rather than dropping it, because a blank where a value belongs reads as a
	// rendering bug nobody can name, while `{key}` says exactly which value was not supplied.
	got := renderTemplate("{a} then {b}", map[string]string{"a": "one"})
	if got != "one then {b}" {
		t.Fatalf("substitution/leniency: got %q", got)
	}
}

func TestAnUnbalancedBraceYieldsTheTemplateRatherThanHalfASentence(t *testing.T) {
	// Same contract as _render in eventlog.py, and for the same reason: a logger may not raise, and a
	// half-substituted sentence is harder to diagnose than an unsubstituted one.
	got := renderTemplate("{a} and {oops", map[string]string{"a": "x"})
	if got != "{a} and {oops" {
		t.Fatalf("an unbalanced brace must yield the template verbatim, got %q", got)
	}
}

func TestASecretShapedFieldNameBlanksItsValueWhateverItLooksLike(t *testing.T) {
	// The reason redaction moved onto the FIELD (redact.Value). Line() can only scan an assembled
	// sentence for `name=value`, so a bare secret in a field used to survive unless the sentence
	// happened to read that way. Here the NAME decides, so the value's shape is irrelevant.
	got := renderTemplate("source {api_key} end", map[string]string{"api_key": "sk-plainlooking"})
	if got == "source sk-plainlooking end" {
		t.Fatal("a secret-shaped field name did not blank its value")
	}
	if got != "source [REDACTED] end" {
		t.Fatalf("unexpected redaction shape: %q", got)
	}
}

func TestOurOwnProseIsNeverScanned(t *testing.T) {
	// The measured hazard that made per-field redaction necessary: redact.Line scans for `name=value`
	// and the templates are OUR words. A template ending in a credential-shaped word must survive
	// intact — the wording of one existing message was already steered away from that shape once,
	// which is a fix that would not have been needed if the scanner had never seen the template.
	tmpl := "The machine token came from {source}"
	got := renderTemplate(tmpl, map[string]string{"source": "a file"})
	if got != "The machine token came from a file" {
		t.Fatalf("the template was altered by redaction: %q", got)
	}
}

func TestAnUncataloguedCodeRefusesInsteadOfInventingASentence(t *testing.T) {
	// A code the catalogue does not know is a code the reader's browser cannot render either, so the
	// honest outcome is a refusal the caller turns into `eventlog.uncatalogued` — never a made-up
	// sentence that would look like a real record.
	if _, ok := Render("service.no_such_code_exists", nil); ok {
		t.Fatal("Render vouched for a code that is not in the catalogue")
	}
}

func TestARealCatalogueCodeRendersWithItsFields(t *testing.T) {
	// One end-to-end pass through the real embedded catalogue, so the test is not purely about the
	// substituter: a code whose template stopped carrying its placeholders would pass every unit
	// assertion above and produce a sentence with no values in it.
	got, ok := Render("service.api_call", map[string]string{
		"method": "GET", "route": "/v1/runs", "status": "200", "dur_ms": "3", "actor": "machine",
	})
	if !ok {
		t.Fatal("service.api_call is not in the embedded catalogue")
	}
	for _, want := range []string{"GET", "/v1/runs", "200", "3", "machine"} {
		if !contains(got, want) {
			t.Fatalf("rendered sentence %q lost the field %q", got, want)
		}
	}
	if contains(got, "{") {
		t.Fatalf("rendered sentence still carries an unsubstituted placeholder: %q", got)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
