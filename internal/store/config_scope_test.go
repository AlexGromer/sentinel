package store

// ADR-109 (Alex's directive): "everything the working person owns belongs to them; only the global
// settings and the tool belong to the master user." For the config domain that means a document is
// identified by (key, OWNER) — and that is a PRIMARY KEY change, the one migration SQLite cannot do
// with an ALTER.
//
// The fixture below writes the OLD table by hand. It has to: a fixture built with the current schema
// would already have the column and the new key, so it would prove that a no-op migration is safe and
// nothing else. That mistake shipped once already in this milestone — the owner index sat in the DDL
// that runs BEFORE the migration, every real pre-identity database refused to open, and the migration
// test missed it for exactly this reason.

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func TestMigratesAPreOwnerConfigTable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control-store.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	// The pre-ADR-109 shape: the key alone is the primary key, and there is no owner column.
	for _, stmt := range []string{
		`CREATE TABLE config (key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT)`,
		`INSERT INTO config(key,value_json,updated_at)
		 VALUES('setup','{"llm":{"backend":"anthropic"}}','2026-01-01T00:00:00Z')`,
	} {
		if _, err := raw.Exec(stmt); err != nil {
			t.Fatalf("seeding the pre-owner config table: %v\n%s", err, stmt)
		}
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	s, err := New(path)
	if err != nil {
		t.Fatalf("opening a database whose config predates ownership failed: %v", err)
	}
	defer s.Close()

	ctx := context.Background()
	// The operator's existing configuration IS the global document — it was written before an account
	// could exist. Losing it here would silently reset a running deployment to no configuration at all.
	got, err := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"})
	if err != nil {
		t.Fatalf("reading the migrated config: %v", err)
	}
	if !got.Found {
		t.Fatal("the operator's configuration did not survive the migration")
	}
	if got.ValueJson != `{"llm":{"backend":"anthropic"}}` {
		t.Errorf("migrated document = %q — the text changed", got.ValueJson)
	}
	if got.UpdatedAt != "2026-01-01T00:00:00Z" {
		t.Errorf("migrated updated_at = %q — the rebuild must carry it across, not restamp it", got.UpdatedAt)
	}

	// And the new key actually works afterwards: a personal document under the SAME key is a second
	// row. Under the old primary key this write would have overwritten the global one — silently, with
	// no error and no way to tell afterwards which layer the surviving text came from.
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{
		Key: "setup", Owner: "alice", ValueJson: `{"run":{"max_steps":10}}`}); err != nil {
		t.Fatalf("writing a personal document onto a migrated store: %v", err)
	}
	global, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"})
	if global.ValueJson != `{"llm":{"backend":"anthropic"}}` {
		t.Fatalf("a personal write overwrote the GLOBAL document: %q", global.ValueJson)
	}
}

// TestPersonalAndGlobalConfigAreSeparateRows is the property the primary-key change exists for, stated
// without reference to migration: two owners and the global layer are three independent documents
// under one key, and touching one never touches another.
func TestPersonalAndGlobalConfigAreSeparateRows(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "control-store.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()

	layers := map[string]string{
		"":      `{"settings":{"log_keep":5}}`,
		"alice": `{"run":{"max_steps":10}}`,
		"bob":   `{"run":{"max_steps":99}}`,
	}
	for owner, doc := range layers {
		if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", Owner: owner, ValueJson: doc}); err != nil {
			t.Fatalf("PutConfig(owner=%q): %v", owner, err)
		}
	}
	for owner, doc := range layers {
		got, err := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: owner})
		if err != nil || !got.Found {
			t.Fatalf("GetConfig(owner=%q): %v found=%v", owner, err, got.GetFound())
		}
		if got.ValueJson != doc {
			t.Errorf("owner %q read %q, wrote %q — the layers are sharing a row", owner, got.ValueJson, doc)
		}
		if got.Owner != owner {
			t.Errorf("GetConfig(owner=%q) came back owned by %q", owner, got.Owner)
		}
	}

	// An UPDATE stays in its own layer.
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{
		Key: "setup", Owner: "alice", ValueJson: `{"run":{"max_steps":11}}`}); err != nil {
		t.Fatal(err)
	}
	if g, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"}); g.ValueJson != layers[""] {
		t.Errorf("updating a personal document changed the global one: %q", g.ValueJson)
	}
	if b, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: "bob"}); b.ValueJson != layers["bob"] {
		t.Errorf("updating alice's document changed bob's: %q", b.ValueJson)
	}

	// So does a DELETE. Removing a personal document must reveal the global one again, not destroy it —
	// "reset my settings to the tool's defaults" is the whole reason a personal layer can be deleted.
	if _, err := s.DeleteConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: "alice"}); err != nil {
		t.Fatal(err)
	}
	if a, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: "alice"}); a.Found {
		t.Error("alice's document survived its own delete")
	}
	if g, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup"}); !g.Found || g.ValueJson != layers[""] {
		t.Errorf("deleting a personal document took the global one with it: found=%v %q", g.Found, g.ValueJson)
	}
	if b, _ := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: "bob"}); !b.Found {
		t.Error("deleting alice's document removed bob's")
	}
}

// TestGetConfigDoesNotFallBackToGlobal pins the deliberate absence of a fallback in the STORE. The
// overlay belongs to the control-API, which knows which sections are the tool's and which are the
// person's; a silent fallback here would make "this account has no personal document" and "it has one
// that happens to match" the same answer, and the layer a value came from is exactly what the UI has
// to show.
func TestGetConfigDoesNotFallBackToGlobal(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "control-store.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	if _, err := s.PutConfig(ctx, &pb.ConfigRecord{Key: "setup", ValueJson: `{"settings":{"log_keep":5}}`}); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetConfig(ctx, &pb.ConfigKey{Key: "setup", Owner: "carol"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Found {
		t.Errorf("an account with no personal document was handed the global one: %q", got.ValueJson)
	}
}
