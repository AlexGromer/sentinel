package main

// POST /v1/runs/{id}/cancel (M9-LIVE).
//
// There was no way to stop a run. A goal-mode run against a real site can walk for minutes, and a stuck
// one — clicking the same disabled control over and over — walked until agentctl's own timeout. The only
// recourse was killing the container, which loses the artifacts of every other run in flight.
//
// Cancelling means signalling a process TREE: control-api spawns agentctl -> the Python brain -> the
// Playwright executor -> Chromium. That is why the run gets its own process group at spawn
// (procgroup_*.go); signalling only the top would leave a browser holding the trace file open.
//
// Graceful first, forced second. SIGTERM gives the executor time to close its trace and the brain time to
// write what it has, so a cancelled run still leaves usable artifacts — the reason to cancel is usually
// to look at what happened so far. SIGKILL follows only if the tree ignores it.

import (
	"net/http"
	"time"
)

const (
	// How long the tree gets to exit on its own before it is forced. Closing a Playwright trace takes
	// well under a second; this is generous enough to be safe and short enough that an operator does not
	// wonder whether the button worked.
	cancelGrace = 3 * time.Second
	// Polling interval while waiting for the graceful exit — fine enough that the usual case returns
	// immediately rather than after a fixed sleep.
	cancelPoll = 100 * time.Millisecond
)

// handleCancelRun stops a running run and reports what it did.
func (s *server) handleCancelRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validRunID(id) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "id must be a bare run id"})
		return
	}

	s.mu.Lock()
	rec, ok := s.runs[id]
	if !ok {
		s.mu.Unlock()
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such run"})
		return
	}
	// A finished run is not an error to cancel — a second click, or a click that raced the run's own
	// exit, should read as "already stopped" rather than a failure the operator has to interpret.
	if rec.State != "running" {
		state, code := rec.State, rec.ExitCode
		s.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{
			"run_id": id, "canceled": false, "state": state, "exit_code": code,
			"reason": "the run had already finished",
		})
		return
	}
	pid := rec.pid
	rec.canceled = true // read by the waiting goroutine, so the terminal state says "canceled", not "crashed"
	s.mu.Unlock()

	if pid == 0 {
		// Between spawnRun inserting the record and the goroutine publishing the pid there is a brief
		// window. The flag is already set, so the run will still be reported as canceled; saying so
		// plainly beats pretending a signal was delivered.
		writeJSON(w, http.StatusOK, map[string]any{
			"run_id": id, "canceled": true, "signalled": false,
			"reason": "the run is starting; it will stop as soon as its process exists",
		})
		return
	}

	if err := killProcTree(pid, false); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error": "could not signal the run: " + err.Error()})
		return
	}

	// Wait briefly for the graceful exit, then force it. Bounded so the request cannot hang.
	forced := false
	deadline := time.Now().Add(cancelGrace)
	for time.Now().Before(deadline) {
		s.mu.RLock()
		done := rec.State != "running"
		s.mu.RUnlock()
		if done {
			break
		}
		time.Sleep(cancelPoll)
	}
	s.mu.RLock()
	stillRunning := rec.State == "running"
	s.mu.RUnlock()
	if stillRunning {
		forced = true
		_ = killProcTree(pid, true)
	}

	s.mu.RLock()
	state, code := rec.State, rec.ExitCode
	s.mu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"run_id": id, "canceled": true, "signalled": true, "forced": forced,
		"state": state, "exit_code": code,
	})
}
