// Write-side redaction for the log sink (GAP-SEC-005, ADR-081).
//
// ADR-067 gave seven `app.*` codes the job of carrying what the APPLICATION UNDER TEST printed into
// runs/<id>/logs/. That is exactly the point — "is my app misbehaving or the tool?" is unanswerable
// without it — but it also means we write someone else's output into our own files: a console.log
// carrying a session token, a URL with ?token=, the body of a 5xx, the text of a dialog. ADR-072 then
// made that stream VERDICT-BEARING, so we write more of it, more often. Nothing redacted it: the sink
// stored the message exactly as it arrived, protected only by 0700 on the directory and a bearer token
// on the read endpoint. Any `tar runs/<id>` or log pasted into a bug report carried the lot.
//
// WHAT IS AND IS NOT MATCHED, and why the difference matters more than the list.
//
// Only NAMED secrets and one STRUCTURALLY distinctive shape (a JWT) are redacted. There is deliberately
// no entropy or "long hex/base64" heuristic, even though GAPS.md proposed one, because our own audit
// trail is made of exactly that shape: `run_id=868db3496d004a1c`, `plan_hash=70a6f8f6c1cc110f`,
// `dom_hash`, screenshot hashes — 16 and 64 hex characters. A rule broad enough to catch an unknown
// token would eat the identifiers these files exist to preserve, and it would do it silently. Better a
// redactor whose limits are stated than one that quietly destroys the evidence.
//
// The vocabulary of "what counts as a credential name" is NOT redefined here. It is
// configguard.Secretish — the same function that refuses a secret-shaped member in a config document —
// so the two cannot drift into disagreeing about the word "token". That function already carries the
// reasoning this would otherwise have to repeat: `api-key`/`api key`/`api.key` all canonicalize to
// `api_key`, while `max_tokens` and `total_tokens` are counters and must survive.
package redact

// MOVED here from cmd/control-api in ADR-098, unchanged apart from the package clause and the
// exported name. It had to move because a SECOND consumer appeared — the trace redactor — and the one
// thing this file must never become is two files. "What counts as a credential" is already shared
// with configguard for exactly that reason; copying the scanner would have re-introduced the drift a
// package boundary was chosen to prevent.

import (
	"regexp"
	"strings"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
	"github.com/AlexGromer/sentinel/internal/configguard"
)

const redactedMark = "[REDACTED]"

var (
	// `Bearer <credential>` in any casing. Unambiguous: nothing in our own output emits this shape.
	reBearer = regexp.MustCompile(`(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{8,})`)

	// A JWT: three base64url segments, the first of which starts with the `{"` header signature `eyJ`.
	// Structurally distinctive enough to need no name beside it, which is what makes it worth matching
	// at all — an unnamed credential is otherwise invisible to this redactor by design.
	reJWT = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*`)

	// `name=value` / `name: value` / `"name": "value"` is NOT matched with a regex, and that is a
	// deliberate correction rather than a style choice — see scanNamedSecrets.
)

// Line removes credential-shaped material from one line of captured output.
//
// Applied to EVERY line the sink sees, not only the `app.*` ones: our own diagnostics quote URLs and
// request bodies too, and a redactor that trusted the source would be trusting the very thing it is
// there to check. A pure string function, so it is testable without a browser, a run or a disk.
func Line(line string) string {
	if line == "" {
		return line
	}
	out := reBearer.ReplaceAllString(line, "$1 "+redactedMark)
	out = reJWT.ReplaceAllString(out, redactedMark)
	out = scanNamedSecrets(out)
	// `Authorization: Bearer <jwt>` is redacted twice — once by reBearer for the credential, once by the
	// scanner because `authorization` is itself a credential-shaped name — and lands as
	// `Authorization: [REDACTED] [REDACTED]`. Nothing leaked either way; the collapse is purely so the
	// line reads like a redaction rather than like a bug in the redactor.
	for strings.Contains(out, redactedMark+" "+redactedMark) {
		out = strings.ReplaceAll(out, redactedMark+" "+redactedMark, redactedMark)
	}
	return out
}

// isOurEventCode reports whether `name` is a catalogued event code — in which case the `code:` that
// opens every one of our log lines is a LABEL, not a credential assignment.
//
// The defect this fixes, measured 2026-08-04 on a real run and caught by a SCREENSHOT rather than by
// any gate: the sink writes `[warn|llm] llm.no_anthropic_key: No AI key (planner) …`, the scanner read
// `llm.no_anthropic_key` as a credential name (it ends in `key`) and blanked what followed the colon —
// so the product's own message reached the operator as `[REDACTED] AI key (planner)`, having lost its
// first word. `fatal.secret_would_leak_to_trace` lost "Stopped:" the same way. Both are codes whose
// names mention credentials precisely because their MESSAGES are about credential handling, which is
// the worst possible set to silently truncate.
//
// This is not a hole. Nothing is skipped: the message itself is still scanned in full, and a genuine
// `api_key=…` further along the same line is still redacted. Only the code TOKEN stops being read as
// a name. The exemption is keyed on the catalogue rather than on the line's shape, so a hostile line
// from the tested application cannot claim it by imitating our prefix — it would have to name a real
// code, and real codes label our sentences, not someone's credential.
func isOurEventCode(name string) bool {
	_, ok := eventcatalog.Lookup(name)
	return ok
}

// isNameChar reports whether a byte may appear in a credential NAME as written in a log line.
func isNameChar(c byte) bool {
	return c == '_' || c == '.' || c == '-' ||
		(c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
}

// isValueEnd reports the delimiters a value cannot cross.
func isValueEnd(c byte) bool {
	switch c {
	case ' ', '\t', '"', '\'', ',', ';', '&', ')', ']', '}', '<', '>':
		return true
	}
	return false
}

// scanNamedSecrets redacts the VALUE of every `name<sep>value` pair whose name is credential-shaped.
//
// Hand-written rather than a regex, because a regex match CONSUMES the span it covers. A log line reads
// `[app] console: session_token=SECRET`: the outer pair `console: session_token=SECRET` matches first,
// `console` is not credential-shaped so the pair is left alone — and the scan resumes AFTER it, past
// the secret it existed to find. The same swallowing happens with `https://host/cb?token=SECRET`.
// Both were caught by the gate, and both are the kind of miss that makes a security control worse than
// useless: it reports success while the credential is on disk.
//
// The scanner walks every separator independently, so an unrelated colon can never hide a later
// assignment. Names are judged by configguard.Secretish — one vocabulary for the whole codebase.
func scanNamedSecrets(line string) string {
	var b strings.Builder
	i, n := 0, len(line)
	for i < n {
		c := line[i]
		if c != '=' && c != ':' {
			b.WriteByte(c)
			i++
			continue
		}
		// Backward: skip spaces, then take the name that ends where we stand.
		j := len(b.String())
		cur := b.String()
		k := j
		for k > 0 && cur[k-1] == ' ' {
			k--
		}
		// A JSON member name arrives as `"password":` — step over the closing quote so the name itself
		// is reachable. The opening quote stays in the builder and is reproduced verbatim, which is what
		// keeps a redacted run.jsonl record valid JSON for the Logs view to parse.
		if k > 0 && cur[k-1] == '"' {
			k--
		}
		nameEnd := k
		for k > 0 && isNameChar(cur[k-1]) {
			k--
		}
		name := cur[k:nameEnd]
		if name == "" || !configguard.Secretish(name) || isOurEventCode(name) {
			b.WriteByte(c)
			i++
			continue
		}
		// Forward: the separator, any spacing, then the value.
		p := i + 1
		for p < n && line[p] == ' ' {
			p++
		}
		if p < n && line[p] == '"' { // quoted: keep the quotes, replace what is between them
			q := p + 1
			for q < n && line[q] != '"' {
				q++
			}
			if q > p+1 {
				b.WriteString(line[i:p] + `"` + redactedMark + `"`)
				i = q + 1
				continue
			}
		}
		q := p
		for q < n && !isValueEnd(line[q]) {
			q++
		}
		// No exclusion for a leading `/` here. The regex version needed one, because `https://host?…`
		// could match with `https` as the NAME; the scanner cannot, since it redacts only when
		// configguard.Secretish(name) holds and `https` is not credential-shaped. Keeping the exclusion
		// after the rewrite would have been strictly harmful — it would let `api_key=/opt/creds/id_rsa`
		// through. A mutation caught it: deleting the guard broke no test, which is what a guard that
		// has stopped guarding anything looks like.
		if q > p {
			b.WriteString(line[i:p] + redactedMark)
			i = q
			continue
		}
		b.WriteByte(c)
		i++
	}
	return b.String()
}
