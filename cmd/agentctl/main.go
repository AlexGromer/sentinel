// Command agentctl is the Sentinel control-plane CLI.
//
// Subcommands:
//
//	agentctl run --target <URL> [--planner h|llm] [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]
//	agentctl run --target <URL> --mode chat --conversation-id <id> [--goal <g>|--describe <d>]   (M9.10 multi-turn, ADR-048)
//	agentctl baseline update --plan <plan.json> [--target <URL>]   (the only golden-baseline mutation path)
//	agentctl locators clear-quarantine
//
// It spawns the Python brain (venv) via subprocess + env (no gRPC yet; M2b) and propagates the
// brain's structured exit code (0 pass / 1 step-fail / 2 golden regression / 3 integrity: plan_hash or golden HMAC).
package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"github.com/AlexGromer/sentinel/internal/redact"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// version is stamped at release build via -ldflags "-X main.version=<tag>" (release.yml); "dev" otherwise.
var version = "dev"

func newRunID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

// newToken mints the per-run shared secret that authenticates brain->store-gateway calls (#23).
// It is passed to the gateway (STORE_TOKEN env) and the brain (STORE_TOKEN run-var), never to argv.
func newToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		// #34: fail closed. A degraded "" token would silently disable authN on BOTH the gateway
		// and the brain (empty STORE_TOKEN → no interceptor), re-opening the #23 surface on a
		// (rare) RNG failure. Abort instead of degrading.
		fmt.Fprintf(os.Stderr, "agentctl: rand.Read for store token: %v\n", err)
		os.Exit(1)
	}
	return hex.EncodeToString(b)
}

// usage lists EVERY subcommand. ADR-088: it used to list four of seven, and the three it omitted were
// the ones a user most needs — `report` is the sole producer of report.html, report.json, metrics.prom
// and junit.xml, and `export-spec` is the migration path to @playwright/test that competitors sell as a
// feature. A subcommand absent from usage exists only for someone reading main.go's switch.
func usage() {
	fmt.Fprintln(os.Stderr, "usage:")
	fmt.Fprintln(os.Stderr, "  agentctl run --target <URL> [--planner heuristic|llm|goal] [--goal <g>|--describe <d>]")
	fmt.Fprintln(os.Stderr, "               [--replay --plan <p>] [--aut-version <sha>] [--ci] [--force-replay]")
	fmt.Fprintln(os.Stderr, "               [--run-config <run.yaml>] [--scenario <name>] [--coverage-target <0..1>]")
	fmt.Fprintln(os.Stderr, "               [--max-steps <n>] [--heal-llm] [--artifact-dir <dir>]        (all flags: agentctl run --help)")
	fmt.Fprintln(os.Stderr, "  agentctl run --target <URL> --mode chat --conversation-id <id> [--goal <g>|--describe <d>]   (M9.10 multi-turn)")
	fmt.Fprintln(os.Stderr, "  agentctl report --run <run-dir>              # report.html + report.json + metrics.prom + junit.xml")
	fmt.Fprintln(os.Stderr, "  agentctl export-spec --plan <plan.json> [-o <file>]   # a frozen plan -> @playwright/test .spec.ts")
	fmt.Fprintln(os.Stderr, "  agentctl export-git --spec <f> --to-git <repo> [--push]  # land authored specs in a repository")
	fmt.Fprintln(os.Stderr, "  agentctl revisions list|show|diff|rollback --test <id>   # a test's history, what changed, put it back")
	fmt.Fprintln(os.Stderr, "  agentctl import --from <dir>|--from-git <repo> [--verify --target <url>]  # transpile an existing suite")
	fmt.Fprintln(os.Stderr, "  agentctl calibrate                          # heal outcomes by strategy + identity verdicts")
	fmt.Fprintln(os.Stderr, "  agentctl redact-trace --trace <trace.zip>   # strip typed values + credentials from a trace (ADR-098)")
	fmt.Fprintln(os.Stderr, "  agentctl purge-store --tables <a,b> --yes [--older-than 720h] [--vacuum]")
	fmt.Fprintln(os.Stderr, "                                             # delete stored foreign text (ADR-100); never automatic")
	fmt.Fprintln(os.Stderr, "  agentctl sweep-downloaded [--dry-run] --yes # delete run dirs a human has downloaded (ADR-103); explicit")
	fmt.Fprintln(os.Stderr, "  agentctl baseline update --plan <plan.json> [--target <URL>]")
	fmt.Fprintln(os.Stderr, "  agentctl locators clear-quarantine")
	fmt.Fprintln(os.Stderr, "  agentctl version")
	fmt.Fprintln(os.Stderr, "")
	apiUsage(os.Stderr) // ADR-107: the store/config half — thin clients over control-api (api.go)
}

func boolEnv(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func mkArtifactDir(repo, runID, override string) string {
	dir := override
	if dir == "" {
		dir = filepath.Join(repo, "runs", runID)
	}
	// #26 (THREAT_MODEL ❹): the run dir holds trace.zip — AUT DOM snapshots + screenshots, which
	// may contain PII. Restrict it to the owner (0700) so other local users can't read it. Chmod
	// after MkdirAll to enforce 0700 even when a permissive umask would have widened the create mode.
	_ = os.MkdirAll(dir, 0o700)
	_ = os.Chmod(dir, 0o700)
	// #34 pt3: manage the runs/ root (0700) + trace retention whenever the artifact dir lives under
	// repo/runs — the default ./runs/<id> tree AND the control-api override path (runs/control-<id>,
	// what the chat-front + OpenAI-compat shim drive). Without this, control-api passed --artifact-dir
	// so override != "" and BOTH the chmod and sweepTraces were skipped, letting trace.zip (AUT
	// DOM/screenshots, possible PII) accumulate unbounded in a Docker/control-api deployment. A truly
	// external --artifact-dir (outside runs/) is still left untouched — we never chmod or sweep a
	// user-supplied directory we don't own.
	runsRoot := filepath.Join(repo, "runs")
	if isUnder(dir, runsRoot) {
		_ = os.Chmod(runsRoot, 0o700)
		sweepTraces(runsRoot)
		sweepLogs(runsRoot)
		sweepRuns(runsRoot)
	}
	return dir
}

// isUnder reports whether path is root itself or nested inside root. It compares cleaned absolute
// paths so a relative --artifact-dir and an absolute repo/runs (or vice versa) still match, and a
// sibling like runs-evil/ next to runs/ does not.
func isUnder(path, root string) bool {
	ap, err1 := filepath.Abs(path)
	ar, err2 := filepath.Abs(root)
	if err1 != nil || err2 != nil {
		return false
	}
	rel, err := filepath.Rel(ar, ap)
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
}

// envInt reads an int env var, falling back to def when unset or unparsable.
func envInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// sweepTraces enforces trace.zip retention (#26, THREAT_MODEL ❹). It keeps the newest
// SENTINEL_TRACE_KEEP traces (default 10; <0 disables count-pruning) and deletes trace.zip from
// older runs, plus any trace older than SENTINEL_TRACE_TTL_HOURS (default 0 = TTL off). Only the
// trace.zip is removed — plan.json / reports stay for the audit trail. Best-effort: every error is
// ignored, retention must never fail a run. The just-created run has no trace.zip yet, so it is safe.
//
// NOTHING HERE HAPPENS SILENTLY (SEC-TRACE-SWEPT-SILENTLY, ARCHITECTURE principle 7). Until the trace
// became downloadable (ADR-099) a removed trace was invisible and no one noticed; now the missing
// download button reads as a loss, so every removal (a) logs a one-line summary to stderr — which a
// control-API run captures into the run log — and (b) drops a `trace-removed` marker in the swept
// run's own directory. The marker is what lets the hub tell "the trace was removed by retention" from
// "this run never had a trace", instead of showing an identical empty state for both. This retention
// stays ON by default (unlike sweepLogs/sweepRuns) because an unbounded pile of trace.zip is the
// exact leak #26 closed; the asymmetry is defensible now only because it is no longer silent.
func sweepTraces(runsRoot string) {
	keep := envInt("SENTINEL_TRACE_KEEP", 10)
	ttlHours := envInt("SENTINEL_TRACE_TTL_HOURS", 0)
	if keep < 0 && ttlHours <= 0 {
		return // both knobs disabled
	}
	entries, err := os.ReadDir(runsRoot)
	if err != nil {
		return
	}
	type trace struct {
		dir string
		mod time.Time
	}
	var traces []trace
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dir := filepath.Join(runsRoot, e.Name())
		info, err := os.Stat(filepath.Join(dir, "trace.zip"))
		if err != nil {
			continue // no trace in this run
		}
		traces = append(traces, trace{dir, info.ModTime()})
	}
	sort.Slice(traces, func(i, j int) bool { return traces[i].mod.After(traces[j].mod) }) // newest first
	now := time.Now()
	removedCount, removedTTL := 0, 0
	for i, tr := range traces {
		tooMany := keep >= 0 && i >= keep
		tooOld := ttlHours > 0 && now.Sub(tr.mod) > time.Duration(ttlHours)*time.Hour
		if !(tooMany || tooOld) {
			continue
		}
		reason := "count" // count-pruning is checked first; a run can be both, count is the headline
		if tooOld && !tooMany {
			reason = "ttl"
		}
		if err := os.Remove(filepath.Join(tr.dir, "trace.zip")); err != nil {
			continue // could not remove -> do not claim we did (no marker, not counted)
		}
		writeTraceRemovedMarker(tr.dir, reason, keep, ttlHours)
		if reason == "ttl" {
			removedTTL++
		} else {
			removedCount++
		}
	}
	if n := removedCount + removedTTL; n > 0 {
		// Audible: one line, counts only, never a path or content — the run log is shareable.
		fmt.Fprintf(os.Stderr, "[agentctl] trace retention: removed %d trace.zip (%d over keep=%d, %d over ttl=%dh)\n",
			n, removedCount, keep, removedTTL, ttlHours)
	}
}

// writeTraceRemovedMarker records, in the swept run's own directory, that its trace.zip was deleted
// by retention rather than never captured — the one bit the hub needs to stop showing "no trace" and
// "trace swept" as the same empty state. Best-effort, like the sweep itself.
func writeTraceRemovedMarker(runDir, reason string, keep, ttlHours int) {
	m := map[string]any{
		"removed_by": "retention", "reason": reason, "keep": keep, "ttl_hours": ttlHours,
		"removed_at": time.Now().UTC().Format(time.RFC3339),
	}
	if b, err := json.Marshal(m); err == nil {
		_ = os.WriteFile(filepath.Join(runDir, "trace-removed.json"), b, 0o600)
	}
}

// sweepRuns enforces retention on the run DIRECTORIES themselves (ADR-099).
//
// The gap this fills was measured, not guessed: `sweepTraces` owns traces and `sweepLogs` owns logs,
// and NOTHING owned the directory holding them. On a dev box that left 344 directories and 606 MB —
// of which 570 MB was `checkpoint.db`, a file neither sweeper looks at. That particular leak is now
// closed at the source (the brain deletes its own checkpoint), and this is the general answer: a run
// directory is a thing with a lifetime, and until now it had none.
//
// OFF BY DEFAULT, like logs and unlike traces, and for the same reason: a run directory holds the
// plan, the reports and the executed plan — the evidence a person came back for. Deleting it on an
// upgrade nobody asked for would be a worse failure than unbounded disk. A trace is a bulky
// by-product and may default to bounded; the record of what the tool did is not.
//
// The NEWEST is never swept even when the knobs say it should be. A person who has just run
// something and finds nothing there learns that the tool eats its own output, and no amount of
// correct arithmetic makes that a good first impression.
//
// Best-effort throughout: retention must never fail a run.
func sweepRuns(runsRoot string) {
	keep := envInt("SENTINEL_RUN_KEEP", 0)          // 0 = off (not "keep none")
	ttlHours := envInt("SENTINEL_RUN_TTL_HOURS", 0) // 0 = off
	if keep <= 0 && ttlHours <= 0 {
		return
	}
	entries, err := os.ReadDir(runsRoot)
	if err != nil {
		return
	}
	type runDir struct {
		path string
		mod  time.Time
	}
	var dirs []runDir
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		dirs = append(dirs, runDir{filepath.Join(runsRoot, e.Name()), info.ModTime()})
	}
	sort.Slice(dirs, func(i, j int) bool { return dirs[i].mod.After(dirs[j].mod) }) // newest first
	now := time.Now()
	for i, d := range dirs {
		if i == 0 {
			continue // never the newest — see the comment above
		}
		tooMany := keep > 0 && i >= keep
		tooOld := ttlHours > 0 && now.Sub(d.mod) > time.Duration(ttlHours)*time.Hour
		if tooMany || tooOld {
			_ = os.RemoveAll(d.path)
		}
	}
}

// sweepLogs enforces retention on runs/<id>/logs/ (GAP-SEC-005, ADR-081). Same shape as sweepTraces:
// keep the newest SENTINEL_LOG_KEEP runs' logs, drop anything older than SENTINEL_LOG_TTL_HOURS.
//
// BOTH DEFAULT TO OFF, unlike traces, and that is the deliberate half of this. A trace is a bulky
// by-product; the logs ARE the diagnosis, and deleting someone's evidence by default — on an upgrade
// they did not ask for — would be a worse failure than the gap being closed. Redaction at write time
// (cmd/control-api/redact.go) is what removes the credentials; retention is disk hygiene the operator
// opts into, not the containment measure. Saying which is which matters: a TTL that quietly ran would
// look like security while doing nothing about the secret already written.
//
// Only the logs/ directory is removed — plan.json, reports and the executed plan stay, so a swept run
// remains auditable and replayable. Best-effort throughout: retention must never fail a run.
func sweepLogs(runsRoot string) {
	keep := envInt("SENTINEL_LOG_KEEP", 0)          // 0 = off (not "keep none")
	ttlHours := envInt("SENTINEL_LOG_TTL_HOURS", 0) // 0 = off
	if keep <= 0 && ttlHours <= 0 {
		return
	}
	entries, err := os.ReadDir(runsRoot)
	if err != nil {
		return
	}
	type logDir struct {
		path string
		mod  time.Time
	}
	var dirs []logDir
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		p := filepath.Join(runsRoot, e.Name(), "logs")
		info, err := os.Stat(p)
		if err != nil || !info.IsDir() {
			continue
		}
		dirs = append(dirs, logDir{p, info.ModTime()})
	}
	sort.Slice(dirs, func(i, j int) bool { return dirs[i].mod.After(dirs[j].mod) }) // newest first
	now := time.Now()
	for i, d := range dirs {
		tooMany := keep > 0 && i >= keep
		tooOld := ttlHours > 0 && now.Sub(d.mod) > time.Duration(ttlHours)*time.Hour
		if tooMany || tooOld {
			_ = os.RemoveAll(d.path)
		}
	}
}

// filteredEnv narrows the inherited environment to a security allowlist (GAP-SEC-001) so unrelated
// host secrets (SSH keys, cloud creds, unrelated tokens) don't leak into the brain and its children.
// DEFAULT-ON since M11.3 (ADR-035): the allowlist is active unless SENTINEL_ENV_ALLOWLIST=0 — an
// explicit opt-out escape hatch for debugging / unusual local setups. When active: runtime essentials
// + the Sentinel/LLM/OTel/Playwright/proxy/TLS families pass, plus any names the user lists in
// SENTINEL_ENV_ALLOW (comma-separated) — required for secretKeyRef secret vars (e.g. AUT_PASSWORD);
// the Helm chart (deploy/sentinel) auto-emits SENTINEL_ENV_ALLOW from extraEnv/extraSecretEnv so those
// reach the brain. Functional run vars (RUN_ID/TARGET_URL/RUN_MODE/…) bypass this filter — they are
// appended after filteredEnv() in spawnBrain, never inherited from the host.
func filteredEnv() []string {
	if os.Getenv("SENTINEL_ENV_ALLOWLIST") == "0" { // opt-out escape hatch — full host-env passthrough
		return os.Environ()
	}
	exact := map[string]bool{
		"PATH": true, "HOME": true, "USER": true, "LOGNAME": true, "SHELL": true, "PWD": true,
		"LANG": true, "LC_ALL": true, "TERM": true, "TMPDIR": true, "TZ": true,
		"ANTHROPIC_API_KEY": true, "OPENAI_API_KEY": true, "CHECKPOINT_DSN": true,
		"STORAGE_STATE": true, "STORAGE_STATE_SAVE": true, "MCP_TRANSPORT": true,
		"ORCH_ADDR": true, "STORE_ADDR": true, "BRAIN_PYTHON": true, "PYTHONPATH": true,
		// M11.3 (ADR-035): metrics push, visual-heal toggle, corporate TLS trust + proxy.
		"PROM_PUSHGATEWAY": true, "HEAL_VISUAL": true,
		"SSL_CERT_FILE": true, "SSL_CERT_DIR": true,
		"HTTP_PROXY": true, "HTTPS_PROXY": true, "NO_PROXY": true,
		"http_proxy": true, "https_proxy": true, "no_proxy": true,
		// #25 (GAP-SEC-001 remainder): the broad NODE_/GIT_ prefixes used to carry these legitimate
		// runtime/TLS vars but ALSO leaked NODE_AUTH_TOKEN (npm registry auth) and GIT_ASKPASS (a
		// program git runs to obtain credentials). Allowlist the specific names instead of the family.
		"NODE_OPTIONS": true, "NODE_EXTRA_CA_CERTS": true,
		"GIT_SSL_CAINFO": true, "GIT_SSL_CAPATH": true,
	}
	for _, n := range strings.Split(os.Getenv("SENTINEL_ENV_ALLOW"), ",") {
		if n = strings.TrimSpace(n); n != "" {
			exact[n] = true
		}
	}
	// NODE_/GIT_ are deliberately NOT prefixes (#25): the family is too broad and leaks credential
	// vars (NODE_AUTH_TOKEN, GIT_ASKPASS). The legitimate runtime/TLS members are exact-allowlisted above.
	prefixes := []string{"LLM_", "OTEL_", "PW_", "PLAYWRIGHT_", "SENTINEL_"}
	var out []string
	for _, kv := range os.Environ() {
		k := kv
		if i := strings.IndexByte(kv, '='); i >= 0 {
			k = kv[:i]
		}
		if exact[k] {
			out = append(out, kv)
			continue
		}
		for _, p := range prefixes {
			if strings.HasPrefix(k, p) {
				out = append(out, kv)
				break
			}
		}
	}
	return out
}

// spawnBrain runs the brain with the common env + extra vars, streams I/O, returns its exit code.
func spawnBrain(repo, runID string, extra []string) int {
	pwExec := "node " + filepath.Join(repo, "pw-executor", "dist", "server.js")
	brainPython := filepath.Join(repo, ".venv", "bin", "python")
	if _, err := os.Stat(brainPython); err != nil {
		brainPython = "python3"
	}
	if v := os.Getenv("BRAIN_PYTHON"); v != "" {
		brainPython = v
	}
	cmd := exec.Command(brainPython, "-m", "brain")
	cmd.Dir = repo
	cmd.Env = append(filteredEnv(), append([]string{
		"RUN_ID=" + runID,
		"PW_EXECUTOR_CMD=" + pwExec,
		"PYTHONPATH=" + repo,
	}, extra...)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		fmt.Fprintf(os.Stderr, "[agentctl] failed to run brain: %v\n", err)
		return 1
	}
	return 0
}

// startGateway launches the Go store-gateway over a Unix socket (ADR-015). If the binary isn't
// built it returns "" so the brain falls back to its LocalStore. Returns (STORE_ADDR, stop()).
// token is handed to the gateway via env (STORE_TOKEN) so it can authenticate brain calls (#23).
func startGateway(repo, runID, token string) (string, func()) {
	gw := filepath.Join(repo, "bin", "store-gateway")
	if _, err := os.Stat(gw); err != nil {
		return "", func() {}
	}
	// socket lives under repo/state (on the project volume), NOT /tmp — /tmp may be full and
	// net.Listen("unix",...) then fails to create the socket, silently dropping to LocalStore.
	_ = os.MkdirAll(filepath.Join(repo, "state"), 0o755)
	sock := filepath.Join(repo, "state", "sentinel-store-"+runID+".sock")
	cmd := exec.Command(gw, "--addr", sock, "--db", filepath.Join(repo, "state", "locators.db"))
	cmd.Dir = repo
	cmd.Stderr = os.Stderr
	// #36: narrow the gateway's env to the same allowlist the brain gets (GAP-SEC-001) instead of the
	// full host env — the gateway only needs PATH + OTEL_* for tracing; STORE_TOKEN is appended here.
	cmd.Env = append(filteredEnv(), "STORE_TOKEN="+token) // #23: gateway authenticates against this
	if err := cmd.Start(); err != nil {
		return "", func() {}
	}
	ok := false
	for i := 0; i < 100; i++ { // wait up to ~5s for the socket to appear
		if _, err := os.Stat(sock); err == nil {
			ok = true
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !ok {
		fmt.Fprintln(os.Stderr, "[agentctl] store-gateway socket never appeared -> LocalStore")
		_ = cmd.Process.Kill()
		return "", func() {}
	}
	return sock, func() {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		_, _ = cmd.Process.Wait()
		_ = os.Remove(sock)
	}
}

// runWithStore starts the gateway, injects STORE_ADDR, runs the brain, then stops the gateway.
func runWithStore(repo, runID string, extra []string) int {
	token := newToken() // #23: per-run secret shared by the gateway and the brain only
	addr, stop := startGateway(repo, runID, token)
	defer stop()
	if addr != "" {
		// STORE_TOKEN is a run-var (appended after filteredEnv) so it always reaches the brain.
		extra = append(extra, "STORE_ADDR="+addr, "STORE_TOKEN="+token)
	}
	return spawnBrain(repo, runID, extra)
}

func cmdRun(repo string, args []string) int {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	target := fs.String("target", "", "target URL (required)")
	artifactDir := fs.String("artifact-dir", "", "artifact dir (default ./runs/<id>)")
	mode := fs.String("mode", "explore", "run mode")
	_ = fs.Bool("explore", false, "explore mode (default; accepted for convenience)")
	planner := fs.String("planner", "heuristic", "planner: heuristic|llm|goal")
	goal := fs.String("goal", "", "NL goal -> goal-mode authoring (GoalPlanner, M9.2a); empty = explore")
	describe := fs.String("describe", "", "NL flow description -> describe-mode (M9.2b); mutually exclusive with --goal")
	scenario := fs.String("scenario", "", "RunConfig scenario name to select (M9.2b)")
	runConfig := fs.String("run-config", "", "path to a RunConfig YAML (mode/goal/planner/budgets/auth/scenarios)")
	coverageTarget := fs.String("coverage-target", "0.85", "coverage target in [0,1]")
	maxSteps := fs.String("max-steps", "40", "max exploration steps (safety backstop)")
	replay := fs.Bool("replay", false, "replay a frozen plan, healing broken locators (M2/M3)")
	planFile := fs.String("plan", "", "path to plan.json (required with --replay)")
	healLLM := fs.Bool("heal-llm", false, "allow Sonnet LLM re-grounding during heal")
	autVersion := fs.String("aut-version", "", "app-under-test version/sha (flake quarantine)")
	ci := fs.Bool("ci", false, "CI mode (forbids --force-replay)")
	force := fs.Bool("force-replay", false, "bypass plan_hash hard-abort (disallowed under --ci)")
	conversationID := fs.String("conversation-id", "", "M9.10 multi-turn conversation id (use with --mode chat); resumes the thread by conversation_id->thread_id (ADR-048)")
	_ = fs.Parse(args)

	// M9.2a (ADR-027): record which flags the user actually set, so RunConfig precedence (flag > file)
	// holds even when the explicit value equals the default. fs.Visit walks ONLY the flags that were set.
	setFlags := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { setFlags[f.Name] = true })
	var explicit []string
	for _, n := range []string{"planner", "coverage-target", "max-steps", "goal", "describe", "scenario"} {
		if setFlags[n] {
			explicit = append(explicit, n)
		}
	}

	if *target == "" {
		fmt.Fprintln(os.Stderr, "error: --target is required")
		return 2
	}
	if *replay && *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan <plan.json> is required with --replay")
		return 2
	}
	runMode := *mode
	if *replay {
		runMode = "replay"
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, *artifactDir)
	fmt.Printf("[agentctl] run_id=%s mode=%s planner=%s target=%s\n", runID, runMode, *planner, *target)
	fmt.Printf("[agentctl] artifacts=%s\n", dir)
	extra := []string{
		"RUN_MODE=" + runMode,
		"TARGET_URL=" + *target,
		"ARTIFACT_DIR=" + dir,
		"PLANNER=" + *planner,
		"COVERAGE_TARGET=" + *coverageTarget,
		"MAX_STEPS=" + *maxSteps,
		"GOAL=" + *goal,
		"DESCRIBE=" + *describe,
		"SCENARIO=" + *scenario,
		"RUN_CONFIG=" + *runConfig,
		"SENTINEL_EXPLICIT=" + strings.Join(explicit, ","),
		"PLAN_FILE=" + *planFile,
		"HEAL_LLM=" + boolEnv(*healLLM),
		"AUT_VERSION=" + *autVersion,
		"CI=" + boolEnv(*ci),
		"FORCE_REPLAY=" + boolEnv(*force),
		// M9.10 (ADR-048): chat-mode conversation thread key. Run-var (appended after filteredEnv), so it
		// always reaches the brain; only read when RUN_MODE=chat. The SENTINEL_ prefix is allowlisted too.
		"SENTINEL_CONVERSATION_ID=" + *conversationID,
	}
	if runNeedsStore(*mode, *replay) {
		return runWithStore(repo, runID, extra)
	}
	return spawnBrain(repo, runID, extra) // explore needs no store
}

// runNeedsStore decides whether this run must start the store-gateway (STORE_ADDR).
//
//   - replay reads the locator/golden/quarantine store, so it always has.
//   - chat mode was the SEC-CHATS-WIRING-GAP bug: _project_chat (brain/__main__.py) writes the
//     browsable `chats` projection only when make_chat_projector() sees STORE_ADDR, and chat runs
//     fell into the storeless branch by omission — the comment said "explore needs no store" and
//     chat was never separated out. The projection was silently a no-op for every chat run, so the
//     multi-turn conversation never appeared in the hub. It adds no new class of data: the goal and
//     turns already live in the checkpointer thread (conversations.db), which stays the source of
//     truth; this is an index over it, and it is cleanable by `agentctl purge-store` (ADR-100).
func runNeedsStore(mode string, replay bool) bool {
	return replay || mode == "chat"
}

func cmdBaseline(repo string, args []string) int {
	if len(args) < 1 || args[0] != "update" {
		fmt.Fprintln(os.Stderr, "usage: agentctl baseline update --plan <plan.json> [--target <URL>]")
		return 2
	}
	fs := flag.NewFlagSet("baseline", flag.ExitOnError)
	planFile := fs.String("plan", "", "path to plan.json (required)")
	target := fs.String("target", "", "target URL (default: the plan's target_url)")
	artifactDir := fs.String("artifact-dir", "", "artifact dir")
	_ = fs.Parse(args[1:])
	if *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan is required")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, *artifactDir)
	fmt.Printf("[agentctl] baseline update run_id=%s plan=%s\n", runID, *planFile)
	return runWithStore(repo, runID, []string{
		"RUN_MODE=baseline",
		"TARGET_URL=" + *target,
		"ARTIFACT_DIR=" + dir,
		"PLAN_FILE=" + *planFile,
	})
}

func cmdLocators(repo string, args []string) int {
	if len(args) < 1 || args[0] != "clear-quarantine" {
		fmt.Fprintln(os.Stderr, "usage: agentctl locators clear-quarantine")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return runWithStore(repo, runID, []string{
		"RUN_MODE=clear-quarantine",
		"ARTIFACT_DIR=" + dir,
	})
}

// cmdExportSpec: agentctl export-spec --plan <p> [-o <file>]  (M4)
func cmdExportSpec(repo string, args []string) int {
	fs := flag.NewFlagSet("export-spec", flag.ExitOnError)
	planFile := fs.String("plan", "", "path to plan.json (required)")
	out := fs.String("o", "", "output .spec.ts path (default <run>/exported.spec.ts)")
	_ = fs.Parse(args)
	if *planFile == "" {
		fmt.Fprintln(os.Stderr, "error: --plan is required")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return spawnBrain(repo, runID, []string{
		"RUN_MODE=export-spec",
		"ARTIFACT_DIR=" + dir,
		"PLAN_FILE=" + *planFile,
		"SPEC_OUT=" + *out,
	})
}

// cmdRevisions: agentctl revisions <list|show|diff|rollback> --test <id> [--rev <a>] [--rev-b <b>]
//
// PROD-VERSIONING's READ surface. The store has been complete since ADR-106 — append-only history,
// step-level diff, rollback that re-appends instead of deleting — and until now NOTHING COULD READ IT
// BACK: no subcommand, no route, no screen. A revision written and unreachable is not history, it is
// a file. This is deliberately thin and adds no policy of its own.
func cmdRevisions(repo string, args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "error: revisions <list|show|diff|rollback> --test <id>")
		return 2
	}
	op := args[0]
	fs := flag.NewFlagSet("revisions", flag.ExitOnError)
	testID := fs.String("test", "", "test id whose history to read (required)")
	rev := fs.String("rev", "", "revision id (show: which one; diff: the OLDER side; rollback: the target)")
	revB := fs.String("rev-b", "", "diff: the NEWER side (default: the head)")
	_ = fs.Parse(args[1:])
	switch op {
	case "list", "show", "diff", "rollback":
	default:
		fmt.Fprintf(os.Stderr, "error: unknown revisions operation %q (want list|show|diff|rollback)\n", op)
		return 2
	}
	if *testID == "" {
		fmt.Fprintln(os.Stderr, "error: --test <id> is required")
		return 2
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return spawnBrain(repo, runID, []string{
		"RUN_MODE=revisions", "ARTIFACT_DIR=" + dir, "REV_OP=" + op,
		"SENTINEL_TEST_ID=" + *testID, "REV_A=" + *rev, "REV_B=" + *revB,
	})
}

// cmdExportGit: agentctl export-git --spec <file>... --to-git <repo> [--branch b] [--subdir d] [--push]
//
// The OUTPUT half of the git channel (🟢OSS). It takes specs that already exist — export-spec turns a
// frozen plan into a .spec.ts — and lands them in a repository, so a team's authored tests live where
// their code lives instead of only in a run directory (runs/ and state/ are gitignored, so today the
// authored tests are outside version control entirely).
//
// PUSH IS OPT-IN. Committing into a local working tree is recoverable; pushing to a remote is an
// outward, irreversible act, and it happens only when asked for by name.
func cmdExportGit(repo string, args []string) int {
	fs := flag.NewFlagSet("export-git", flag.ExitOnError)
	toGit := fs.String("to-git", "", "git repository (URL or local path) to write the specs into (required)")
	branch := fs.String("branch", "", "branch to commit on (created or reset)")
	subdir := fs.String("subdir", "", "directory inside the repository to write into (e.g. e2e)")
	message := fs.String("message", "", "commit message (default names the specs)")
	push := fs.Bool("push", false, "push after committing — OFF by default: writing to a remote is outward and irreversible")
	var specs multiFlag
	fs.Var(&specs, "spec", "path to a .spec.ts to export (repeatable) (required)")
	_ = fs.Parse(args)
	if *toGit == "" || len(specs) == 0 {
		fmt.Fprintln(os.Stderr, "error: --to-git <repo> and at least one --spec <file> are required")
		return 2
	}
	files := map[string][]byte{}
	for _, s := range specs {
		b, err := os.ReadFile(s)
		if err != nil {
			fmt.Fprintf(os.Stderr, "export-git: %v\n", err)
			return 3
		}
		files[filepath.Base(s)] = b
	}
	// WHERE the commit lands decides whether it survives, and getting this wrong makes the command
	// silently do nothing. A clone is a TEMPORARY directory: committing into it and not pushing
	// throws the commit away the moment the command exits, while still printing "committed <sha>".
	// Measured — the first version did exactly that, twice in a row, reporting success both times.
	//
	// So: a local WORKING TREE is written in place (that is what "put the tests in my checkout"
	// means), and anything else must be pushed or it is refused outright.
	worktree, cleanup := *toGit, false
	if st, err := os.Stat(*toGit); err != nil || !st.IsDir() || isBareOrNotAWorktree(*toGit) {
		if !*push {
			fmt.Fprintf(os.Stderr, "error: %s is not a local working tree, so the commit would only "+
				"exist in a temporary clone and be discarded. Pass --push to send it, or point "+
				"--to-git at a checkout to write in place.\n", *toGit)
			return 2
		}
		clone, err := gitClone(*toGit, *branch)
		if err != nil {
			// A branch that does not exist yet is ordinary for an export; clone the default and let
			// -B create it, rather than refusing the first export a repository ever receives.
			clone, err = gitClone(*toGit, "")
			if err != nil {
				fmt.Fprintf(os.Stderr, "export-git: %v\n", err)
				return 3
			}
		}
		worktree, cleanup = clone, true
	}
	if cleanup {
		defer os.RemoveAll(worktree)
	}
	clone := worktree
	msg := *message
	if msg == "" {
		names := make([]string, 0, len(files))
		for n := range files {
			names = append(names, n)
		}
		sort.Strings(names)
		msg = "test(sentinel): export " + strings.Join(names, ", ")
	}
	sha, err := gitCommitInto(clone, *subdir, msg, files, *push, *branch)
	if err != nil {
		fmt.Fprintf(os.Stderr, "export-git: %v\n", err)
		return 1
	}
	if sha == "" {
		fmt.Printf("export-git: no change — the repository already holds these %d spec(s)\n", len(files))
		return 0
	}
	fmt.Printf("export-git: committed %s (%d spec(s))%s\n", sha[:8], len(files),
		map[bool]string{true: " and pushed", false: " — not pushed (use --push)"}[*push])
	return 0
}

// multiFlag collects a repeatable string flag.
type multiFlag []string

func (m *multiFlag) String() string     { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error { *m = append(*m, v); return nil }

// cmdReport: agentctl report --run <dir>  (M4) — HTML+JSON report + Prometheus metrics
// cmdImport: agentctl import --from <dir>  (PROD-IMPORT, ADR-105)
//
// Channel 1 — the filesystem path — is the main one because in CI the repository is already checked
// out: there is nowhere to import FROM and no reason to, a directory is enough. It transpiles the
// existing suite and writes the rewrite report (import-report.json). No browser, no LLM, no network.
func cmdImport(repo string, args []string) int {
	fs := flag.NewFlagSet("import", flag.ExitOnError)
	from := fs.String("from", "", "directory of existing tests to import (e.g. ./tests)")
	fromGit := fs.String("from-git", "", "git repository (URL or local path) to clone and import from")
	ref := fs.String("ref", "", "branch/tag to clone with --from-git (default: the repository's default)")
	mapFile := fs.String("map", "", "optional explore-map JSON to ground imported steps against the real app")
	verify := fs.Bool("verify", false, "explore --target first and ground the import against THAT map (needs a browser; off by default so import stays offline)")
	target := fs.String("target", "", "URL to explore when --verify is set")
	artifactDir := fs.String("artifact-dir", "", "where to write import-report.json (default ./runs/<id>)")
	_ = fs.Parse(args)
	if (*from == "") == (*fromGit == "") {
		fmt.Fprintln(os.Stderr, "error: give exactly one of --from <dir> or --from-git <repo>")
		return 2
	}
	if *fromGit != "" {
		// git is a way to REACH the files; the filesystem channel then does the actual work. A local
		// path clones with no network at all, which is what keeps this usable in an air-gapped
		// install and what lets the gate run against a bare repo in a temp dir.
		clone, err := gitClone(*fromGit, *ref)
		if err != nil {
			fmt.Fprintf(os.Stderr, "import: %v\n", err)
			return 3
		}
		defer os.RemoveAll(clone)
		fmt.Fprintf(os.Stderr, "cloned %s -> %s\n", *fromGit, clone)
		*from = clone
	}
	if *verify && *target == "" {
		fmt.Fprintln(os.Stderr, "error: --verify needs --target <url> — there is nothing to verify against otherwise")
		return 2
	}
	if *verify && *mapFile != "" {
		// Both would silently pick one. Refuse: "grounded against the app" and "grounded against this
		// file" are different claims, and the report must not be ambiguous about which it made.
		fmt.Fprintln(os.Stderr, "error: --verify and --map are mutually exclusive (one explores the app, the other reads a file)")
		return 2
	}
	abs, err := filepath.Abs(*from)
	if err != nil {
		fmt.Fprintf(os.Stderr, "import: %v\n", err)
		return 1
	}
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, *artifactDir)

	resolvedMap := *mapFile
	if *verify {
		// The two halves of the diagnosis in one command: EXPLORE the real application to learn what
		// it actually has, then ground the imported suite against THAT. Until the explore map became
		// an artifact this could not be written at all — grounding could compute the answer and
		// nothing could produce its input, so the only map in existence was a test fixture.
		//
		// A separate run directory on purpose: the explore is its own run with its own trace and its
		// own retention, and folding it into the import's directory would put a browser run's
		// artifacts under a report that claims to be browser-less.
		exploreID := newRunID()
		exploreDir := mkArtifactDir(repo, exploreID, "")
		fmt.Fprintf(os.Stderr, "verify: exploring %s -> %s\n", *target, exploreDir)
		if rc := spawnBrain(repo, exploreID, []string{
			"RUN_MODE=explore", "ARTIFACT_DIR=" + exploreDir, "TARGET_URL=" + *target,
		}); rc > 1 {
			// exit 1 means the explore found a problem in the APPLICATION, which is a result, not a
			// failure to explore — the map is still written. Anything above that is a real failure.
			fmt.Fprintf(os.Stderr, "verify: explore failed (exit %d); nothing to ground against\n", rc)
			return rc
		}
		resolvedMap = filepath.Join(exploreDir, "site-map.json")
		if _, err := os.Stat(resolvedMap); err != nil {
			fmt.Fprintf(os.Stderr, "verify: the explore produced no site-map.json (%v) — the target may have no interactive elements\n", err)
			return 3
		}
	}

	extra := []string{"RUN_MODE=import", "ARTIFACT_DIR=" + dir, "IMPORT_DIR=" + abs}
	if resolvedMap != "" {
		if m, err := filepath.Abs(resolvedMap); err == nil {
			extra = append(extra, "IMPORT_MAP="+m)
		}
	}
	return spawnBrain(repo, runID, extra)
}

func cmdReport(repo string, args []string) int {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	runDir := fs.String("run", "", "run directory containing heal-report.json (required)")
	_ = fs.Parse(args)
	if *runDir == "" {
		fmt.Fprintln(os.Stderr, "error: --run <dir> is required")
		return 2
	}
	return spawnBrain(repo, newRunID(), []string{
		"RUN_MODE=report",
		"ARTIFACT_DIR=" + *runDir,
		"REPORT_DIR=" + *runDir,
	})
}

// cmdRedactTrace: agentctl redact-trace --trace <path>  (ADR-098)
//
// A subcommand rather than a step folded into `report`, for two reasons that both come down to WHEN.
// The trace is written the moment the run ends and the report is built afterwards, so folding this in
// would leave the raw archive on disk for the whole gap. And a replay driven directly (`python -m
// brain`) never reaches `report` at all, which would make the redaction depend on how the run was
// started — the worst property a security control can have.
//
// It fails LOUDLY. The caller's contract (brain/__main__.py) is to DELETE the trace when this returns
// non-zero: a trace that could not be redacted is not a degraded artifact, it is a leak, and keeping
// it because the cleanup failed would invert the whole point.
func cmdRedactTrace(args []string) int {
	fs := flag.NewFlagSet("redact-trace", flag.ExitOnError)
	trace := fs.String("trace", "", "path to trace.zip (required)")
	_ = fs.Parse(args)
	if *trace == "" {
		fmt.Fprintln(os.Stderr, "error: --trace <path> is required")
		return 2
	}
	st, err := redact.TraceFile(*trace)
	if err != nil {
		fmt.Fprintf(os.Stderr, "redact-trace: %v\n", err)
		return 1
	}
	// Counts, never content: a tool that printed what it found would be a second copy of the leak.
	fmt.Printf("redacted %s: %d entries (%d images copied), %d typed values, %d narrated logs, "+
		"%d snapshot values, %d text lines\n",
		*trace, st.Entries, st.Images, st.TypedValues, st.NarratedLogs, st.SnapshotVals, st.TextualLines)
	return 0
}

// cmdCalibrate: agentctl calibrate  (M4) — heal precision/histogram from healing_audit
func cmdCalibrate(repo string, args []string) int {
	runID := newRunID()
	dir := mkArtifactDir(repo, runID, "")
	return runWithStore(repo, runID, []string{
		"RUN_MODE=calibrate",
		"ARTIFACT_DIR=" + dir,
	})
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	if a := os.Args[1]; a == "version" || a == "--version" {
		fmt.Println(version) // no cwd/brain needed — a plain sanity check for install.sh (M11.5)
		os.Exit(0)
	}
	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cwd: %v\n", err)
		os.Exit(1)
	}
	var code int
	switch os.Args[1] {
	case "run":
		code = cmdRun(repo, os.Args[2:])
	case "baseline":
		code = cmdBaseline(repo, os.Args[2:])
	case "locators":
		code = cmdLocators(repo, os.Args[2:])
	case "revisions":
		code = cmdRevisions(repo, os.Args[2:])
	case "export-git":
		code = cmdExportGit(repo, os.Args[2:])
	case "export-spec":
		code = cmdExportSpec(repo, os.Args[2:])
	case "import":
		code = cmdImport(repo, os.Args[2:])
	case "report":
		code = cmdReport(repo, os.Args[2:])
	case "calibrate":
		code = cmdCalibrate(repo, os.Args[2:])
	case "redact-trace":
		code = cmdRedactTrace(os.Args[2:])
	case "purge-store":
		code = cmdPurgeStore(repo, os.Args[2:])
	case "sweep-downloaded":
		code = cmdSweepDownloaded(repo, os.Args[2:])
	default:
		// ADR-107: the store/config half of the product, projected onto the CLI as thin clients over the
		// routes control-api already serves (api.go). Matched LAST so a locally-implemented subcommand
		// always wins over a same-named remote verb — a `run` that quietly needed a server would be a
		// different tool wearing the same name.
		if v, rest := findAPIVerb(os.Args[1:]); v != nil {
			code = cmdAPI(repo, v, rest)
		} else {
			usage()
			code = 2
		}
	}
	os.Exit(code)
}
