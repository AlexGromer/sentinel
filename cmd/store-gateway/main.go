// Command store-gateway is the Sentinel persistence service (M2b-1, ADR-015): the sole SQLite
// writer, exposed over gRPC on a Unix-domain socket. agentctl spawns it for store-backed modes.
package main

import (
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"google.golang.org/grpc"

	"github.com/AlexGromer/sentinel/internal/store"
	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func main() {
	addr := flag.String("addr", "", "unix socket path to listen on (required)")
	dbPath := flag.String("db", "state/locators.db", "sqlite database path")
	flag.Parse()
	if *addr == "" {
		fmt.Fprintln(os.Stderr, "store-gateway: --addr <unix-socket> is required")
		os.Exit(2)
	}

	srv, err := store.New(*dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "store-gateway: open db: %v\n", err)
		os.Exit(1)
	}
	defer srv.Close()

	_ = os.Remove(*addr) // clear a stale socket
	lis, err := net.Listen("unix", *addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "store-gateway: listen %s: %v\n", *addr, err)
		os.Exit(1)
	}

	g := grpc.NewServer()
	pb.RegisterPersistenceServiceServer(g, srv)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		g.GracefulStop()
	}()

	fmt.Fprintf(os.Stderr, "[store-gateway] listening on unix:%s db=%s\n", *addr, *dbPath)
	if err := g.Serve(lis); err != nil {
		fmt.Fprintf(os.Stderr, "store-gateway: serve: %v\n", err)
		os.Exit(1)
	}
}
