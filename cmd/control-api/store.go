// M13 (ADR-049/050): the control-API's client to the persistent store-gateway (StoreService). It
// persists the `runs` domain so runs survive a control-API restart (today the in-memory map is lost).
// FAIL-OPEN: when CONTROL_API_STORE_ADDR is unset, or the gateway is unreachable, the control-API keeps
// working purely in-memory (the standalone/offline path is unchanged). The in-memory map stays the
// authoritative source for LIVE runs (it owns the SSE stream); the gateway persists metadata + serves
// historical runs from prior processes.
// M14 wave W3: also fronts the `scenarios`/`tests`/`chats` domains, which have NO in-memory fallback
// (unlike runs) — a gateway error there degrades to an empty list / not-found response (main.go handlers).
// M15 (ADR-051): also fronts the `results`/`metrics` domains (persistResult on finish → SaveResult +
// IngestMetrics; read by /v1/results and /v1/trends for the SPA native charts).
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

// --- scenarios / tests / chats (M14 wave W3: HTTP surface + library/conversation management) -----
// Same best-effort, fail-open style as the runs helpers above: a gateway error is logged and
// swallowed; the caller degrades to an empty result (list) or a not-found response (get/promote).

// saveScenario persists a scenario (best-effort; logs + continues on error). Used by the finish-
// goroutine to wire the scenarios domain to a real caller (M14_CONTRACT.md §3).
func (c *storeClient) saveScenario(sc *storepb.Scenario) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.SaveScenario(ctx, sc); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store SaveScenario(%s): %v (scenario not persisted)\n", sc.ScenarioId, err)
	}
}

func (c *storeClient) getScenario(id string) (*storepb.Scenario, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	sc, err := c.cl.GetScenario(ctx, &storepb.ScenarioId{ScenarioId: id})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store GetScenario(%s): %v\n", id, err)
		return nil, false
	}
	if !sc.Found {
		return nil, false
	}
	return sc, true
}

// listScenarios returns (nil,false) on gateway error; target=="" lists all scenarios.
func (c *storeClient) listScenarios(target string) (*storepb.ScenarioList, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	sl, err := c.cl.ListScenarios(ctx, &storepb.ListScenariosReq{Limit: 1000, Target: target})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store ListScenarios: %v (falling back to empty)\n", err)
		return nil, false
	}
	return sl, true
}

// deleteScenario is best-effort; a gateway error is logged, not surfaced (delete stays idempotent
// from the HTTP caller's point of view — see handleDeleteScenario).
func (c *storeClient) deleteScenario(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.DeleteScenario(ctx, &storepb.ScenarioId{ScenarioId: id}); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store DeleteScenario(%s): %v\n", id, err)
	}
}

// promoteTest freezes a scenario into a test. Returns (nil,false) on gateway error; the returned
// record's Found is false when the scenario_id doesn't exist (nothing to promote).
func (c *storeClient) promoteTest(req *storepb.PromoteReq) (*storepb.TestRecord, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	t, err := c.cl.PromoteTest(ctx, req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store PromoteTest(%s): %v\n", req.ScenarioId, err)
		return nil, false
	}
	return t, true
}

func (c *storeClient) getTest(id string) (*storepb.TestRecord, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	t, err := c.cl.GetTest(ctx, &storepb.TestId{TestId: id})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store GetTest(%s): %v\n", id, err)
		return nil, false
	}
	if !t.Found {
		return nil, false
	}
	return t, true
}

func (c *storeClient) listTests() (*storepb.TestList, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	tl, err := c.cl.ListTests(ctx, &storepb.ListTestsReq{Limit: 1000})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store ListTests: %v (falling back to empty)\n", err)
		return nil, false
	}
	return tl, true
}

func (c *storeClient) deleteTest(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.DeleteTest(ctx, &storepb.TestId{TestId: id}); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store DeleteTest(%s): %v\n", id, err)
	}
}

func (c *storeClient) getChat(id string) (*storepb.ChatProjection, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	ch, err := c.cl.GetChat(ctx, &storepb.ConversationId{ConversationId: id})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store GetChat(%s): %v\n", id, err)
		return nil, false
	}
	if !ch.Found {
		return nil, false
	}
	return ch, true
}

func (c *storeClient) listChats() (*storepb.ChatList, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	cl, err := c.cl.ListChats(ctx, &storepb.ListChatsReq{Limit: 1000})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store ListChats: %v (falling back to empty)\n", err)
		return nil, false
	}
	return cl, true
}

func (c *storeClient) deleteChat(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.DeleteChat(ctx, &storepb.ConversationId{ConversationId: id}); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store DeleteChat(%s): %v\n", id, err)
	}
}

// --- results / metrics (M15, ADR-051: metrics-in-UI) -------------------------------------------
// Written by the finish-goroutine (persistResult); read by the /v1/results and /v1/trends handlers.
// Same best-effort, fail-open style: a gateway error is logged and swallowed. The metrics domain is
// the base data substrate a commercial enterprise-BI module reads as a pure consumer (ADR-056 seam).

// saveResult persists a run's result record (best-effort; logs + continues on error).
func (c *storeClient) saveResult(rr *storepb.ResultRecord) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.SaveResult(ctx, rr); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store SaveResult(%s): %v (result not persisted)\n", rr.RunId, err)
	}
}

// getResult fetches one run's result. Returns (nil,false) on miss or gateway error.
func (c *storeClient) getResult(id string) (*storepb.ResultRecord, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	rr, err := c.cl.GetResult(ctx, &storepb.RunId{RunId: id})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store GetResult(%s): %v\n", id, err)
		return nil, false
	}
	if !rr.Found {
		return nil, false
	}
	return rr, true
}

// listResults returns (nil,false) on gateway error; the caller degrades to an empty list.
func (c *storeClient) listResults(limit, offset int64) (*storepb.ResultList, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	rl, err := c.cl.ListResults(ctx, &storepb.ListResultsReq{Limit: limit, Offset: offset})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store ListResults: %v (falling back to empty)\n", err)
		return nil, false
	}
	return rl, true
}

// ingestMetrics writes a batch of metric points (best-effort). An empty/nil batch is a no-op.
func (c *storeClient) ingestMetrics(b *storepb.MetricsBatch) {
	if b == nil || len(b.Points) == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := c.cl.IngestMetrics(ctx, b); err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store IngestMetrics(%d pts): %v (metrics not persisted)\n", len(b.Points), err)
	}
}

// trends returns the last `window` points of a metric (chronological), for the SPA sparklines.
// Returns (nil,false) on gateway error; the caller degrades to an empty series.
func (c *storeClient) trends(metric string, window int64) (*storepb.TrendReply, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	tr, err := c.cl.Trends(ctx, &storepb.TrendReq{Metric: metric, Window: window})
	if err != nil {
		fmt.Fprintf(os.Stderr, "[control-api] store Trends(%s): %v (falling back to empty)\n", metric, err)
		return nil, false
	}
	return tr, true
}
