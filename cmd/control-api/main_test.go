package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

// testRepoRoot holds one throwaway directory per newTestServer(), all under a single parent removed at
// the end of the run.
//
// The package used to build every test server with repo="." — the package DIRECTORY — so anything the
// server wrote under <repo>/state or <repo>/runs landed in the source tree. That stayed invisible while
// /v1/config answered 501 without touching disk. ADR-075 gave the standalone tier a real file, and a
// test promptly wrote cmd/control-api/state/config.json into the working tree, where a LATER test read
// it back and asserted on it: /readyz reported the config check "ok" because of a file an earlier test
// had left behind. A test whose result depends on what an earlier test left in the source tree is not
// measuring the code, so the isolation is per server, not per package.
var testRepoRoot string

func TestMain(m *testing.M) {
	d, err := os.MkdirTemp("", "control-api-test-repo-")
	if err != nil {
		panic(err)
	}
	testRepoRoot = d
	code := m.Run()
	_ = os.RemoveAll(d)
	os.Exit(code)
}

func newTestServer() *server {
	d, err := os.MkdirTemp(testRepoRoot, "srv-")
	if err != nil {
		panic(err) // TestMain guarantees the parent exists; nothing useful can run without a repo
	}
	return &server{
		repo:      d,
		agentctl:  "/nonexistent/agentctl",
		token:     "secret-tok",
		corsAllow: map[string]bool{"https://alexgromer.github.io": true},
		runs:      map[string]*run{},
	}
}

func TestHealthz(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz: got %d want 200", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["status"] != "ok" {
		t.Fatalf("healthz body: %v (err=%v)", body, err)
	}
}

func TestCreateRunRequiresToken(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"file:///x.html"}`))
	newTestServer().mux().ServeHTTP(rec, req) // no Authorization header
	if rec.Code != http.StatusForbidden {
		t.Fatalf("create-run without token: got %d want 403", rec.Code)
	}
}

func TestCreateRunRejectsBadTarget(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"javascript:alert(1)"}`))
	req.Header.Set("Authorization", "Bearer secret-tok")
	newTestServer().mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("create-run bad target: got %d want 400 (no agentctl spawned)", rec.Code)
	}
}

func TestCORSPreflightAllowed(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodOptions, "/v1/runs", nil)
	req.Header.Set("Origin", "https://alexgromer.github.io")
	newTestServer().mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight: got %d want 204", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "https://alexgromer.github.io" {
		t.Fatalf("preflight ACAO: got %q", got)
	}
}

func TestCORSDisallowedOrigin(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("Origin", "https://evil.example")
	newTestServer().mux().ServeHTTP(rec, req)
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("disallowed origin must get no ACAO, got %q", got)
	}
}

// newRunServerWithScript backs the server with a temp repo + a fake agentctl whose script body is given.
func newRunServerWithScript(t *testing.T, body string) (*server, string) {
	t.Helper()
	repo := t.TempDir()
	script := filepath.Join(repo, "fake-agentctl.sh")
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("write fake agentctl: %v", err)
	}
	return &server{
		repo:      repo,
		agentctl:  script,
		token:     "secret-tok",
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
	}, repo
}

// newRunServer returns a server backed by a temp repo + a fake agentctl that echoes a line to
// stdout and one to stderr, then exits 1 (a real "the test found a problem" exit code).
func newRunServer(t *testing.T) (*server, string) {
	t.Helper()
	return newRunServerWithScript(t, "#!/bin/sh\necho 'planning step 1'\necho 'walking page' 1>&2\nexit 1\n")
}

// createRunAndWait POSTs a run and waits briefly for the spawned process to finish, returning its id.
func createRunAndWait(t *testing.T, s *server) string {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(`{"target":"file:///x.html"}`))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("create run: got %d want 202 (%s)", rec.Code, rec.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("create run body: %v", err)
	}
	id := resp["run_id"]
	for i := 0; i < 200; i++ {
		s.mu.RLock()
		st := s.runs[id].State
		s.mu.RUnlock()
		if st != "running" {
			return id
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("run %s did not finish in time", id)
	return ""
}

func TestRunEventsStream(t *testing.T) {
	s, _ := newRunServer(t)
	id := createRunAndWait(t, s)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/events", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("events: got %d want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("events content-type: %q want text/event-stream", ct)
	}
	body := rec.Body.String()
	// M14 tail 1: the injected run.finished reaches SSE as a RAW @@AGUI line inside a log event (SSE is
	// never AG-UI-typed — that is WS-only; here we just confirm the terminal line is present).
	for _, want := range []string{"event: state", "event: log", "planning step 1", "walking page", `"exit_code":1`, "run.finished", "event: done"} {
		if !strings.Contains(body, want) {
			t.Fatalf("events body missing %q:\n%s", want, body)
		}
	}
}

func TestRunEventsRequiresToken(t *testing.T) {
	s, _ := newRunServer(t)
	id := createRunAndWait(t, s)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/events", nil) // no Authorization
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("events without token: got %d want 403", rec.Code)
	}
}

func TestRunArtifact(t *testing.T) {
	s, repo := newRunServer(t)
	id := createRunAndWait(t, s)
	artDir := filepath.Join(repo, "runs", "control-"+id)
	if err := os.MkdirAll(artDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(artDir, "scenario.json"), []byte(`{"steps":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}

	// happy path — whitelisted file present
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=scenario.json", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"steps"`) {
		t.Fatalf("artifact happy: got %d body=%s", rec.Code, rec.Body.String())
	}

	// no token → 403
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=scenario.json", nil)
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("artifact no token: got %d want 403", rec.Code)
	}

	// non-whitelisted / traversal names → 400 (no file read)
	for _, bad := range []string{"evil.txt", "../../etc/passwd", "sub/scenario.json", ""} {
		rec = httptest.NewRecorder()
		req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name="+url.QueryEscape(bad), nil)
		req.Header.Set("Authorization", "Bearer secret-tok")
		s.mux().ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("artifact bad name %q: got %d want 400", bad, rec.Code)
		}
	}

	// whitelisted but missing file → 404
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=report.json", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("artifact missing file: got %d want 404", rec.Code)
	}
}

func TestParseChatInstruction(t *testing.T) {
	one := func(content string) []chatMessage { return []chatMessage{{Role: "user", Content: content}} }
	cases := []struct {
		name, model                    string
		msgs                           []chatMessage
		wantMode, wantTarget, wantText string
	}{
		{"describe default", "sentinel", one("describe: open login\ntarget: https://a"), "describe", "https://a", "open login"},
		{"model goal suffix", "sentinel-goal", one("log in\ntarget: file:///x.html"), "goal", "file:///x.html", "log in"},
		{"content prefix wins over model", "sentinel-goal", one("describe: do thing\ntarget: https://b"), "describe", "https://b", "do thing"},
		{"explore prefix", "sentinel", one("explore:\ntarget: https://c"), "explore", "https://c", ""},
		{"no target", "sentinel", one("describe: x"), "describe", "", "x"},
		{"bare url anywhere", "sentinel", one("test https://d/login please"), "describe", "https://d/login", "test https://d/login please"},
		{"multi-message: most-recent target + last instruction", "sentinel", []chatMessage{
			{Role: "user", Content: "describe: open A\ntarget: https://a"},
			{Role: "assistant", Content: "ok"},
			{Role: "user", Content: "describe: open B\ntarget: https://b"},
		}, "describe", "https://b", "open B"},
	}
	for _, c := range cases {
		mode, target, text := parseChatInstruction(c.model, c.msgs)
		if mode != c.wantMode || target != c.wantTarget || text != c.wantText {
			t.Errorf("%s: got (%q,%q,%q) want (%q,%q,%q)", c.name, mode, target, text, c.wantMode, c.wantTarget, c.wantText)
		}
	}
}

func chatBody(model, content string, stream bool) string {
	b, _ := json.Marshal(chatRequest{Model: model, Messages: []chatMessage{{Role: "user", Content: content}}, Stream: stream})
	return string(b)
}

func TestChatCompletionsRequiresToken(t *testing.T) {
	s, _ := newRunServer(t)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(chatBody("sentinel", "describe: x\ntarget: file:///x.html", false)))
	s.mux().ServeHTTP(rec, req) // no token
	if rec.Code != http.StatusForbidden {
		t.Fatalf("chat without token: got %d want 403", rec.Code)
	}
}

func TestChatCompletionsNonStream(t *testing.T) {
	s, _ := newRunServer(t)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(chatBody("sentinel", "describe: open login\ntarget: file:///x.html", false)))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("chat: got %d want 200 (%s)", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("chat body: %v", err)
	}
	if resp["object"] != "chat.completion" {
		t.Fatalf("object: %v", resp["object"])
	}
	content := resp["choices"].([]any)[0].(map[string]any)["message"].(map[string]any)["content"].(string)
	for _, want := range []string{"planning step 1", "walking page", "the test found a problem (exit 1)"} {
		if !strings.Contains(content, want) {
			t.Fatalf("content missing %q:\n%s", want, content)
		}
	}
}

func TestChatCompletionsStream(t *testing.T) {
	s, _ := newRunServer(t)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(chatBody("sentinel", "describe: open login\ntarget: file:///x.html", true)))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("chat stream: got %d want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"chat.completion.chunk", "planning step 1", "the test found a problem (exit 1)", `"finish_reason":"stop"`, "data: [DONE]"} {
		if !strings.Contains(body, want) {
			t.Fatalf("stream body missing %q:\n%s", want, body)
		}
	}
}

func TestChatCompletionsNoTarget(t *testing.T) {
	s, _ := newRunServer(t)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(chatBody("sentinel", "describe: open login", false)))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("no-target: got %d want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "Set a target") {
		t.Fatalf("no-target should ask for a target:\n%s", rec.Body.String())
	}
}

// --- M9.9 replay / baseline (ADR-047) -----------------------------------------------------------

// newArgvCapturingServer backs the server with a fake agentctl that records its argv (one arg per
// line) to <repo>/argv.txt and exits with exitCode. Returns the server, repo, and the argv path.
func newArgvCapturingServer(t *testing.T, exitCode int) (s *server, repo, argvPath string) {
	t.Helper()
	repo = t.TempDir()
	argvPath = filepath.Join(repo, "argv.txt")
	script := filepath.Join(repo, "fake-agentctl.sh")
	// ADR-089: APPEND, with a separator between invocations. The run path now spawns agentctl twice —
	// once for the run and once for `report` — and a script that overwrote left the tests reading the
	// SECOND argv while claiming to assert the first.
	body := "#!/bin/sh\nprintf '%s\\n' \"$@\" >> '" + argvPath + "'\nprintf -- '--\\n' >> '" + argvPath + "'\nexit " + strconv.Itoa(exitCode) + "\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("write capturing agentctl: %v", err)
	}
	return &server{
		repo:      repo,
		agentctl:  script,
		token:     "secret-tok",
		corsAllow: map[string]bool{},
		runs:      map[string]*run{},
	}, repo, argvPath
}

// seedPriorPlan writes a frozen-plan file (plan.json or scenario.json) for a prior run id under
// <repo>/runs/control-<priorID>/, returning its absolute path (what from_run must resolve to).
func seedPriorPlan(t *testing.T, repo, priorID, name, content string) string {
	t.Helper()
	dir := filepath.Join(repo, "runs", "control-"+priorID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func runBody(t *testing.T, m map[string]string) string {
	t.Helper()
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// postRun POSTs a JSON run body with the test token and returns the recorder (no wait).
func postRun(t *testing.T, s *server, body string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/runs", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	return rec
}

// postRunAndWait POSTs a run body, requires 202, and waits for the spawned process to finish.
func postRunAndWait(t *testing.T, s *server, body string) string {
	t.Helper()
	rec := postRun(t, s, body)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("create run: got %d want 202 (%s)", rec.Code, rec.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("create run body: %v", err)
	}
	id := resp["run_id"]
	for i := 0; i < 200; i++ {
		s.mu.RLock()
		st := s.runs[id].State
		s.mu.RUnlock()
		if st != "running" {
			return id
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("run %s did not finish in time", id)
	return ""
}

// readArgv returns the FIRST invocation's argv — the run itself. The report spawn that follows it is
// asserted separately by readArgvAll, so a test about how a run is launched is not quietly rewritten
// into a test about how its report is.
func readArgv(t *testing.T, argvPath string) []string {
	t.Helper()
	all := readArgvAll(t, argvPath)
	if len(all) == 0 {
		t.Fatal("no agentctl invocation recorded")
	}
	return all[0]
}

// readArgvAll returns every invocation, in order.
func readArgvAll(t *testing.T, argvPath string) [][]string {
	t.Helper()
	b, err := os.ReadFile(argvPath)
	if err != nil {
		t.Fatalf("read argv: %v", err)
	}
	var out [][]string
	var cur []string
	for _, ln := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if ln == "--" {
			out = append(out, cur)
			cur = nil
			continue
		}
		cur = append(cur, ln)
	}
	if len(cur) > 0 {
		out = append(out, cur)
	}
	return out
}

// TestCreateRunReplayArgv: mode=replay + from_run → `agentctl run --target <plan target> --artifact-dir
// <new> --replay --plan <prior plan.json>` (target derived from the frozen plan's target_url).
func TestCreateRunReplayArgv(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	planPath := seedPriorPlan(t, repo, "prior1", "plan.json",
		`{"target_url":"https://app.example","plan_id":"p1","plan_hash":"h","steps":[]}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior1"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://app.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id), "--replay", "--plan", planPath}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("replay argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunBaselineArgv: mode=baseline → `agentctl baseline update --plan <p> --artifact-dir <new>
// --target <plan target>` (the only golden-write path; target defaulted from the plan).
func TestCreateRunBaselineArgv(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	planPath := seedPriorPlan(t, repo, "prior2", "plan.json", `{"target_url":"https://app.example","steps":[]}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "baseline", "from_run": "prior2"}))
	argv := readArgv(t, argvPath)
	want := []string{"baseline", "update", "--plan", planPath,
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id), "--target", "https://app.example"}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("baseline argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunBaselineNoTarget: a plan without target_url → baseline omits --target (agentctl falls
// back to the plan's own target_url).
func TestCreateRunBaselineNoTarget(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	planPath := seedPriorPlan(t, repo, "prior3", "plan.json", `{"steps":[]}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "baseline", "from_run": "prior3"}))
	argv := readArgv(t, argvPath)
	want := []string{"baseline", "update", "--plan", planPath, "--artifact-dir", filepath.Join(repo, "runs", "control-"+id)}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("baseline (no target) argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunReplayScenarioFallback: with only scenario.json present, from_run resolves to it.
func TestCreateRunReplayScenarioFallback(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	scenPath := seedPriorPlan(t, repo, "prior4", "scenario.json", `{"target_url":"https://app.example","steps":[]}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior4"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://app.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id), "--replay", "--plan", scenPath}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("scenario-fallback argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunReplayPrefersPlanOverScenario: when both exist, plan.json wins (resolution order).
func TestCreateRunReplayPrefersPlanOverScenario(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	planPath := seedPriorPlan(t, repo, "prior5", "plan.json", `{"target_url":"https://app.example"}`)
	seedPriorPlan(t, repo, "prior5", "scenario.json", `{"target_url":"https://other.example"}`)
	_ = postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior5"}))
	argv := readArgv(t, argvPath)
	if argv[len(argv)-1] != planPath {
		t.Fatalf("--plan should prefer plan.json (%s), got %s", planPath, argv[len(argv)-1])
	}
	if argv[2] != "https://app.example" {
		t.Fatalf("target should come from plan.json, got %s", argv[2])
	}
}

// TestCreateRunReplayRequestTargetOverrides: an explicit request target wins over the plan's target_url.
func TestCreateRunReplayRequestTargetOverrides(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	planPath := seedPriorPlan(t, repo, "prior7", "plan.json", `{"steps":[]}`) // no target_url
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior7", "target": "https://override.example"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://override.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id), "--replay", "--plan", planPath}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("override argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunReplayRejectsBadFromRun: from_run must be a bare run_id — no path separators/traversal.
func TestCreateRunReplayRejectsBadFromRun(t *testing.T) {
	s, _ := newRunServer(t) // default stub; none of these must spawn it
	for _, fr := range []string{"", "../../etc", "a/b", `a\b`, "..", "x/../y"} {
		rec := postRun(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": fr}))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("from_run %q: got %d want 400 (%s)", fr, rec.Code, rec.Body.String())
		}
	}
	s.mu.RLock()
	n := len(s.runs)
	s.mu.RUnlock()
	if n != 0 {
		t.Fatalf("a rejected from_run must not spawn a run, got %d", n)
	}
}

// TestCreateRunReplayMissingPlan: from_run resolving to a dir with no plan.json/scenario.json → 400.
func TestCreateRunReplayMissingPlan(t *testing.T) {
	s, _ := newRunServer(t)
	rec := postRun(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "ghost"}))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("missing plan: got %d want 400 (%s)", rec.Code, rec.Body.String())
	}
}

// TestCreateRunReplayNeedsTarget: replay with neither a request target nor a plan target_url → 400.
func TestCreateRunReplayNeedsTarget(t *testing.T) {
	s, repo := newRunServer(t)
	seedPriorPlan(t, repo, "prior6", "plan.json", `{"steps":[]}`) // no target_url
	rec := postRun(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior6"}))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("replay no target: got %d want 400 (%s)", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "replay needs a target") {
		t.Fatalf("expected target error, got %s", rec.Body.String())
	}
}

// TestReplayExitCodePropagation: a replay run's structured exit code (here 2) reaches the run record.
func TestReplayExitCodePropagation(t *testing.T) {
	s, repo, _ := newArgvCapturingServer(t, 2) // golden-regression-style exit
	seedPriorPlan(t, repo, "prior8", "plan.json", `{"target_url":"https://app.example"}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior8"}))
	s.mu.RLock()
	st, code := s.runs[id].State, s.runs[id].ExitCode
	s.mu.RUnlock()
	if st != "done" || code != 2 {
		t.Fatalf("replay exit: state=%q code=%d want done/2", st, code)
	}
}

// TestConfigSchemaIncludesReplayBaseline: the WebUI's form source-of-truth advertises the new modes.
func TestConfigSchemaIncludesReplayBaseline(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema: got %d want 200", rec.Code)
	}
	var body struct {
		Modes []string `json:"modes"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	has := map[string]bool{}
	for _, m := range body.Modes {
		has[m] = true
	}
	for _, want := range []string{"explore", "goal", "describe", "replay", "baseline", "chat"} {
		if !has[want] {
			t.Fatalf("config-schema modes missing %q: %v", want, body.Modes)
		}
	}
}

// --- M9.10 multi-turn (ADR-048): conversation_id → `--mode chat --conversation-id` argv --------------

// TestCreateRunChatGoalArgv: a goal request carrying conversation_id spawns
// `agentctl run --target <t> --artifact-dir <new> --mode chat --conversation-id <id> --goal <g>`.
func TestCreateRunChatGoalArgv(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	id := postRunAndWait(t, s, runBody(t, map[string]string{
		"target": "https://app.example", "goal": "log in", "conversation_id": "conv-abc123"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://app.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id),
		"--mode", "chat", "--conversation-id", "conv-abc123", "--goal", "log in"}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("chat goal argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunChatDescribeArgv: a describe request with conversation_id → `--mode chat` + describe.
func TestCreateRunChatDescribeArgv(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	id := postRunAndWait(t, s, runBody(t, map[string]string{
		"target": "https://app.example", "describe": "pay the bill", "conversation_id": "conv_42"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://app.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id),
		"--mode", "chat", "--conversation-id", "conv_42", "--describe", "pay the bill"}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("chat describe argv:\n got %#v\nwant %#v", argv, want)
	}
}

// TestCreateRunNoConversationIDStaysOneShot: WITHOUT conversation_id, a goal run is unchanged — no
// `--mode chat`/`--conversation-id` leak into argv (one-shot regression).
func TestCreateRunNoConversationIDStaysOneShot(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	id := postRunAndWait(t, s, runBody(t, map[string]string{
		"target": "https://app.example", "goal": "log in", "planner": "goal"}))
	argv := readArgv(t, argvPath)
	want := []string{"run", "--target", "https://app.example",
		"--artifact-dir", filepath.Join(repo, "runs", "control-"+id), "--planner", "goal", "--goal", "log in"}
	if !reflect.DeepEqual(argv, want) {
		t.Fatalf("one-shot (no conversation_id) argv:\n got %#v\nwant %#v", argv, want)
	}
	for _, a := range argv {
		if a == "--mode" || a == "--conversation-id" {
			t.Fatalf("one-shot run must not carry %q: %#v", a, argv)
		}
	}
}

// TestCreateRunChatRejectsBadConversationID: a malformed conversation_id is 400'd and never spawns
// (it becomes the persisted thread key — defense in depth against control chars / oversize / separators).
func TestCreateRunChatRejectsBadConversationID(t *testing.T) {
	s, _ := newRunServer(t) // default stub; none of these must spawn it
	for _, cid := range []string{"a/b", `a\b`, "..", "../x", "bad id", "a\tb", "x.y", strings.Repeat("x", 129)} {
		rec := postRun(t, s, runBody(t, map[string]string{
			"target": "https://app.example", "goal": "g", "conversation_id": cid}))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("conversation_id %q: got %d want 400 (%s)", cid, rec.Code, rec.Body.String())
		}
	}
	s.mu.RLock()
	n := len(s.runs)
	s.mu.RUnlock()
	if n != 0 {
		t.Fatalf("a rejected conversation_id must not spawn a run, got %d", n)
	}
}

// TestConfigSchemaIncludesConversationID: the WebUI form source-of-truth advertises the chat field.
func TestConfigSchemaIncludesConversationID(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	var body struct {
		Fields map[string]any `json:"fields"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	if _, ok := body.Fields["conversation_id"]; !ok {
		t.Fatalf("config-schema fields missing conversation_id: %v", body.Fields)
	}
}

// TestConfigSchemaIncludesLLMBackend: M11.5 PR-3 (ADR-060) — the form source-of-truth advertises the
// LLM-backend surface (brain/llm.py make_backend) so the wizard can render its Model & Auth step, and
// api_key is described as a secret but never carries a value.
func TestConfigSchemaIncludesLLMBackend(t *testing.T) {
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema: got %d want 200", rec.Code)
	}
	var body struct {
		Backends []string                  `json:"backends"`
		Roles    []string                  `json:"roles"`
		LLM      map[string]map[string]any `json:"llm"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	// backends enum mirrors brain/llm.py make_backend (anthropic|openai|sampling)
	hasB := map[string]bool{}
	for _, b := range body.Backends {
		hasB[b] = true
	}
	for _, want := range []string{"anthropic", "openai", "sampling"} {
		if !hasB[want] {
			t.Fatalf("config-schema backends missing %q: %v", want, body.Backends)
		}
	}
	// roles advertise the LLM_<KEY>_<ROLE> override surface
	if len(body.Roles) != 2 || body.Roles[0] != "planner" || body.Roles[1] != "heal" {
		t.Fatalf("config-schema roles = %v, want [planner heal]", body.Roles)
	}
	// the nested llm.backend.enum must stay in lockstep with the top-level backends (built from one slice)
	enumRaw, _ := body.LLM["backend"]["enum"].([]any)
	if len(enumRaw) != len(body.Backends) {
		t.Fatalf("llm.backend.enum %v disagrees with backends %v", enumRaw, body.Backends)
	}
	for i, v := range enumRaw {
		if s, _ := v.(string); s != body.Backends[i] {
			t.Fatalf("llm.backend.enum[%d]=%v != backends[%d]=%q", i, v, i, body.Backends[i])
		}
	}
	// every LLM descriptor is present and carries the exact env var name from brain/llm.py
	wantEnv := map[string]string{
		"backend": "LLM_BACKEND", "model": "LLM_MODEL", "base_url": "LLM_BASE_URL",
		"api_key": "LLM_API_KEY", "vision": "LLM_VISION", "structured": "LLM_STRUCTURED",
	}
	for field, env := range wantEnv {
		d, ok := body.LLM[field]
		if !ok {
			t.Fatalf("config-schema llm missing field %q: %v", field, body.LLM)
		}
		if d["env"] != env {
			t.Fatalf("config-schema llm.%s env = %v, want %q", field, d["env"], env)
		}
	}
	// api_key must be flagged secret (wizard renders a password field) and must NOT carry a value
	if body.LLM["api_key"]["secret"] != true {
		t.Fatalf("config-schema llm.api_key must be secret:true, got %v", body.LLM["api_key"]["secret"])
	}
	if _, leaked := body.LLM["api_key"]["default"]; leaked {
		t.Fatalf("config-schema llm.api_key must never carry a default/value")
	}
}

// TestBackendPresetsParseAndMatchSchema: M11.5 PR-3 (ADR-060) — docs/backend-presets.json parses and
// every preset's backend is one the schema advertises (the "parses and matches" acceptance gate).
func TestBackendPresetsParseAndMatchSchema(t *testing.T) {
	// go test runs with CWD = the package dir (cmd/control-api); the presets live at repo-root docs/.
	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "backend-presets.json"))
	if err != nil {
		t.Fatalf("read docs/backend-presets.json (must run from cmd/control-api): %v", err)
	}
	var doc struct {
		Presets map[string]struct {
			Backend string `json:"backend"`
		} `json:"presets"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("docs/backend-presets.json does not parse: %v", err)
	}
	if len(doc.Presets) < 9 {
		t.Fatalf("docs/backend-presets.json: got %d presets, want >=9", len(doc.Presets))
	}
	// the schema-advertised backend enum is the source of truth
	rec := httptest.NewRecorder()
	newTestServer().mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	var schema struct {
		Backends []string `json:"backends"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &schema); err != nil {
		t.Fatalf("config-schema body: %v", err)
	}
	allowed := map[string]bool{}
	for _, b := range schema.Backends {
		allowed[b] = true
	}
	for name, p := range doc.Presets {
		if p.Backend == "" {
			t.Fatalf("preset %q is missing a backend", name)
		}
		if !allowed[p.Backend] {
			t.Fatalf("preset %q backend %q not in schema enum %v", name, p.Backend, schema.Backends)
		}
	}
}

// TestRunArtifactReplayOutputs: replay/baseline outputs are whitelisted and fetchable.
func TestRunArtifactReplayOutputs(t *testing.T) {
	s, repo := newRunServer(t)
	id := createRunAndWait(t, s)
	artDir := filepath.Join(repo, "runs", "control-"+id)
	if err := os.MkdirAll(artDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"heal-report.json", "baseline-report.json"} {
		if err := os.WriteFile(filepath.Join(artDir, name), []byte(`{"ok":true}`), 0o644); err != nil {
			t.Fatal(err)
		}
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name="+name, nil)
		req.Header.Set("Authorization", "Bearer secret-tok")
		s.mux().ServeHTTP(rec, req)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"ok"`) {
			t.Fatalf("artifact %s: got %d body=%s", name, rec.Code, rec.Body.String())
		}
	}
}

// TestSettingsSchemaDefaultsMatchCode: a settings descriptor states a default, and the code that reads
// the variable states one too. Two numbers for one decision drift — and this one drifts SILENTLY, since
// a wrong default in the wizard is indistinguishable from a right one until someone compares behaviour
// against the form. So the schema is checked against the source that actually reads each variable.
//
// Text-matched rather than executed: the readers live in three languages (Go agentctl, Python brain) and
// several are package-private, so importing them here is not possible. A grep-shaped assertion is weaker
// than a call, and it is what is available — but it is strictly stronger than no assertion, which is
// what guarded these numbers before.
func TestSettingsSchemaDefaultsMatchCode(t *testing.T) {
	repo := filepath.Join("..", "..")
	// env -> the literal that must appear next to it where the code reads it.
	want := map[string]string{
		"SENTINEL_LOG_KEEP":            "0",
		"SENTINEL_LOG_TTL_HOURS":       "0",
		"SENTINEL_TRACE_KEEP":          "10",
		"SENTINEL_TRACE_TTL_HOURS":     "0",
		"SENTINEL_HEAL_AUTO":           "0.85",
		"SENTINEL_HEAL_FLAG":           "0.60",
		"SENTINEL_FAIL_ON_HEAL":        "0",
		"SENTINEL_FAIL_ON_APP_ERRORS":  "0",
		"SENTINEL_AUTO_HITL_THRESHOLD": "0",
		"SENTINEL_TAKEOVER_TIMEOUT":    "1800",
	}
	sources := []string{
		filepath.Join(repo, "cmd", "agentctl", "main.go"),
		filepath.Join(repo, "brain", "healing.py"),
		filepath.Join(repo, "brain", "replay.py"),
		filepath.Join(repo, "brain", "graph.py"),
		filepath.Join(repo, "brain", "__main__.py"),
	}
	var blob string
	for _, f := range sources {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("reading %s: %v", f, err)
		}
		blob += string(b)
	}
	if len(blob) < 1000 {
		t.Fatal("sources came back empty — every assertion below would be vacuous")
	}

	for name, d := range settingsSchema {
		desc, ok := d.(map[string]any)
		if !ok {
			t.Fatalf("settings.%s is not a descriptor", name)
		}
		env, _ := desc["env"].(string)
		if env == "" {
			t.Fatalf("settings.%s has no env", name)
		}
		if _, hasDefault := desc["default"]; !hasDefault {
			t.Fatalf("settings.%s has no default — the wizard would render an empty field", name)
		}
		hint, ok := desc["hint"].(map[string]string)
		if !ok || hint["ru"] == "" || hint["en"] == "" {
			// The whole point of surfacing a setting is that a person can tell what it does, and the
			// wizard is bilingual — a one-language hint would code-switch the interface (ADR-061).
			t.Fatalf("settings.%s needs a bilingual hint, got %v", name, desc["hint"])
		}
		lit, checked := want[env]
		if !checked {
			continue // a variable whose reader lives outside the files above (e.g. TypeScript)
		}
		// Both directions, and the first one is the one that matters. Checking only that the code
		// contains `lit` compares this test's own table against the code and never looks at the
		// schema at all — the descriptor could say anything. Caught by mutating the schema and
		// watching the test stay green.
		// Compared as NUMBERS: Go prints 0.6 where the Python reader spells 0.60, and those are the
		// same default. A string comparison would report a difference that does not exist, and the
		// usual cure for a noisy check is to delete it.
		got := fmt.Sprintf("%v", desc["default"])
		gotF, gotErr := strconv.ParseFloat(got, 64)
		litF, litErr := strconv.ParseFloat(lit, 64)
		same := got == lit || (gotErr == nil && litErr == nil && gotF == litF)
		if !same {
			t.Errorf("settings.%s: schema default %s, but %s is read with %s", name, got, env, lit)
		}
		if !strings.Contains(blob, `"`+env+`", `+lit) && !strings.Contains(blob, `"`+env+`", "`+lit+`"`) {
			t.Errorf("settings.%s: no reader of %s uses %s — this test's table is stale", name, env, lit)
		}
	}
}

// TestReportIsChainedIntoTheRunPath: ADR-089. `agentctl report` is the sole producer of report.html,
// report.json, metrics.prom and junit.xml, and nothing ever called it for a UI-launched run — measured
// on this repo before the fix: 192 runs/control-* directories, ZERO with metrics.prom. All four sat in
// the fetch whitelist and answered 404 in practice, so the product's primary path produced neither the
// human surface (report.html: identity verdicts, per-step outcomes, before→after locators) nor the
// machine one (junit.xml, the contract ADR-073 exists for).
//
// Asserted against the source: the alternative is a full browser run inside a unit test. The live
// end-to-end check was done by hand against a real control-api and is recorded in the PR.
func TestReportIsChainedIntoTheRunPath(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	body := string(src)

	if !strings.Contains(body, "s.generateReport(rec, artDir, cmd.Env, reportState, reportCanceled)") {
		t.Fatal("the run teardown no longer calls generateReport — a UI run stops producing any report")
	}
	// Order matters: report.json is an input persistResult reads, so generating after it would persist
	// a result assembled from a file that did not exist yet.
	gi := strings.Index(body, "s.generateReport(rec, artDir, cmd.Env, reportState, reportCanceled)")
	pi := strings.Index(body, "s.persistResult(rec)")
	if gi < 0 || pi < 0 || gi > pi {
		t.Fatalf("generateReport must precede persistResult (got %d vs %d)", gi, pi)
	}
	// A reporting failure must not rewrite the run's outcome: the verdict is already decided.
	fn := body[strings.Index(body, "func (s *server) generateReport"):]
	fn = fn[:strings.Index(fn, "\n}")]
	// Assignment, not comparison: `rec.State =` is a substring of `rec.State ==`, and the function
	// legitimately READS the state to decide whether there is anything to report. Matching the
	// substring flagged its own guard clause — the same trap that caught a check earlier today.
	assign := regexp.MustCompile(`rec\.(State|ExitCode)\s*=[^=]`)
	if assign.MatchString(fn) {
		t.Error("generateReport must not change the run's state or exit code")
	}
	// …but it must not be silent either.
	if !strings.Contains(fn, "codeReportFailed") {
		t.Error("a failed report must emit a catalogued code, or the artifacts vanish with no explanation")
	}
	// And every artifact it produces must be fetchable, or it exists only on the server's disk.
	for _, a := range []string{"report.html", "report.json", "metrics.prom", "junit.xml"} {
		if !artifactWhitelist[a] {
			t.Errorf("%s is produced but not in artifactWhitelist — the UI would get 400", a)
		}
	}
}

// TestReportSpawnFollowsTheRun: the report is not just called somewhere in the file — it is the SECOND
// agentctl invocation of a finished run, with the run's own artifact dir. Source-matching (the test
// above) proves the call site exists; this proves it actually runs.
func TestReportSpawnFollowsTheRun(t *testing.T) {
	s, repo, argvPath := newArgvCapturingServer(t, 0)
	seedPriorPlan(t, repo, "prior1", "plan.json",
		`{"target_url":"https://app.example","plan_id":"p1","plan_hash":"h","steps":[]}`)
	id := postRunAndWait(t, s, runBody(t, map[string]string{"mode": "replay", "from_run": "prior1"}))

	all := readArgvAll(t, argvPath)
	if len(all) != 2 {
		t.Fatalf("want 2 agentctl invocations (run, report), got %d: %#v", len(all), all)
	}
	want := []string{"report", "--run", filepath.Join(repo, "runs", "control-"+id)}
	if !reflect.DeepEqual(all[1], want) {
		t.Fatalf("report argv:\n got %#v\nwant %#v", all[1], want)
	}
}

// TestReportIsSkippedForARunThatProducedNothing: a spawn failure has no artifacts to report on, and
// `agentctl report` would exit non-zero for the honest reason that there is nothing there. Reporting
// that as a warning would train the reader to ignore the warning.
func TestReportIsSkippedForARunThatProducedNothing(t *testing.T) {
	repo := t.TempDir()
	argvPath := filepath.Join(repo, "argv.txt")
	s := &server{repo: repo, agentctl: filepath.Join(repo, "does-not-exist"),
		token: "secret-tok", corsAllow: map[string]bool{}, runs: map[string]*run{}}
	postRunAndWait(t, s, runBody(t, map[string]string{"target": "https://app.example"}))
	if _, err := os.Stat(argvPath); err == nil {
		t.Fatal("a run that never spawned must not trigger a report spawn")
	}
}

// ADR-099: the trace is downloadable, served as a binary attachment, and byte-exact.
//
// Written as a BEHAVIOURAL test after a mutation walked straight through the Python source check
// that stood in for it: that check asserted the substring `"trace.zip": true` appears in the
// whitelist, and commenting the entry out leaves `// "trace.zip": true,` — which contains it. The
// only assertion a comment cannot satisfy is one that asks the server for the file.
func TestTraceIsDownloadableAsBinary(t *testing.T) {
	s, repo := newRunServer(t)
	id := createRunAndWait(t, s)
	artDir := filepath.Join(repo, "runs", "control-"+id)
	if err := os.MkdirAll(artDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// A real zip's magic plus a byte that is not valid UTF-8: anything treating the response as text
	// mangles it, which is the failure a JSON content type would produce silently.
	body := []byte("PK\x03\x04\xff\xfe binary trace bytes")
	if err := os.WriteFile(filepath.Join(artDir, "trace.zip"), body, 0o600); err != nil {
		t.Fatal(err)
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=trace.zip", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("the trace is not reachable through the API: got %d — the post-mortem of a failed "+
			"run needs shell access to the server again", rec.Code)
	}
	if got := rec.Body.Bytes(); !bytes.Equal(got, body) {
		t.Fatalf("the trace arrived altered: %q want %q", got, body)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/zip" {
		t.Fatalf("Content-Type = %q, want application/zip (a zip served as text arrives corrupted)", ct)
	}
	if cd := rec.Header().Get("Content-Disposition"); !strings.Contains(cd, "attachment") {
		t.Fatalf("Content-Disposition = %q — the browser must never be invited to open a trace", cd)
	}
	// The negative control: the path guard still holds for everything not on the list.
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/runs/"+id+"/artifact?name=checkpoint.db", nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	if rec.Code == http.StatusOK {
		t.Fatal("an unlisted artifact was served; opening the trace must not open the directory")
	}
}
