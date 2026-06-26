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
	return &pb.Control{Abort: false}, nil
}

func (o *orchestrator) Abort(_ context.Context, r *pb.AbortRequest) (*pb.AbortReply, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	rs := o.get(r.RunId)
	rs.breached, rs.reason = true, "external abort: "+r.Reason
	return &pb.AbortReply{Ok: true}, nil
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
