package main

// agentctl purge-store (ADR-100) — the operator-invoked cleanup of foreign text already stored in
// SQLite. Sibling of `agentctl redact-trace` (ADR-098): same class of operation, same posture — the
// human asks for it by name, the tool reports counts and never content.
//
// It is a subcommand and NOT a step inside `run` for the reason the whole arc turns on: write-time
// redaction cannot reach what was written before it existed, and the reach-back has to be a decision
// somebody takes, not a side effect of running a test. It is likewise absent from the start-of-run
// sweep block, where sweepTraces/sweepLogs/sweepRuns live.

import (
	"context"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func cmdPurgeStore(repo string, args []string) int {
	fs := flag.NewFlagSet("purge-store", flag.ExitOnError)
	tables := fs.String("tables", "", "comma-separated tables to purge (required; there is no default)")
	olderThan := fs.Duration("older-than", 0, "only rows older than this (e.g. 720h); omit to purge every row")
	vacuum := fs.Bool("vacuum", false,
		"rewrite the database so the freed bytes are really gone — this also destroys recoverability")
	yes := fs.Bool("yes", false, "confirm: this deletes rows and cannot be undone")
	_ = fs.Parse(args)

	// An empty scope is refused rather than read as "everything". The gateway refuses it too; failing
	// here as well means a forgotten flag costs an error message instead of a round trip.
	names := splitList(*tables)
	if len(names) == 0 {
		fmt.Fprintln(os.Stderr, "error: --tables is required — an empty scope is refused, not treated as \"all tables\"")
		fmt.Fprintln(os.Stderr, "       purgeable: "+strings.Join(purgeableTableNames(), ", "))
		return 2
	}
	if !*yes {
		fmt.Fprintf(os.Stderr, "refusing to purge %s without --yes: this deletes rows and cannot be undone\n",
			strings.Join(names, ","))
		return 2
	}

	var cutoff float64
	if *olderThan > 0 {
		cutoff = float64(time.Now().Add(-*olderThan).Unix())
	}

	cl, closeFn, err := dialStore(repo)
	if err != nil {
		fmt.Fprintf(os.Stderr, "purge-store: %v\n", err)
		return 1
	}
	defer closeFn()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute) // VACUUM rewrites the file
	defer cancel()
	rep, err := cl.PurgeStore(ctx, &storepb.PurgeReq{
		Tables: names, OlderThanEpoch: cutoff, Vacuum: *vacuum,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "purge-store: %v\n", err)
		return 1
	}

	// Counts, never content — a tool that printed what it deleted would be a second copy of the leak
	// (the rule `redact-trace` already follows).
	var total int64
	for _, c := range rep.Counts {
		fmt.Printf("purged %-16s %d rows\n", c.Table, c.Rows)
		total += c.Rows
	}
	fmt.Printf("total: %d rows\n", total)

	// The consequence of the chosen policy is stated every time, because the two policies differ in
	// exactly the way a caller cannot see from the row counts.
	switch {
	case rep.VacuumSkipped != "":
		fmt.Printf("VACUUM: SKIPPED — %s\n", rep.VacuumSkipped)
		fmt.Println("        rows are deleted, but their bytes remain in the file's freed pages.")
	case rep.Vacuumed:
		fmt.Println("VACUUM: done — the freed bytes are gone, and so is any chance of recovering them.")
	default:
		fmt.Println("VACUUM: not requested — rows are gone from queries, but THEIR BYTES REMAIN in the")
		fmt.Println("        file's freed pages. Re-run with --vacuum to scrub them, if your retention")
		fmt.Println("        policy permits destroying recoverability.")
	}
	for _, c := range rep.CapabilitiesLost {
		fmt.Println("lost:  " + c)
	}
	return 0
}

// dialStore reaches the gateway that owns the database. STORE_ADDR wins when set — that is the
// long-lived gateway of a service/compose deployment, and the only way to reach control-store.db
// inside its container. Otherwise this is a standalone checkout, so we start a gateway over
// state/locators.db exactly as a run would, purge, and stop it again.
func dialStore(repo string) (storepb.StoreServiceClient, func(), error) {
	addr, token := os.Getenv("STORE_ADDR"), os.Getenv("STORE_TOKEN")
	stopGateway := func() {}
	if addr == "" {
		token = newToken()
		var stop func()
		addr, stop = startGateway(repo, "purge", token)
		if addr == "" {
			return nil, func() {}, fmt.Errorf(
				"no store-gateway: set STORE_ADDR to reach a running one, or build bin/store-gateway")
		}
		stopGateway = stop
	}
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithChainUnaryInterceptor(purgeTokenInterceptor(token)))
	if err != nil {
		stopGateway()
		return nil, func() {}, err
	}
	return storepb.NewStoreServiceClient(conn), func() {
		_ = conn.Close()
		stopGateway()
	}, nil
}

// purgeTokenInterceptor mirrors the control-API's storeTokenInterceptor: the gateway authenticates
// callers with a shared per-run secret (#23), and a no-auth gateway gets an empty token.
func purgeTokenInterceptor(token string) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if token != "" {
			ctx = metadata.AppendToOutgoingContext(ctx, "x-sentinel-store-token", token)
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// purgeableTableNames is the CLI's copy of the gateway's answer, used only to make the usage message
// helpful. The gateway remains the authority: it validates the scope again and refuses an unknown
// name, so a drift here costs a worse error message, never a wrong purge.
func purgeableTableNames() []string {
	out := []string{"chats", "golden_snapshots", "healed_locators", "healing_audit",
		"metrics", "results", "runs", "scenarios", "tests"}
	sort.Strings(out)
	return out
}
