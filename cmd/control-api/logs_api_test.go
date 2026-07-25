package main

// Gates for the log-reading surface (M9-LIVE). Collapsing lives here rather than in the sink, so
// this is where the "34 repeats read as one row with a count" claim is actually proven.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeRunLogs lays down a run.jsonl the way the sink would, and returns the run id.
func writeRunLogs(t *testing.T, repo string, lines []string) string {
	t.Helper()
	id := "aaaaaaaaaaaaaaaa"
	dir := filepath.Join(repo, "runs", "control-"+id, "logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "run.jsonl"),
		[]byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return id
}

// getLogs calls the endpoint and decodes the envelope.
func getLogs(t *testing.T, repo, id, query string) map[string]any {
	t.Helper()
	s := &server{token: "secret-tok", repo: repo, runs: map[string]*run{}}
	r := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/logs"+query, nil)
	r.Header.Set("Authorization", "Bearer secret-tok")
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET logs%s: got %d body=%s", query, rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v (%s)", err, rec.Body.String())
	}
	return out
}

func recordsOf(t *testing.T, env map[string]any) []logRecord {
	t.Helper()
	raw, err := json.Marshal(env["records"])
	if err != nil {
		t.Fatal(err)
	}
	var recs []logRecord
	if err := json.Unmarshal(raw, &recs); err != nil {
		t.Fatalf("records: %v", err)
	}
	return recs
}

// THE point of the feature: a run that looped 34 times reads as one row with a count. The stuck run
// that motivated all of this said nothing about being stuck.
func TestLogsCollapseConsecutiveRepeats(t *testing.T) {
	repo := t.TempDir()
	lines := []string{`{"seq":1,"ts":"t","lvl":"info","cat":"run","code":"run.config","msg":"started"}`}
	for i := 0; i < 34; i++ {
		lines = append(lines,
			`{"seq":`+itoa(i+2)+`,"ts":"t","lvl":"debug","cat":"heal","code":"heal.explore_stub","msg":"no healing in explore"}`)
	}
	lines = append(lines, `{"seq":36,"ts":"t","lvl":"info","cat":"run","code":"run.store_mode","msg":"local"}`)
	id := writeRunLogs(t, repo, lines)

	env := getLogs(t, repo, id, "")
	recs := recordsOf(t, env)
	if len(recs) != 3 {
		t.Fatalf("34 identical records must read as one row (want 3), got %d: %+v", len(recs), recs)
	}
	if recs[1].Code != "heal.explore_stub" || recs[1].N != 34 {
		t.Fatalf("want the loop as one row with n=34, got %+v", recs[1])
	}
	// `scanned` reports the truth of the file, so the count is auditable against it.
	if got := env["scanned"].(float64); got != 36 {
		t.Fatalf("scanned should report every record on disk (36), got %v", got)
	}
}

// Only CONSECUTIVE identical records merge: interleaved ones mean the run was doing different things,
// and merging those would rewrite history.
func TestLogsDoNotCollapseInterleaved(t *testing.T) {
	repo := t.TempDir()
	id := writeRunLogs(t, repo, []string{
		`{"seq":1,"lvl":"debug","cat":"heal","code":"heal.explore_stub","msg":"same"}`,
		`{"seq":2,"lvl":"info","cat":"run","code":"run.store_mode","msg":"local"}`,
		`{"seq":3,"lvl":"debug","cat":"heal","code":"heal.explore_stub","msg":"same"}`,
	})
	if recs := recordsOf(t, getLogs(t, repo, id, "")); len(recs) != 3 {
		t.Fatalf("non-consecutive repeats must stay separate, got %d: %+v", len(recs), recs)
	}
}

// A run with no log file must be distinguishable from a run whose logs matched nothing. Answering an
// empty 200 for both is the exact defect the library/results endpoints had earlier in this milestone —
// "not loading" and "nothing there" looked identical.
func TestLogsDistinguishNotRecordedFromNoMatch(t *testing.T) {
	repo := t.TempDir()

	env := getLogs(t, repo, "bbbbbbbbbbbbbbbb", "")
	if env["recorded"].(bool) {
		t.Fatal("a run with no log file must report recorded=false")
	}
	if env["reason"] == nil || env["reason"].(string) == "" {
		t.Fatal("recorded=false must come with a reason the operator can read")
	}

	id := writeRunLogs(t, repo, []string{
		`{"seq":1,"lvl":"info","cat":"run","code":"run.config","msg":"started"}`,
	})
	env = getLogs(t, repo, id, "?lvl=error")
	if !env["recorded"].(bool) {
		t.Fatal("a run WITH a log file must report recorded=true even when nothing matches")
	}
	if recs := recordsOf(t, env); len(recs) != 0 {
		t.Fatalf("no record is error-level here, got %+v", recs)
	}
}

// Filters are AND-combined, and `lvl` is a MINIMUM so "show me problems" is one parameter.
func TestLogsFilters(t *testing.T) {
	repo := t.TempDir()
	id := writeRunLogs(t, repo, []string{
		`{"seq":1,"lvl":"debug","cat":"heal","mod":"brain.graph","code":"heal.explore_stub","msg":"stub"}`,
		`{"seq":2,"lvl":"warn","cat":"llm","mod":"brain.llm","code":"llm.no_anthropic_key","msg":"No AI key"}`,
		`{"seq":3,"lvl":"error","cat":"browser","code":"browser.pipe_closed","msg":"pipe closed"}`,
		`{"seq":4,"lvl":"info","cat":"run","code":"run.store_mode","msg":"local"}`,
	})
	for _, tc := range []struct {
		query string
		want  int
	}{
		{"", 4},
		{"?lvl=warn", 2},            // warn AND error
		{"?lvl=error", 1},           //
		{"?cat=llm", 1},             //
		{"?mod=brain.llm", 1},       //
		{"?code=run.store_mode", 1}, //
		{"?q=ai+key", 1},            // case-insensitive substring of the message
		{"?q=NOTHING", 0},           //
		{"?after=3", 1},             // seq is exclusive, for tailing
		{"?lvl=warn&cat=llm", 1},    // AND
		{"?limit=2", 2},             //
	} {
		t.Run(tc.query, func(t *testing.T) {
			if recs := recordsOf(t, getLogs(t, repo, id, tc.query)); len(recs) != tc.want {
				t.Fatalf("%s: got %d records want %d (%+v)", tc.query, len(recs), tc.want, recs)
			}
		})
	}
}

// Degradations are gathered from the WHOLE file, never only the page returned — a paged-out or
// filtered-out degradation would let a run that never used the LLM look clean on its verdict.
func TestLogsDegradationsSpanWholeFileNotPage(t *testing.T) {
	repo := t.TempDir()
	id := writeRunLogs(t, repo, []string{
		`{"seq":1,"lvl":"warn","cat":"llm","code":"llm.no_anthropic_key","msg":"No AI key","degrades":true}`,
		`{"seq":2,"lvl":"info","cat":"run","code":"run.store_mode","msg":"local"}`,
		`{"seq":3,"lvl":"warn","cat":"heal","code":"heal.budget_exhausted","msg":"spent","degrades":true}`,
	})
	// A filter that excludes both degradations, and a limit that would page them out anyway.
	env := getLogs(t, repo, id, "?cat=run&limit=1")
	raw, _ := json.Marshal(env["degradations"])
	var degs []string
	_ = json.Unmarshal(raw, &degs)
	if len(degs) != 2 || degs[0] != "llm.no_anthropic_key" || degs[1] != "heal.budget_exhausted" {
		t.Fatalf("degradations must survive filtering and paging, got %v", degs)
	}
}

// The endpoint is token-gated like the SSE stream: logs are more revealing than a bare status poll.
func TestLogsRequireToken(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	r := httptest.NewRequest(http.MethodGet, "/v1/runs/aaaaaaaaaaaaaaaa/logs", nil)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("unauthenticated logs read: got %d want 403", rec.Code)
	}
}

// A run id that is not a bare id is refused before any path is built, so it can never walk out of
// runs/. `validRunID` deliberately admits `-` and `_` (recorder session ids use them), so the guard
// under test is the charset and length, not "looks like hex".
func TestLogsRejectBadRunID(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	for _, bad := range []string{
		"..%2f..%2fetc",         // encoded traversal — must not decode into a path
		"a.b",                   // a dot is not in the charset, so no ../ can ever form
		"a/b",                   //
		strings.Repeat("a", 99), // over the 64-char bound
		"привет",                // non-ASCII
	} {
		t.Run(bad, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/v1/runs/"+bad+"/logs", nil)
			r.Header.Set("Authorization", "Bearer secret-tok")
			rec := httptest.NewRecorder()
			s.mux().ServeHTTP(rec, r)
			if rec.Code == http.StatusOK {
				t.Fatalf("run id %q must be refused, got 200: %s", bad, rec.Body.String())
			}
		})
	}
}

// A torn last line — a live run writing while the endpoint reads — must not fail the whole request.
func TestLogsToleratePartialLastLine(t *testing.T) {
	repo := t.TempDir()
	id := writeRunLogs(t, repo, []string{
		`{"seq":1,"lvl":"info","cat":"run","code":"run.config","msg":"started"}`,
		`{"seq":2,"lvl":"info","cat":"run","code":"run.st`, // cut mid-write
	})
	if recs := recordsOf(t, getLogs(t, repo, id, "")); len(recs) != 1 {
		t.Fatalf("a torn line must be skipped, not fatal: got %+v", recs)
	}
}

// The catalogue endpoint serves the shipped bytes, unauthenticated, so the browser can render the
// reader's language without the server doing i18n.
func TestEventsCatalogServesBilingualBytes(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/events-catalog", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("catalogue: got %d", rec.Code)
	}
	var cat struct {
		Events map[string]struct {
			RU string `json:"ru"`
			EN string `json:"en"`
		} `json:"events"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &cat); err != nil {
		t.Fatalf("catalogue is not valid JSON: %v", err)
	}
	e, ok := cat.Events["llm.no_anthropic_key"]
	if !ok {
		t.Fatal("the catalogue must carry the diagnostics the UI has to render")
	}
	if e.RU == "" || e.EN == "" {
		t.Fatalf("both languages must reach the browser, got ru=%q en=%q", e.RU, e.EN)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
