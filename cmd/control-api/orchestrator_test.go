//go:build !windows

package main

// ADR-126 — control-api registers runs with the orchestrator, and ENFORCES the breach it declares.
//
// ⚠ UNIX-ONLY, and that is a statement about the subject rather than a convenience. The enforcement
// under test is a process-GROUP signal, and `setProcGroup` is an empty function on Windows
// (procgroup_windows.go) — a copy of these tests there would assert nothing about the mechanism and
// would pass whether or not it worked.
//
// Each test here asserts an OUTCOME rather than a call. "ReportEvent was invoked" would pass over the
// defect that matters — a supervisor that polls diligently and then does nothing with the answer —
// and this repository has already paid for assertions of that shape once (a mutation deleting a
// journal call survived every gate).

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"

	"google.golang.org/grpc"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

// alive reports whether the process still exists. Signal 0 performs the permission and existence
// checks and delivers nothing — the standard way to ask, and the only one that does not disturb what
// it is asking about.
func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

// budgetOrch is a RunControl that answers `abort` after a set number of ReportEvent calls, and records
// what it was told. It stands in for a real orchestrator's verdict without reimplementing its ledger:
// what is under test here is control-api's reaction, not the orchestrator's arithmetic (that has its
// own tests in cmd/orchestrator).
type budgetOrch struct {
	pb.UnimplementedRunControlServer
	mu          sync.Mutex
	started     []*pb.StartRunRequest
	polls       int
	abortAfter  int // 0 = never abort
	abortReason string
}

func (b *budgetOrch) StartRun(_ context.Context, r *pb.StartRunRequest) (*pb.StartRunReply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.started = append(b.started, r)
	return &pb.StartRunReply{Ok: true}, nil
}

func (b *budgetOrch) ReportEvent(_ context.Context, e *pb.RunEvent) (*pb.Control, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.polls++
	if b.abortAfter > 0 && b.polls >= b.abortAfter {
		return &pb.Control{Abort: true, Reason: b.abortReason}, nil
	}
	return &pb.Control{}, nil
}

func (b *budgetOrch) snapshot() (int, []*pb.StartRunRequest) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.polls, append([]*pb.StartRunRequest(nil), b.started...)
}

func startBudgetOrch(t *testing.T, o *budgetOrch) string {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "orch.sock")
	lis, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	g := grpc.NewServer()
	pb.RegisterRunControlServer(g, o)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)
	return "unix:" + sock
}

// sleeperRun starts a real child process in its own group and registers it as a running run, so the
// supervisor has something it can actually signal. A fake pid would let a broken kill path pass.
func sleeperRun(t *testing.T, s *server, id string) *run {
	t.Helper()
	cmd := exec.Command("sleep", "120")
	setProcGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	rec := &run{ID: id, State: "running", pid: cmd.Process.Pid}
	s.mu.Lock()
	s.runs[id] = rec
	s.mu.Unlock()
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() {
		_ = killProcTree(cmd.Process.Pid, true)
	})
	return rec
}

func TestRunIsRegisteredWithTheOrchestratorUnderThisProcessId(t *testing.T) {
	o := &budgetOrch{}
	s := newTestServer()
	s.orchAddr = startBudgetOrch(t, o)

	s.registerRun("run-abc", "1234", "567", "")

	_, started := o.snapshot()
	if len(started) != 1 {
		t.Fatalf("StartRun calls: %d, want 1", len(started))
	}
	// The id is the point of the whole call. Before ADR-126 the orchestrator minted its own, so a
	// later Takeover(run_id) from this process addressed a run it had never heard of.
	if started[0].RunId != "run-abc" {
		t.Errorf("registered as %q, want %q — the orchestrator must know the run by the id the hub, "+
			"the store and Takeover all use", started[0].RunId, "run-abc")
	}
	if started[0].PlanTokenLimit != 1234 || started[0].HealTokenLimit != 567 {
		t.Errorf("budgets did not arrive: plan=%d heal=%d, want 1234/567",
			started[0].PlanTokenLimit, started[0].HealTokenLimit)
	}
	// An unset budget is NOT a budget of zero: the orchestrator reads 0 as "use the deployment
	// default", and sending anything else would silently make an omitted limit into a hard one.
	if started[0].TotalTokenLimit != 0 {
		t.Errorf("an unset total budget arrived as %d, want 0 (= use the deployment default)",
			started[0].TotalTokenLimit)
	}
}

func TestABreachDeclaredByTheOrchestratorActuallyStopsTheRun(t *testing.T) {
	o := &budgetOrch{abortAfter: 1, abortReason: "plan budget 50000 reached"}
	s := newTestServer()
	s.orchAddr = startBudgetOrch(t, o)
	rec := sleeperRun(t, s, "run-breach")
	pid := rec.pid

	go s.superviseRun(rec)

	// The outcome, not the call: the process the run leads must be gone. Polled rather than slept on,
	// because a fixed sleep is either flaky on a loaded machine or slow on an idle one.
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if !alive(pid) {
			break // the process group is gone: the ceiling was enforced
		}
		time.Sleep(100 * time.Millisecond)
	}
	if alive(pid) {
		t.Fatal("the orchestrator declared a breach and the run is still running — the supervisor " +
			"polled and did nothing with the answer, which is exactly the defect this test exists for")
	}

	s.mu.RLock()
	canceled, errText := rec.canceled, rec.Error
	s.mu.RUnlock()
	// A budget stop is a DELIBERATE stop. Without the flag the waiting goroutine reports the run as
	// crashed, and an operator cannot tell a ceiling from a segfault.
	if !canceled {
		t.Error("the run was stopped but not marked canceled — it will be reported as a crash")
	}
	if errText == "" {
		t.Error("nothing recorded WHY the run was stopped; 'canceled' with no reason is the shape of " +
			"an unexplained failure")
	}
}

func TestAnAbsentOrchestratorChangesNothingAboutARun(t *testing.T) {
	s := newTestServer() // orchAddr == "" — the pre-ADR-126 deployment, and every one that opts out
	rec := sleeperRun(t, s, "run-unwired")
	pid := rec.pid

	s.registerRun("run-unwired", "10", "10", "10") // must not panic, must not block
	done := make(chan struct{})
	go func() { s.superviseRun(rec); close(done) }()

	select {
	case <-done: // returned immediately, as it must
	case <-time.After(5 * time.Second):
		t.Fatal("superviseRun did not return with no orchestrator wired — supervision is an addition " +
			"to a run, never a precondition for one")
	}
	if !alive(pid) {
		t.Fatal("the run was killed although no orchestrator was wired")
	}
}

func TestReadinessTellsTheTruthAboutTheOrchestrator(t *testing.T) {
	// The whole reason the gate demanded a probe: this component's absence is INVISIBLE from outside.
	// Runs still start, still finish, still produce artifacts — only the ceiling, the takeover and the
	// map gate quietly stop existing. A probe that answered `ok` regardless would be worse than none,
	// because it would put a green tick next to the thing that is missing.
	o := &budgetOrch{}
	s := newTestServer()

	if got := s.probeOrchestrator(); got.Status != "skipped" {
		t.Errorf("no orchestrator configured -> %q, want \"skipped\": a deployment that deliberately "+
			"runs none is supported, not broken, and `error` would make it permanently 503", got.Status)
	}

	s.orchAddr = startBudgetOrch(t, o)
	if got := s.probeOrchestrator(); got.Status != "ok" {
		t.Errorf("a live orchestrator -> %q (%s), want \"ok\"", got.Status, got.Detail)
	}

	// And the case that matters: configured, but not there. This is the state a deployment lands in
	// when the service dies or was never started, and it is exactly what must NOT read as healthy.
	s.orchAddr = "unix:/nonexistent/orch.sock"
	got := s.probeOrchestrator()
	if got.Status != "error" {
		t.Errorf("a configured but unreachable orchestrator -> %q, want \"error\" — reporting anything "+
			"else puts a green tick on a component that is gone", got.Status)
	}
	if got.Detail == "" {
		t.Error("the error carries no detail; an operator reading /readyz needs to know WHICH address failed")
	}
}

func TestAnOrchestratorThatDiesMidRunLeavesTheRunAlone(t *testing.T) {
	o := &budgetOrch{}
	addr := startBudgetOrch(t, o)
	s := newTestServer()
	// A path that no longer answers: the socket file is gone, exactly as it is after the service
	// stops (the orchestrator removes it on shutdown, deliberately, so an unanswered path is absent
	// rather than silently dead).
	s.orchAddr = addr
	_ = os.Remove(addr[len("unix:"):])

	rec := sleeperRun(t, s, "run-orphaned")
	pid := rec.pid
	done := make(chan struct{})
	go func() { s.superviseRun(rec); close(done) }()

	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatal("superviseRun kept retrying a dead orchestrator instead of giving up once")
	}
	if !alive(pid) {
		t.Fatal("losing the orchestrator killed the run — losing the supervisor is not losing the work")
	}
}
