package main

// Gates for stopping a run and for the store marker (M9-LIVE). Both close the same class of defect: the
// interface could not tell the operator what state the system was actually in.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"testing"
	"time"
)

func postCancel(t *testing.T, s *server, id string, tok bool) (int, map[string]any) {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, "/v1/runs/"+id+"/cancel", nil)
	if tok {
		r.Header.Set("Authorization", "Bearer secret-tok")
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, r)
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return rec.Code, body
}

func TestCancelRequiresToken(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	if code, _ := postCancel(t, s, "aaaaaaaaaaaaaaaa", false); code != http.StatusForbidden {
		t.Fatalf("unauthenticated cancel: got %d want 403", code)
	}
}

func TestCancelUnknownRun(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	if code, _ := postCancel(t, s, "aaaaaaaaaaaaaaaa", true); code != http.StatusNotFound {
		t.Fatalf("unknown run: got %d want 404", code)
	}
}

func TestCancelRejectsBadRunID(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}}
	for _, bad := range []string{"a.b", "a/b", "привет"} {
		if code, _ := postCancel(t, s, bad, true); code == http.StatusOK {
			t.Fatalf("run id %q must be refused, got 200", bad)
		}
	}
}

// Cancelling a finished run is NOT an error: a second click, or one that raced the run's own exit, must
// read as "already stopped" rather than a failure the operator has to interpret.
func TestCancelFinishedRunIsNotAnError(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(),
		runs: map[string]*run{"aaaaaaaaaaaaaaaa": {ID: "aaaaaaaaaaaaaaaa", State: "done", ExitCode: 0}}}
	code, body := postCancel(t, s, "aaaaaaaaaaaaaaaa", true)
	if code != http.StatusOK {
		t.Fatalf("cancelling a finished run: got %d want 200", code)
	}
	if body["canceled"] != false {
		t.Fatalf("a finished run was reported as canceled: %v", body)
	}
	if body["reason"] == nil || body["reason"] == "" {
		t.Fatal("the answer must say WHY nothing was canceled")
	}
}

// The whole point: a run signalled by a human must not be reported as a crash. A killed process exits
// -1, which is indistinguishable from a signal-killed failure unless the deliberate stop is recorded.
func TestCanceledRunReportsCanceledNotFailed(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{},
		agentctl: "/bin/sh"}
	// A real child that ignores nothing and simply sleeps: enough to be signalled while "running".
	cmd := exec.Command("/bin/sh", "-c", "sleep 30")
	setProcGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Skipf("cannot spawn a child here: %v", err)
	}
	rec := &run{ID: "bbbbbbbbbbbbbbbb", State: "running", pid: cmd.Process.Pid, stream: newRunStream()}
	s.runs[rec.ID] = rec

	// Mirror spawnRun's waiting goroutine: it is what turns the flag into the terminal state.
	done := make(chan struct{})
	go func() {
		err := cmd.Wait()
		s.mu.Lock()
		if rec.canceled {
			rec.State, rec.ExitCode = "canceled", -1
		} else if err != nil {
			rec.State, rec.ExitCode = "done", -1
		} else {
			rec.State, rec.ExitCode = "done", 0
		}
		s.mu.Unlock()
		close(done)
	}()

	code, body := postCancel(t, s, rec.ID, true)
	if code != http.StatusOK {
		t.Fatalf("cancel: got %d body=%v", code, body)
	}
	if body["canceled"] != true || body["signalled"] != true {
		t.Fatalf("cancel should report it signalled the run: %v", body)
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the child never exited after being signalled")
	}
	s.mu.RLock()
	state, exit := rec.State, rec.ExitCode
	s.mu.RUnlock()
	if state != "canceled" {
		t.Fatalf("a deliberate stop must read as canceled, got %q (exit %d) — indistinguishable from a crash",
			state, exit)
	}
}

// A cancel arriving in the window between the record existing and its pid being published must still take
// effect, and must say plainly that nothing was signalled yet rather than implying a delivered signal.
func TestCancelBeforePidIsPublished(t *testing.T) {
	rec := &run{ID: "cccccccccccccccc", State: "running", pid: 0}
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{rec.ID: rec}}
	code, body := postCancel(t, s, rec.ID, true)
	if code != http.StatusOK {
		t.Fatalf("got %d body=%v", code, body)
	}
	if body["canceled"] != true {
		t.Fatalf("the request must take effect: %v", body)
	}
	if body["signalled"] != false {
		t.Fatalf("no process existed, so it must not claim to have signalled one: %v", body)
	}
	s.mu.RLock()
	flagged := rec.canceled
	s.mu.RUnlock()
	if !flagged {
		t.Fatal("the cancel flag must be set so the run still reports canceled when it starts")
	}
}

// --- store marker ---------------------------------------------------------------------------------

// An empty list means BOTH "nothing saved yet" and "this deployment saves nothing", and those looked
// identical — which is why the library read as broken. The fact now travels with the data.
func TestListEndpointsCarryStoreMarker(t *testing.T) {
	s := &server{token: "secret-tok", repo: t.TempDir(), runs: map[string]*run{}} // store == nil
	for _, path := range []string{"/v1/scenarios", "/v1/tests", "/v1/chats", "/v1/results",
		"/v1/trends?metric=coverage"} {
		t.Run(path, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, path, nil)
			r.Header.Set("Authorization", "Bearer secret-tok")
			rec := httptest.NewRecorder()
			s.mux().ServeHTTP(rec, r)
			if rec.Code != http.StatusOK {
				t.Fatalf("%s: got %d", path, rec.Code)
			}
			var body map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("%s: %v", path, err)
			}
			if body["store"] != false {
				t.Fatalf("%s must report store=false in the standalone tier, got %v", path, body["store"])
			}
			reason, _ := body["store_reason"].(string)
			if reason == "" {
				t.Fatalf("%s: store=false must come with a reason an operator can act on", path)
			}
			// The reason has to name the actual remedy, not merely state the problem.
			if !contains(reason, "store-gateway") || !contains(reason, "CONTROL_API_STORE_ADDR") {
				t.Fatalf("%s: the reason must name how to fix it, got %q", path, reason)
			}
		})
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
