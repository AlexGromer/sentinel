// QA-REPORT-SERVICE (ADR-119): the aggregate Prometheus scrape, moved off the deleted
// `cmd/report-service` and onto the process that actually runs in every deployment.
//
// WHY IT COULD NOT BE MOVED AS IT WAS. report-service concatenated `runs/*/metrics.prom` byte for
// byte. That never worked and nobody saw it, because the binary was built, packaged and signed but
// launched by nothing — no compose file, no Dockerfile, no install script. brain/report.py::_metrics
// writes BARE series: `sentinel_run_steps 7`, no `# HELP`, no `# TYPE`, and no label that separates
// one run from another. Concatenating two runs therefore produces the SAME series twice with the same
// (empty) label set, which is not an aggregate — it is a duplicate sample, and a scrape containing one
// is a broken scrape however the server chooses to react. So this file does three things the
// concatenation did not: it injects the run identity as a label, it groups the samples by family and
// prints the `# HELP`/`# TYPE` header each family is supposed to carry, and it refuses a run whose file
// would collide anyway.
//
// The per-run artifact is left exactly as it is. It is documented as a node_exporter textfile
// (brain/report.py) and served file-by-file through the artifact whitelist, and 192 run directories on
// this repository alone already exist WITHOUT a run label — a fix at the producer would not reach a
// single one of them. Labelling here is what makes old and new runs aggregate the same way.
//
// SCOPING. The route is `authed`, and the numbers are filtered to the caller's own runs. The aggregate
// is where ADR-109's "unowned rows stay visible" rule deliberately does NOT apply: that rule exists so
// runs created before accounts existed remain reachable BY NAME, and /v1/runs/{id} still answers for
// them. Summing runs nobody can be shown to own into one number for whoever asks is a different act,
// and it is the shape of ADR-109's original defect — an aggregate has no `{id}` for the guard to
// resolve, so the scoping has to live in the handler (the same place, and the same reason, as
// handleListRuns and GET /v1/service-log).
//
// The machine token and a deployment with no accounts are unscoped (`owner == ""`) and see everything:
// that is the operator's scrape, and it is the only caller Prometheus should be configured as.
//
// A consequence worth stating because it is a design choice and not an accident: a store that does not
// answer makes ownerOfRow report "no row", which for a SCOPED caller means the run is not theirs and is
// omitted. Scoping fails CLOSED — the same decision accountsExist makes for the same reason.
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// metricsCacheTTL bounds how often a caller can drive a real walk of runs/. The route is
	// credentialled, so this is not the amplifier defence /readyz needs — it is a bound on disk work
	// that grows with the number of runs, on a route a scraper polls forever. Ten seconds is under any
	// sane scrape interval, so a scraper still gets fresh numbers every scrape.
	metricsCacheTTL = 10 * time.Second
	// metricsMaxRuns caps how many run directories one scrape reads, NEWEST FIRST. Without it the
	// response grows linearly with the number of runs ever performed and never shrinks — measured on
	// this repository: 192 `runs/control-*` directories plus 115 parked ones. What is dropped is
	// REPORTED (sentinel_metrics_runs_omitted), because a silent cap reads exactly like full coverage.
	metricsMaxRuns = 500
	// metricsMaxFileBytes bounds one artifact. A run's metrics.prom is a few hundred bytes; anything
	// this size is not one, and reading it into memory on a polled route is how a scrape becomes a
	// memory event.
	metricsMaxFileBytes = 1 << 20
)

// runSamples is one run's contribution: its identity, who owns it, and its samples already carrying
// the run label, bucketed by family.
type runSamples struct {
	id       string
	owner    string
	families map[string][]string
}

// metricsScan is the memoized result of one walk of runs/. It holds every run, unfiltered — the
// per-caller scoping is applied when the response is rendered, so one walk serves every caller and no
// caller's view is ever cached under another's credential.
type metricsScan struct {
	at           time.Time
	runs         []runSamples
	omitted      map[string]int // reason -> run directories not included
	linesDropped int
}

type metricsState struct {
	mu       sync.Mutex
	scan     *metricsScan
	inflight bool
	done     chan struct{}
}

// omission reasons. Declared as constants rather than written at each site so the label values in the
// response cannot drift from the ones the gate asserts.
const (
	omitCap        = "cap"
	omitUnreadable = "unreadable"
	omitTooLarge   = "too_large"
	omitConflict   = "conflict"
)

// omitReasons is the full set, printed even at zero. A series that appears only once something goes
// wrong cannot be alerted on, because there is no baseline to compare it against.
var omitReasons = []string{omitCap, omitUnreadable, omitTooLarge, omitConflict}

// handleMetricsAgg serves the aggregate scrape.
func (s *server) handleMetricsAgg(w http.ResponseWriter, r *http.Request) {
	c, _ := s.callerOf(r)
	owner := c.owner() // "" = machine token, or a deployment with no accounts: unscoped
	scan := s.metricsScan()

	// Group across runs: a Prometheus text response must carry every sample of a family together,
	// under one HELP/TYPE header. Concatenating whole files cannot do that even in principle.
	byFamily := map[string][]string{}
	included := 0
	for i := range scan.runs {
		rs := &scan.runs[i]
		if owner != "" && rs.owner != owner {
			continue
		}
		included++
		for fam, lines := range rs.families {
			byFamily[fam] = append(byFamily[fam], lines...)
		}
	}

	var b strings.Builder
	fams := make([]string, 0, len(byFamily))
	for fam := range byFamily {
		fams = append(fams, fam)
	}
	sort.Strings(fams)
	for _, fam := range fams {
		lines := byFamily[fam]
		sort.Strings(lines)
		writeFamily(&b, fam, lines)
	}

	// The aggregator's own numbers. They answer the question the old concatenation left unanswerable:
	// how much of the truth is in this response.
	writeFamily(&b, "sentinel_metrics_runs_included",
		[]string{fmt.Sprintf("sentinel_metrics_runs_included %d", included)})
	omitLines := make([]string, 0, len(omitReasons))
	for _, reason := range omitReasons {
		omitLines = append(omitLines, fmt.Sprintf("sentinel_metrics_runs_omitted{reason=%q} %d", reason, scan.omitted[reason]))
	}
	sort.Strings(omitLines)
	writeFamily(&b, "sentinel_metrics_runs_omitted", omitLines)
	writeFamily(&b, "sentinel_metrics_lines_dropped",
		[]string{fmt.Sprintf("sentinel_metrics_lines_dropped %d", scan.linesDropped)})

	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = io.WriteString(w, b.String())
}

// writeFamily prints one metric family with the header every family is supposed to have.
//
// The TYPE is DERIVED from the name (`_total` is a counter, everything else a gauge) rather than read
// from a table of families kept here. A table would be a second statement of what brain/report.py
// emits, and the failure mode of a second statement is that a metric added there arrives here
// unannounced and untyped — which is the whole class of defect principle 5 is about.
func writeFamily(b *strings.Builder, family string, lines []string) {
	if len(lines) == 0 {
		return
	}
	fmt.Fprintf(b, "# HELP %s %s\n", family, helpFor(family))
	kind := "gauge"
	if strings.HasSuffix(family, "_total") {
		kind = "counter"
	}
	fmt.Fprintf(b, "# TYPE %s %s\n", family, kind)
	for _, l := range lines {
		b.WriteString(l)
		b.WriteByte('\n')
	}
}

// helpFor describes a family. The three families this file produces itself get a real sentence; every
// family that came off disk gets the one true thing that can be said about it without keeping a copy of
// the producer's documentation here.
func helpFor(family string) string {
	switch family {
	case "sentinel_metrics_runs_included":
		return "Run directories whose metrics are included in this response, after scoping to the caller."
	case "sentinel_metrics_runs_omitted":
		return "Run directories the aggregator did not include, by reason."
	case "sentinel_metrics_lines_dropped":
		return "Sample lines skipped because they are not Prometheus textfile syntax."
	}
	return "Sentinel per-run metric, aggregated from runs/<id>/metrics.prom by control-api."
}

// metricsScan returns the memoized walk, refreshing it at most once per metricsCacheTTL. The disk I/O
// and the owner lookups run with NO lock held, and concurrent callers wait on the in-flight walk rather
// than starting their own — the shape readiness() uses, for the same reason.
func (s *server) metricsScan() *metricsScan {
	s.metrics.mu.Lock()
	for {
		if s.metrics.scan != nil && time.Since(s.metrics.scan.at) < metricsCacheTTL {
			sc := s.metrics.scan
			s.metrics.mu.Unlock()
			return sc
		}
		if s.metrics.inflight {
			done := s.metrics.done
			s.metrics.mu.Unlock()
			<-done
			s.metrics.mu.Lock()
			continue
		}
		break
	}
	s.metrics.inflight = true
	s.metrics.done = make(chan struct{})
	done := s.metrics.done
	s.metrics.mu.Unlock()

	sc := s.walkRunMetrics() // disk + store I/O, NO lock held

	s.metrics.mu.Lock()
	s.metrics.inflight = false
	close(done)
	s.metrics.scan = sc
	s.metrics.mu.Unlock()
	return sc
}

// walkRunMetrics reads every run directory's metrics.prom, newest first, up to the cap.
//
// ⚠ It cannot fail the request. This repository has run directories owned by root (left by containers
// that run without a `user:`) which no amount of correct code can read; a scrape that answered 5xx
// because one directory is unreadable would take the whole deployment's numbers down over one
// leftover. Unreadable is COUNTED and skipped.
func (s *server) walkRunMetrics() *metricsScan {
	sc := &metricsScan{at: time.Now(), omitted: map[string]int{}}
	dir := filepath.Join(s.repo, "runs")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return sc // no runs/ yet: an empty aggregate is the honest answer, not an error
	}

	type cand struct {
		name string
		mod  time.Time
	}
	cands := make([]cand, 0, len(entries))
	for _, e := range entries {
		// IsDir() is false for a symlink (DirEntry reports the link's own type), so a link planted in
		// runs/ cannot make this read outside the tree. Dot-directories are skipped: runs/.park is where
		// finished runs are moved to keep `go test ./...` off them.
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		info, ierr := e.Info()
		if ierr != nil {
			sc.omitted[omitUnreadable]++
			continue
		}
		cands = append(cands, cand{name: e.Name(), mod: info.ModTime()})
	}
	sort.Slice(cands, func(i, j int) bool {
		if cands[i].mod.Equal(cands[j].mod) {
			return cands[i].name < cands[j].name // ties broken by name so a scrape is reproducible
		}
		return cands[i].mod.After(cands[j].mod)
	})
	if len(cands) > metricsMaxRuns {
		sc.omitted[omitCap] += len(cands) - metricsMaxRuns
		cands = cands[:metricsMaxRuns]
	}

	for _, c := range cands {
		id := runIDOfDir(c.name)
		path := filepath.Join(dir, c.name, "metrics.prom")
		info, serr := os.Stat(path)
		if serr != nil {
			if os.IsNotExist(serr) {
				continue // a run that produced no metrics is not an omission; most runs are this
			}
			sc.omitted[omitUnreadable]++
			continue
		}
		if info.Size() > metricsMaxFileBytes {
			sc.omitted[omitTooLarge]++
			continue
		}
		raw, rerr := os.ReadFile(path)
		if rerr != nil {
			sc.omitted[omitUnreadable]++
			continue
		}
		fams, dropped, conflict := parseRunMetrics(string(raw), id)
		if conflict {
			// The file already declares a `run` label. Adding ours would emit the same label name twice,
			// which is invalid; keeping theirs would let two runs claim one identity. Neither is a scrape
			// worth serving, so this run is left out and said so.
			sc.omitted[omitConflict]++
			continue
		}
		sc.linesDropped += dropped
		if len(fams) == 0 {
			continue
		}
		owner, _ := s.ownerOfRow(domainRun, id)
		sc.runs = append(sc.runs, runSamples{id: id, owner: owner, families: fams})
	}
	return sc
}

// runIDOfDir maps a run DIRECTORY to the run id the rest of the product uses. control-api writes
// `runs/control-<id>` (main.go, spawnRun); `agentctl run` writes `runs/<id>` directly. Both are one run
// to the person looking at it, so both resolve to the same identifier here — which is also what makes
// the owner lookup below able to find a row at all.
func runIDOfDir(name string) string {
	return strings.TrimPrefix(name, "control-")
}

// parseRunMetrics turns one metrics.prom into samples carrying `run="<id>"`, bucketed by family.
// It returns the number of lines it could not read as syntax, and whether the file already carries a
// `run` label (see the caller).
func parseRunMetrics(body, id string) (map[string][]string, int, bool) {
	out := map[string][]string{}
	dropped := 0
	for _, line := range strings.Split(body, "\n") {
		t := strings.TrimSpace(line)
		// Comments are dropped rather than passed through: the file's own header is one line of prose,
		// and a `# HELP` from a per-run file would be repeated once per run in a response that prints
		// exactly one per family.
		if t == "" || strings.HasPrefix(t, "#") {
			continue
		}
		family, labels, value, ok := splitSample(t)
		if !ok {
			dropped++
			continue
		}
		if hasRunLabel(labels) {
			return nil, 0, true
		}
		inner := `run="` + escapeLabelValue(id) + `"`
		if labels != "" {
			inner += "," + labels
		}
		out[family] = append(out[family], family+"{"+inner+"} "+value)
	}
	return out, dropped, false
}

// splitSample takes `name`, optional `{labels}`, and the value off one textfile line. Written by hand
// rather than pulled in as a dependency: the input is one generator's output (brain/report.py), the
// grammar needed is this small, and a Prometheus client library would bring an exposition format we do
// not produce and a registry we do not use.
func splitSample(line string) (family, labels, value string, ok bool) {
	i := 0
	for i < len(line) && isNameByte(line[i], i == 0) {
		i++
	}
	if i == 0 {
		return "", "", "", false
	}
	family = line[:i]
	rest := line[i:]
	if strings.HasPrefix(rest, "{") {
		end := closingBrace(rest)
		if end < 0 {
			return "", "", "", false
		}
		labels = strings.TrimSpace(rest[1:end])
		rest = rest[end+1:]
	}
	value = strings.TrimSpace(rest)
	if value == "" || (len(rest) > 0 && !isSpaceByte(rest[0])) {
		return "", "", "", false
	}
	// A sample line is `name value [timestamp]`; anything else is not one.
	if len(strings.Fields(value)) > 2 {
		return "", "", "", false
	}
	return family, labels, value, true
}

// closingBrace finds the `}` that closes the label set, ignoring braces inside quoted label values.
func closingBrace(s string) int {
	inQuote := false
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '\\':
			if inQuote {
				i++ // the escaped character cannot end the quote
			}
		case '"':
			inQuote = !inQuote
		case '}':
			if !inQuote {
				return i
			}
		}
	}
	return -1
}

// hasRunLabel reports whether a label set already binds `run`.
func hasRunLabel(labels string) bool {
	depth := 0
	name := strings.Builder{}
	inQuote := false
	for i := 0; i < len(labels); i++ {
		ch := labels[i]
		if inQuote {
			if ch == '\\' {
				i++
				continue
			}
			if ch == '"' {
				inQuote = false
			}
			continue
		}
		switch ch {
		case '"':
			inQuote = true
		case '=':
			if depth == 0 && strings.TrimSpace(name.String()) == "run" {
				return true
			}
			depth = 1
		case ',':
			depth = 0
			name.Reset()
		default:
			if depth == 0 {
				name.WriteByte(ch)
			}
		}
	}
	return false
}

func isNameByte(b byte, first bool) bool {
	switch {
	case b >= 'a' && b <= 'z', b >= 'A' && b <= 'Z', b == '_', b == ':':
		return true
	case b >= '0' && b <= '9':
		return !first
	}
	return false
}

func isSpaceByte(b byte) bool { return b == ' ' || b == '\t' }

// escapeLabelValue applies the escaping the exposition format requires. A run id is hex today, but the
// id comes from a DIRECTORY NAME, and a directory name is not something this process chose.
func escapeLabelValue(v string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`)
	return r.Replace(v)
}
