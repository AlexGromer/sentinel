// Command orchestrator is the Sentinel run supervisor (M8, ADR-021): a long-lived gRPC RunControl
// server that spawns the Python brain, reconciles its per-step token deltas against the run budget,
// and enforces a model-INDEPENDENT hard ceiling — signalling abort via ReportEvent and, as a
// backstop, SIGTERM-ing the brain subprocess if it does not converge within a grace period.
//
// Usage: orchestrator --target <URL> [--planner llm|heuristic] [--mode explore|replay] [--plan <p>]
//
//	[--plan-token-limit N] [--heal-token-limit N] [--total-token-limit N] [--kill-grace 10s]
//
// Uses only google.golang.org/grpc + the generated internal/orchestrator/pb stubs (no OTel dep).
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/AlexGromer/sentinel/internal/orchestrator/pb"
)

func newRunID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

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

func (o *orchestrator) breachOf(runID string) (bool, string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if rs := o.runs[runID]; rs != nil {
		return rs.breached, rs.reason
	}
	return false, ""
}

func main() {
	addr := flag.String("addr", "", "unix socket to listen on (default state/sentinel-orch-<id>.sock)")
	target := flag.String("target", "", "target URL for the brain run")
	planner := flag.String("planner", "llm", "planner: heuristic|llm")
	mode := flag.String("mode", "explore", "brain RUN_MODE (explore|replay)")
	planFile := flag.String("plan", "", "plan.json (replay)")
	planLimit := flag.Int64("plan-token-limit", 50000, "plan token budget")
	healLimit := flag.Int64("heal-token-limit", 20000, "heal token budget")
	totalLimit := flag.Int64("total-token-limit", 0, "total token budget (0=off)")
	grace := flag.Duration("kill-grace", 10*time.Second, "grace before SIGTERM after a budget breach")
	flag.Parse()

	repo, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: cwd: %v\n", err)
		os.Exit(1)
	}
	runID := newRunID()
	sock := *addr
	if sock == "" {
		_ = os.MkdirAll(filepath.Join(repo, "state"), 0o755)
		sock = filepath.Join(repo, "state", "sentinel-orch-"+runID+".sock")
	}
	_ = os.Remove(sock)
	lis, err := net.Listen("unix", sock)
	if err != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: listen %s: %v\n", sock, err)
		os.Exit(1)
	}
	defer os.Remove(sock)

	orch := newOrchestrator(*planLimit, *healLimit, *totalLimit)
	g := grpc.NewServer()
	pb.RegisterRunControlServer(g, orch)
	go func() { _ = g.Serve(lis) }()
	defer g.GracefulStop()
	orch.StartRun(context.Background(), &pb.StartRunRequest{
		RunId: runID, PlanTokenLimit: *planLimit, HealTokenLimit: *healLimit, TotalTokenLimit: *totalLimit})

	// spawn the brain pointed at this orchestrator
	brainPython := filepath.Join(repo, ".venv", "bin", "python")
	if _, statErr := os.Stat(brainPython); statErr != nil {
		brainPython = "python3"
	}
	if v := os.Getenv("BRAIN_PYTHON"); v != "" {
		brainPython = v
	}
	artifactDir := filepath.Join(repo, "runs", runID)
	_ = os.MkdirAll(artifactDir, 0o755)
	cmd := exec.Command(brainPython, "-m", "brain")
	cmd.Dir = repo
	cmd.Env = append(os.Environ(),
		"RUN_ID="+runID, "RUN_MODE="+*mode, "TARGET_URL="+*target,
		"PLANNER="+*planner, "PLAN_FILE="+*planFile,
		"PW_EXECUTOR_CMD=node "+filepath.Join(repo, "pw-executor", "dist", "server.js"),
		"PYTHONPATH="+repo, "ORCH_ADDR="+sock, "ARTIFACT_DIR="+artifactDir,
		fmt.Sprintf("PLAN_TOKEN_LIMIT=%d", *planLimit),
		fmt.Sprintf("HEAL_TOKEN_LIMIT=%d", *healLimit),
		fmt.Sprintf("TOTAL_TOKEN_LIMIT=%d", *totalLimit),
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	fmt.Fprintf(os.Stderr, "[orchestrator] run_id=%s mode=%s target=%s orch=%s\n", runID, *mode, *target, sock)
	if startErr := cmd.Start(); startErr != nil {
		fmt.Fprintf(os.Stderr, "orchestrator: start brain: %v\n", startErr)
		os.Exit(1)
	}

	done := make(chan int, 1)
	go func() {
		werr := cmd.Wait()
		code := 0
		if ee, ok := werr.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else if werr != nil {
			code = 1
		}
		done <- code
	}()

	// hard-ceiling watchdog: SIGTERM the brain if a breach persists past the grace period.
	var breachAt time.Time
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case code := <-done:
			fmt.Fprintf(os.Stderr, "[orchestrator] brain exited code=%d\n", code)
			os.Exit(code)
		case <-ticker.C:
			if breached, reason := orch.breachOf(runID); breached {
				if breachAt.IsZero() {
					breachAt = time.Now()
					fmt.Fprintf(os.Stderr, "[orchestrator] budget breach (%s) — grace %s before SIGTERM\n", reason, *grace)
				} else if time.Since(breachAt) > *grace {
					fmt.Fprintln(os.Stderr, "[orchestrator] grace elapsed -> SIGTERM brain (hard ceiling)")
					_ = cmd.Process.Signal(syscall.SIGTERM)
				}
			}
		}
	}
}
