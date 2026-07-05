package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// TestVerdictEnumAndDuration covers the two pure helpers M15 adds.
func TestVerdictEnumAndDuration(t *testing.T) {
	for exit, want := range map[int]string{0: "pass", 1: "problem", 2: "regression", 3: "integrity", 7: "problem"} {
		if got := verdictEnum(exit); got != want {
			t.Fatalf("verdictEnum(%d)=%q want %q", exit, got, want)
		}
	}
	if d := durationMs("2026-07-05T00:00:00Z", "2026-07-05T00:00:03Z"); d != 3000 {
		t.Fatalf("durationMs=%d want 3000", d)
	}
	if d := durationMs("bad", "worse"); d != 0 {
		t.Fatalf("durationMs(unparseable)=%d want 0", d)
	}
	if d := durationMs("2026-07-05T00:00:05Z", "2026-07-05T00:00:00Z"); d != 0 {
		t.Fatalf("durationMs(negative)=%d want 0 (clamped)", d)
	}
}

// TestPersistResultOnFinish exercises the finish-goroutine wiring (main.go's persistResult, M15):
// a heal-report + plan.json artifact -> a ResultRecord (verdict enum + heal/fail + coverage + duration)
// AND metric points (trends) carrying labels_json={mode,target} (the ADR-056 commercial-BI seam).
func TestPersistResultOnFinish(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	dir := t.TempDir()
	// replay run: heal-report.json carries steps/heal/fail/regressions
	heal := `{"plan_id":"run7-plan","mode":"replay","exit_code":2,"healed":1,"failed":0,` +
		`"steps":[{"step_id":1},{"step_id":2}],"regressions":[{"kinds":["visual"]}]}`
	if err := os.WriteFile(filepath.Join(dir, "heal-report.json"), []byte(heal), 0o644); err != nil {
		t.Fatal(err)
	}
	// coverage comes from plan.json (authoring artifact); present here to prove BOTH are read
	if err := os.WriteFile(filepath.Join(dir, "plan.json"), []byte(`{"plan_id":"run7-plan","coverage_achieved":0.75}`), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: "run7", State: "done", Target: "https://app.example", Mode: "replay", ExitCode: 2,
		StartedAt: "2026-07-05T00:00:00Z", FinishedAt: "2026-07-05T00:00:04Z", ArtifactDir: dir}
	s.persistResult(rec)

	rr, ok := sc.getResult("run7")
	if !ok {
		t.Fatal("persistResult: result not saved")
	}
	if rr.Verdict != "regression" || rr.ExitCode != 2 || rr.Healed != 1 || rr.Failed != 0 {
		t.Fatalf("result = %+v", rr)
	}
	if rr.Coverage != 0.75 {
		t.Fatalf("coverage = %v want 0.75 (from plan.json)", rr.Coverage)
	}
	if rr.DurationMs != 4000 {
		t.Fatalf("duration_ms = %d want 4000", rr.DurationMs)
	}
	if rr.PlanId != "run7-plan" {
		t.Fatalf("plan_id = %q want run7-plan", rr.PlanId)
	}

	// all 7 metric points ingested with the correct name->value mapping (catches a pts-slice transposition);
	// exactly 1 point per name (only run7 exists) also asserts the count.
	mval := func(name string) float64 {
		ms, err := sc.cl.QueryMetrics(context.Background(), &storepb.MetricsQuery{Name: name})
		if err != nil || len(ms.Points) != 1 {
			t.Fatalf("QueryMetrics(%s) = %+v err=%v (want exactly 1 point)", name, ms, err)
		}
		return ms.Points[0].Value
	}
	for name, want := range map[string]float64{
		"pass": 0, "coverage": 0.75, "healed": 1, "failed": 0, "regressions": 1, "steps": 2, "duration_ms": 4000,
	} {
		if got := mval(name); got != want {
			t.Fatalf("metric %q = %v want %v", name, got, want)
		}
	}
	// the coverage point carries labels_json {mode,target} (the ADR-056 commercial-BI seam)
	ms, _ := sc.cl.QueryMetrics(context.Background(), &storepb.MetricsQuery{Name: "coverage"})
	var lbl map[string]string
	if json.Unmarshal([]byte(ms.Points[0].LabelsJson), &lbl) != nil || lbl["mode"] != "replay" || lbl["target"] != "https://app.example" {
		t.Fatalf("labels_json = %q (want {mode:replay,target:https://app.example})", ms.Points[0].LabelsJson)
	}
	// the Trends RPC (SPA sparkline feed) returns the coverage series chronologically
	if tr, ok := sc.trends("coverage", 10); !ok || len(tr.Points) != 1 || tr.Points[0].Value != 0.75 {
		t.Fatalf("coverage trend = %+v ok=%v", tr, ok)
	}

	// fail-open: no artifacts -> still saves a minimal record (verdict from exit), no panic
	rec2 := &run{ID: "run8", State: "done", Mode: "goal", ExitCode: 0, StartedAt: "2026-07-05T00:00:00Z",
		FinishedAt: "2026-07-05T00:00:01Z", ArtifactDir: t.TempDir()}
	s.persistResult(rec2)
	if rr2, ok := sc.getResult("run8"); !ok || rr2.Verdict != "pass" {
		t.Fatalf("minimal result = %+v ok=%v (verdict from exit 0)", rr2, ok)
	}

	// a run that FAILED TO SPAWN (State="failed", ExitCode stays 0) must NOT be recorded as a result —
	// else verdictEnum(0)="pass" would log a crash as a pass and inflate the pass-rate (verify finding).
	recFail := &run{ID: "run-nospawn", State: "failed", Error: "exec: agentctl not found",
		StartedAt: "2026-07-05T00:00:00Z", FinishedAt: "2026-07-05T00:00:00Z", ArtifactDir: t.TempDir()}
	s.persistResult(recFail)
	if _, ok := sc.getResult("run-nospawn"); ok {
		t.Fatal("persistResult: a failed-to-spawn run must not be recorded as a result")
	}

	// no store configured -> must not panic (fail-open)
	newTestServer().persistResult(rec)
}

// TestResultsTrendsHTTP drives the /v1/results + /v1/trends surface through the real mux (auth gating).
func TestResultsTrendsHTTP(t *testing.T) {
	sc, err := newStoreClient(startTestGateway(t, ""), "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s := storeBackedTestServer(sc)

	sc.saveResult(&storepb.ResultRecord{RunId: "r1", Verdict: "pass", ExitCode: 0, Coverage: 0.9, DurationMs: 1200, Healed: 2})
	sc.ingestMetrics(&storepb.MetricsBatch{Points: []*storepb.MetricPoint{
		{RunId: "r1", Name: "coverage", Value: 0.9, LabelsJson: `{"mode":"goal"}`},
	}})

	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/results", nil, ""); rec.Code != http.StatusForbidden {
		t.Fatalf("results without token: got %d want 403", rec.Code)
	}
	rec, body := doJSON(t, s, http.MethodGet, "/v1/results", nil, s.token)
	if results, _ := body["results"].([]any); rec.Code != http.StatusOK || len(results) != 1 {
		t.Fatalf("list results: code=%d body=%v", rec.Code, body)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/results/r1", nil, s.token)
	if rec.Code != http.StatusOK || body["verdict"] != "pass" || body["coverage"] != 0.9 {
		t.Fatalf("get result: code=%d body=%v", rec.Code, body)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/results/nope", nil, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("get missing result: got %d want 404", rec.Code)
	}
	rec, body = doJSON(t, s, http.MethodGet, "/v1/trends?metric=coverage&window=10", nil, s.token)
	if points, _ := body["points"].([]any); rec.Code != http.StatusOK || len(points) != 1 {
		t.Fatalf("trends: code=%d body=%v", rec.Code, body)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/trends", nil, s.token); rec.Code != http.StatusBadRequest {
		t.Fatalf("trends without metric param: got %d want 400", rec.Code)
	}
}

// TestResultsMetricsFailOpenNoStore mirrors the other domains: no gateway -> reads degrade to
// empty/404, /v1/trends needs a metric (400), nothing 503s or panics.
func TestResultsMetricsFailOpenNoStore(t *testing.T) {
	s := newTestServer() // s.store is nil
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/results", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("list results no store: got %d want 200 (graceful empty)", rec.Code)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/results/x", nil, s.token); rec.Code != http.StatusNotFound {
		t.Fatalf("get result no store: got %d want 404", rec.Code)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/trends?metric=coverage", nil, s.token); rec.Code != http.StatusOK {
		t.Fatalf("trends no store: got %d want 200 (graceful empty)", rec.Code)
	}
	if rec, _ := doJSON(t, s, http.MethodGet, "/v1/trends", nil, s.token); rec.Code != http.StatusBadRequest {
		t.Fatalf("trends no metric: got %d want 400", rec.Code)
	}
}
