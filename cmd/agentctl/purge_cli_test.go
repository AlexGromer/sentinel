package main

// The CLI layer of `agentctl purge-store`, covered separately from the RPC it calls.
//
// Written because the mutation pass found the gap: internal/store/purge_test.go proves the SERVER
// refuses an empty scope, and nothing proved the command does. A guard that exists only on the far
// side of a network call is a guard the operator meets after the round trip, and — more to the point
// — the --yes confirmation has no server-side counterpart at all, so if it is not tested here it is
// not tested anywhere.
//
// These cases all return before dialling, which is what makes them testable without a gateway. That
// is not an accident of the test: refusing before opening a connection is the behaviour worth having.

import (
	"strings"
	"testing"
)

// TestPurgeStoreCLIRefusesBeforeItConnects.
// Kills: --tables defaulting to every table when omitted.
// Kills: dropping the --yes confirmation (nothing else covers it — the RPC has no such concept).
// Kills: treating a comma-only or whitespace-only --tables value as a non-empty scope.
func TestPurgeStoreCLIRefusesBeforeItConnects(t *testing.T) {
	// A repo path that cannot hold a gateway: if any of these cases reached the dial, it would fail
	// with a different code (1) than the refusal we are asserting (2), so the assertion below cannot
	// be satisfied by accidentally connecting to something.
	repo := t.TempDir()

	for _, tc := range []struct {
		name string
		args []string
	}{
		{"no flags at all", nil},
		{"empty scope", []string{"--tables", "", "--yes"}},
		{"comma-only scope", []string{"--tables", ",,", "--yes"}},
		{"whitespace-only scope", []string{"--tables", "   ", "--yes"}},
		{"scope but no confirmation", []string{"--tables", "healing_audit"}},
		// An unknown name is a usage mistake and earns the usage code without a round trip. Found by
		// running the real command: the gateway's refusal arrives as a generic error and exited 1,
		// so a typo read as an infrastructure failure.
		{"unknown table", []string{"--tables", "healing_audit,nope", "--yes"}},
		{"config is never offered", []string{"--tables", "config", "--yes"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if code := cmdPurgeStore(repo, tc.args); code != 2 {
				t.Fatalf("exit code = %d, want 2 (refusal) — a purge must not proceed on this input", code)
			}
		})
	}
}

// TestSplitListDropsEmptiesAndTrims pins the parsing that the refusal above depends on: without it,
// "--tables ,," would arrive at the gateway as three empty names rather than an empty scope.
func TestSplitListDropsEmptiesAndTrims(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want []string
	}{
		{"", nil},
		{",,", nil},
		{"  ", nil},
		{"runs", []string{"runs"}},
		{" runs , healing_audit ", []string{"runs", "healing_audit"}},
		{"runs,,healing_audit,", []string{"runs", "healing_audit"}},
	} {
		got := splitList(tc.in)
		if strings.Join(got, "|") != strings.Join(tc.want, "|") {
			t.Errorf("splitList(%q) = %v, want %v", tc.in, got, tc.want)
		}
	}
}

// TestPurgeableTableNamesReflectsTheGateway. The CLI now reads the gateway's published set instead
// of copying it, so this does not re-check the list member by member — that would only assert that
// store.PurgeableTables() equals itself. What is worth pinning is the two properties the CLI relies
// on when it validates a scope locally.
//
// Kills: reintroducing a hard-coded list in the CLI (it would go stale against the registry and the
// non-empty check below is the only thing that would still hold).
// Kills: adding `config` to the purgeable registry — the CLI would then offer a table whose whole
// point is that it must not be emptied.
func TestPurgeableTableNamesReflectsTheGateway(t *testing.T) {
	got := purgeableTableNames()
	if len(got) == 0 {
		t.Fatal("no purgeable tables — the local scope check would reject everything")
	}
	for _, n := range got {
		if n == "config" {
			t.Fatal("`config` is offered as purgeable: it is live configuration, not accumulated history")
		}
	}
	// The inventory gate (internal/store) is what pins the membership itself; here it is enough that
	// the names the CLI validates against are the ones the gateway will accept.
	if !strings.Contains(strings.Join(got, ","), "healing_audit") {
		t.Fatalf("purgeableTableNames() = %v, missing healing_audit", got)
	}
}

// TestStoreTargetPrefixesABareSocketPath is a regression test for a bug that shipped past every
// other gate in this change: startGateway returns a BARE socket path, grpc.NewClient needs a target,
// and passing the path through unchanged fails with "produced zero addresses". Nothing caught it
// because every other test stops before the dial — it took running the real command against a real
// gateway. Hence a pure function, so the conversion is reachable by a test at all.
func TestStoreTargetPrefixesABareSocketPath(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		// What startGateway returns and what STORE_ADDR carries by convention (brain/store.py:282
		// prefixes the same way).
		{"/abs/state/sentinel-store-purge.sock", "unix:/abs/state/sentinel-store-purge.sock"},
		// What a compose deployment hands out — already a target, must not be double-prefixed.
		{"unix:/app/state/store.sock", "unix:/app/state/store.sock"},
		{"dns:///gateway:50051", "dns:///gateway:50051"},
		{"passthrough:///127.0.0.1:50051", "passthrough:///127.0.0.1:50051"},
		// host:port is a valid target on its own.
		{"127.0.0.1:50051", "127.0.0.1:50051"},
	} {
		if got := storeTarget(tc.in); got != tc.want {
			t.Errorf("storeTarget(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
