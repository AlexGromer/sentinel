package main

// Log boundary (M9-LIVE). Turns one merged stdout+stderr stream into the artifacts a person can
// actually read, splitting it by AUDIENCE rather than by severity:
//
//	runs/control-<id>/logs/run.jsonl      structured diagnostics — what the Logs view renders
//	runs/control-<id>/logs/run.log        the raw stream, 1:1 — nothing is ever lost
//	runs/control-<id>/logs/events.jsonl   the AG-UI frames — the run's NARRATIVE, its own stream
//
// Why the split matters. The @@AGUI frames are 82% of a run's output, and they are not log noise:
// they carry "step 2 of 40", "click button 'Sign in'", the healing strategy and its outcome — the
// story of the run. Levelling a story destroys it, so it gets its own file and its own view, and the
// diagnostics file stops being 82% protocol.
//
// The frames stay in the in-memory ring buffer regardless: ws.go's run subscription replays it to
// drive the live timeline. This sink is a SECOND consumer of the same lines (see lineWriter), which
// is what keeps the split from touching the live path at all.
//
// WRITES ARE IMMEDIATE. Collapsing repeats and nesting stack frames are PRESENTATION concerns and
// live on the read side (handleRunLogs), not here. The first version held a record back to count its
// repeats, and a live run proved that wrong twice over: a stuck run emits nothing different, so the
// held record stayed out of the file for as long as the loop lasted — the very case collapsing exists
// to expose — and the repeats in a real run arrive ~5s apart, so any deadline short enough to keep
// the loop visible was also too short to collapse anything. Append every record as it arrives; let
// the reader group them. Nothing is held, nothing is invisible, nothing is lost to a kill.
//
// Nothing here may fail a run. Every write error is swallowed after the first report — a full disk
// must not turn a passing test into a failure.

import (
	"encoding/json"
	"fmt"
	"github.com/AlexGromer/sentinel/internal/redact"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	eventcatalog "github.com/AlexGromer/sentinel/brain"
)

// The wire format brain/eventlog.py emits: `[warn|llm] llm.no_anthropic_key: message`.
var reCatalogued = regexp.MustCompile(`^\[(debug|info|warn|error)\|([a-z]+)\] ([a-z][A-Za-z0-9_.]*): (.*)$`)

const (
	agUIPrefix   = "@@AGUI "
	defaultMaxMB = 50
	rotateSuffix = ".1"
)

// logEnvMB reads a positive megabyte count from the environment. A missing, unparseable or negative
// value falls back to the default rather than disabling rotation by accident; an explicit 0 does
// disable it, which is a legitimate choice for a short debugging run.
func logEnvMB(name string, def int) int {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return def
	}
	return n
}

// logRecord is one line of run.jsonl. Field names are short because a long run writes many of them.
type logRecord struct {
	Seq int    `json:"seq"`
	TS  string `json:"ts"`
	Lvl string `json:"lvl"`
	Cat string `json:"cat"`
	// Src is the source axis (tool / application / testing), derived from Cat so the two cannot
	// disagree. It answers "is my app misbehaving or the tool?" before any subsystem question.
	Src      string `json:"src,omitempty"`
	Mod      string `json:"mod,omitempty"`
	Code     string `json:"code"`
	Msg      string `json:"msg"`
	Phase    string `json:"phase,omitempty"`
	Degrades bool   `json:"degrades,omitempty"`
	// Fault is set only on a record that can END the run (HEALTH-004): whose problem the ending is.
	// It rides on the record rather than being recomputed by readers so run.jsonl stays self-describing
	// — the file is what a person greps after the fact, when neither the process nor the UI is around.
	Fault string `json:"fault,omitempty"`
	// N is set by the READER, never on disk: handleRunLogs collapses consecutive identical records
	// and reports how many there were. It is what makes a stuck run legible — a loop reads as one row
	// with a count instead of 34 identical rows nobody scrolls through.
	N int `json:"n,omitempty"`
	// Parent links a stack-trace frame to the error record above it, so one Node failure renders as
	// one problem that expands rather than twenty rows that look like twenty problems.
	Parent int `json:"parent,omitempty"`
	// Raw carries the original line for an unclassified record, so run.jsonl stays self-describing
	// and a person grepping it is never left without the source text.
	Raw string `json:"raw,omitempty"`
	// Step is the run step this record happened during, correlated at the boundary (see logSink.step).
	Step int `json:"step,omitempty"`
}

type logSink struct {
	mu       sync.Mutex
	dir      string
	raw      *os.File
	diag     *os.File
	events   *os.File
	rawSize  int64
	maxBytes int64
	seq      int
	closed   bool
	reported bool // first write error is reported once; the rest are silent

	// stackParent is the seq of the last error a stack frame may attach to, or 0 when the last line
	// was not an error. Frames arrive back-to-back with their error, so this needs no timer.
	stackParent int
	// lastFaultCode/lastFault remember the most recent record that declared a fault — i.e. one the
	// catalogue says can END a run. The exit code alone cannot say whose problem the ending is: exit 3
	// is a refusal to start (tool), a corrupt plan (test) AND a malformed request (config), and until
	// HEALTH-004 all three reached the dashboard as "integrity / plan_hash mismatch".
	//
	// LAST wins rather than first: a run can log a recoverable terminal-class code and carry on (a
	// skipped import file is exit 1 but the run keeps importing), and what decided the ending is the
	// one nearest the end. Read after cmd.Wait, so every line has already been through write().
	lastFaultCode string
	lastFault     string
	// step is the last step number seen on an AG-UI step.progress frame. Both streams share ONE ordered
	// pipe, so a diagnostic that arrives after step N's frame happened during step N — which is how a
	// record learns which step it belongs to without any protocol change. It is what turns "the site
	// threw an error" into "step 4 threw an error".
	step int
}

// newLogSink creates runs/control-<id>/logs/ and opens the three files. A sink that cannot be
// created is returned as nil, and every method tolerates a nil receiver — a run must not depend on
// its log files existing.
func newLogSink(artifactDir string) *logSink {
	dir := filepath.Join(artifactDir, "logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "control-api: cannot create %s: %v (run continues, no log files)\n", dir, err)
		return nil
	}
	open := func(name string) *os.File {
		f, err := os.OpenFile(filepath.Join(dir, name), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "control-api: cannot open %s/%s: %v\n", dir, name, err)
			return nil
		}
		return f
	}
	s := &logSink{dir: dir, raw: open("run.log"), diag: open("run.jsonl"), events: open("events.jsonl"),
		maxBytes: int64(logEnvMB("SENTINEL_LOG_MAX_MB", defaultMaxMB)) << 20}
	if s.raw == nil && s.diag == nil && s.events == nil {
		return nil
	}
	return s
}

// write consumes one line of the merged run output.
func (s *logSink) write(line string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return
	}

	// GAP-SEC-005 / ADR-081: redact BEFORE anything is written, and for every line rather than only the
	// `app.*` ones. This is the single choke point — run.log, run.jsonl and events.jsonl all descend
	// from here — so a secret cannot reach disk by taking another path out of this function.
	line = redact.Line(line)

	s.writeRaw(line)

	// The narrative goes to its own file and NEVER into the diagnostics — that is the whole point of
	// the split. The prefix is stripped so events.jsonl is a clean JSONL stream a reader can consume
	// without knowing about our stdout convention.
	if frame, ok := strings.CutPrefix(line, agUIPrefix); ok {
		s.writeLine(s.events, frame)
		s.noteStep(frame)
		return
	}

	rec := s.classify(line)
	if rec == nil {
		return
	}
	rec.Src = eventcatalog.SourceOf(rec.Cat)
	// HEALTH-004: a code the catalogue marks terminal carries WHOSE problem the ending is, and that is
	// the only place the precise answer exists — see the lastFault field for why the exit code cannot
	// supply it. Recorded on the line AND remembered for the verdict; two readers, one decision.
	if f := eventcatalog.FaultOf(rec.Code); f != "" {
		rec.Fault = f
		s.lastFaultCode, s.lastFault = rec.Code, f
	}
	// A summary belongs to the run, not to whichever step happened to be last. Stamping it would be
	// temporally true and semantically noise — "Explore finished … step 3" invites reading the summary
	// as a fact about step 3.
	if rec.Step == 0 && rec.Phase != "report" {
		rec.Step = s.step
	}
	s.emit(rec)
}

// terminalFault reports the last code that declared a fault, and that fault. Empty when the run
// logged no terminal code at all — which is the normal case for a run that simply passed or whose
// steps simply failed, and is why the caller falls back to the exit code rather than to "unknown".
// Nil-safe: a run whose log files could not be created still has to produce a verdict.
func (s *logSink) terminalFault() (code, fault string) {
	if s == nil {
		return "", ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastFaultCode, s.lastFault
}

// noteStep remembers the step number from a step.progress frame. Parsed narrowly and failure-tolerantly:
// a frame we cannot read must leave the previous step in place rather than clear it, since a missing
// number is worse than a slightly stale one.
func (s *logSink) noteStep(frame string) {
	if !strings.Contains(frame, `"step.progress"`) {
		return
	}
	var env struct {
		Data struct {
			N int `json:"n"`
		} `json:"data"`
	}
	if json.Unmarshal([]byte(frame), &env) == nil && env.Data.N > 0 {
		s.step = env.Data.N
	}
}

// classify turns a line into a record: parsed directly when it came from our own emitter, matched
// against the catalogue's ordered rules when it came from a tool we do not instrument.
func (s *logSink) classify(line string) *logRecord {
	if m := reCatalogued.FindStringSubmatch(line); m != nil {
		lvl, cat, code, msg := m[1], m[2], m[3], m[4]
		rec := &logRecord{Lvl: lvl, Cat: cat, Code: code, Msg: msg, Mod: eventcatalog.Module(code)}
		if e, ok := eventcatalog.Lookup(code); ok {
			// The catalogue wins on level and category: the emitter renders them into the line from
			// the same source, but a stale binary paired with a newer catalogue should follow the
			// catalogue rather than the line.
			rec.Lvl, rec.Cat, rec.Phase, rec.Degrades = e.Lvl, e.Cat, e.Phase, e.Degrades
		}
		return rec
	}
	for _, f := range eventcatalog.ForeignRules() {
		if !f.Matches(line) {
			continue
		}
		// A stack-trace continuation belongs to the error above it, not to a record of its own —
		// otherwise one Node failure becomes 20 rows and reads like 20 problems.
		rec := &logRecord{Lvl: f.Lvl, Cat: f.Cat, Code: f.Code, Msg: line, Raw: line}
		if f.AttachToPrevious {
			if s.stackParent == 0 {
				return rec // a stray frame with no error above it stands on its own
			}
			rec.Parent = s.stackParent
		}
		return rec
	}
	return &logRecord{Lvl: "info", Cat: "system", Code: "system.unclassified", Msg: line, Raw: line}
}

// emit stamps and appends a record immediately, and remembers whether a following stack frame has an
// error to attach to.
func (s *logSink) emit(rec *logRecord) {
	s.seq++
	rec.Seq = s.seq
	rec.TS = time.Now().UTC().Format(time.RFC3339Nano)
	if b, err := json.Marshal(rec); err == nil {
		s.writeLine(s.diag, string(b))
	}
	if rec.Parent == 0 {
		// An error opens a frame-attachment window; anything else closes it, so frames never latch
		// onto an unrelated error further up.
		if rec.Lvl == "error" {
			s.stackParent = rec.Seq
		} else {
			s.stackParent = 0
		}
	}
}

func (s *logSink) writeRaw(line string) {
	if s.raw == nil {
		return
	}
	// Rotate by size so a runaway run cannot fill the disk. One generation is kept: the tail is what
	// diagnoses a failure, and a rotated head is worth more than an unbounded file.
	if s.maxBytes > 0 && s.rawSize+int64(len(line))+1 > s.maxBytes {
		_ = s.raw.Close()
		cur := filepath.Join(s.dir, "run.log")
		_ = os.Rename(cur, cur+rotateSuffix)
		f, err := os.OpenFile(cur, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			s.raw = nil
			return
		}
		s.raw, s.rawSize = f, 0
	}
	n, err := fmt.Fprintln(s.raw, line)
	s.rawSize += int64(n)
	s.report(err)
}

func (s *logSink) writeLine(f *os.File, line string) {
	if f == nil {
		return
	}
	_, err := fmt.Fprintln(f, line)
	s.report(err)
}

// report surfaces the first write failure and stays quiet afterwards — a full disk should say so
// once, not once per line.
func (s *logSink) report(err error) {
	if err == nil || s.reported {
		return
	}
	s.reported = true
	fmt.Fprintf(os.Stderr, "control-api: log write failed in %s: %v (run continues)\n", s.dir, err)
}

// close flushes the held record and closes the files. Safe to call more than once.
func (s *logSink) close() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return
	}
	s.closed = true
	for _, f := range []*os.File{s.raw, s.diag, s.events} {
		if f != nil {
			_ = f.Close()
		}
	}
}
