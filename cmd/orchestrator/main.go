// Command orchestrator is the Sentinel run supervisor (M8, ADR-021; wired by ADR-126): a long-lived
// gRPC RunControl SERVICE that reconciles per-step token deltas against each run's budget, carries the
// operator's takeover/return (ADR-054) and the map gate's answer (ADR-108c) to the brain, and declares
// a budget breach so the process that owns the run can stop it.
//
// Usage: orchestrator --serve [--addr <unix socket>] [--plan-token-limit N] [--heal-token-limit N]
//
//	[--total-token-limit N]
//
// WHY THIS IS A SERVICE AND NOT A PER-RUN COMMAND (ADR-126). It used to be the latter: `main` minted
// its own run id, listened on `state/sentinel-orch-<id>.sock`, spawned the brain itself and exited
// with it. Three things followed, and all three are why nothing ever wired it:
//
//  1. `CONTROL_API_ORCH_ADDR` is read ONCE at control-api startup, so a per-run socket path could
//     never be addressed — two concurrent runs had no answer at all.
//  2. The id in `Takeover(run_id)` comes from control-api; the id this process minted was a different
//     string, so the call could not have found the run even if it had arrived.
//  3. Spawning the brain made this a SECOND owner of the run lifecycle, next to `agentctl` — and it
//     spawned `python -m brain` directly, bypassing agentctl's env allowlist and its store wiring.
//
// The proto has said "service" all along: `StartRun(run_id, limits)` is a registration call, which a
// one-run process has no use for. So the one-run path is gone, `agentctl` is again the single owner of
// the run, and `runs` — a map that was already keyed by run id — serves as many runs as ask.
//
// ⚠ THE HARD CEILING MOVED, IT DID NOT VANISH, and that is the one thing ADR-021 must not lose. The
// old backstop was `cmd.Process.Signal(SIGTERM)`, available only because this process was the brain's
// parent. A service cannot do that: under compose each service has its OWN PID namespace, so
// signalling another container's processes is not merely discouraged, it is impossible. Enforcement
// therefore belongs to control-api, which already owns a strictly better mechanism —
// `cmd/control-api/cancel.go` signals the whole process GROUP (SIGTERM, then SIGKILL), reaching the
// brain, the executor AND Chromium, where the old backstop reached a single process. This service
// DECIDES; control-api ACTS.
//
// Uses only google.golang.org/grpc + the generated internal/orchestrator/pb stubs (no OTel dep).
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

// runState tracks cumulative spend + limits for one run (0 limit = that gate is off).
type runState struct {
	planTokens, healTokens, totalTokens int64
	planLimit, healLimit, totalLimit    int64
	breached                            bool
	reason                              string
	takeover                            bool // M9.8 F4 (ADR-054): operator takeover pending (set by Takeover, cleared by Return)
	// ADR-108c: the map gate. "" = nobody has answered yet, which is what makes the gate a gate; the
	// brain waits on an empty answer rather than proceeding. "approve" | "reject" once a person decides.
	mapDecision, mapReason string
}

type orchestrator struct {
	pb.UnimplementedRunControlServer
	mu                         sync.Mutex
	runs                       map[string]*runState
	defPlan, defHeal, defTotal int64
}

func newOrchestrator(defPlan, defHeal, defTotal int64) *orchestrator {
	return &orchestrator{runs: map[string]*runState{}, defPlan: defPlan, defHeal: defHeal, defTotal: defTotal}
}

// get returns the run's state (lazily created with default limits). Caller holds o.mu.
func (o *orchestrator) get(runID string) *runState {
	rs := o.runs[runID]
	if rs == nil {
		rs = &runState{planLimit: o.defPlan, healLimit: o.defHeal, totalLimit: o.defTotal}
		o.runs[runID] = rs
	}
	return rs
}

func (o *orchestrator) StartRun(_ context.Context, r *pb.StartRunRequest) (*pb.StartRunReply, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	rs := o.get(r.RunId)
	if r.PlanTokenLimit > 0 {
		rs.planLimit = r.PlanTokenLimit
	}
	if r.HealTokenLimit > 0 {
		rs.healLimit = r.HealTokenLimit
	}
	if r.TotalTokenLimit > 0 {
		rs.totalLimit = r.TotalTokenLimit
	}
	fmt.Fprintf(os.Stderr, "[orchestrator] StartRun %s plan=%d heal=%d total=%d\n",
		r.RunId, rs.planLimit, rs.healLimit, rs.totalLimit)
	return &pb.StartRunReply{Ok: true}, nil
}

func (o *orchestrator) ReportEvent(_ context.Context, e *pb.RunEvent) (*pb.Control, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	rs := o.get(e.RunId)
	delta := e.PromptTokens + e.CompletionTokens
	if e.Node == "heal" {
		rs.healTokens += delta
	} else {
		rs.planTokens += delta
	}
	rs.totalTokens += delta
	if !rs.breached {
		switch {
		case rs.totalLimit > 0 && rs.totalTokens >= rs.totalLimit:
			rs.breached, rs.reason = true, fmt.Sprintf("total budget %d reached", rs.totalLimit)
		case rs.planLimit > 0 && rs.planTokens >= rs.planLimit:
			rs.breached, rs.reason = true, fmt.Sprintf("plan budget %d reached", rs.planLimit)
		case rs.healLimit > 0 && rs.healTokens >= rs.healLimit:
			rs.breached, rs.reason = true, fmt.Sprintf("heal budget %d reached", rs.healLimit)
		}
	}
	if rs.breached {
		return &pb.Control{Abort: true, Reason: rs.reason}, nil
	}
	// M9.8 F4 (ADR-054): a pending takeover pauses the brain. abort (a hard stop) already took
	// precedence above; takeover only fires when the run is otherwise allowed to continue.
	if rs.takeover {
		return &pb.Control{Takeover: true, Reason: "operator takeover"}, nil
	}
	// ADR-108c: carried on EVERY reply, not only when set. The brain polls this node while it waits, and
	// an answer that only appeared on some replies would be a race the brain could not see through.
	return &pb.Control{Abort: false, MapDecision: rs.mapDecision, Reason: rs.mapReason}, nil
}

func (o *orchestrator) Abort(_ context.Context, r *pb.AbortRequest) (*pb.AbortReply, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	rs := o.get(r.RunId)
	rs.breached, rs.reason = true, "external abort: "+r.Reason
	return &pb.AbortReply{Ok: true}, nil
}

// Takeover sets a per-run takeover flag (M9.8 F4, ADR-054). The next ReportEvent reply then carries
// Control.takeover=true, so the brain interrupt()s + persists at its superstep boundary and yields the
// live browser to the operator. External signal — forwarded by the control-API over its WebSocket.
func (o *orchestrator) Takeover(_ context.Context, r *pb.TakeoverRequest) (*pb.TakeoverReply, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.get(r.RunId).takeover = true
	fmt.Fprintf(os.Stderr, "[orchestrator] Takeover %s reason=%q\n", r.RunId, r.Reason)
	return &pb.TakeoverReply{Ok: true}, nil
}

// Return clears the takeover flag (M9.8 F4, ADR-054); the brain's next poll sees no takeover pending and
// resumes the checkpointer thread (Command(resume=...)) from exactly where it paused.
func (o *orchestrator) Return(_ context.Context, r *pb.ReturnRequest) (*pb.ReturnReply, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.get(r.RunId).takeover = false
	fmt.Fprintf(os.Stderr, "[orchestrator] Return %s\n", r.RunId)
	return &pb.ReturnReply{Ok: true}, nil
}

// DecideMap records the operator's answer to the map gate (ADR-108c). The brain sees it on its next
// ReportEvent reply, at its own superstep boundary — the same shape as Takeover.
//
// An unknown decision is REFUSED rather than stored: a typo that reached the brain as neither approve
// nor reject would leave the run waiting forever on a gate somebody believes they answered.
func (o *orchestrator) DecideMap(_ context.Context, r *pb.MapDecisionRequest) (*pb.MapDecisionReply, error) {
	if r.Decision != "approve" && r.Decision != "reject" {
		return nil, status.Errorf(codes.InvalidArgument, "decision must be approve|reject, got %q", r.Decision)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	rs := o.get(r.RunId)
	rs.mapDecision, rs.mapReason = r.Decision, r.Reason
	fmt.Fprintf(os.Stderr, "[orchestrator] DecideMap %s decision=%s reason=%q\n", r.RunId, r.Decision, r.Reason)
	return &pb.MapDecisionReply{Ok: true}, nil
}

// ⚠ `breachOf` USED TO LIVE HERE and was deleted by ADR-126, deliberately rather than left unused.
// It served the watchdog that SIGTERM-ed the brain this process had spawned; with the spawn gone it
// answered a question nobody asked. Keeping it would have been worse than removing it: a live-looking
// method named after the hard ceiling is exactly what convinces the next reader that this service
// still enforces one. It does not — it DECIDES, and control-api enforces (see the file docstring).
// The breach reaches the outside the same way everything else does: on the next `ReportEvent` reply.
func main() {
	serve := flag.Bool("serve", false, "run as a long-lived RunControl service (the only mode since ADR-126)")
	addr := flag.String("addr", "", "unix socket to listen on (default <repo>/state/orch.sock)")
	planLimit := flag.Int64("plan-token-limit", 50000, "default plan token budget for a run that sets none of its own")
	healLimit := flag.Int64("heal-token-limit", 20000, "default heal token budget for a run that sets none of its own")
	totalLimit := flag.Int64("total-token-limit", 0, "default total token budget (0=off)")
	flag.Parse()

	// ⚠ REQUIRED rather than defaulted, deliberately. Before ADR-126 this binary with no flags meant
	// "supervise one run and exit"; anyone still holding an old invocation would otherwise now get a
	// process that sits there serving nobody, which looks exactly like a working service. An explicit
	// flag turns that silence into a message that names what changed and what to do instead.
	if !*serve {
		fmt.Fprintln(os.Stderr,
			"orchestrator: --serve is required.\n\n"+
				"This is a long-lived RunControl SERVICE (ADR-126). The per-run form it used to have —\n"+
				"  orchestrator --target <URL> [--mode ...] [--plan ...]\n"+
				"— is gone: it spawned the brain itself, which made it a second owner of the run beside\n"+
				"agentctl, and its per-run socket could not be addressed by control-api's single\n"+
				"CONTROL_API_ORCH_ADDR. Start runs with `agentctl run` (or through control-api), and run\n"+
				"this alongside them:\n\n"+
				"  orchestrator --serve --addr /app/state/orch.sock")
		os.Exit(2)
	}

	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: cwd: %v\n", err)
		os.Exit(1)
	}
	sock := *addr
	if sock == "" {
		_ = os.MkdirAll(filepath.Join(repo, "state"), 0o755)
		sock = filepath.Join(repo, "state", "orch.sock")
	}
	// A socket left by an unclean shutdown makes `listen` fail with "address already in use" over a
	// path nothing is serving. Same problem, and the same one-line answer, as store-gateway's.
	_ = os.Remove(sock)
	lis, err := net.Listen("unix", sock)
	if err != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: listen %s: %v\n", sock, err)
		os.Exit(1)
	}

	orch := newOrchestrator(*planLimit, *healLimit, *totalLimit)
	g := grpc.NewServer()
	pb.RegisterRunControlServer(g, orch)

	// The socket EXISTING is what control-api dials and what the compose healthcheck tests, so it must
	// not outlive the process: a path that answers nothing is worse than a path that is absent, because
	// only an actual call can tell the first from a healthy service.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-stop
		fmt.Fprintln(os.Stderr, "[orchestrator] signal received - stopping")
		g.GracefulStop()
		_ = os.Remove(sock)
	}()

	fmt.Fprintf(os.Stderr, "[orchestrator] serving RunControl on %s (defaults plan=%d heal=%d total=%d)\n",
		sock, *planLimit, *healLimit, *totalLimit)
	if err := g.Serve(lis); err != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: serve: %v\n", err)
		_ = os.Remove(sock)
		os.Exit(1)
	}
	_ = os.Remove(sock)
}
