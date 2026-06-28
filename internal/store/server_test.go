package store

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func newTest(t *testing.T) *Server {
	s, err := New(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestGoldenRoundTrip(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	if g, _ := s.GetGolden(ctx, &pb.PageKey{PageKey: "index.html"}); g.Found {
		t.Fatal("expected not found")
	}
	if _, err := s.SaveGolden(ctx, &pb.Golden{PageKey: "index.html", A11YHash: "a", ScreenshotHash: "s"}); err != nil {
		t.Fatal(err)
	}
	g, _ := s.GetGolden(ctx, &pb.PageKey{PageKey: "index.html"})
	if !g.Found || g.A11YHash != "a" || g.ScreenshotHash != "s" {
		t.Fatalf("got %+v", g)
	}
}

func TestLocatorRoundTrip(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	k := &pb.LocatorKey{PagePath: "p", SemanticId: "sid", DomSubtreeHash: "h"}
	if r, _ := s.Lookup(ctx, k); r.Found {
		t.Fatal("expected not found")
	}
	if _, err := s.SaveLocator(ctx, &pb.LocatorRecord{
		PagePath: "p", SemanticId: "sid", Strategy: "testid", Value: "{}",
		Confidence: 0.95, DomSubtreeHash: "h", Status: "active"}); err != nil {
		t.Fatal(err)
	}
	r, _ := s.Lookup(ctx, k)
	if !r.Found || r.Strategy != "testid" || r.Confidence != 0.95 {
		t.Fatalf("got %+v", r)
	}
}

func TestQuarantineAndReset(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	for i := 0; i < 3; i++ {
		s.RecordStep(ctx, &pb.StepResult{PlanId: "p", StepKey: "k", Passed: false, AutSha: "A"})
	}
	if q, _ := s.IsQuarantined(ctx, &pb.StepKey{PlanId: "p", StepKey: "k"}); !q.Quarantined {
		t.Fatal("expected quarantined after 3 fails")
	}
	q2, _ := s.RecordStep(ctx, &pb.StepResult{PlanId: "p", StepKey: "k", Passed: false, AutSha: "B"})
	if q2.Quarantined {
		t.Fatal("expected reset on aut-sha change")
	}
}

// TestTokenAuthInterceptor (#23, THREAT_MODEL ❷ / STRIDE-E): the gateway rejects calls without a
// valid per-run token and admits those that present it. This is the unit test the issue asks for.
func TestTokenAuthInterceptor(t *testing.T) {
	const tok = "s3cr3t-token"
	interceptor := TokenAuthInterceptor(tok)
	called := false
	handler := func(_ context.Context, _ any) (any, error) { called = true; return "ok", nil }
	info := &grpc.UnaryServerInfo{FullMethod: "/sentinel.persistence.v1.PersistenceService/SaveGolden"}

	// no metadata -> Unauthenticated, handler never runs.
	if _, err := interceptor(context.Background(), nil, info, handler); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("no-metadata: want Unauthenticated, got %v", err)
	}
	// wrong token -> Unauthenticated.
	ctxBad := metadata.NewIncomingContext(context.Background(), metadata.Pairs(StoreTokenMDKey, "nope"))
	if _, err := interceptor(ctxBad, nil, info, handler); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bad-token: want Unauthenticated, got %v", err)
	}
	if called {
		t.Fatal("handler must not run for rejected calls")
	}
	// valid token -> handler runs.
	ctxOK := metadata.NewIncomingContext(context.Background(), metadata.Pairs(StoreTokenMDKey, tok))
	if _, err := interceptor(ctxOK, nil, info, handler); err != nil {
		t.Fatalf("valid-token: unexpected err %v", err)
	}
	if !called {
		t.Fatal("handler must run for an authenticated call")
	}
}

// TestGoldenIntegrityTamper (#24, THREAT_MODEL ❷ / STRIDE-T): a golden row whose fields are edited
// out-of-band (direct SQL / full DB swap) fails its HMAC at read time, so replay fails closed.
func TestGoldenIntegrityTamper(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	if _, err := s.SaveGolden(ctx, &pb.Golden{PageKey: "p.html", A11YHash: "a", ScreenshotHash: "sh"}); err != nil {
		t.Fatal(err)
	}
	// clean read verifies and returns the golden.
	if g, err := s.GetGolden(ctx, &pb.PageKey{PageKey: "p.html"}); err != nil || !g.Found || g.A11YHash != "a" {
		t.Fatalf("clean read: err=%v g=%+v", err, g)
	}
	// tamper the row directly, NOT through SaveGolden (which would re-MAC it).
	if _, err := s.db.Exec("UPDATE golden_snapshots SET a11y_hash=? WHERE page_key=?", "EVIL", "p.html"); err != nil {
		t.Fatal(err)
	}
	if g, err := s.GetGolden(ctx, &pb.PageKey{PageKey: "p.html"}); status.Code(err) != codes.DataLoss {
		t.Fatalf("tampered read: want DataLoss, got err=%v g=%+v", err, g)
	}
}

// TestGoldenMacStripRejected (#24): the MAC-strip downgrade — forge a row AND drop its mac to dodge
// verification — is rejected. Because the key already exists, a NULL mac is treated as tampered, not
// legacy. (Regression for the adversarial-review finding crypto-24-1.)
func TestGoldenMacStripRejected(t *testing.T) {
	s, ctx := newTest(t), context.Background()
	if _, err := s.SaveGolden(ctx, &pb.Golden{PageKey: "p.html", A11YHash: "a", ScreenshotHash: "sh"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("UPDATE golden_snapshots SET a11y_hash='EVIL', mac=NULL WHERE page_key='p.html'"); err != nil {
		t.Fatal(err)
	}
	if g, err := s.GetGolden(ctx, &pb.PageKey{PageKey: "p.html"}); status.Code(err) != codes.DataLoss {
		t.Fatalf("stripped mac: want DataLoss, got err=%v g=%+v", err, g)
	}
}

// TestGoldenMacBackfillOnUpgrade (#24): opening a pre-#24 DB (legacy NULL-mac row, no golden.key yet)
// MACs the existing rows once (trust-on-first-use), so a legitimate baseline keeps verifying — while
// a later strip is still rejected (covered by TestGoldenMacStripRejected).
func TestGoldenMacBackfillOnUpgrade(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "t.db")
	// Simulate a pre-#24 DB directly (no golden.key, legacy row with no mac).
	raw, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("CREATE TABLE golden_snapshots (page_key TEXT PRIMARY KEY, a11y_hash TEXT, screenshot_hash TEXT, created_at REAL)"); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("INSERT INTO golden_snapshots(page_key,a11y_hash,screenshot_hash,created_at) VALUES('p.html','a','sh',0.0)"); err != nil {
		t.Fatal(err)
	}
	_ = raw.Close()

	s, err := New(dbPath) // first open: mints the key + backfills the legacy row
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	g, err := s.GetGolden(context.Background(), &pb.PageKey{PageKey: "p.html"})
	if err != nil || !g.Found || g.A11YHash != "a" {
		t.Fatalf("backfilled legacy row should verify: err=%v g=%+v", err, g)
	}
}
