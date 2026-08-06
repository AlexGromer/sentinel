// Package svclog is the SERVICE-plane journal: what the tool itself did, as opposed to what a run did.
//
// HEALTH-005. Measured 2026-08-04, and the measurement is the whole justification: `runs/<id>/logs/`
// is about a RUN and only a run — `newLogSink` is called from exactly one place, the moment a run is
// spawned. Everything else the product does either went to stderr (the container's journal: not a
// file, not catalogued, not filterable, not in the UI) or was not recorded at all. `session.go` and
// `configfile.go` had ZERO logging lines of either kind, so creating an account, changing a password,
// granting admin, DELETING an account and editing the global config were all traceless. The `users`
// table stores the RESULT (an account exists) and never the EVENT (who created it, when, from where).
//
// WHY A FILE AND NOT A STORE TABLE. `s.store == nil` is a supported tier, not a failure (ADR-075):
// a standalone deployment keeps its config in a file and has no gateway at all. An audit journal that
// is absent exactly where single operators run is not an audit journal. A file also survives the one
// event most worth recording — the store being unreachable — which a store-backed journal could not.
//
// WHY ONE FILE FOR EVERY SERVICE. `svc` names the writer. Four files would mean answering "what
// happened at 14:32?" by opening four of them and merging by hand, which is how a journal stops being
// read. Concurrent appends from separate processes are why each record is written with a single
// `Write` of one complete line: a line is at most a few hundred bytes, far below PIPE_BUF, so
// interleaving cannot tear a record in half.
//
// LEVELS ARE THE ANSWER TO VOLUME, NOT SELECTION (Alex's directive: log everything, and have levels).
// Reads are `debug`, mutations `info`, a refusal or a foreign-row touch `warn`, a server error
// `error`. The default `SENTINEL_SERVICE_LOG_LEVEL=info` keeps the hub's own polling — measured at
// `/v1/runs/{id}` every 2s, so ~300 records per ten-minute run — out of the file, and one variable
// puts it back. Filtering happens at WRITE time deliberately: a journal whose noise floor is a read
// filter still pays the disk for what it hides.
package svclog

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Record is ONE line of a Sentinel log stream — run or service. There is deliberately one struct for
// both: the run journal and the service journal are the same stream shape with different fields
// filled in, and two structs describing one wire format is the drift this codebase keeps paying for
// (see `lvKindOf` against the event catalogue). Every service field is `omitempty`, so a run record
// serialises byte-for-byte as it did before this package existed.
type Record struct {
	Seq int    `json:"seq"`
	TS  string `json:"ts"`
	Lvl string `json:"lvl"`
	Cat string `json:"cat"`
	// Src is the source axis (tool / application / testing), derived from Cat — or overridden per code
	// where the emitter and the subject differ (ADR-115).
	Src      string `json:"src,omitempty"`
	Mod      string `json:"mod,omitempty"`
	Code     string `json:"code"`
	Msg      string `json:"msg"`
	Phase    string `json:"phase,omitempty"`
	Degrades bool   `json:"degrades,omitempty"`
	// Fault (ADR-113) is set only on a record that can END a run: whose problem the ending is.
	Fault string `json:"fault,omitempty"`
	// N is set by the READER, never on disk: consecutive identical records collapse into one carrying
	// how many there were.
	N int `json:"n,omitempty"`
	// Parent links a stack-trace frame to the error record above it.
	Parent int `json:"parent,omitempty"`
	// Raw carries the original line for an unclassified record.
	Raw string `json:"raw,omitempty"`
	// Step is the run step this record happened during. Never set on a service record — the service
	// plane has no steps, and leaving it zero is what makes that legible rather than implied.
	Step int `json:"step,omitempty"`

	// --- service plane (HEALTH-005) ------------------------------------------------------------
	// Svc names the binary that wrote the record: control-api, store-gateway, browser, agentctl.
	Svc string `json:"svc,omitempty"`
	// Actor is WHO caused it, as a person reads it: an account name, "machine" for the bearer token,
	// or "anonymous". Distinct from Owner: an admin deleting someone else's scenario is actor=admin,
	// owner=<the other account>, and collapsing the two would lose exactly the case worth recording.
	Actor string `json:"actor,omitempty"`
	// Owner is WHOSE the affected row or event is. Scoped reads compare against this.
	Owner  string `json:"owner,omitempty"`
	Method string `json:"method,omitempty"`
	Route  string `json:"route,omitempty"`
	Status int    `json:"status,omitempty"`
	DurMs  int64  `json:"dur_ms,omitempty"`
	// Foreign marks a call that touched a row belonging to somebody else and was refused for it.
	Foreign bool `json:"foreign,omitempty"`
}

// Rank orders the levels. Shared with the run-log reader so one vocabulary decides both what is
// written and what is shown; an unknown level ranks 0 and is therefore never filtered out, because a
// record we cannot classify is the last thing that should disappear.
func Rank(lvl string) int {
	switch strings.ToLower(strings.TrimSpace(lvl)) {
	case "debug":
		return 10
	case "info":
		return 20
	case "warn":
		return 30
	case "error":
		return 40
	}
	return 0
}

const (
	// FileName is the journal, and Rotated is its one kept generation. One generation rather than N:
	// the tail is what answers a question, and an unbounded audit file is a disk-filling surface an
	// operator did not ask for. Deleting more than that is an explicit operator command (ADR-100's
	// posture), never an automatic sweep — an audit record that removes itself is not evidence.
	FileName = "service.jsonl"
	Rotated  = "service.jsonl.1"

	defaultMaxMB = 32
	// DefaultLevel keeps read traffic out of the file unless it is asked for. `debug` is the setting
	// that records every API call including the hub's own polling.
	DefaultLevel = "info"
)

// Writer appends records to the service journal. A nil *Writer is safe to use: every method tolerates
// it, because no service may fail to start merely because it could not open its log.
type Writer struct {
	mu       sync.Mutex
	path     string
	rotated  string
	f        *os.File
	size     int64
	maxBytes int64
	seq      int
	svc      string
	minRank  int
	closed   bool
	// reported ensures a broken journal says so ONCE. Silence would be the very defect this package
	// exists to remove; a message per record would bury it.
	reported bool
}

// Open prepares <stateDir>/logs/service.jsonl for appending.
//
// Returns nil — never an error — when the journal cannot be opened, and prints why. A service that
// refused to start because its audit file was unavailable would convert a logging problem into an
// outage; a service that starts silently without one would be the silent degradation HEALTH-002
// forbids. Saying so once and continuing is the third option, and the one this product already takes
// for the run sink.
func Open(stateDir, svc string) *Writer {
	dir := filepath.Join(stateDir, "logs")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		fmt.Fprintf(os.Stderr, "%s: cannot create %s: %v (service journal disabled)\n", svc, dir, err)
		return nil
	}
	path := filepath.Join(dir, FileName)
	// 0640, not 0644: the journal carries account names, routes and addresses. The directory is 0750
	// for the same reason. Neither is a substitute for the read endpoint's scoping — it is the floor
	// under it, for whoever has the disk.
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: cannot open %s: %v (service journal disabled)\n", svc, path, err)
		return nil
	}
	var size int64
	if st, err := f.Stat(); err == nil {
		size = st.Size()
	}
	return &Writer{
		path: path, rotated: filepath.Join(dir, Rotated), f: f, size: size, svc: svc,
		maxBytes: int64(envMB("SENTINEL_SERVICE_LOG_MAX_MB", defaultMaxMB)) << 20,
		minRank:  Rank(envStr("SENTINEL_SERVICE_LOG_LEVEL", DefaultLevel)),
	}
}

// Log stamps and appends one record. Fields the caller left empty are filled in: the writing service,
// the timestamp, the sequence, and the level's default of `info`.
//
// `redact` is applied by the CALLER rather than here, and that is deliberate: this package would
// otherwise import it and the trace redactor would gain a second entry point. The one caller that
// writes foreign-influenced text (the API hook, which carries account names and routes) redacts
// before calling.
func (w *Writer) Log(r Record) {
	if w == nil {
		return
	}
	if r.Lvl == "" {
		r.Lvl = "info"
	}
	if Rank(r.Lvl) < w.minRank {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed || w.f == nil {
		return
	}
	w.seq++
	r.Seq, r.Svc = w.seq, w.svc
	if r.TS == "" {
		r.TS = time.Now().UTC().Format(time.RFC3339Nano)
	}
	b, err := json.Marshal(&r)
	if err != nil {
		return
	}
	w.rotateLocked(int64(len(b)) + 1)
	n, err := w.f.Write(append(b, '\n'))
	w.size += int64(n)
	if err != nil && !w.reported {
		w.reported = true
		fmt.Fprintf(os.Stderr, "%s: service journal write failed: %v (further failures are silent)\n", w.svc, err)
	}
}

// rotateLocked keeps ONE previous generation, so a runaway writer cannot fill the disk while the
// recent past stays readable. The caller holds the lock.
func (w *Writer) rotateLocked(incoming int64) {
	if w.maxBytes <= 0 || w.size+incoming <= w.maxBytes {
		return
	}
	_ = w.f.Close()
	_ = os.Rename(w.path, w.rotated)
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		w.f = nil
		return
	}
	w.f, w.size = f, 0
}

func (w *Writer) Close() {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f != nil {
		_ = w.f.Close()
	}
	w.closed = true
}

// Supervisor reports how this process was started — measured, never guessed.
//
// The question is Alex's: "even the first stage — bringing the service up, whether that is docker
// compose, systemctl or anything else". An operator reading "the service restarted at 03:14" needs to
// know whether something restarted it or a person did.
//
// Every signal here is one the supervisor itself sets, so nothing is inferred from our own
// configuration: systemd exports INVOCATION_ID and JOURNAL_STREAM to the units it starts, and a
// container has /.dockerenv or a `docker`/`containerd` scope in its cgroup path. When neither holds,
// the answer is "manual" — and when the cgroup cannot even be read, it is "unknown" rather than a
// guess dressed as a fact.
func Supervisor() string {
	if os.Getenv("INVOCATION_ID") != "" || os.Getenv("JOURNAL_STREAM") != "" {
		return "systemd"
	}
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return "container"
	}
	b, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return "unknown"
	}
	s := string(b)
	if strings.Contains(s, "docker") || strings.Contains(s, "containerd") || strings.Contains(s, "kubepods") {
		return "container"
	}
	return "manual"
}

func envStr(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

// envMB reads a positive megabyte count. A missing, unparseable or negative value falls back to the
// default rather than raising — a typo in a deployment variable must not stop a service from starting.
// `0` is explicit and means "never rotate".
func envMB(name string, def int) int {
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
