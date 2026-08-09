package main

// QA-REPORT-SERVICE (ADR-119) — the gate for the aggregate scrape.
//
// The code this replaces had NO tests at all: `cmd/report-service` measured 0.0% coverage, and the
// defect that made it invalid (concatenating unlabelled series) was therefore never anybody's red
// test — it was invisible because the binary was never launched. Every assertion below is written
// against a property the old concatenation VIOLATED or could not have.

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// writeRunMetrics plants one run directory with a metrics.prom.
func writeRunMetrics(t *testing.T, s *server, dirName, body string) {
	t.Helper()
	d := filepath.Join(s.repo, "runs", dirName)
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", d, err)
	}
	if err := os.WriteFile(filepath.Join(d, "metrics.prom"), []byte(body), 0o644); err != nil {
		t.Fatalf("write metrics.prom: %v", err)
	}
}

// scrape performs GET /metrics through the real mux with the given credential.
func scrape(t *testing.T, s *server, token string) (int, string) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	return rec.Code, rec.Body.String()
}

// oneRunFixture is what brain/report.py::_metrics actually writes today: bare series, no HELP, no
// TYPE, and nothing that says which run they belong to.
const oneRunFixture = `# Sentinel run metrics (Prometheus textfile format)
sentinel_run_steps 7
sentinel_run_exit_code 0
sentinel_heal_total 1
sentinel_heal_by_strategy_total{strategy="L2"} 1
sentinel_failed_total 0
`

// TestMetricsAggregateKeepsEveryRunsSeries — (a) nothing a run reported is lost on the way through.
func TestMetricsAggregateKeepsEveryRunsSeries(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)

	code, body := scrape(t, s, s.token)
	if code != http.StatusOK {
		t.Fatalf("GET /metrics: got %d want 200\n%s", code, body)
	}
	for _, want := range []string{
		`sentinel_run_steps{run="r1"} 7`,
		`sentinel_run_exit_code{run="r1"} 0`,
		`sentinel_heal_total{run="r1"} 1`,
		`sentinel_heal_by_strategy_total{run="r1",strategy="L2"} 1`,
		`sentinel_failed_total{run="r1"} 0`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q in:\n%s", want, body)
		}
	}
	if !strings.Contains(body, "sentinel_metrics_runs_included 1") {
		t.Errorf("the aggregator did not say how many runs it included:\n%s", body)
	}
}

// TestMetricsAggregateNeverEmitsADuplicateSeries — (b) THE defect. Two runs, byte-identical files:
// the old concatenation produced `sentinel_run_steps 7` twice with the same (empty) label set, which
// is a duplicate sample rather than an aggregate. Nothing in the response may repeat a
// name+labelset.
func TestMetricsAggregateNeverEmitsADuplicateSeries(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	writeRunMetrics(t, s, "control-r2", oneRunFixture)

	_, body := scrape(t, s, s.token)
	seen := map[string]bool{}
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		series := line
		if i := strings.LastIndex(line, " "); i > 0 {
			series = line[:i] // name+labels, without the value
		}
		if seen[series] {
			t.Errorf("series %q appears twice in one scrape — that is the defect this route was "+
				"rewritten to remove, not an aggregate:\n%s", series, body)
		}
		seen[series] = true
	}
	if !strings.Contains(body, `sentinel_run_steps{run="r1"} 7`) ||
		!strings.Contains(body, `sentinel_run_steps{run="r2"} 7`) {
		t.Errorf("both runs must still be present, distinguished by label:\n%s", body)
	}
}

// TestMetricsAggregateHeaderAppearsOncePerFamily — a family's HELP/TYPE is printed once, before its
// samples. Two runs contributing to one family must not produce two headers.
func TestMetricsAggregateHeaderAppearsOncePerFamily(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	writeRunMetrics(t, s, "control-r2", oneRunFixture)

	_, body := scrape(t, s, s.token)
	for _, fam := range []string{"sentinel_run_steps", "sentinel_heal_by_strategy_total"} {
		if n := strings.Count(body, "# HELP "+fam+" "); n != 1 {
			t.Errorf("# HELP %s appears %d times, want 1:\n%s", fam, n, body)
		}
		if n := strings.Count(body, "# TYPE "+fam+" "); n != 1 {
			t.Errorf("# TYPE %s appears %d times, want 1:\n%s", fam, n, body)
		}
	}
	if !strings.Contains(body, "# TYPE sentinel_failed_total counter") {
		t.Errorf("a _total family must be typed counter:\n%s", body)
	}
	if !strings.Contains(body, "# TYPE sentinel_run_steps gauge") {
		t.Errorf("a non-_total family must be typed gauge:\n%s", body)
	}
	// Ordering: the header must precede the samples of its family, which is what makes the response
	// parseable at all.
	hdr := strings.Index(body, "# TYPE sentinel_run_steps gauge")
	first := strings.Index(body, `sentinel_run_steps{run=`)
	if hdr < 0 || first < 0 || hdr > first {
		t.Errorf("TYPE for sentinel_run_steps does not precede its samples:\n%s", body)
	}
}

// TestMetricsAggregateSkipsUnreadableInsteadOf500 — (c). This repository really does contain run
// directories left root-owned by containers; one of them must not take the deployment's numbers down.
func TestMetricsAggregateSkipsUnreadableInsteadOf500(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: file modes cannot make a read fail")
	}
	s := newTestServer()
	writeRunMetrics(t, s, "control-good", oneRunFixture)
	writeRunMetrics(t, s, "control-bad", oneRunFixture)
	if err := os.Chmod(filepath.Join(s.repo, "runs", "control-bad", "metrics.prom"), 0o000); err != nil {
		t.Fatalf("chmod: %v", err)
	}

	code, body := scrape(t, s, s.token)
	if code != http.StatusOK {
		t.Fatalf("one unreadable artifact produced %d — the scrape must survive it: %s", code, body)
	}
	if !strings.Contains(body, `sentinel_run_steps{run="good"} 7`) {
		t.Errorf("the readable run was lost too:\n%s", body)
	}
	if !strings.Contains(body, `sentinel_metrics_runs_omitted{reason="unreadable"} 1`) {
		t.Errorf("the skipped run was not REPORTED — a silent skip reads exactly like full coverage:\n%s", body)
	}
}

// TestMetricsAggregateSkipsAnUnreadableDirectory — the OTHER half of (c), and the one this repository
// actually produces: containers that run without a `user:` leave run directories owned by root, so the
// failure is on the DIRECTORY (stat of the artifact inside it is refused), not on a file whose mode we
// could read. Found by mutation: removing the counter on the stat path left every test green, because
// the file-mode fixture never reaches it.
func TestMetricsAggregateSkipsAnUnreadableDirectory(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: file modes cannot make a read fail")
	}
	s := newTestServer()
	writeRunMetrics(t, s, "control-good", oneRunFixture)
	writeRunMetrics(t, s, "control-locked", oneRunFixture)
	locked := filepath.Join(s.repo, "runs", "control-locked")
	if err := os.Chmod(locked, 0o000); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o755) }) // or the temp tree cannot be removed

	code, body := scrape(t, s, s.token)
	if code != http.StatusOK {
		t.Fatalf("one unreadable directory produced %d — the scrape must survive it: %s", code, body)
	}
	if !strings.Contains(body, `sentinel_run_steps{run="good"} 7`) {
		t.Errorf("the readable run was lost too:\n%s", body)
	}
	if !strings.Contains(body, `sentinel_metrics_runs_omitted{reason="unreadable"} 1`) {
		t.Errorf("the unreadable DIRECTORY was skipped without being reported:\n%s", body)
	}
}

// TestMetricsAggregateStaysInsideRuns — (d). The aggregate takes no name from the caller, so the way
// out of the tree would be a planted symlink; DirEntry.IsDir() reports the link's own type, and this
// pins that the behaviour is relied upon rather than incidental. Dot-directories (runs/.park) are
// skipped for the same walk.
func TestMetricsAggregateStaysInsideRuns(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)

	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "metrics.prom"),
		[]byte("sentinel_run_steps 999\n"), 0o644); err != nil {
		t.Fatalf("write outside fixture: %v", err)
	}
	if err := os.Symlink(outside, filepath.Join(s.repo, "runs", "control-escape")); err != nil {
		t.Skipf("symlinks unavailable here: %v", err)
	}
	writeRunMetrics(t, s, ".park", "sentinel_run_steps 777\n")

	_, body := scrape(t, s, s.token)
	if strings.Contains(body, "999") {
		t.Errorf("a symlinked directory in runs/ was followed out of the tree:\n%s", body)
	}
	if strings.Contains(body, "777") {
		t.Errorf("runs/.park was walked; the parking directory is not a run:\n%s", body)
	}
	if !strings.Contains(body, `sentinel_run_steps{run="r1"} 7`) {
		t.Errorf("the real run was lost:\n%s", body)
	}
}

// TestMetricsAggregateHidesAnotherAccountsNumbers — (e). Remove the owner filter in the handler and
// this goes red; that is the mutation it exists for.
func TestMetricsAggregateHidesAnotherAccountsNumbers(t *testing.T) {
	s := newTestServer()
	s.runs["mine"] = &run{ID: "mine", Owner: "ua"}
	s.runs["theirs"] = &run{ID: "theirs", Owner: "ub"}
	writeRunMetrics(t, s, "control-mine", "sentinel_run_steps 3\n")
	writeRunMetrics(t, s, "control-theirs", "sentinel_run_steps 11\n")

	tok := s.sessions.mint("ua", "alice", false, sessionTTL())
	code, body := scrape(t, s, tok)
	if code != http.StatusOK {
		t.Fatalf("scoped scrape: got %d want 200\n%s", code, body)
	}
	if !strings.Contains(body, `sentinel_run_steps{run="mine"} 3`) {
		t.Errorf("alice cannot see her own run's numbers:\n%s", body)
	}
	if strings.Contains(body, `run="theirs"`) || strings.Contains(body, " 11") {
		t.Errorf("alice was shown bob's numbers — an aggregate is where ADR-109's defect comes back, "+
			"because there is no {id} for the guard to scope:\n%s", body)
	}
	if !strings.Contains(body, "sentinel_metrics_runs_included 1") {
		t.Errorf("included count does not match the scoped view:\n%s", body)
	}
}

// TestMetricsAggregateUnscopedCallerSeesEverything — the other half of the same decision: the machine
// token is the operator's scrape and is deliberately not filtered. Without this, "scoped correctly"
// and "returns nothing" would be indistinguishable.
func TestMetricsAggregateUnscopedCallerSeesEverything(t *testing.T) {
	s := newTestServer()
	s.runs["mine"] = &run{ID: "mine", Owner: "ua"}
	s.runs["theirs"] = &run{ID: "theirs", Owner: "ub"}
	writeRunMetrics(t, s, "control-mine", "sentinel_run_steps 3\n")
	writeRunMetrics(t, s, "control-theirs", "sentinel_run_steps 11\n")

	_, body := scrape(t, s, s.token) // machine token: owner == ""
	if !strings.Contains(body, `sentinel_run_steps{run="mine"} 3`) ||
		!strings.Contains(body, `sentinel_run_steps{run="theirs"} 11`) {
		t.Errorf("the machine token must see the whole deployment:\n%s", body)
	}
	if !strings.Contains(body, "sentinel_metrics_runs_included 2") {
		t.Errorf("included count wrong for the unscoped caller:\n%s", body)
	}
}

// TestMetricsAggregateRefusesAnonymous — the route is credentialled, and the report-service handler it
// replaces was not.
func TestMetricsAggregateRefusesAnonymous(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	code, body := scrape(t, s, "")
	if code != http.StatusForbidden {
		t.Fatalf("anonymous GET /metrics: got %d want 403\n%s", code, body)
	}
	if strings.Contains(body, "sentinel_run_steps") {
		t.Errorf("the refusal leaked the numbers it refused:\n%s", body)
	}
}

// TestMetricsAggregateReportsWhatItDropped — a run whose file already binds `run` cannot be merged
// (two runs would claim one identity, or the label name would appear twice), and a line that is not
// textfile syntax is not a sample. Both are counted, because a silent drop reads as full coverage.
func TestMetricsAggregateReportsWhatItDropped(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	writeRunMetrics(t, s, "control-conflict", `sentinel_run_steps{run="somebody-elses-id"} 5`+"\n")
	writeRunMetrics(t, s, "control-junk", "sentinel_run_steps 1\nthis is not a sample\n")
	// A run that produced no metrics.prom at all. This is the COMMON case, not an edge one: 192
	// `runs/control-*` directories were measured on this repository and NONE of them had metrics —
	// the artifact only started being produced on the UI path with ADR-089. Counting those as
	// omissions would put ~192 into a gauge that is supposed to mean "something went wrong", and the
	// number nobody can act on is the number nobody looks at. Found by mutation: deleting the
	// IsNotExist branch left every other assertion green.
	if err := os.MkdirAll(filepath.Join(s.repo, "runs", "control-nometrics"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	_, body := scrape(t, s, s.token)
	if !strings.Contains(body, `sentinel_metrics_runs_omitted{reason="unreadable"} 0`) {
		t.Errorf("a run that simply produced no metrics.prom was filed as an omission:\n%s", body)
	}
	if strings.Contains(body, "somebody-elses-id") {
		t.Errorf("a file that declares its own run identity was merged anyway:\n%s", body)
	}
	if !strings.Contains(body, `sentinel_metrics_runs_omitted{reason="conflict"} 1`) {
		t.Errorf("the conflicting run was dropped without saying so:\n%s", body)
	}
	if !strings.Contains(body, "sentinel_metrics_lines_dropped 1") {
		t.Errorf("the unparseable line was dropped without saying so:\n%s", body)
	}
	if !strings.Contains(body, `sentinel_run_steps{run="junk"} 1`) {
		t.Errorf("one bad line discarded the run's good samples:\n%s", body)
	}
	// Every reason is printed even at zero: a series that only appears once something goes wrong has
	// no baseline and cannot be alerted on.
	for _, reason := range omitReasons {
		if !strings.Contains(body, fmt.Sprintf("sentinel_metrics_runs_omitted{reason=%q}", reason)) {
			t.Errorf("reason %q has no series at all:\n%s", reason, body)
		}
	}
}

// TestMetricsAggregateCapIsReported — the response may not grow without bound, and what the bound cut
// off has to be visible. metricsMaxRuns is read here rather than restated, so lowering the constant
// cannot leave this test asserting a number the product no longer uses.
func TestMetricsAggregateCapIsReported(t *testing.T) {
	s := newTestServer()
	// Each directory carries a metrics.prom, or the cap would be measured over runs that contribute
	// nothing and the response would be bounded by the fixture rather than by the code. Found by
	// mutation: with empty directories, deleting the truncation itself left this test green because
	// the reported number was computed before it.
	// Mtimes are set explicitly, oldest first, so the test can say WHICH three the cap drops. The cap
	// keeps the NEWEST runs — the ones somebody is plausibly looking at — and "keeps 500 of 503" would
	// be satisfied just as well by keeping the oldest, which is the opposite of useful.
	base := time.Now().Add(-24 * time.Hour)
	for i := 0; i < metricsMaxRuns+3; i++ {
		name := fmt.Sprintf("control-r%04d", i)
		writeRunMetrics(t, s, name, fmt.Sprintf("sentinel_run_steps %d\n", i))
		when := base.Add(time.Duration(i) * time.Minute)
		if err := os.Chtimes(filepath.Join(s.repo, "runs", name), when, when); err != nil {
			t.Fatalf("chtimes: %v", err)
		}
	}
	_, body := scrape(t, s, s.token)
	for i := 0; i < 3; i++ {
		if strings.Contains(body, fmt.Sprintf(`run="r%04d"`, i)) {
			t.Errorf("run r%04d is among the three OLDEST and should have been the one dropped", i)
		}
	}
	if !strings.Contains(body, fmt.Sprintf(`run="r%04d"`, metricsMaxRuns+2)) {
		t.Errorf("the newest run was dropped by the cap:\n%s", body[:min(len(body), 400)])
	}
	if !strings.Contains(body, `sentinel_metrics_runs_omitted{reason="cap"} 3`) {
		t.Errorf("the cap dropped run directories silently:\n%s", body)
	}
	if !strings.Contains(body, fmt.Sprintf("sentinel_metrics_runs_included %d", metricsMaxRuns)) {
		t.Errorf("the response is not actually bounded by metricsMaxRuns=%d:\n%s",
			metricsMaxRuns, body[:min(len(body), 400)])
	}
	if n := strings.Count(body, `sentinel_run_steps{run=`); n != metricsMaxRuns {
		t.Errorf("%d run series in the response, want %d — the cap is reported but not applied",
			n, metricsMaxRuns)
	}
}

// TestMetricsAggregateIsCached — the walk is memoized, so a scraper polling this route does not turn
// into disk load proportional to poll rate times run count.
func TestMetricsAggregateIsCached(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", "sentinel_run_steps 1\n")
	if _, body := scrape(t, s, s.token); !strings.Contains(body, `sentinel_run_steps{run="r1"} 1`) {
		t.Fatalf("first scrape did not see the run:\n%s", body)
	}
	writeRunMetrics(t, s, "control-r2", "sentinel_run_steps 2\n")
	if _, body := scrape(t, s, s.token); strings.Contains(body, `run="r2"`) {
		t.Errorf("the second scrape re-walked the disk within the TTL — the memo does nothing:\n%s", body)
	}
	s.metrics.mu.Lock()
	s.metrics.scan = nil // expire it the way time would
	s.metrics.mu.Unlock()
	if _, body := scrape(t, s, s.token); !strings.Contains(body, `run="r2"`) {
		t.Errorf("after expiry the walk did not pick up the new run:\n%s", body)
	}
}

// TestMetricsAggregateEmptyDeploymentIsValid — no runs/ at all still produces a valid scrape rather
// than an error, and the self-describing families are the floor that proves the response was built at
// all instead of being empty for a reason nobody noticed.
func TestMetricsAggregateEmptyDeploymentIsValid(t *testing.T) {
	s := newTestServer()
	code, body := scrape(t, s, s.token)
	if code != http.StatusOK {
		t.Fatalf("empty deployment: got %d want 200\n%s", code, body)
	}
	if !strings.Contains(body, "sentinel_metrics_runs_included 0") {
		t.Errorf("an empty deployment must still say so:\n%s", body)
	}
	if n := strings.Count(body, "# TYPE "); n < 3 {
		t.Errorf("only %d families declared a TYPE — the response is not a Prometheus scrape:\n%s", n, body)
	}
}

// TestMetricsAggregateIsByteStable — the body is built by walking maps, and Go randomises map order
// deliberately. Two scrapes of the same state that differ only in line order make every diff of a
// captured scrape unreadable and every future golden useless. Found by mutation: removing either sort
// left all the content assertions green, because each of them looks for a substring.
//
// Eight comparisons rather than one: a single pair can agree by luck over a small family set.
func TestMetricsAggregateIsByteStable(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	writeRunMetrics(t, s, "control-r2", oneRunFixture)
	writeRunMetrics(t, s, "control-r3", oneRunFixture)

	_, first := scrape(t, s, s.token)
	for i := 0; i < 8; i++ {
		if _, again := scrape(t, s, s.token); again != first {
			t.Fatalf("scrape %d differs from the first over unchanged state:\n--- first ---\n%s\n--- again ---\n%s",
				i+1, first, again)
		}
	}
}

// TestMetricsAggregateOrdersSamplesByRunID — within a family the samples are ordered by run id, not by
// how recently the run finished. The difference is what makes two scrapes taken hours apart diffable:
// ordered by id, a new run inserts ONE line; ordered by recency (which is the walk's own order, and
// what you get for free), a new run pushes every existing line down and the diff is the whole body.
//
// ⚠ RECORDED AS AN EQUIVALENT MUTATION, not fixed: walkRunMetrics breaks equal mtimes by directory
// name. That tie-break is invisible through this response — the samples are re-sorted by run id here
// anyway, and the cap test uses distinct mtimes. It is kept because it decides WHICH runs the cap
// keeps among same-second runs, and the alternative is a choice that depends on readdir order.
func TestMetricsAggregateOrdersSamplesByRunID(t *testing.T) {
	s := newTestServer()
	// bbb is written LAST, so the walk (newest first) would put it first without the sort.
	writeRunMetrics(t, s, "control-aaa", "sentinel_run_steps 1\n")
	older := time.Now().Add(-time.Hour)
	if err := os.Chtimes(filepath.Join(s.repo, "runs", "control-aaa"), older, older); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	writeRunMetrics(t, s, "control-bbb", "sentinel_run_steps 2\n")

	_, body := scrape(t, s, s.token)
	ia := strings.Index(body, `sentinel_run_steps{run="aaa"}`)
	ib := strings.Index(body, `sentinel_run_steps{run="bbb"}`)
	if ia < 0 || ib < 0 {
		t.Fatalf("both runs must be present:\n%s", body)
	}
	if ia > ib {
		t.Errorf("samples are ordered by recency, not by run id — every new run then rewrites the "+
			"whole body instead of adding a line:\n%s", body)
	}
}

// TestMetricsAggregateEscapesTheRunLabel — the run id comes from a DIRECTORY NAME, which this process
// did not choose. An unescaped quote would break the label set of every family it appears in.
func TestMetricsAggregateEscapesTheRunLabel(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, `control-a"b`, "sentinel_run_steps 1\n")
	_, body := scrape(t, s, s.token)
	if !strings.Contains(body, `sentinel_run_steps{run="a\"b"} 1`) {
		t.Errorf("the run label was not escaped:\n%s", body)
	}
}

// TestMetricsAggregateContentTypeIsPrometheusText — a scraper decides how to parse from this header.
func TestMetricsAggregateContentTypeIsPrometheusText(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/plain") ||
		!strings.Contains(ct, "version=0.0.4") {
		t.Errorf("Content-Type %q is not the Prometheus exposition format", ct)
	}
}

// TestMetricsAggregateOutputParsesAsExposition — a shape check over the whole body rather than over
// the lines this test happens to name: every non-comment line must be a well-formed sample, and every
// family that has samples must have declared its type. This is what catches a family added upstream
// arriving unannounced, which naming families one by one cannot.
func TestMetricsAggregateOutputParsesAsExposition(t *testing.T) {
	s := newTestServer()
	writeRunMetrics(t, s, "control-r1", oneRunFixture)
	writeRunMetrics(t, s, "control-r2", oneRunFixture)
	_, body := scrape(t, s, s.token)

	sample := regexp.MustCompile(`^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9]`)
	typed := map[string]bool{}
	families := map[string]bool{}
	for _, line := range strings.Split(body, "\n") {
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "# TYPE ") {
			typed[strings.Fields(line)[2]] = true
			continue
		}
		if strings.HasPrefix(line, "#") {
			continue
		}
		if !sample.MatchString(line) {
			t.Errorf("line is not a valid exposition sample: %q", line)
			continue
		}
		families[strings.FieldsFunc(line, func(r rune) bool { return r == '{' || r == ' ' })[0]] = true
	}
	if len(families) < 6 {
		t.Fatalf("only %d families in the response — this check would pass over nothing: %v", len(families), families)
	}
	for f := range families {
		if !typed[f] {
			t.Errorf("family %s has samples but no # TYPE", f)
		}
	}
}
