package main

// Gates for the log boundary (M9-LIVE). The claims under test are the ones the feature exists for:
// the narrative leaves the diagnostics file, a repeated line collapses into a count, a foreign stack
// trace becomes one record, nothing is lost from the raw stream, and a degradation is reachable for
// the verdict.

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// drain feeds lines through a sink and returns the parsed diagnostics plus the raw file contents.
func drain(t *testing.T, lines []string) (recs []logRecord, rawText, eventsText string, sink *logSink) {
	t.Helper()
	dir := t.TempDir()
	sink = newLogSink(dir)
	if sink == nil {
		t.Fatal("newLogSink returned nil for a writable temp dir")
	}
	for _, l := range lines {
		sink.write(l)
	}
	sink.close()

	logs := filepath.Join(dir, "logs")
	f, err := os.Open(filepath.Join(logs, "run.jsonl"))
	if err != nil {
		t.Fatalf("open run.jsonl: %v", err)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		var r logRecord
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			t.Fatalf("run.jsonl line is not valid JSON: %v (%s)", err, sc.Text())
		}
		recs = append(recs, r)
	}
	raw, _ := os.ReadFile(filepath.Join(logs, "run.log"))
	ev, _ := os.ReadFile(filepath.Join(logs, "events.jsonl"))
	return recs, string(raw), string(ev), sink
}

// The single most load-bearing claim: AG-UI frames were 82% of a run's output and are NOT log noise.
// They must land in their own file, stripped of the stdout convention, and must never appear among
// the diagnostics.
func TestSinkRoutesNarrativeOutOfDiagnostics(t *testing.T) {
	recs, raw, events, _ := drain(t, []string{
		`@@AGUI {"type":"run.started","run_id":"abc","seq":1}`,
		`[info|run] run.config: Run abc started: mode explore`,
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":2,"data":{"n":2,"total":40}}`,
	})
	for _, r := range recs {
		if strings.Contains(r.Msg, "@@AGUI") || strings.Contains(r.Raw, "@@AGUI") {
			t.Fatalf("an AG-UI frame reached the diagnostics: %+v", r)
		}
	}
	if len(recs) != 1 || recs[0].Code != "run.config" {
		t.Fatalf("want exactly the one diagnostic, got %+v", recs)
	}
	if strings.Count(events, "\n") != 2 {
		t.Fatalf("events.jsonl should hold both frames, got:\n%s", events)
	}
	if strings.Contains(events, "@@AGUI") {
		t.Fatalf("events.jsonl must be clean JSONL without the stdout prefix, got:\n%s", events)
	}
	// The raw stream keeps everything verbatim — the split must never lose a byte.
	for _, want := range []string{"@@AGUI", "run.config"} {
		if !strings.Contains(raw, want) {
			t.Fatalf("run.log lost %q; it must be the stream 1:1:\n%s", want, raw)
		}
	}
}

// The disk holds EVERY record, uncollapsed and unstamped with a count. Collapsing is the reader's
// job (see the endpoint tests): a first attempt held records back on the write side to count repeats,
// and a live run showed that keeps a stuck run out of its own log file for as long as the loop lasts.
func TestSinkWritesEveryRepeatImmediately(t *testing.T) {
	lines := []string{`[info|run] run.config: Run abc started: mode explore`}
	for i := 0; i < 34; i++ {
		lines = append(lines, `[debug|heal] heal.explore_stub: Healing does not apply during explore`)
	}
	lines = append(lines, `[info|run] run.store_mode: Store: local`)
	recs, _, _, _ := drain(t, lines)

	if len(recs) != 36 {
		t.Fatalf("the sink must append every record (want 36), got %d", len(recs))
	}
	for i, r := range recs {
		if r.Seq != i+1 {
			t.Fatalf("seq must be dense and monotonic: record %d has seq %d", i, r.Seq)
		}
		if r.N != 0 {
			t.Fatalf("the count is the reader's field, never written to disk: %+v", r)
		}
		if r.TS == "" {
			t.Fatalf("every record needs a timestamp: %+v", r)
		}
	}
}

// A record must be readable BEFORE the run ends and before close() — otherwise a stuck run, or one
// killed mid-loop, shows nothing at all. This is the defect the first design had.
func TestSinkRecordIsVisibleWithoutClose(t *testing.T) {
	dir := t.TempDir()
	s := newLogSink(dir)
	if s == nil {
		t.Fatal("newLogSink returned nil")
	}
	t.Cleanup(s.close)
	s.write(`[warn|llm] llm.no_anthropic_key: No AI key (planner)`)

	b, err := os.ReadFile(filepath.Join(dir, "logs", "run.jsonl"))
	if err != nil || !strings.Contains(string(b), "llm.no_anthropic_key") {
		t.Fatalf("a record must hit the file as it arrives, not at close: err=%v content=%q", err, b)
	}
}

// The 23-line Node EPIPE stack trace looked like a product crash and was really a pipe closing at
// teardown. It has to read as one problem, with the frames kept for whoever wants them.
func TestSinkCollapsesForeignStackTrace(t *testing.T) {
	recs, _, _, _ := drain(t, []string{
		`[pw-executor] browser launched (headless)`,
		`node:events:487`,
		`Error: write EPIPE`,
		`    at Socket._write (node:net:1039:8)`,
		`    at writeOrBuffer (node:internal/streams/writable:570:12)`,
		`[info|run] run.store_mode: Store: local`,
	})
	var browser, pipe, store, pipeSeq int
	for _, r := range recs {
		switch r.Code {
		case "browser.launched":
			browser++
		case "browser.pipe_closed":
			pipe++
			if r.Lvl != "error" {
				t.Fatalf("a closed pipe is an error-level record, got %q", r.Lvl)
			}
			pipeSeq = r.Seq
		case "run.store_mode":
			store++
		}
	}
	if browser != 1 || pipe != 1 || store != 1 {
		t.Fatalf("want one record each (browser=%d pipe=%d store=%d) from %+v", browser, pipe, store, recs)
	}
	// Frames are their own records but LINKED to the error, so the UI renders one expandable problem
	// instead of twenty rows. Keeping them as records is what makes them greppable and filterable.
	var frames int
	for _, r := range recs {
		if strings.HasPrefix(strings.TrimSpace(r.Msg), "at ") {
			frames++
			if r.Parent != pipeSeq {
				t.Fatalf("a stack frame must point at the error above it (parent=%d want %d): %+v",
					r.Parent, pipeSeq, r)
			}
			if r.Lvl != "debug" {
				t.Fatalf("a frame is debug detail, not a problem of its own: %+v", r)
			}
		}
	}
	if frames != 2 {
		t.Fatalf("want both frames linked, got %d", frames)
	}
}

// The catalogue, not the wire, decides level and category — so a stale binary paired with a newer
// catalogue follows the catalogue. Here the wire lies about both.
func TestSinkTrustsCatalogueOverWire(t *testing.T) {
	recs, _, _, _ := drain(t, []string{`[debug|system] llm.no_anthropic_key: No AI key (planner)`})
	if len(recs) != 1 {
		t.Fatalf("want one record, got %+v", recs)
	}
	if recs[0].Lvl != "warn" || recs[0].Cat != "llm" {
		t.Fatalf("catalogue must win over the wire: want warn/llm, got %s/%s", recs[0].Lvl, recs[0].Cat)
	}
	if !recs[0].Degrades {
		t.Fatal("llm.no_anthropic_key is a silent degradation and must be flagged as one")
	}
	if recs[0].Mod != "brain.llm" {
		t.Fatalf("module should resolve from the catalogue, got %q", recs[0].Mod)
	}
}

// An unrecognised line still becomes a record with a level and a category — the catch-all rule means
// nothing is ever dropped on the floor, and the raw text travels with it.
func TestSinkClassifiesUnknownLine(t *testing.T) {
	recs, _, _, _ := drain(t, []string{`something entirely unexpected from a tool we do not own`})
	if len(recs) != 1 {
		t.Fatalf("want one record, got %+v", recs)
	}
	if recs[0].Lvl == "" || recs[0].Cat == "" {
		t.Fatalf("catch-all must supply a level and category: %+v", recs[0])
	}
	if !strings.Contains(recs[0].Raw, "entirely unexpected") {
		t.Fatalf("the original text must survive on the record: %+v", recs[0])
	}
}

// Rotation bounds the raw file. One generation is kept because the tail diagnoses the failure.
func TestSinkRotatesRawBySize(t *testing.T) {
	t.Setenv("SENTINEL_LOG_MAX_MB", "0")
	dir := t.TempDir()
	// 0 disables rotation — an explicit, documented choice that must be honoured, not overridden.
	s := newLogSink(dir)
	for i := 0; i < 200; i++ {
		s.write(strings.Repeat("x", 1024))
	}
	s.close()
	if _, err := os.Stat(filepath.Join(dir, "logs", "run.log.1")); !os.IsNotExist(err) {
		t.Fatal("SENTINEL_LOG_MAX_MB=0 must disable rotation, but a rotated file appeared")
	}
}

// A sink whose directory cannot be created must not stop a run: every method is nil-safe.
func TestSinkNilIsSafe(t *testing.T) {
	var s *logSink
	s.write("anything") // must not panic
	s.close()
}

// logEnvMB must fall back rather than silently disable rotation on a malformed value — an explicit 0
// disables it, garbage does not.
func TestLogEnvMBFallsBack(t *testing.T) {
	for _, tc := range []struct{ set, want string }{
		{"", "50"}, {"garbage", "50"}, {"-5", "50"}, {"0", "0"}, {"7", "7"},
	} {
		t.Run("value="+tc.set, func(t *testing.T) {
			t.Setenv("SENTINEL_LOG_MAX_MB", tc.set)
			if tc.set == "" {
				os.Unsetenv("SENTINEL_LOG_MAX_MB")
			}
			got := logEnvMB("SENTINEL_LOG_MAX_MB", 50)
			if want := tc.want; got != atoiOrZero(want) {
				t.Fatalf("logEnvMB(%q) = %d want %s", tc.set, got, want)
			}
		})
	}
}

func atoiOrZero(s string) int {
	n := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0
		}
		n = n*10 + int(c-'0')
	}
	return n
}

// HEALTH-004 PR-1c: three codes override the source their category implies, and the SINK is what
// stamps that onto every record — so this drives the sink, not the accessor.
//
// Written because a mutation survived: replacing the override lookup in eventcatalog.SourceOfCode
// with a no-op left everything green. The Python gate checks the catalogue's DATA (the entries say
// `application`), and nothing checked that the Go side reads it — while the Go side is the only
// thing that decides what lands in run.jsonl, and therefore what the audience filter can find.
func TestSinkHonoursTheSourceOverride(t *testing.T) {
	recs, _, _, _ := drain(t, []string{
		// Emitted by the healer, and a statement about the application: the interface moved.
		`[info|heal] heal.drift_rebind: The interface changed, but the element was found again`,
		// The healer's own diagnostics must NOT move with it.
		`[warn|heal] heal.budget_exhausted: the heal budget is spent`,
	})
	if len(recs) != 2 {
		t.Fatalf("want 2 records, got %d: %+v", len(recs), recs)
	}
	if recs[0].Src != "application" {
		t.Errorf("heal.drift_rebind must be sourced to the application (the interface is what moved), got %q. "+
			"With the derived source, a reader filtering `business` sees nothing about the one thing that "+
			"changed under their test.", recs[0].Src)
	}
	if recs[0].Cat != "heal" {
		t.Errorf("the CATEGORY must stay `heal`, got %q — drift is a healing concept and someone "+
			"filtering self-healing has to keep finding it", recs[0].Cat)
	}
	if recs[1].Src != "tool" {
		t.Errorf("heal.budget_exhausted must stay with the tool, got %q — an override applied to the "+
			"whole category would re-file our own exhausted budget as the application's fault", recs[1].Src)
	}
}

// ADR-067: the source axis and the step correlation. Both are what let a tester ask "which step went
// wrong, and was it my application or the tool?" — the question the Logs view exists to answer.
func TestSinkTagsSourceAndStep(t *testing.T) {
	recs, _, _, _ := drain(t, []string{
		`[info|run] run.config: Run abc started: mode explore`,
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":4,"data":{"n":4,"total":40,"desc":"click cart"}}`,
		`[error|app] app.js_error: The page under test threw an error: TypeError: cart.total is undefined`,
		// The line the product actually emits since HEALTH-004 PR-1b — it carries the REASON now. The
		// fixture said "This is the step that went wrong", a sentence nothing produces any more; a
		// fixture built on text the product has stopped writing tests only itself.
		`[error|test] test.step_failed: Step 4 (click) FAILED — the application did not answer as expected: browser.click: Timeout 30000ms exceeded. Expected True, observed —`,
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":5,"data":{"n":5,"total":40,"desc":"pay"}}`,
		`[warn|app] app.http_error: The site answered 500 to POST /api/pay`,
	})
	if len(recs) != 4 {
		t.Fatalf("want 4 diagnostics (the frames are narrative), got %d: %+v", len(recs), recs)
	}
	// A record before any step frame belongs to no step rather than to a guessed one.
	if recs[0].Step != 0 {
		t.Fatalf("a record preceding every step frame must carry no step, got %d", recs[0].Step)
	}
	if recs[0].Src != "tool" {
		t.Fatalf("run.config is the tool's own log, got src=%q", recs[0].Src)
	}
	for i, want := range []struct {
		src  string
		step int
	}{{"tool", 0}, {"application", 4}, {"testing", 4}, {"application", 5}} {
		if recs[i].Src != want.src || recs[i].Step != want.step {
			t.Fatalf("record %d (%s): src=%q step=%d, want src=%q step=%d",
				i, recs[i].Code, recs[i].Src, recs[i].Step, want.src, want.step)
		}
	}
}

// A step frame we cannot parse must leave the previous number in place: a slightly stale step is far
// more useful than none, and clearing it would silently drop the correlation for the rest of the run.
func TestSinkKeepsStepOnUnparseableFrame(t *testing.T) {
	recs, _, _, _ := drain(t, []string{
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":2,"data":{"n":7,"total":40}}`,
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":3,"data":{"n":`,
		`[error|app] app.js_error: The page under test threw an error: boom`,
	})
	if len(recs) != 1 || recs[0].Step != 7 {
		t.Fatalf("want the last good step (7) preserved, got %+v", recs)
	}
}

// A summary must not inherit the last step: it is a fact about the run, and "Explore finished … step 3"
// invites reading it as a fact about step 3.
func TestSinkDoesNotStampStepOnSummary(t *testing.T) {
	recs, _, _, _ := drain(t, []string{
		`@@AGUI {"type":"step.progress","run_id":"abc","seq":2,"data":{"n":3,"total":3}}`,
		`[error|app] app.js_error: The page under test threw an error: boom`,
		`[info|test] test.explore_complete: Explore finished: 3 steps, coverage 1.00`,
	})
	if len(recs) != 2 {
		t.Fatalf("want 2 records, got %+v", recs)
	}
	if recs[0].Step != 3 {
		t.Fatalf("an in-step record must carry the step, got %d", recs[0].Step)
	}
	if recs[1].Step != 0 {
		t.Fatalf("a report-phase summary must carry no step, got %d", recs[1].Step)
	}
}

// `__main__` must read as `brain.main`, not `brain.main__` — trimming only the prefix left the
// trailing underscores on screen in the technical register.
func TestSinkModuleNameIsReadable(t *testing.T) {
	recs, _, _, _ := drain(t, []string{`[info|run] run.config: Run abc started: mode explore`})
	if len(recs) != 1 {
		t.Fatalf("want one record, got %+v", recs)
	}
	if recs[0].Mod != "brain.main" {
		t.Fatalf("module should read as brain.main, got %q", recs[0].Mod)
	}
}
