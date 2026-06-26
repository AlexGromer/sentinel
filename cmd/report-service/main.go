// Command report-service (M8, ADR-021) serves Sentinel run reports over HTTP for the long-lived
// orchestrator/service mode — distinct from the ephemeral CronJob's Pushgateway path (ADR-018,
// reconciled: a long-lived process IS scrapeable; an ephemeral job is not, so it pushes).
//
//	GET /report/<run_id>   -> HTML (or JSON with ?format=json) from <runs>/<run_id>/heal-report.json
//	GET /metrics           -> Prometheus text (concatenated <runs>/*/metrics.prom)
//	GET /healthz           -> ok
//
// Reads artifacts written by the brain's M4 report generator. Uses only the Go stdlib (no new deps).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"html/template"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

var reportTmpl = template.Must(template.New("report").Parse(`<!doctype html>
<html><head><meta charset="utf-8"><title>Sentinel — {{.PlanID}}</title>
<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>
</head><body>
<h1>Sentinel run {{.PlanID}}</h1>
<p>mode={{.Mode}} · exit={{.ExitCode}} · healed={{.Healed}} · failed={{.Failed}} · regressions={{len .Regressions}}</p>
<table><tr><th>step</th><th>type</th><th>outcome</th></tr>
{{range .Steps}}<tr><td>{{.StepID}}</td><td>{{.Type}}</td><td>{{.Outcome}}</td></tr>
{{end}}</table>
</body></html>`))

type step struct {
	StepID  int    `json:"step_id"`
	Type    string `json:"type"`
	Outcome string `json:"outcome"`
}

type report struct {
	PlanID      string           `json:"plan_id"`
	Mode        string           `json:"mode"`
	ExitCode    int              `json:"exit_code"`
	Healed      int              `json:"healed"`
	Failed      int              `json:"failed"`
	Regressions []map[string]any `json:"regressions"`
	Steps       []step           `json:"steps"`
}

func loadReport(path string) (*report, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var r report
	if err := json.Unmarshal(b, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

func main() {
	addr := flag.String("addr", ":8089", "HTTP listen address")
	runsDir := flag.String("runs", "runs", "runs directory")
	flag.Parse()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("/report/", func(w http.ResponseWriter, req *http.Request) {
		id := strings.TrimPrefix(req.URL.Path, "/report/")
		if id == "" || strings.Contains(id, "..") || strings.ContainsRune(id, '/') {
			http.Error(w, "bad run id", http.StatusBadRequest)
			return
		}
		p := filepath.Join(*runsDir, id, "heal-report.json")
		r, err := loadReport(p)
		if err != nil {
			http.Error(w, "report not found", http.StatusNotFound)
			return
		}
		if req.URL.Query().Get("format") == "json" {
			w.Header().Set("Content-Type", "application/json")
			if b, rerr := os.ReadFile(p); rerr == nil {
				_, _ = w.Write(b)
			}
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_ = reportTmpl.Execute(w, r)
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		entries, _ := os.ReadDir(*runsDir)
		var names []string
		for _, e := range entries {
			if e.IsDir() {
				names = append(names, e.Name())
			}
		}
		sort.Strings(names)
		for _, n := range names {
			if b, err := os.ReadFile(filepath.Join(*runsDir, n, "metrics.prom")); err == nil {
				_, _ = w.Write(b)
				fmt.Fprintln(w)
			}
		}
	})

	fmt.Fprintf(os.Stderr, "[report-service] listening on %s runs=%s\n", *addr, *runsDir)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		fmt.Fprintf(os.Stderr, "report-service: %v\n", err)
		os.Exit(1)
	}
}
