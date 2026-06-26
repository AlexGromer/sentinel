// Command store-gateway is the Sentinel persistence service (M2b-1, ADR-015): the sole SQLite
// writer, exposed over gRPC on a Unix-domain socket. agentctl spawns it for store-backed modes.
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"google.golang.org/grpc"

	"github.com/AlexGromer/sentinel/internal/store"
	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// setupTracing configures an OTLP tracer + W3C propagator iff OTEL_EXPORTER_OTLP_ENDPOINT is set
// (M8, ADR-021); otherwise it is a no-op (zero overhead). Returns a shutdown func.
func setupTracing(ctx context.Context) func() {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return func() {}
	}
	exp, err := otlptracegrpc.New(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[store-gateway] otel exporter: %v (tracing off)\n", err)
		return func() {}
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp),
		sdktrace.WithResource(resource.NewSchemaless(attribute.String("service.name", "sentinel-store-gateway"))),
	)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	return func() { _ = tp.Shutdown(context.Background()) }
}

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

	shutdown := setupTracing(context.Background())
	defer shutdown()

	// otelgrpc StatsHandler: server spans + W3C extraction from incoming metadata (no-op if tracing off).
	g := grpc.NewServer(grpc.StatsHandler(otelgrpc.NewServerHandler()))
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
