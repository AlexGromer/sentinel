package redact

// Trace redaction (ADR-098, `GAP-RISK-010` follow-on).
//
// ADR-084 narrowed the WINDOW — `trace.zip` is written only for a run that did not finish clean. This
// closes the CONTENT.
//
// WHAT IS ACTUALLY IN THERE, measured on a real archive rather than assumed. A trace of one login
// against a page that posts credentials contains:
//
//	trace.trace      newline-delimited JSON. The typed password appears FOUR times, as
//	                 {"type":"before","method":"fill","params":{"selector":…,"value":"<secret>"}}
//	                 and again inside free-text "log" entries quoting the call.
//	trace.network    newline-delimited JSON. Request headers as {"name":"Authorization",
//	                 "value":"Bearer <token>"} — the header NAME is credential-shaped, so the line
//	                 scanner already reaches it.
//	resources/*.json the POST body: {"username":…,"password":"<secret>"}. The name is credential-
//	                 shaped, so the line scanner reaches it too.
//	resources/*.html the page's own source, which may hardcode a token.
//	trace.stacks     text; source paths in practice.
//	resources/*.jpeg PIXELS.
//
// The backlog item said "apply the ADR-081 scanner" and that turned out to be HALF a design. Measured:
// `Secretish("value")` is false — correctly, because `value` is what a search box also has — so the
// named-secret scanner reaches every case above EXCEPT the one that matters most: the text the tool
// itself typed. That case is not a naming problem, it is a structural one, and it is solved
// structurally.
//
// TWO PASSES, and they are not interchangeable:
//
//  1. STRUCTURAL, over trace.trace: the value of an input verb is replaced whatever it is called and
//     whatever it looks like. This is the user's data, not our diagnostics. No guessing, so no false
//     negative — the alternative (redact when the nearby selector looks credential-shaped) is a guess
//     about SOMEONE ELSE'S markup, and a field labelled "PIN" or labelled nothing would sail through
//     silently, which is the failure mode that makes a security control worse than useless.
//  2. TEXTUAL, over every non-image entry: the same `Line` scanner control-api's log sink uses. One
//     vocabulary; a second copy of "what is a secret" is the drift this package exists to prevent.
//
// The structural pass has THREE rules, and the second and third were found by running the first and
// looking at what survived — not by reading the format. A trace redacted by rule 1 alone still
// carried the password three times:
//
//   * `{"type":"log","callId":"call@18","message":"  fill(\"<secret>\")"}` — the driver's own
//     narration of the call. It has no `method` field, so rule 1 skips it. Rule 2 blanks the message
//     of a log entry whose callId belongs to an input verb: exact, because the callId is the same
//     identifier the call itself carries, with no matching on the text.
//   * `{"type":"frame-snapshot", …,["INPUT",{"__playwright_value_":"<secret>"},…]}` — the DOM
//     snapshot. Playwright records live input state in a synthetic attribute, because the DOM does
//     not serialise the `value` PROPERTY as an attribute. Rule 3 blanks that attribute wherever it
//     appears. It is Playwright's own name — one of thirty `__playwright_*` keys in this version, and the
//     only one carrying free text (the others hold booleans and offsets) — so this is not a guess
//     about the application's markup.
//
// PIXELS ARE NOT TOUCHED, by decision. Cleaning them means OCR plus masking — unreliable and
// expensive, and a redactor that half-works on screenshots is worse than one that says it does not
// try. The lever offered instead is `SENTINEL_TRACE_SCREENSHOTS=0`, which stops them being recorded
// at all: whoever needs confidentiality of frames turns them off, whoever needs the post-mortem keeps
// them.
//
// ⚠ A WINDOW REMAINS, and it is stated rather than papered over. Playwright writes the archive itself;
// there is no hook between "bytes hit the disk" and "we can read them". So the raw trace exists for as
// long as it takes to rewrite it — same process, immediately after. ADR-084 could avoid its window
// (discarding is a supported option); this one cannot, and the honest response is to say so and to
// FAIL CLOSED: a caller that cannot redact must delete.

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/AlexGromer/sentinel/internal/configguard"
)

// InputVerbs are the trace methods whose parameters carry text a person typed.
//
// Taken from the Playwright protocol names that appear in `trace.trace`, not from our own verb list:
// the file records what the driver did, and our `browser.fill` becomes `fill` there. `press` carries
// a key rather than a value, and a key can be a credential when a test types one character at a time.
var InputVerbs = map[string]bool{
	"fill": true, "type": true, "press": true, "insertText": true, "setInputFiles": true,
}

// inputParams are the parameter names those verbs use to carry the typed text.
var inputParams = []string{"value", "text", "key", "files"}

// Placeholder marks a value the redactor removed. Distinct from the log sink's `[REDACTED]` on
// purpose: a reader of a trace should be able to tell that the emptiness is OURS and deliberate,
// rather than the application having submitted an empty field.
const Placeholder = "[REDACTED-BY-SENTINEL]"

// isImage reports whether an archive entry is pixels, which are copied through untouched.
func isImage(name string) bool {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".jpeg", ".jpg", ".png", ".webp", ".gif", ".avif":
		return true
	}
	return false
}

// Stats is what a caller reports to a human. Counts, not content: a redactor that logged what it
// found would be a second copy of the leak.
type Stats struct {
	Entries      int // archive members seen
	Images       int // copied through untouched (pixels are not redacted — see the file comment)
	TypedValues  int // rule 1: parameters of an input verb
	NarratedLogs int // rule 2: the driver's own log line quoting such a call
	SnapshotVals int // rule 3: `__playwright_value_` in a DOM snapshot
	TextualLines int // lines the named/structural scanner changed
}

// snapshotValueAttr is Playwright's synthetic attribute for live input state. The DOM does not
// serialise the `value` PROPERTY as an attribute, so a snapshot that reproduced the page faithfully
// would lose what was typed — Playwright therefore records it under this name. It is one of ~30
// `__playwright_*` keys in this version and the only one carrying free text; the rest hold booleans
// and offsets, so there is nothing else here to redact and nothing to guess at.
const snapshotValueAttr = "__playwright_value_"

// TraceFile rewrites `path` in place with every text entry redacted.
//
// Atomic: it writes a sibling temp file and renames. A crash halfway must not leave a truncated
// archive where a valid one was, and must never leave a PARTIALLY redacted one that looks done.
func TraceFile(path string) (Stats, error) {
	var st Stats
	in, err := zip.OpenReader(path)
	if err != nil {
		return st, fmt.Errorf("open trace: %w", err)
	}
	defer in.Close()

	tmp, err := os.CreateTemp(filepath.Dir(path), ".trace-redact-*.zip")
	if err != nil {
		return st, fmt.Errorf("temp file: %w", err)
	}
	tmpName := tmp.Name()
	// Best-effort cleanup on every failure path below; a successful rename makes this a no-op.
	defer func() { _ = os.Remove(tmpName) }()

	out := zip.NewWriter(tmp)
	for _, f := range in.File {
		st.Entries++
		rc, err := f.Open()
		if err != nil {
			tmp.Close()
			return st, fmt.Errorf("read %s: %w", f.Name, err)
		}
		body, err := io.ReadAll(rc)
		rc.Close()
		if err != nil {
			tmp.Close()
			return st, fmt.Errorf("read %s: %w", f.Name, err)
		}
		if isImage(f.Name) {
			st.Images++
		} else {
			body = redactEntry(f.Name, body, &st)
		}
		// The header is copied so timestamps and the method survive; only the size changes.
		hdr := f.FileHeader
		w, err := out.CreateHeader(&hdr)
		if err != nil {
			tmp.Close()
			return st, fmt.Errorf("write %s: %w", f.Name, err)
		}
		if _, err := w.Write(body); err != nil {
			tmp.Close()
			return st, fmt.Errorf("write %s: %w", f.Name, err)
		}
	}
	if err := out.Close(); err != nil {
		tmp.Close()
		return st, fmt.Errorf("finish archive: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return st, fmt.Errorf("close temp: %w", err)
	}
	// Inherit the original's mode rather than the temp file's 0600-by-umask: the run directory is
	// already 0700, and silently tightening one file inside it would be a surprise, not a policy.
	if fi, err := os.Stat(path); err == nil {
		_ = os.Chmod(tmpName, fi.Mode().Perm())
	}
	if err := os.Rename(tmpName, path); err != nil {
		return st, fmt.Errorf("replace trace: %w", err)
	}
	return st, nil
}

// redactEntry applies the structural pass where the entry's shape allows it, and the textual pass
// everywhere.
func redactEntry(name string, body []byte, st *Stats) []byte {
	lines := bytes.Split(body, []byte("\n"))
	// Rule 2 needs to know which calls were input verbs BEFORE it can judge a log line, and the log
	// line may arrive before or after the call in the file. One cheap sweep first, so the decision is
	// never order-dependent.
	inputCalls := inputCallIDs(lines)
	for i, ln := range lines {
		if len(bytes.TrimSpace(ln)) == 0 {
			continue
		}
		// Structural first: a JSON line whose method is an input verb has its typed value removed
		// before the textual scanner ever sees it, so the outcome does not depend on what the value
		// happened to look like.
		if out, hit := redactTypedValues(ln); hit {
			st.TypedValues++
			lines[i] = out
			ln = out
		}
		if out, hit := redactNarration(ln, inputCalls); hit {
			st.NarratedLogs++
			lines[i] = out
			ln = out
		}
		if out, n := redactSnapshotValues(ln); n > 0 {
			st.SnapshotVals += n
			lines[i] = out
			ln = out
		}
		// The textual pass is STRUCTURE-AWARE on a JSON line and raw everywhere else.
		//
		// Running the raw line scanner over JSON corrupts it, and this is measured rather than
		// feared: `trace.network` contains `"cookies":[]`, `Secretish("cookies")` is true, so the
		// scanner replaced the value and produced `"cookies":[REDACTED]]` — two lines of a real
		// archive stopped being parseable. A trace the viewer cannot open is a worse outcome than a
		// trace with a secret in it, because the second at least still tells you something.
		if out, n := redactJSONText(ln); n > 0 {
			st.TextualLines += n
			lines[i] = out
		} else if !isJSONLine(ln) {
			if red := Line(string(ln)); red != string(ln) {
				st.TextualLines++
				lines[i] = []byte(red)
			}
		}
	}
	return bytes.Join(lines, []byte("\n"))
}

// redactTypedValues blanks the input-carrying parameters of one trace line.
//
// Re-encoded through encoding/json rather than patched as text: a value containing a quote or a brace
// would make string surgery produce an archive the trace viewer cannot open, and an unreadable trace
// is a worse outcome than a redacted one. A line that is not JSON, or not an input verb, is returned
// untouched and left to the textual pass.
func redactTypedValues(line []byte) ([]byte, bool) {
	var rec map[string]json.RawMessage
	if err := json.Unmarshal(line, &rec); err != nil {
		return line, false
	}
	var method string
	if raw, ok := rec["method"]; ok {
		_ = json.Unmarshal(raw, &method)
	}
	if !InputVerbs[method] {
		return line, false
	}
	rawParams, ok := rec["params"]
	if !ok {
		return line, false
	}
	var params map[string]json.RawMessage
	if json.Unmarshal(rawParams, &params) != nil {
		return line, false
	}
	changed := false
	for _, k := range inputParams {
		raw, present := params[k]
		if !present {
			continue
		}
		// Already redacted: skip, so a second pass reports finding nothing rather than claiming a hit
		// it did not make. The bytes would be identical either way — the lie would be in the COUNT,
		// and a statistic that overstates a security control is the kind of number this codebase has
		// spent the day removing.
		var cur string
		if json.Unmarshal(raw, &cur) == nil && cur == Placeholder {
			continue
		}
		b, err := json.Marshal(Placeholder)
		if err != nil {
			continue
		}
		params[k] = b
		changed = true
	}
	if !changed {
		return line, false
	}
	nb, err := json.Marshal(params)
	if err != nil {
		return line, false
	}
	rec["params"] = nb
	out, err := json.Marshal(rec)
	if err != nil {
		return line, false
	}
	return out, true
}

// inputCallIDs collects the callId of every input-verb call in one file.
//
// Separate sweep because `trace.trace` interleaves a call with the driver's narration of it, and the
// narration can precede the call record. Judging a log line by the text it happens to contain would
// be the guess this design avoids everywhere else.
func inputCallIDs(lines [][]byte) map[string]bool {
	ids := map[string]bool{}
	for _, ln := range lines {
		var rec struct {
			Method string `json:"method"`
			CallID string `json:"callId"`
		}
		if json.Unmarshal(ln, &rec) != nil {
			continue
		}
		if rec.CallID != "" && InputVerbs[rec.Method] {
			ids[rec.CallID] = true
		}
	}
	return ids
}

// redactNarration blanks the message of a log entry that belongs to an input-verb call.
//
// The driver writes `{"type":"log","callId":"call@18","message":"  fill(\"<secret>\")"}` — its own
// account of the call, carrying the argument verbatim. Matched by callId, never by the text: the same
// identifier the call record carries, so a narration in another language or another format is still
// caught, and an unrelated log line never is.
func redactNarration(line []byte, inputCalls map[string]bool) ([]byte, bool) {
	if len(inputCalls) == 0 {
		return line, false
	}
	var rec map[string]json.RawMessage
	if json.Unmarshal(line, &rec) != nil {
		return line, false
	}
	var typ, callID string
	if raw, ok := rec["type"]; ok {
		_ = json.Unmarshal(raw, &typ)
	}
	if raw, ok := rec["callId"]; ok {
		_ = json.Unmarshal(raw, &callID)
	}
	if typ != "log" || !inputCalls[callID] {
		return line, false
	}
	rawMsg, ok := rec["message"]
	if !ok {
		return line, false
	}
	var cur string
	if json.Unmarshal(rawMsg, &cur) == nil && cur == Placeholder {
		return line, false // already redacted — see redactTypedValues
	}
	b, err := json.Marshal(Placeholder)
	if err != nil {
		return line, false
	}
	rec["message"] = b
	out, err := json.Marshal(rec)
	if err != nil {
		return line, false
	}
	return out, true
}

// redactSnapshotValues blanks every `__playwright_value_` attribute in a DOM snapshot.
//
// The snapshot is a deeply nested mix of arrays and objects, so the walk is generic rather than
// path-based: a shape-specific traversal would silently stop finding values the day Playwright nests
// them one level deeper, and stopping silently is the failure this whole file is about.
func redactSnapshotValues(line []byte) ([]byte, int) {
	if !bytes.Contains(line, []byte(snapshotValueAttr)) {
		return line, 0 // the common case: no decode, no re-encode
	}
	var doc interface{}
	if json.Unmarshal(line, &doc) != nil {
		return line, 0
	}
	n := 0
	var walk func(v interface{}) interface{}
	walk = func(v interface{}) interface{} {
		switch t := v.(type) {
		case map[string]interface{}:
			for k, sub := range t {
				if k == snapshotValueAttr {
					if _, already := sub.(string); already && sub.(string) == Placeholder {
						continue
					}
					t[k] = Placeholder
					n++
					continue
				}
				t[k] = walk(sub)
			}
			return t
		case []interface{}:
			for i := range t {
				t[i] = walk(t[i])
			}
			return t
		}
		return v
	}
	doc = walk(doc)
	out, err := json.Marshal(doc)
	if err != nil {
		return line, 0
	}
	return out, n
}

// isJSONLine reports whether a line is a JSON document, and therefore must not be edited as text.
func isJSONLine(line []byte) bool {
	var v interface{}
	return json.Unmarshal(line, &v) == nil
}

// redactJSONText applies the credential vocabulary to a JSON line WITHOUT text surgery.
//
// Two rules while walking, and they cover the two shapes a secret takes in these files:
//
//   - an object member whose KEY is credential-shaped and whose value is a string — `{"password":"…"}`
//     in a POST body. The raw scanner found these by reading `name<sep>value` out of the text; walking
//     the structure finds the same pairs without the possibility of consuming a bracket.
//   - a string LEAF containing a structurally distinctive credential — `"Bearer …"`, a JWT. `Line`
//     already knows those shapes, so it is applied to the leaf rather than reimplemented.
//
// Returns the count of values changed, and 0 for a line that is not JSON, so the caller knows to fall
// back to the raw scanner for `resources/*.html` and `trace.stacks`.
func redactJSONText(line []byte) ([]byte, int) {
	var doc interface{}
	if json.Unmarshal(line, &doc) != nil {
		return line, 0
	}
	n := 0
	var walk func(v interface{}) interface{}
	walk = func(v interface{}) interface{} {
		switch t := v.(type) {
		case map[string]interface{}:
			for k, sub := range t {
				if s, isStr := sub.(string); isStr {
					if configguard.Secretish(k) {
						if s != Placeholder {
							t[k] = Placeholder
							n++
						}
						continue
					}
					if red := Line(s); red != s {
						t[k] = red
						n++
					}
					continue
				}
				t[k] = walk(sub)
			}
			return t
		case []interface{}:
			for i := range t {
				if s, isStr := t[i].(string); isStr {
					if red := Line(s); red != s {
						t[i] = red
						n++
					}
					continue
				}
				t[i] = walk(t[i])
			}
			return t
		case string:
			if red := Line(t); red != t {
				n++
				return red
			}
			return t
		}
		return v
	}
	doc = walk(doc)
	if n == 0 {
		return line, 0
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return line, 0
	}
	return out, n
}
