package main

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// ADR-076. brain has written pass_with_drift / pass_with_app_faults / problem_drift /
// problem_app_faults into heal-report.json since ADR-071/072, while the Go side derived the stored
// verdict from the exit code alone — so the Results domain and the hub saw only the coarse four and the
// distinction died at the process boundary. The run's own word wins; anything the Go side does not
// recognise falls back to the exit code rather than travelling on into the store.
func TestResultVerdictPrefersTheRunsOwnWord(t *testing.T) {
	for _, tc := range []struct {
		artifact string
		exit     int
		want     string
		why      string
	}{
		{"pass_with_drift", 0, "pass_with_drift", "a pass that needed repairs is not a clean pass"},
		{"pass_with_app_faults", 0, "pass_with_app_faults", "the application misbehaved while the test passed"},
		{"problem_drift", 1, "problem_drift", "a threshold reddened the build, not a failed step"},
		{"problem_app_faults", 1, "problem_app_faults", "same, for the application's own faults"},
		{"", 0, "pass", "no artifact word: the exit code decides"},
		{"", 2, "regression", "no artifact word: the exit code decides"},
		{"", 3, "integrity", "no artifact word: the exit code decides"},
		{"pass", 0, "pass", "a coarse word from the artifact adds nothing"},
		// The whitelist earns its keep here. `verdict` is a free string in a file on disk; a value the
		// readers cannot render must not reach the Results domain, and a NEWER brain inventing a state an
		// OLDER control-API has never heard of must degrade to something true rather than something novel.
		{"regression", 0, "pass", "an artifact word outside the known set never overrides the exit code"},
		{"catastrophe", 1, "problem", "an unknown state from a newer brain degrades to the exit code"},
	} {
		if got := resultVerdict(tc.artifact, tc.exit); got != tc.want {
			t.Errorf("resultVerdict(%q, %d) = %q want %q — %s", tc.artifact, tc.exit, got, tc.want, tc.why)
		}
	}
}

// The end-to-end claim: a replay run whose report says pass_with_drift is RECORDED as pass_with_drift,
// and the counts behind that word reach the metrics domain. Both halves matter — the word alone tells
// the hub which badge to draw, the counts are what a trend can be built from.
func TestPersistResultCarriesRefinedVerdictAndCounts(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	dir := t.TempDir()
	heal := `{"plan_id":"p1","mode":"replay","exit_code":0,"verdict":"pass_with_drift","healed":3,"failed":0,` +
		`"steps":[{"step_id":1},{"step_id":2}],"regressions":[],` +
		`"drift":{"rebind":2,"reground":1,"elements":[{"step":1,"kind":"rebind"},{"step":2,"kind":"reground"}]},` +
		`"app_faults":{"counts":{"app.js_error":9},"total":13,"errors":12}}`
	if err := os.WriteFile(filepath.Join(dir, "heal-report.json"), []byte(heal), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: "drifty", State: "done", Target: "file:///app/x.html", Mode: "replay", ExitCode: 0,
		StartedAt: "2026-07-26T00:00:00Z", FinishedAt: "2026-07-26T00:00:02Z", ArtifactDir: dir}
	s.persistResult(rec)

	rr, ok := sc.getResult("drifty")
	if !ok {
		t.Fatal("result not saved")
	}
	if rr.Verdict == "pass" {
		t.Fatal("the store recorded a plain \"pass\" — ADR-071's distinction died at the process boundary again")
	}
	if rr.Verdict != "pass_with_drift" {
		t.Fatalf("verdict = %q want pass_with_drift", rr.Verdict)
	}

	mval := func(name string) (float64, int) {
		ms, err := sc.cl.QueryMetrics(context.Background(), &storepb.MetricsQuery{Name: name})
		if err != nil {
			t.Fatalf("QueryMetrics(%s): %v", name, err)
		}
		if len(ms.Points) == 0 {
			return 0, 0
		}
		return ms.Points[0].Value, len(ms.Points)
	}
	for name, want := range map[string]float64{
		"drift_total": 3, "drift_rebind": 2, "drift_reground": 1,
		"app_faults_total": 13, "app_faults_errors": 12,
	} {
		got, n := mval(name)
		if n == 0 {
			t.Fatalf("metric %q was never ingested — the counts behind the verdict never left the artifact", name)
		}
		if got != want {
			t.Fatalf("metric %q = %v want %v", name, got, want)
		}
	}

	// The refined word must not touch the pass-rate arithmetic: `pass` is an exit-code fact, and a run
	// that passed with drift still passed. Conflating the two would silently re-price every trend.
	if got, n := mval("pass"); n == 0 || got != 1 {
		t.Fatalf("pass metric = %v (points=%d) want 1 — a pass_with_drift run still passed", got, n)
	}
}

// Absent is not zero. explore/goal runs carry no heal-report at all, and a replay with no drift is a
// different fact from a mode that cannot report drift; emitting 0 for both would put points into a
// series that the second kind of run has no business appearing in.
func TestNoDriftBlockMeansNoDriftPoints(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "plan.json"),
		[]byte(`{"plan_id":"explore1","coverage_achieved":0.5}`), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: "explorer", State: "done", Mode: "goal", ExitCode: 0,
		StartedAt: "2026-07-26T00:00:00Z", FinishedAt: "2026-07-26T00:00:01Z", ArtifactDir: dir}
	s.persistResult(rec)

	// The run WAS recorded — otherwise "no drift points" would be true for the boring reason.
	if rr, ok := sc.getResult("explorer"); !ok || rr.Verdict != "pass" {
		t.Fatalf("explore run not recorded properly: %+v ok=%v", rr, ok)
	}
	if ms, err := sc.cl.QueryMetrics(context.Background(), &storepb.MetricsQuery{Name: "coverage"}); err != nil ||
		len(ms.Points) != 1 {
		t.Fatalf("the run ingested no metrics at all, so the check below proves nothing: %+v err=%v", ms, err)
	}
	for _, name := range []string{"drift_total", "drift_rebind", "drift_reground", "app_faults_total", "app_faults_errors"} {
		ms, err := sc.cl.QueryMetrics(context.Background(), &storepb.MetricsQuery{Name: name})
		if err != nil {
			t.Fatalf("QueryMetrics(%s): %v", name, err)
		}
		if len(ms.Points) != 0 {
			t.Fatalf("metric %q got %d point(s) from a run that cannot report it", name, len(ms.Points))
		}
	}
}

// GET /v1/runs/{id} and the runs list are what the hub reads; the refined verdict has to survive the
// HTTP surface too, not just the gRPC write.
func TestRefinedVerdictSurvivesTheResultsEndpoint(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "heal-report.json"),
		[]byte(`{"plan_id":"p2","mode":"replay","exit_code":1,"verdict":"problem_app_faults","healed":0,"failed":0,`+
			`"app_faults":{"total":40,"errors":31}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	s.persistResult(&run{ID: "faulty", State: "done", Mode: "replay", ExitCode: 1,
		StartedAt: "2026-07-26T00:00:00Z", FinishedAt: "2026-07-26T00:00:02Z", ArtifactDir: dir})

	rec, body := doJSON(t, s, http.MethodGet, "/v1/results", nil, "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /v1/results = %d (%s)", rec.Code, rec.Body.String())
	}
	list, _ := body["results"].([]any)
	if len(list) == 0 {
		t.Fatalf("results list is empty, so the verdict assertion below would be vacuous: %v", body)
	}
	row, _ := list[0].(map[string]any)
	if row["verdict"] != "problem_app_faults" {
		t.Fatalf("verdict over HTTP = %v want problem_app_faults", row["verdict"])
	}
}
