package main

// ADR-126 — control-api talks to the RunControl orchestrator, and enforces what it decides.
//
// WHAT WAS WRONG. The orchestrator was built by M8 (ADR-021), packaged into the .deb since, and
// launched by NOTHING. `ws.go::forwardControl` could already call it — but only if somebody set
// `CONTROL_API_ORCH_ADDR`, which no compose file, no Dockerfile and no installer ever did. So
// takeover/return (ADR-054) and the map gate (ADR-108c) were dead in EVERY shipped configuration:
// `brain/runcontrol.py::_Noop.wired` answered False, and `brain/health.py` read that as "no operator".
//
// THE DIVISION OF LABOUR, and it is the whole design. The orchestrator DECIDES — it reconciles token
// deltas against the run's budget and declares a breach. This process ENFORCES, because it is the only
// one that can: under compose every service has its own PID namespace, so the orchestrator cannot
// signal a run's processes at all. We spawn `agentctl` into its own process group (`setProcGroup`),
// which is why `killProcTree` reaches the brain, the executor AND Chromium — strictly more than the
// single-process SIGTERM the orchestrator used to manage when it was the brain's parent.
//
// ⚠ FAIL-OPEN, EVERY WAY IN. No orchestrator wired, an unreachable socket, a service that dies
// mid-run — none of it stops a run. Supervision is an addition to a run, never a precondition for one:
// a deployment that never had an orchestrator behaves exactly as it did before this file existed, and
// one whose orchestrator dies keeps its runs. Each failure is SAID once, not per poll — a supervisor
// that floods the log the moment it loses its peer is a supervisor people turn off.

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

const (
	// How often a running run is asked whether it has breached its budget. Two seconds is chosen
	// against what it costs to be wrong in each direction: a breach noticed late spends a couple of
	// seconds of tokens, while polling hard would put a gRPC round trip in front of every run for the
	// whole of its life. The brain's own reporting is the fast path; this is the backstop's heartbeat.
	orchPollInterval = 2 * time.Second
	// A supervision call is a local unix-socket round trip. Long enough to survive a busy host, short
	// enough that a hung orchestrator cannot pin this goroutine for a whole poll interval.
	orchCallTimeout = 3 * time.Second
	// ⚠ The node name this process reports under. It must NOT be "heal" — `ReportEvent` files a delta
	// under the heal budget for that one name and under the plan budget for everything else, so a
	// poll named "heal" would be counted against the wrong ledger. It carries zero tokens either way,
	// but the name is data, and the one place it is read is a branch.
	orchPollNode = "control-api"
	// ⚠ The run id `/readyz` asks under. Reserved and NOT a real run: `validRunID` accepts only hex,
	// so this name can never collide with one — a probe that happened to reuse a live id would file
	// its (zero) delta against that run's ledger and, worse, would read that run's abort verdict as
	// its own readiness answer.
	orchProbeRunID = "readyz-probe"
)

// orchClient dials the orchestrator. A fresh connection per operation, matching `forwardControl`:
// these are low-frequency control calls over a unix socket, and a pooled connection would have to be
// re-dialled after any orchestrator restart — that is state to get wrong in exchange for microseconds.
func (s *server) orchClient() (pb.RunControlClient, func(), error) {
	if s.orchAddr == "" {
		return nil, nil, fmt.Errorf("no orchestrator wired (set CONTROL_API_ORCH_ADDR)")
	}
	conn, err := grpc.NewClient(s.orchAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, nil, err
	}
	return pb.NewRunControlClient(conn), func() { _ = conn.Close() }, nil
}

// registerRun tells the orchestrator this run exists, and under which budgets.
//
// Called with THIS process's run id — the same string the hub shows, the store persists and
// `Takeover(run_id)` will later carry. Before ADR-126 the orchestrator minted its own id, so the two
// could never match and an operator's takeover would have addressed a run that, as far as the
// orchestrator was concerned, did not exist.
//
// Budgets ride here rather than through the environment for the reason `writeRunConfig` already
// documents: `PLAN_TOKEN_LIMIT` and friends do not pass agentctl's env allowlist. The orchestrator
// treats 0 as "use the deployment default", so an unset budget is not a budget of zero.
func (s *server) registerRun(runID string, plan, heal, total string) {
	cl, done, err := s.orchClient()
	if err != nil {
		return // not wired, or a bad address — reported once at startup, not per run
	}
	defer done()
	ctx, cancel := context.WithTimeout(context.Background(), orchCallTimeout)
	defer cancel()
	atoi := func(v string) int64 {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n < 0 {
			return 0
		}
		return n
	}
	if _, err := cl.StartRun(ctx, &pb.StartRunRequest{
		RunId:           runID,
		PlanTokenLimit:  atoi(plan),
		HealTokenLimit:  atoi(heal),
		TotalTokenLimit: atoi(total),
	}); err != nil {
		// Fail-open and said once: a run that could not be registered still runs, it is merely
		// unsupervised — and an operator reading the journal has to be able to tell that apart from a
		// run that was supervised and stayed within budget.
		fmt.Fprintf(os.Stderr, "[control-api] run %s could not be registered with the orchestrator (%v) "+
			"— it runs UNSUPERVISED: no budget ceiling, no takeover, no map gate\n", runID, err)
	}
}

// superviseRun polls the orchestrator for this run's verdict until the run ends, and stops the run
// when a budget breach is declared.
//
// ⚠ THE POLL IS A ZERO-TOKEN `ReportEvent`, NOT A NEW RPC, and that is not a shortcut. It is the
// idiom this codebase already has: `brain/runcontrol.py::poll()` is literally
// `report(run_id, node, 0, 0)`. Reusing the verb means no proto change, no stub regeneration in two
// languages, and no second way of asking the same question — the ledger is untouched because the
// delta is zero.
func (s *server) superviseRun(rec *run) {
	if s.orchAddr == "" {
		return
	}
	ticker := time.NewTicker(orchPollInterval)
	defer ticker.Stop()
	for range ticker.C {
		s.mu.RLock()
		state, pid := rec.State, rec.pid
		s.mu.RUnlock()
		if state != "running" {
			return // the run finished on its own; nothing left to supervise
		}
		if pid == 0 {
			continue // spawned but the pid is not published yet — nothing to signal even if it breached
		}

		cl, done, err := s.orchClient()
		if err != nil {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), orchCallTimeout)
		ctl, err := cl.ReportEvent(ctx, &pb.RunEvent{RunId: rec.ID, Node: orchPollNode, Status: "running"})
		cancel()
		done()
		if err != nil {
			// The orchestrator went away mid-run. Say it ONCE and stop supervising: retrying forever
			// would fill the journal with one line every two seconds for the rest of the run, and the
			// run itself is unaffected — losing the supervisor is not losing the work.
			fmt.Fprintf(os.Stderr, "[control-api] lost the orchestrator while supervising run %s (%v) "+
				"— it continues UNSUPERVISED from here\n", rec.ID, err)
			return
		}
		if !ctl.GetAbort() {
			continue
		}

		// The hard ceiling, applied where it can be applied. `canceled` is set BEFORE the signal for
		// the same reason `handleCancelRun` does it: the waiting goroutine reads that flag to decide
		// whether the terminal state is "canceled" or "crashed", and a signal that landed first would
		// make a deliberate stop look like a failure of the run.
		reason := ctl.GetReason()
		fmt.Fprintf(os.Stderr, "[control-api] run %s breached its budget (%s) — stopping it\n", rec.ID, reason)
		s.mu.Lock()
		rec.canceled = true
		if rec.Error == "" {
			rec.Error = "budget ceiling: " + reason
		}
		s.mu.Unlock()
		s.stopRunTree(rec, pid)
		return
	}
}
