// M13 (ADR-049/050): the control-API's client to the persistent store-gateway (StoreService). It
// persists the `runs` domain so runs survive a control-API restart (today the in-memory map is lost).
// FAIL-OPEN: when CONTROL_API_STORE_ADDR is unset, or the gateway is unreachable, the control-API keeps
// working purely in-memory (the standalone/offline path is unchanged). The in-memory map stays the
// authoritative source for LIVE runs (it owns the SSE stream); the gateway persists metadata + serves
// historical runs from prior processes.
package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

const storeCallTimeout = 3 * time.Second

// storeClient wraps the StoreService gRPC client. All calls are best-effort: an error is logged and
// swallowed (never fails a run), so a down gateway degrades to in-memory behavior.
type storeClient struct {
	cl   storepb.StoreServiceClient
	conn *grpc.ClientConn
}

// storeTokenInterceptor attaches the per-run shared secret to outgoing metadata (matches the gateway's
// TokenAuthInterceptor, key "x-sentinel-store-token"). No-op when token is empty (gateway --no-auth).
func storeTokenInterceptor(token string) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "x-sentinel-store-token", token)
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
}

// newStoreClient dials the store-gateway at addr (a gRPC target, e.g. "unix:/abs/state/store.sock").
// grpc.NewClient is lazy — a bad addr surfaces on the first RPC, not here — so we do a cheap ListRuns
// probe to fail fast at startup when the gateway isn't actually reachable.
func newStoreClient(addr, token string) (*storeClient, error) {
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithChainUnaryInterceptor(storeTokenInterceptor(token)))
	if err != nil {
		return nil, err
	}
	sc := &storeClient{cl: storepb.NewStoreServiceClient(conn), conn: conn}
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := sc.cl.ListRuns(ctx, &storepb.ListRunsReq{Limit: 1}); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return sc, nil
}

func (c *storeClient) close() {
	if c != nil && c.conn != nil {
		_ = c.conn.Close()
	}
}

func runToRecord(r *run) *storepb.RunRecord {
	return &storepb.RunRecord{
		RunId: r.ID, ConversationId: r.ConversationID, Mode: r.Mode, Target: r.Target, Planner: r.Planner,
		State: r.State, ExitCode: int64(r.ExitCode), ArtifactDir: r.ArtifactDir, Error: r.Error,
		StartedAt: r.StartedAt, FinishedAt: r.FinishedAt,
	}
}

func recordToRun(rec *storepb.RunRecord) *run {
	// A historical run reconstructed from the gateway has no live stream (its process is gone); the
	// SSE/events endpoints stay in-memory-only, but state + artifact_dir (for artifact fetch) survive.
	return &run{
		ID: rec.RunId, ConversationID: rec.ConversationId, Mode: rec.Mode, Target: rec.Target,
		Planner: rec.Planner, State: rec.State, ExitCode: int(rec.ExitCode), ArtifactDir: rec.ArtifactDir,
		Error: rec.Error, StartedAt: rec.StartedAt, FinishedAt: rec.FinishedAt,
	}
}

// upsertRun persists a run's current state (best-effort; logs + continues on error).
func (c *storeClient) upsertRun(r *run) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.UpsertRun(ctx, runToRecord(r)); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store UpsertRun(%s): %v (continuing in-memory)\n", r.ID, err)
	}
}

// getRun fetches a historical run from the gateway. Returns (nil,false) on miss or gateway error.
func (c *storeClient) getRun(id string) (*run, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	rec, err := c.cl.GetRun(ctx, &storepb.RunId{RunId: id})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store GetRun(%s): %v\n", id, err)
		return nil, false
	}
	if !rec.Found {
		return nil, false
	}
	return recordToRun(rec), true
}

// listRuns returns all persisted runs. Returns (nil,false) on gateway error (caller falls back to memory).
func (c *storeClient) listRuns() ([]*run, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	rl, err := c.cl.ListRuns(ctx, &storepb.ListRunsReq{Limit: 1000})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store ListRuns: %v (falling back to in-memory)\n", err)
		return nil, false
	}
	out := make([]*run, 0, len(rl.Runs))
	for _, rec := range rl.Runs {
		out = append(out, recordToRun(rec))
	}
	return out, true
}
