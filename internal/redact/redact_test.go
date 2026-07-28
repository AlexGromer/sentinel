package redact

import (
	"encoding/json"
	"strings"
	"testing"
)

// The fixture VALUES below are deliberately unrealistic placeholders. The redactor keys on the NAME
// (configguard.Secretish) and never on the shape of the value, so a realistic-looking credential would
// test nothing extra — it would only add a high-entropy literal for the pre-commit secret scanner to
// flag, training us to wave away its findings. One such literal was written here first and gitleaks
// caught it, which is the gate working.
//
// GAP-SEC-005 / ADR-081. ADR-067 made the log sink carry what the APPLICATION UNDER TEST printed, and
// ADR-072 made that stream verdict-bearing — so we write someone else's output into our own files,
// more of it and more often, with nothing removing credentials on the way in.
//
// The two halves of this gate matter equally. Redacting a secret is easy; NOT redacting our own audit
// trail is what makes the redactor usable, because run_id/plan_hash/dom_hash are 16- and 64-character
// hex — exactly the shape any entropy heuristic would eat.

func TestRedactsNamedAndStructuralSecrets(t *testing.T) {
	for _, tc := range []struct{ name, in, mustNotContain string }{
		{"bearer header", `GET /v1/runs -H "Authorization: Bearer sk-live-abc123def456"`, "sk-live-abc123def456"},
		{"bearer lowercase", `authorization: bearer aVeryLongCredential99`, "aVeryLongCredential99"},
		{"jwt anywhere", `[app] console: user=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27u`, "dBjftJeZ4CVPmB92K27u"},
		{"query token", `[app] navigating to https://app.example/cb?token=SECRETVALUE123&next=/home`, "SECRETVALUE123"},
		{"query api key", `GET https://api.example/v1?api_key=AKIAIOSFODNN7EXAMPLE`, "AKIAIOSFODNN7EXAMPLE"},
		{"json password", `[app] console: {"user":"demo","password":"hunter2plaintext"}`, "hunter2plaintext"},
		{"prose assignment", `[app] console: session_token=NOT-A-REAL-TOKEN-PLACEHOLDER`, "NOT-A-REAL-TOKEN-PLACEHOLDER"},
		{"hyphenated name", `[app] console: api-key=THE-REAL-KEY-VALUE`, "THE-REAL-KEY-VALUE"},
		{"cookie", `[app] set-cookie: sessionid=zzzTOPSECRETzzz; Path=/`, "zzzTOPSECRETzzz"},
		// A credential whose VALUE looks like a path. The first draft excluded slash-leading values —
		// a guard the regex version needed and the scanner does not — and it let exactly this through.
		{"secret value that is a path", `[app] console: api_key=/opt/creds/id_rsa_prod`, "/opt/creds/id_rsa_prod"},
		{"token value with a slash", `[app] console: access_token=ab/cd+ef==`, "ab/cd+ef=="},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := Line(tc.in)
			if strings.Contains(got, tc.mustNotContain) {
				t.Fatalf("secret survived redaction:\n in:  %s\n out: %s", tc.in, got)
			}
			if !strings.Contains(got, redactedMark) {
				t.Fatalf("nothing was marked as redacted, so the line may simply have been dropped: %s", got)
			}
		})
	}
}

// The half that decides whether anyone can use these files afterwards. Every string below is real
// output this system emits, and each one is the shape a broad entropy rule would destroy.
func TestLeavesOurOwnAuditTrailIntact(t *testing.T) {
	for _, tc := range []struct{ name, line, mustSurvive string }{
		{"run id", `[agentctl] run_id=868db3496d004a1c mode=replay planner=heuristic`, "868db3496d004a1c"},
		{"plan hash", `EXPLORE COMPLETE — 8 steps, coverage=0.43, plan_hash=70a6f8f6c1cc110f`, "70a6f8f6c1cc110f"},
		{"dom hash", `{"dom_hash":"cd8b14c2303c7827","step":2}`, "cd8b14c2303c7827"},
		{"sha256", `golden a11y_hash=` + strings.Repeat("ab", 32), strings.Repeat("ab", 32)},
		{"token counters", `[llm] max_tokens=800 total_tokens=551 prompt_tokens=215`, "551"},
		{"a url without credentials", `[app] navigating to file:///app/testdata/fixtures/l2.html`, "l2.html"},
		{"element name", `[plan] click button 'Confirm payment'`, "Confirm payment"},
		{"a store socket path", `[store-gateway] listening on unix:/opt/x/state/sentinel-store-868db3496d004a1c.sock`, "868db3496d004a1c"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := Line(tc.line)
			if !strings.Contains(got, tc.mustSurvive) {
				t.Fatalf("redaction ate the audit trail:\n in:  %s\n out: %s\n lost: %s", tc.line, got, tc.mustSurvive)
			}
		})
	}
}

// `max_tokens` and `total_tokens` are counters, not credentials — the distinction lives in
// configguard.Secretish and is reused rather than reimplemented, so the two cannot drift apart.
func TestCounterNamesAreNotTreatedAsCredentials(t *testing.T) {
	line := `[llm] usage: {"max_tokens": 800, "total_tokens": 551, "api_key": "sk-REAL"}`
	got := Line(line)
	if strings.Contains(got, "sk-REAL") {
		t.Fatalf("api_key survived: %s", got)
	}
	for _, keep := range []string{"800", "551"} {
		if !strings.Contains(got, keep) {
			t.Fatalf("a token COUNTER was redacted (%s): %s", keep, got)
		}
	}
}

// run.jsonl is parsed by the Logs view, so a redacted record has to remain valid JSON — otherwise the
// fix for a secret leak becomes an outage of the diagnostics.
func TestARedactedJSONRecordStaysParseable(t *testing.T) {
	line := `{"seq":7,"lvl":"error","cat":"app","code":"app.console_error","msg":"login failed","password":"hunter2","url":"https://x/cb?token=ABC123DEF"}`
	got := Line(line)
	var m map[string]any
	if err := json.Unmarshal([]byte(got), &m); err != nil {
		t.Fatalf("redaction broke the JSON record: %v\n%s", err, got)
	}
	if m["password"] != redactedMark {
		t.Fatalf("password not redacted: %v", m["password"])
	}
	if s, _ := m["url"].(string); strings.Contains(s, "ABC123DEF") {
		t.Fatalf("query token survived inside the JSON value: %v", m["url"])
	}
	// The record must still be USEFUL: identity and diagnosis fields untouched.
	if m["seq"] != float64(7) || m["code"] != "app.console_error" || m["msg"] != "login failed" {
		t.Fatalf("redaction damaged the diagnostic fields: %v", m)
	}
}

func TestOverlappingRedactionsCollapse(t *testing.T) {
	got := Line(`[app] console: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SIG`)
	if strings.Contains(got, redactedMark+" "+redactedMark) {
		t.Fatalf("double marker survived: %s", got)
	}
	if !strings.Contains(got, redactedMark) {
		t.Fatalf("the credential was not redacted at all: %s", got)
	}
	if strings.Contains(got, "SIG") {
		t.Fatalf("the JWT signature survived: %s", got)
	}
}
