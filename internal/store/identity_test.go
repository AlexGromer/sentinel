package store

// ADR-109 data-layer gates. The acceptance criterion is "two accounts see different sets", and it is
// asserted here at the layer that decides it — the SQL — rather than only through the API above it.

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/AlexGromer/sentinel/internal/store/pb"
)

func idStore(t *testing.T) *Server {
	t.Helper()
	s, err := New(filepath.Join(t.TempDir(), "control-store.db"))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

// TestTwoOwnersSeeDifferentSets is Alex's criterion, stated as a test.
func TestTwoOwnersSeeDifferentSets(t *testing.T) {
	s, ctx := idStore(t), context.Background()
	for _, r := range []*pb.RunRecord{
		{RunId: "a1", Owner: "alice", State: "done", StartedAt: "2026-07-30T00:00:01Z"},
		{RunId: "a2", Owner: "alice", State: "done", StartedAt: "2026-07-30T00:00:02Z"},
		{RunId: "b1", Owner: "bob", State: "done", StartedAt: "2026-07-30T00:00:03Z"},
		{RunId: "x1", Owner: "", State: "done", StartedAt: "2026-07-30T00:00:04Z"}, // pre-identity row
	} {
		if _, err := s.UpsertRun(ctx, r); err != nil {
			t.Fatalf("UpsertRun %s: %v", r.RunId, err)
		}
	}

	got := func(owner string) []string {
		l, err := s.ListRuns(ctx, &pb.ListRunsReq{Owner: owner})
		if err != nil {
			t.Fatalf("ListRuns(%q): %v", owner, err)
		}
		out := make([]string, 0, len(l.Runs))
		for _, r := range l.Runs {
			out = append(out, r.RunId)
		}
		// Total has to agree with the rows, or a paginated UI shows "3 runs" above a list of one.
		if int(l.Total) != len(l.Runs) {
			t.Errorf("ListRuns(%q): total=%d but returned %d rows", owner, l.Total, len(l.Runs))
		}
		return out
	}

	alice, bob, all := got("alice"), got("bob"), got("")
	if len(alice) != 2 || len(bob) != 1 {
		t.Fatalf("scoping failed: alice=%v bob=%v", alice, bob)
	}
	for _, id := range alice {
		if id == "b1" {
			t.Error("alice can see bob's run")
		}
	}
	for _, id := range bob {
		if id == "a1" || id == "a2" {
			t.Error("bob can see alice's run")
		}
	}
	// An empty owner is the MACHINE token, and it sees everything including the unowned row.
	if len(all) != 4 {
		t.Errorf("an unscoped list returned %d rows, want all 4: %v", len(all), all)
	}
	// The pre-identity row belongs to nobody — NOT to whoever asks first.
	for _, id := range append(append([]string{}, alice...), bob...) {
		if id == "x1" {
			t.Error("an unowned row was handed to an account — a row written before identity existed " +
				"must not be adopted by the first person to log in")
		}
	}
}

// TestOwnerRoundTripsThroughEveryDomain: the column is not only filterable but READ BACK. A domain that
// stores the owner and returns it empty would scope correctly and still show a person a row with no
// visible owner, which is how "whose is this?" becomes unanswerable.
func TestOwnerRoundTripsThroughEveryDomain(t *testing.T) {
	s, ctx := idStore(t), context.Background()
	if _, err := s.UpsertRun(ctx, &pb.RunRecord{RunId: "r", Owner: "alice"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SaveScenario(ctx, &pb.Scenario{ScenarioId: "s", Owner: "alice", PlanHash: "h"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PromoteTest(ctx, &pb.PromoteReq{ScenarioId: "s", Name: "n", Owner: "alice"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.UpsertChat(ctx, &pb.ChatProjection{ConversationId: "c", Owner: "alice"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SaveResult(ctx, &pb.ResultRecord{RunId: "r", Owner: "alice", Verdict: "pass"}); err != nil {
		t.Fatal(err)
	}

	run, _ := s.GetRun(ctx, &pb.RunId{RunId: "r"})
	sc, _ := s.GetScenario(ctx, &pb.ScenarioId{ScenarioId: "s"})
	tl, _ := s.ListTests(ctx, &pb.ListTestsReq{})
	if len(tl.Tests) != 1 {
		t.Fatalf("expected one promoted test, got %d", len(tl.Tests))
	}
	te := tl.Tests[0]
	ch, _ := s.GetChat(ctx, &pb.ConversationId{ConversationId: "c"})
	re, _ := s.GetResult(ctx, &pb.RunId{RunId: "r"})
	for name, owner := range map[string]string{
		"runs": run.Owner, "scenarios": sc.Owner, "tests": te.Owner, "chats": ch.Owner, "results": re.Owner,
	} {
		if owner != "alice" {
			t.Errorf("%s: owner round-tripped as %q, want \"alice\"", name, owner)
		}
	}
}

func TestUsersCRUD(t *testing.T) {
	s, ctx := idStore(t), context.Background()
	if _, err := s.UpsertUser(ctx, &pb.User{UserId: "u1", Name: "alice", PwHash: "pbkdf2-sha256$1$x$y", IsAdmin: true}); err != nil {
		t.Fatalf("UpsertUser: %v", err)
	}
	byName, err := s.GetUser(ctx, &pb.UserRef{Name: "alice"})
	if err != nil || !byName.Found {
		t.Fatalf("GetUser(name): %v found=%v", err, byName.Found)
	}
	if byName.UserId != "u1" || !byName.IsAdmin || byName.PwHash == "" {
		t.Errorf("GetUser(name) returned %+v", byName)
	}
	byID, _ := s.GetUser(ctx, &pb.UserRef{UserId: "u1"})
	if !byID.Found || byID.Name != "alice" {
		t.Errorf("GetUser(id) returned %+v", byID)
	}

	// A list must not carry credentials.
	list, err := s.ListUsers(ctx, &pb.Empty{})
	if err != nil || list.Total != 1 {
		t.Fatalf("ListUsers: %v total=%d", err, list.Total)
	}
	if list.Users[0].PwHash != "" {
		t.Error("ListUsers returned a password hash — a credential that never enters a reply cannot be " +
			"logged or rendered by a caller that meant no harm")
	}

	// Missing account: found=false, not an error. A wrong username is a normal login outcome.
	missing, err := s.GetUser(ctx, &pb.UserRef{Name: "nobody"})
	if err != nil {
		t.Errorf("GetUser on a missing name errored instead of answering: %v", err)
	} else if missing.Found {
		t.Error("a missing account reported found=true")
	}

	// An empty ref must match nothing rather than the first row.
	if _, err := s.GetUser(ctx, &pb.UserRef{}); err == nil {
		t.Error("GetUser accepted a reference carrying neither id nor name")
	}

	// No credential -> refused, so a name cannot be occupied by an account nobody can log into.
	if _, err := s.UpsertUser(ctx, &pb.User{UserId: "u2", Name: "bob"}); err == nil {
		t.Error("UpsertUser stored a user with no credential")
	}

	// Deletion leaves the rows the account owned, unowned rather than destroyed.
	if _, err := s.UpsertRun(ctx, &pb.RunRecord{RunId: "kept", Owner: "u1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DeleteUser(ctx, &pb.UserRef{UserId: "u1"}); err != nil {
		t.Fatalf("DeleteUser: %v", err)
	}
	if g, _ := s.GetUser(ctx, &pb.UserRef{UserId: "u1"}); g.Found {
		t.Error("the account survived deletion")
	}
	if r, _ := s.GetRun(ctx, &pb.RunId{RunId: "kept"}); !r.Found {
		t.Error("deleting an account destroyed a run it owned — history someone else may rely on")
	}
}

// TestOpensAGenuinelyPreIdentityDatabase: an OLD database — one whose tables were created before the
// owner column existed — must open.
//
// This exists because the migration test below did NOT catch a defect that broke exactly this. It
// built its "old" database with the CURRENT schema, closed it and reopened it, so the column was there
// the whole time and the test proved only that a no-op ALTER is safe. Meanwhile the owner INDEX sat in
// storeSchema, which runs BEFORE the migration, and every real pre-identity database failed to open
// with "no such column: owner". A fixture that shares the code's assumptions cannot test them.
//
// So the tables here are written by hand, in their pre-ADR-109 shape.
func TestOpensAGenuinelyPreIdentityDatabase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control-store.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	old := []string{
		`CREATE TABLE runs (run_id TEXT PRIMARY KEY, conversation_id TEXT, mode TEXT, target TEXT,
		  planner TEXT, state TEXT, exit_code INTEGER, artifact_dir TEXT, error TEXT, started_at TEXT,
		  finished_at TEXT)`,
		`CREATE TABLE scenarios (scenario_id TEXT PRIMARY KEY, name TEXT, target TEXT, run_mode TEXT,
		  plan_hash TEXT, steps_json TEXT, unmatched INTEGER, tags TEXT, source_run_id TEXT, created_at TEXT)`,
		`CREATE TABLE tests (test_id TEXT PRIMARY KEY, scenario_id TEXT, plan_hash TEXT, name TEXT,
		  schedule TEXT, enabled INTEGER, last_status TEXT, last_run_id TEXT, created_at TEXT)`,
		`CREATE TABLE chats (conversation_id TEXT PRIMARY KEY, last_target TEXT, turn_count INTEGER,
		  last_active TEXT, last_goal TEXT, summary TEXT, updated_at TEXT)`,
		`CREATE TABLE results (run_id TEXT PRIMARY KEY, plan_id TEXT, mode TEXT, verdict TEXT,
		  exit_code INTEGER, healed INTEGER, failed INTEGER, regressions_json TEXT, steps_json TEXT,
		  coverage REAL, duration_ms INTEGER, created_at TEXT)`,
		`CREATE TABLE metrics (run_id TEXT, ts REAL, name TEXT, value REAL, labels_json TEXT)`,
		// Every column supplied, because the OLD binary's INSERT supplied every column too — a row with
		// NULLs in it would be a fixture nothing ever wrote, and a test of a situation that cannot arise.
		// The one column that IS genuinely NULL on an upgraded row is `owner`, added by the ALTER above,
		// and the read path COALESCEs exactly that.
		`INSERT INTO runs(run_id,conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,
		  started_at,finished_at)
		 VALUES('ancient','','explore','http://old','heuristic','done',0,'/runs/ancient','',
		  '2026-01-01T00:00:00Z','2026-01-01T00:01:00Z')`,
	}
	for _, stmt := range old {
		if _, err := raw.Exec(stmt); err != nil {
			t.Fatalf("seeding the pre-identity schema: %v\n%s", err, stmt)
		}
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	s, err := New(path)
	if err != nil {
		t.Fatalf("opening a pre-identity database failed: %v", err)
	}
	defer s.Close()

	r, err := s.GetRun(context.Background(), &pb.RunId{RunId: "ancient"})
	if err != nil {
		t.Fatalf("reading a migrated row failed: %v", err)
	}
	if !r.Found {
		t.Fatal("the pre-existing row did not survive the migration")
	}
	if r.Owner != "" {
		t.Errorf("a migrated row came back owned by %q — it must be unowned", r.Owner)
	}
	// And it is USABLE afterwards, not merely openable: the migration has to leave a store that writes.
	if _, err := s.UpsertRun(context.Background(), &pb.RunRecord{RunId: "fresh", Owner: "alice"}); err != nil {
		t.Fatalf("writing to a migrated store failed: %v", err)
	}
	l, err := s.ListRuns(context.Background(), &pb.ListRunsReq{Owner: "alice"})
	if err != nil || len(l.Runs) != 1 || l.Runs[0].RunId != "fresh" {
		t.Fatalf("scoping does not work on a migrated store: %v %+v", err, l)
	}
}

// TestOwnerColumnMigratesOntoAnOlderDB: opening a store twice must be safe, which is what an upgrade
// over a pre-identity DB looks like. ensureColumn is idempotent or the second open fails.
func TestOwnerColumnMigratesOntoAnOlderDB(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control-store.db")
	first, err := New(path)
	if err != nil {
		t.Fatalf("first open: %v", err)
	}
	if _, err := first.UpsertRun(context.Background(), &pb.RunRecord{RunId: "old"}); err != nil {
		t.Fatal(err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	second, err := New(path)
	if err != nil {
		t.Fatalf("re-opening an existing DB failed — the owner migration is not idempotent: %v", err)
	}
	defer second.Close()
	r, err := second.GetRun(context.Background(), &pb.RunId{RunId: "old"})
	if err != nil || !r.Found {
		t.Fatalf("the pre-existing row did not survive the migration: %v found=%v", err, r.Found)
	}
	if r.Owner != "" {
		t.Errorf("a migrated row came back owned by %q — it must be unowned", r.Owner)
	}
}
