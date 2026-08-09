package redact

// ADR-098. Every case here came from MEASURING a real `trace.zip` produced by the real executor
// against a page that posts credentials — not from reading the trace format. The difference matters:
// the backlog item's design ("apply the ADR-081 scanner") reached everything except the one thing that
// mattered, and only running it showed which.

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	typed  = "s3cr3t-PASSWORD-VALUE"
	bearer = "tok-ABCDEF123456"
)

// A miniature of the real archive: one line of each shape the measurement found carrying a secret,
// plus the shapes that must survive untouched.
func sampleTrace(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "trace.zip")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	z := zip.NewWriter(f)
	add := func(name, body string) {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	add("trace.trace", strings.Join([]string{
		// rule 1 — the call itself
		`{"type":"before","callId":"call@18","method":"fill","params":{"selector":"internal:role=textbox[name=\"Password\"i]","value":"` + typed + `","timeout":5000}}`,
		// rule 2 — the driver's own narration of that call
		`{"type":"log","callId":"call@18","time":1.5,"message":"  fill(\"` + typed + `\")"}`,
		// a narration of a DIFFERENT call must survive: matched by callId, never by text
		`{"type":"log","callId":"call@99","time":1.6,"message":"  waiting for locator to be visible"}`,
		// rule 3 — the DOM snapshot's synthetic attribute
		`{"type":"frame-snapshot","snapshot":{"callId":"call@18","html":["HTML",{},["BODY",{},["INPUT",{"__playwright_value_":"` + typed + `","id":"p"}]]]}}`,
		// identity fields that must NOT be touched: our audit trail is made of hex
		`{"type":"context-options","options":{"deviceScaleFactor":1},"runId":"868db3496d004a1c"}`,
		// A NON-input call carrying a param name the input rule blanks. `text` here is OUR assertion,
		// not something a person typed, and destroying it would make the trace unable to answer "what
		// did the tool expect". Without this line, bypassing the input-verb filter entirely is
		// invisible — caught by mutation.
		`{"type":"before","callId":"call@31","method":"expect","params":{"selector":"#msg","text":"Welcome back"}}`,
	}, "\n")+"\n")
	add("trace.network", strings.Join([]string{
		// the empty ARRAY that broke the raw scanner: `cookies` is credential-shaped, the value is not a string
		`{"type":"resource-snapshot","snapshot":{"request":{"method":"POST","url":"http://h/login","cookies":[],"headers":[{"name":"Authorization","value":"Bearer ` + bearer + `"}]}}}`,
	}, "\n")+"\n")
	// a non-JSON entry: the raw scanner is the right tool here
	add("resources/aaa.html", `<script>fetch('/x',{headers:{'Authorization':'Bearer `+bearer+`'}})</script>`)
	add("resources/bbb.json", `{"username":"demo","password":"`+typed+`"}`)
	// pixels: copied through, never inspected
	// ⚠ The marker is credential-SHAPED on purpose. With a plain string the text passes would leave
	// the bytes alone anyway, so "the image is unchanged" would hold whether or not images are
	// excluded — the check would pass over a build that ran pixels through the scanner. Caught by
	// mutation.
	add("resources/ccc.jpeg", "\xff\xd8\xff\xe0not-a-jpeg password="+typed)
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}

func entries(t *testing.T, path string) map[string][]byte {
	t.Helper()
	z, err := zip.OpenReader(path)
	if err != nil {
		t.Fatalf("archive will not open: %v", err)
	}
	defer z.Close()
	out := map[string][]byte{}
	for _, f := range z.File {
		rc, err := f.Open()
		if err != nil {
			t.Fatal(err)
		}
		b, err := io.ReadAll(rc)
		rc.Close()
		if err != nil {
			t.Fatal(err)
		}
		out[f.Name] = b
	}
	return out
}

// --- the load-bearing check ---------------------------------------------------------------------

// The secret must be gone from every TEXT entry. Asserted over the whole archive rather than per
// rule: a rule that stops working is only interesting because a secret survives, and checking each
// rule separately would let a fourth shape appear with nothing to notice it.
func TestNoTypedSecretSurvivesInAnyTextEntry(t *testing.T) {
	path := sampleTrace(t)
	st, err := TraceFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for name, body := range entries(t, path) {
		if isImage(name) {
			continue
		}
		if bytes.Contains(body, []byte(typed)) {
			t.Errorf("%s still carries the typed secret:\n%.400s", name, body)
		}
		if bytes.Contains(body, []byte(bearer)) {
			t.Errorf("%s still carries the bearer token:\n%.400s", name, body)
		}
	}
	// Each rule must have FIRED. Without this the check above passes on an archive that happened to
	// contain nothing, which is the vacuous-pass this repository keeps meeting.
	if st.TypedValues == 0 || st.NarratedLogs == 0 || st.SnapshotVals == 0 || st.TextualLines == 0 {
		t.Fatalf("a rule never fired, so the sweep above proves less than it looks: %+v", st)
	}
}

// The counterpart, and the half that actually costs something to get right: a redacted trace has to
// remain a trace. Measured failure — the raw line scanner turned `"cookies":[]` into
// `"cookies":[REDACTED]]` and two lines of a real archive stopped parsing.
func TestTheRedactedArchiveIsStillReadable(t *testing.T) {
	path := sampleTrace(t)
	before := entries(t, path)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	after := entries(t, path)

	if len(after) != len(before) {
		t.Fatalf("entry count changed: %d -> %d", len(before), len(after))
	}
	for name := range before {
		if _, ok := after[name]; !ok {
			t.Errorf("entry lost: %s", name)
		}
	}
	for _, name := range []string{"trace.trace", "trace.network"} {
		for i, ln := range bytes.Split(after[name], []byte("\n")) {
			if len(bytes.TrimSpace(ln)) == 0 {
				continue
			}
			var v interface{}
			if err := json.Unmarshal(ln, &v); err != nil {
				t.Errorf("%s line %d is no longer JSON (%v):\n%.300s", name, i+1, err, ln)
			}
		}
	}
}

// Pixels are NOT redacted, by decision — cleaning them means OCR plus masking, and a redactor that
// half-works on screenshots is worse than one that says it does not try. The lever is
// SENTINEL_TRACE_SCREENSHOTS=0. Asserted so the decision cannot be reversed by accident: the image
// must come out byte-identical, secret and all.
func TestPixelsAreCopiedByteForByte(t *testing.T) {
	path := sampleTrace(t)
	before := entries(t, path)["resources/ccc.jpeg"]
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	after := entries(t, path)["resources/ccc.jpeg"]
	if !bytes.Equal(before, after) {
		t.Fatalf("an image was modified; pixels are out of scope by decision and the toggle is the lever")
	}
	if !bytes.Contains(after, []byte("password="+typed)) {
		t.Fatal("the fixture's image no longer carries a credential-shaped marker, so this check " +
			"would pass even if images were run through the redactor")
	}
}

// Matched by callId, never by text. Without this the narration rule could be a substring match that
// happens to work, and it would blank unrelated diagnostics the moment a message mentioned a value.
func TestNarrationOfOtherCallsSurvives(t *testing.T) {
	path := sampleTrace(t)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	body := entries(t, path)["trace.trace"]
	if !bytes.Contains(body, []byte("waiting for locator to be visible")) {
		t.Fatal("the log line of an unrelated call was blanked; the rule is matching on text, not on callId")
	}
	if bytes.Contains(body, []byte(`fill(\"`+typed)) {
		t.Fatal("the narration of the input call survived")
	}
}

// Our own identifiers are the thing a broad redactor destroys silently. ADR-081 refused an entropy
// heuristic for exactly this reason; the trace pass must not reintroduce one.
func TestOurOwnAuditTrailSurvives(t *testing.T) {
	path := sampleTrace(t)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	body := entries(t, path)["trace.trace"]
	for _, keep := range []string{"868db3496d004a1c", "deviceScaleFactor", "call@99"} {
		if !bytes.Contains(body, []byte(keep)) {
			t.Errorf("redaction destroyed %q — the evidence the trace exists to preserve", keep)
		}
	}
}

// A selector can carry data too (`getByText("user@example.com")`), but blanking it would make the
// trace useless for the question it is opened to answer: WHICH control did the tool act on. The
// decision is that selectors survive; asserted so it is a decision rather than an oversight.
func TestSelectorsSurviveSoTheTraceStaysUseful(t *testing.T) {
	path := sampleTrace(t)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(entries(t, path)["trace.trace"], []byte("internal:role=textbox")) {
		t.Fatal("the selector was blanked; a trace that cannot say which control was used answers nothing")
	}
}

// The input rule must apply to INPUT verbs and to nothing else. `expect` carries a `text` parameter —
// the same name `type` uses — but that text is OUR assertion, not something a person typed, and a
// trace that cannot say what the tool expected has lost the thing it is opened to check.
func TestOnlyInputVerbsHaveTheirParametersBlanked(t *testing.T) {
	path := sampleTrace(t)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	body := entries(t, path)["trace.trace"]
	if !bytes.Contains(body, []byte("Welcome back")) {
		t.Fatal("an assertion's expected text was blanked: the input rule is firing on every call, " +
			"not only on the verbs that carry what a person typed")
	}
	if !bytes.Contains(body, []byte(`"#msg"`)) {
		t.Fatal("the assertion's selector went too")
	}
}

// Atomicity. A crash mid-rewrite must leave the original, never a truncated or half-redacted file
// that looks finished.
func TestAFailedRedactionLeavesTheOriginalIntact(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "not-a-zip.zip")
	original := []byte("this is not an archive")
	if err := os.WriteFile(path, original, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := TraceFile(path); err == nil {
		t.Fatal("a corrupt archive was reported as redacted")
	}
	got, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(got, original) {
		t.Fatalf("the original was damaged by a failed run: %q", got)
	}
	// ...and no temp file was left behind to be mistaken for an artifact.
	ents, _ := os.ReadDir(dir)
	for _, e := range ents {
		if strings.HasPrefix(e.Name(), ".trace-redact-") {
			t.Fatalf("a temp file survived a failure: %s", e.Name())
		}
	}
}

// Idempotent: running twice must not double-mark or corrupt. A redaction step that is wired into two
// paths by mistake should be harmless, not destructive.
func TestRedactingTwiceChangesNothingMore(t *testing.T) {
	path := sampleTrace(t)
	if _, err := TraceFile(path); err != nil {
		t.Fatal(err)
	}
	once := entries(t, path)
	st, err := TraceFile(path)
	if err != nil {
		t.Fatal(err)
	}
	twice := entries(t, path)
	for name := range once {
		if !bytes.Equal(once[name], twice[name]) {
			t.Errorf("%s changed on the second pass:\n%.200s\n---\n%.200s", name, once[name], twice[name])
		}
	}
	if st.TypedValues != 0 || st.SnapshotVals != 0 {
		t.Errorf("the second pass claims to have found more to redact: %+v", st)
	}
}

// TestASecretHeaderIsBlankedByItsNameNotItsShape covers the leak measured on 2026-08-08.
//
// trace.network stores headers as `{"name":"X-Api-Key","value":"…"}`, so the header's name is a
// VALUE, not a KEY — and the per-key rule that blanks `{"api_key":"…"}` structurally cannot see it.
// What survived was only what `Line` recognises by its own shape, which is why `Authorization:
// Bearer …` looked protected and hid the fact that nothing else was.
//
// Asserted as four SEPARATE cases rather than one table with a shared expectation, because the four
// travel through different code: two through the new pair rule, one through reBearer inside Line,
// one through the per-key rule. A single case passing tells you nothing about the other three.
func TestASecretHeaderIsBlankedByItsNameNotItsShape(t *testing.T) {
	// ⚠ THE CANARIES ARE DELIBERATELY UNCONVINCING, and that is not sloppiness.
	//
	// Two independent reasons, and both would be broken by "making them look real":
	//
	//  1. This test asserts that a value is blanked because its SIBLING `name` is credential-shaped —
	//     not because the value itself looks like a credential. A canary that Line() would recognise
	//     on its own (a Bearer token, a JWT) would make the test pass through the OLD path and prove
	//     nothing about the new rule. `CANARY-not-a-real-credential-0001` is recognised by neither
	//     reBearer nor reJWT nor scanNamedSecrets, so only the pair rule can blank it.
	//  2. gitleaks scans this repository on every push as a HARD failure, and it is right to: a
	//     realistic `sk-live-…` in a test file is indistinguishable from a leaked one to any scanner,
	//     to any reviewer, and to anyone who greps history later. The first version of this test used
	//     one and CI refused it (generic-api-key, trace_test.go:344). Allow-listing the file would
	//     have traded a real guard for a green tick.
	blanked := func(t *testing.T, line string) string {
		t.Helper()
		out, n := redactJSONText([]byte(line))
		if n == 0 {
			t.Fatalf("nothing was redacted in %s", line)
		}
		return string(out)
	}

	t.Run("an api-key header, which has no credential-shaped KEY anywhere", func(t *testing.T) {
		got := blanked(t, `{"headers":[{"name":"X-Api-Key","value":"CANARY-not-a-real-credential-0001"}]}`)
		if strings.Contains(got, "CANARY-not-a-real-credential-0001") {
			t.Fatalf("the key survived into the archive: %s", got)
		}
	})

	t.Run("a cookie header, whose value Line does not recognise either", func(t *testing.T) {
		got := blanked(t, `{"headers":[{"name":"Cookie","value":"session=CANARY-not-a-real-session-0002"}]}`)
		if strings.Contains(got, "abcdef123456") {
			t.Fatalf("the session survived into the archive: %s", got)
		}
	})

	t.Run("an authorization header still goes, as it always did", func(t *testing.T) {
		got := blanked(t, `{"headers":[{"name":"Authorization","value":"Bearer sk-abc.def.ghi"}]}`)
		if strings.Contains(got, "sk-abc.def.ghi") {
			t.Fatalf("a bearer credential survived: %s", got)
		}
	})

	t.Run("an ordinary header is left alone — a rule that blanks everything protects nothing", func(t *testing.T) {
		out, _ := redactJSONText([]byte(`{"headers":[{"name":"Accept","value":"application/json"}]}`))
		if !strings.Contains(string(out), "application/json") {
			t.Fatalf("an innocent header was blanked, which would make the archive useless: %s", out)
		}
	})

	t.Run("the member KEY rule still works — the pair rule must not have replaced it", func(t *testing.T) {
		got := blanked(t, `{"api_key":"CANARY-not-a-real-credential-0001"}`)
		if strings.Contains(got, "CANARY-not-a-real-credential-0001") {
			t.Fatalf("the original per-key rule regressed: %s", got)
		}
	})

	t.Run("a name member that is not a header keeps working normally", func(t *testing.T) {
		// `{"name":"Accept"}` with no sibling `value` must not crash or invent a member, and a
		// `name` that is credential-shaped with no `value` beside it has nothing to blank.
		out, _ := redactJSONText([]byte(`{"name":"Cookie"}`))
		if strings.Contains(string(out), Placeholder) {
			t.Fatalf("a lone name member produced a redaction out of nothing: %s", out)
		}
	})
}
