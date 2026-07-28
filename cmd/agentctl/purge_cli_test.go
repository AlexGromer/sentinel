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

// TestPurgeableTableNamesMatchTheGateway keeps the CLI's usage hint honest. The gateway stays the
// authority — it validates the scope again — so a drift here costs a misleading error message rather
// than a wrong purge, which is exactly why it would otherwise go unnoticed.
func TestPurgeableTableNamesMatchTheGateway(t *testing.T) {
	// Deliberately spelled out rather than imported from internal/store: importing the map would make
	// this test agree with the code by construction and assert nothing at all.
	want := []string{"chats", "golden_snapshots", "healed_locators", "healing_audit",
		"metrics", "results", "runs", "scenarios", "tests"}
	got := purgeableTableNames()
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("purgeableTableNames() = %v, want %v", got, want)
	}
	// config must NOT be offered: it is live configuration, and the gateway refuses it.
	for _, n := range got {
		if n == "config" {
			t.Fatal("the CLI offers `config`, which the gateway refuses to purge")
		}
	}
}
