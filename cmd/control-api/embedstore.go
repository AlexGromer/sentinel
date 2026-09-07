// The store-gateway, hosted IN PROCESS when no external one is configured (ADR-158).
//
// DIRECTIVE THIS EXISTS TO SATISFY (Alex, 2026-09-06): «функциональность standalone и
// многопользовательская не должна отличаться, у нас одна версия» — the standalone tier and the
// multi-user tier must not differ in what the product can DO. There is one version.
//
// WHAT THE DIFFERENCE WAS. Until this file, `CONTROL_API_STORE_ADDR` being unset left `s.store` nil,
// and nil is not a smaller deployment — it is a different product. Local accounts were impossible
// (`handleCreateUser` answers 503 «local accounts need a store-gateway»), and with accounts
// impossible so were sign-in, per-account scoping, and everything downstream of an owner. A first
// run on that tier could not create an administrator at all: the setup grant handed out by ADR-156
// could never be spent.
//
// WHY NOT A SECOND IMPLEMENTATION. The obvious fix is a file-backed account store beside
// `provider-keys.json` (ADR-146). It was rejected, and the reason is the directive itself: two
// implementations of one feature DRIFT. The moment accounts live in a file here and in SQLite there,
// every later change to identity has two homes, two sets of edge cases and one set of tests — which
// is exactly the divergence the directive orders removed, reintroduced one layer down.
//
// WHAT THIS DOES INSTEAD. `internal/store.Server` is the SAME SQLite implementation the gateway
// process runs. Here it is hosted on an in-memory listener and reached through the SAME generated
// gRPC client, so `storeClient` and every one of its 28 methods are untouched. There is one storage
// implementation, one client, one code path and one set of tests; the tiers differ only in whether
// the listener has a socket on it. That is a deployment difference, not a product difference.
//
// WHY bufconn AND NOT A UNIX SOCKET. A socket under `state/` would be reachable by any process of
// the same UID, which is the precise property ADR-146 cites as the reason a credential must not sit
// behind the gateway. An embedded store has no reason to be reachable from outside the process that
// owns it, so it is not: `bufconn` has no filesystem presence and no address to connect to. It also
// cannot collide with the compose deployment's own `state/store.sock`.
//
// COST OF THE IMPORT, MEASURED. `modernc.org/sqlite` is pure Go (go.mod:12) — no cgo — so pulling
// the store into this binary keeps the six-platform cross-build working. Measured, not assumed.
package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/AlexGromer/sentinel/internal/store"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// embeddedStoreBuffer is the in-memory listener's buffer. It bounds a single in-flight message, not
// the database: gRPC frames are small and the SQLite writer is serialized behind its own mutex
// anyway (internal/store: single-writer, ADR-007).
const embeddedStoreBuffer = 1 << 20

// embeddedStoreDBName sits beside the other databases under state/, so an embedded deployment's
// accounts survive a restart exactly as a gateway-backed one's do. The name differs from the
// gateway's own `control-store.db` on purpose: the two are never the same deployment, and a shared
// name would invite someone to point a gateway at a file an embedded server still holds open.
const embeddedStoreDBName = "embedded-store.db"

// embeddedStore is a running in-process store-gateway. Its only exported surface is a dialer, so a
// caller cannot reach the gRPC server except through the same client an external gateway is reached
// through.
type embeddedStore struct {
	lis  *bufconn.Listener
	grpc *grpc.Server
	path string
}

// startEmbeddedStore brings up internal/store on an in-memory listener.
//
// A failure here is returned rather than fatal: the caller keeps the historical behaviour of a
// deployment that cannot reach storage (in-memory runs, features that need a store reporting why),
// which is strictly better than refusing to start. What must NOT happen is failing silently — the
// caller journals the reason.
func startEmbeddedStore(repo string) (*embeddedStore, error) {
	dir := filepath.Join(repo, "state")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("state dir: %w", err)
	}
	path := filepath.Join(dir, embeddedStoreDBName)
	srv, err := store.New(path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	g := grpc.NewServer()
	storepb.RegisterPersistenceServiceServer(g, srv)
	storepb.RegisterStoreServiceServer(g, srv) // the same pair the gateway process registers
	lis := bufconn.Listen(embeddedStoreBuffer)
	es := &embeddedStore{lis: lis, grpc: g, path: path}
	// Serve returns when the listener closes, which stop() does. A serve error after that point is
	// the shutdown itself and is not worth reporting; before it, there is nothing a caller could do
	// that the client's own failures will not already say more precisely.
	go func() { _ = g.Serve(lis) }()
	return es, nil
}

// dialer hands grpc.NewClient a way to reach the in-memory listener. The address argument is ignored
// by construction: there is exactly one embedded server per process and it has no address.
func (e *embeddedStore) dialer() func(context.Context, string) (net.Conn, error) {
	return func(ctx context.Context, _ string) (net.Conn, error) { return e.lis.DialContext(ctx) }
}

func (e *embeddedStore) stop() {
	if e == nil {
		return
	}
	e.grpc.Stop()
	_ = e.lis.Close()
}

// newEmbeddedStoreClient wires the ordinary storeClient to the in-process server. Note what is NOT
// here: no token interceptor. A token authenticates a caller ACROSS a socket other processes can
// reach; this listener has no socket and no other caller, so a shared secret would protect nothing
// and would only add a way to misconfigure the one path that cannot be misconfigured today.
func newEmbeddedStoreClient(e *embeddedStore) (*storeClient, error) {
	conn, err := grpc.NewClient("passthrough:///embedded-store",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(e.dialer()))
	if err != nil {
		return nil, err
	}
	sc := &storeClient{cl: storepb.NewStoreServiceClient(conn), conn: conn}
	ctx, cancel := context.WithTimeout(context.Background(), storeCallTimeout)
	defer cancel()
	if _, err := sc.cl.ListRuns(ctx, &storepb.ListRunsReq{Limit: 1}); err != nil {
		return sc, err
	}
	return sc, nil
}
