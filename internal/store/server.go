// Package store implements the Sentinel PersistenceService (M2b-1, ADR-015): the sole SQLite
// writer, exposed over gRPC. It replaces brain/store.py's local SQLite (restoring ADR-007).
// SQL mirrors brain/store.py 1:1 so behavior is identical. Pure-Go driver (modernc.org/sqlite).
package store

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	_ "modernc.org/sqlite"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

const schema = `
CREATE TABLE IF NOT EXISTS healed_locators (
  page_path TEXT, semantic_id TEXT, strategy TEXT, value TEXT, confidence REAL,
  dom_subtree_hash TEXT, status TEXT, times_used INTEGER DEFAULT 0, created_at REAL,
  PRIMARY KEY (page_path, semantic_id, dom_subtree_hash)
);
CREATE TABLE IF NOT EXISTS healing_audit (
  run_id TEXT, step INTEGER, semantic_id TEXT, page_path TEXT, strategy TEXT,
  original TEXT, healed TEXT, confidence REAL, outcome TEXT, dom_hash TEXT, ts REAL,
  identity TEXT
);
CREATE TABLE IF NOT EXISTS golden_snapshots (
  page_key TEXT PRIMARY KEY, a11y_hash TEXT, screenshot_hash TEXT, created_at REAL, mac TEXT
);
CREATE TABLE IF NOT EXISTS step_failures (
  plan_id TEXT, step_key TEXT, last5 TEXT, last_aut_sha TEXT, quarantined INTEGER DEFAULT 0,
  PRIMARY KEY (plan_id, step_key)
);`

// storeSchema (M13, ADR-050): the 5 StoreService domains. Portable SQL only (no INSERT OR REPLACE /
// SQLite pragmas) so a Postgres backend (M13-service) drops in behind STORE_DSN with the same DDL.
const storeSchema = `
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, conversation_id TEXT, mode TEXT, target TEXT, planner TEXT,
  state TEXT, exit_code INTEGER, artifact_dir TEXT, error TEXT, started_at TEXT, finished_at TEXT,
  owner TEXT
);
CREATE TABLE IF NOT EXISTS scenarios (
  scenario_id TEXT PRIMARY KEY, name TEXT, target TEXT, run_mode TEXT, plan_hash TEXT,
  steps_json TEXT, unmatched INTEGER, tags TEXT, source_run_id TEXT, created_at TEXT, owner TEXT
);
CREATE TABLE IF NOT EXISTS tests (
  test_id TEXT PRIMARY KEY, scenario_id TEXT, plan_hash TEXT, name TEXT, schedule TEXT,
  enabled INTEGER, last_status TEXT, last_run_id TEXT, created_at TEXT, owner TEXT
);
CREATE TABLE IF NOT EXISTS chats (
  conversation_id TEXT PRIMARY KEY, last_target TEXT, turn_count INTEGER, last_active TEXT,
  last_goal TEXT, summary TEXT, updated_at TEXT, owner TEXT
);
CREATE TABLE IF NOT EXISTS results (
  run_id TEXT PRIMARY KEY, plan_id TEXT, mode TEXT, verdict TEXT, exit_code INTEGER,
  healed INTEGER, failed INTEGER, regressions_json TEXT, steps_json TEXT, coverage REAL,
  duration_ms INTEGER, created_at TEXT, owner TEXT, fault_domain TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  run_id TEXT, ts REAL, name TEXT, value REAL, labels_json TEXT, owner TEXT
);
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, name TEXT UNIQUE, pw_hash TEXT, is_admin INTEGER, created_at TEXT
);
-- ADR-109 (Alex's directive): the tool belongs to the master user, everything the working person
-- configures belongs to them. So a config document is identified by (key, OWNER), not by key alone --
-- "" is the global document and any other value is one account's. With key alone, a personal "setup"
-- would overwrite the global one instead of layering over it.
CREATE TABLE IF NOT EXISTS config (
  key TEXT, owner TEXT NOT NULL DEFAULT '', value_json TEXT, updated_at TEXT,
  PRIMARY KEY (key, owner)
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_ts ON metrics(name, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_scenarios_target ON scenarios(target);
CREATE INDEX IF NOT EXISTS idx_users_name ON users(name);`

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// Server is the SQLite-backed store-gateway: the legacy heal/trust PersistenceService (M2b) plus the
// M13 StoreService (5 domains, ADR-050). Writes are serialized (single-writer, ADR-007).
type Server struct {
	pb.UnimplementedPersistenceServiceServer
	pb.UnimplementedStoreServiceServer // M13 (ADR-050): runs/scenarios/tests/chats/results/metrics
	db                                 *sql.DB
	mu                                 sync.Mutex
	goldenKey                          []byte // #24: HMAC key for golden_snapshots integrity (state/golden.key)
}

func New(path string) (*Server, error) {
	// M13 (ADR-050) scaffold: a Postgres backend behind STORE_DSN is deferred to M13-service
	// (M11/ADR-053). Recognize the env now and fail loudly rather than silently serving SQLite when
	// Postgres was requested (mirrors CHECKPOINT_DSN in brain/__main__.py:_checkpointer).
	if dsn := os.Getenv("STORE_DSN"); dsn != "" {
		return nil, fmt.Errorf("STORE_DSN is set but the Postgres backend is deferred to M13-service "+
			"(M11/ADR-053); unset STORE_DSN to use the SQLite path %q", path)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err = db.Exec("PRAGMA journal_mode=WAL;"); err != nil {
		return nil, err
	}
	if _, err = db.Exec(schema); err != nil {
		return nil, err
	}
	if _, err = db.Exec(storeSchema); err != nil { // M13: the 5 StoreService domains
		return nil, err
	}
	if err = ensureColumn(db, "healing_audit", "identity"); err != nil { // ADR-082: pre-identity DBs
		return nil, err
	}
	// ADR-109: pre-identity DBs get the owner column. Empty for every existing row, which is what
	// "unowned" means and what keeps a single-team install behaving exactly as it did.
	for _, t := range []string{"runs", "scenarios", "tests", "chats", "results", "metrics"} {
		if err = ensureColumn(db, t, "owner"); err != nil {
			return nil, err
		}
	}
	// HEALTH-004: pre-fault databases get the column. Empty on every existing row, which reads as "we
	// never decided whose problem this was" — deliberately NOT `none`, because backfilling an answer we
	// do not have would make old rows claim a clean bill of health they were never given.
	if err = ensureColumn(db, "results", "fault_domain"); err != nil {
		return nil, err
	}
	// The index comes AFTER the column, and that ordering is the whole point. It first lived in
	// storeSchema, which runs before this loop — so on a FRESH database it worked (the column is created
	// in the same DDL) and on an EXISTING one the gateway refused to open at all: "no such column:
	// owner". An upgrade-breaking defect that only appears on a database old enough to matter, and the
	// migration test missed it by building its "old" database with the NEW schema.
	if _, err = db.Exec("CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner)"); err != nil {
		return nil, err
	}
	// config is the one table whose PRIMARY KEY changed, and SQLite cannot ALTER one — so it is
	// rebuilt rather than extended. Existing rows become the GLOBAL document (owner=""), which is what
	// they always were: written by whoever ran the wizard, before an account could exist.
	if err = ensureConfigOwner(db); err != nil {
		return nil, err
	}
	if err = ensureGoldenMacColumn(db); err != nil { // migrate pre-#24 DBs (no mac column)
		return nil, err
	}
	keyPath := filepath.Join(filepath.Dir(path), "golden.key")
	_, statErr := os.Stat(keyPath)
	keyExisted := statErr == nil
	key, err := loadOrCreateGoldenKey(keyPath)
	if err != nil {
		return nil, err
	}
	s := &Server{db: db, goldenKey: key}
	// #24: the FIRST time the key is minted (fresh install or upgrade over a pre-#24 DB) we MAC
	// whatever golden rows already exist (trust-on-first-use), then require a valid MAC on every read.
	// This closes the MAC-strip downgrade: once the key exists, a NULL/empty mac is treated as tampered
	// rather than silently trusted, so a DB swap or row edit that drops the mac is detected.
	if !keyExisted {
		if err := s.backfillGoldenMACs(); err != nil {
			return nil, err
		}
	}
	_ = os.Chmod(path, 0o600) // #24 (opt): restrict locators.db to the owner (best-effort)
	return s, nil
}

// backfillGoldenMACs MACs any golden row missing one (NULL/empty), under the current key. Run once,
// when the key is first created, to migrate pre-#24 baselines without breaking them (#24, TOFU).
func (s *Server) backfillGoldenMACs() error {
	rows, err := s.db.Query("SELECT page_key,a11y_hash,screenshot_hash FROM golden_snapshots WHERE mac IS NULL OR mac=''")
	if err != nil {
		return err
	}
	type row struct{ pk, a11y, shot string }
	var pending []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.pk, &r.a11y, &r.shot); err != nil {
			rows.Close()
			return err
		}
		pending = append(pending, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, r := range pending {
		if _, err := s.db.Exec("UPDATE golden_snapshots SET mac=? WHERE page_key=?",
			goldenMAC(s.goldenKey, r.pk, r.a11y, r.shot), r.pk); err != nil {
			return err
		}
	}
	return nil
}

// ensureColumn adds `col TEXT` to `table` when it is missing — the idempotent ALTER that a schema
// built from CREATE TABLE IF NOT EXISTS needs, since that statement does nothing to a table which
// already exists. ADR-082 generalised the pattern rather than copying ensureGoldenMacColumn a second
// time: this DDL is duplicated verbatim in brain/store.py with no parity gate between them, so every
// extra hand-written copy is one more place the two languages can silently disagree.
//
// `table` and `col` are compile-time literals from this package — never user input — because SQLite
// cannot parameterise identifiers and a string-formatted DDL would otherwise be an injection site.
func ensureColumn(db *sql.DB, table, col string) error {
	rows, err := db.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		return err
	}
	has := false
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			rows.Close()
			return err
		}
		if name == col {
			has = true
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	if has {
		return nil
	}
	_, err = db.Exec("ALTER TABLE " + table + " ADD COLUMN " + col + " TEXT")
	return err
}

// hasColumn reports whether `table` has `col`. Split out of ensureColumn because the config migration
// below cannot use the ALTER path at all: it changes a PRIMARY KEY, which SQLite has no statement for.
func hasColumn(db *sql.DB, table, col string) (bool, error) {
	rows, err := db.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		return false, err
	}
	defer rows.Close()
	found := false
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return false, err
		}
		if name == col {
			found = true
		}
	}
	return found, rows.Err()
}

// ensureConfigOwner migrates a pre-ADR-109 `config` table, whose PRIMARY KEY was the key alone.
//
// A rebuild, not an ALTER: adding the column would be one statement, but the key would stay `key`, so
// a personal document would OVERWRITE the global one — the same row, silently, with no error and no
// way to tell afterwards which layer the surviving text came from. Existing rows migrate to owner=""
// because that is what they are: the global configuration, written before any account existed.
//
// The whole rebuild runs in one transaction. A half-migrated config is worse than an unmigrated one:
// the reader would find a table with the right shape and none of the operator's settings in it.
func ensureConfigOwner(db *sql.DB) error {
	has, err := hasColumn(db, "config", "owner")
	if err != nil || has {
		return err
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck // no-op after Commit; the point is that a failure leaves nothing
	stmts := []string{
		`CREATE TABLE config_adr109 (
		   key TEXT, owner TEXT NOT NULL DEFAULT '', value_json TEXT, updated_at TEXT,
		   PRIMARY KEY (key, owner)
		 )`,
		`INSERT INTO config_adr109(key,owner,value_json,updated_at) SELECT key,'',value_json,updated_at FROM config`,
		`DROP TABLE config`,
		`ALTER TABLE config_adr109 RENAME TO config`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("config owner migration (%.40s…): %w", q, err)
		}
	}
	return tx.Commit()
}

// ensureGoldenMacColumn adds the `mac` column to a golden_snapshots table created before #24.
// CREATE TABLE IF NOT EXISTS won't alter an existing table, so upgraded DBs need this idempotent ALTER.
func ensureGoldenMacColumn(db *sql.DB) error {
	rows, err := db.Query("PRAGMA table_info(golden_snapshots)")
	if err != nil {
		return err
	}
	has := false
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			rows.Close()
			return err
		}
		if name == "mac" {
			has = true
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	if has {
		return nil
	}
	_, err = db.Exec("ALTER TABLE golden_snapshots ADD COLUMN mac TEXT")
	return err
}

// loadOrCreateGoldenKey returns the HMAC key at path, generating a fresh 32-byte key (0600) on first
// use. The key lives beside locators.db; a full DB swap by a peer without the key cannot forge MACs.
func loadOrCreateGoldenKey(path string) ([]byte, error) {
	if b, err := os.ReadFile(path); err == nil && len(b) >= 16 {
		return b, nil
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil { // lost a race (concurrent gateway) -> read the winner's key
		if b, rerr := os.ReadFile(path); rerr == nil && len(b) >= 16 {
			return b, nil
		}
		return nil, err
	}
	defer f.Close()
	if _, err := f.Write(key); err != nil {
		return nil, err
	}
	return key, nil
}

// goldenMAC is HMAC-SHA256 over the integrity-bearing golden fields (created_at is excluded so the
// MAC is stable across the Go and Python implementations, which format floats differently).
func goldenMAC(key []byte, pageKey, a11y, shot string) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(pageKey))
	m.Write([]byte{0x1f})
	m.Write([]byte(a11y))
	m.Write([]byte{0x1f})
	m.Write([]byte(shot))
	return hex.EncodeToString(m.Sum(nil))
}

func (s *Server) Close() error {
	// Checkpoint the WAL into the main DB so writes are durable + visible to the next
	// short-lived gateway process (agentctl spawns one per invocation). Without this a golden
	// saved by `baseline update`'s gateway can be invisible to the later `replay` gateway.
	_, _ = s.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	return s.db.Close()
}

func (s *Server) Lookup(_ context.Context, k *pb.LocatorKey) (*pb.LocatorRecord, error) {
	r := &pb.LocatorRecord{}
	err := s.db.QueryRow(
		"SELECT strategy,value,confidence,status FROM healed_locators WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=? AND status='active'",
		k.PagePath, k.SemanticId, k.DomSubtreeHash).Scan(&r.Strategy, &r.Value, &r.Confidence, &r.Status)
	if err == sql.ErrNoRows {
		return &pb.LocatorRecord{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	r.Found = true
	return r, nil
}

func (s *Server) EvictStale(_ context.Context, e *pb.EvictRequest) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		"UPDATE healed_locators SET status='deprecated' WHERE page_path=? AND semantic_id=? AND dom_subtree_hash!=? AND status='active'",
		e.PagePath, e.SemanticId, e.CurrentHash)
	return &pb.Empty{}, err
}

func (s *Server) SaveLocator(_ context.Context, r *pb.LocatorRecord) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := r.Status
	if status == "" {
		status = "active"
	}
	_, err := s.db.Exec(
		"INSERT OR REPLACE INTO healed_locators(page_path,semantic_id,strategy,value,confidence,dom_subtree_hash,status,times_used,created_at) "+
			"VALUES(?,?,?,?,?,?,?,COALESCE((SELECT times_used FROM healed_locators WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=?),0),?)",
		r.PagePath, r.SemanticId, r.Strategy, r.Value, r.Confidence, r.DomSubtreeHash, status,
		r.PagePath, r.SemanticId, r.DomSubtreeHash, now())
	return &pb.Empty{}, err
}

func (s *Server) BumpUsed(_ context.Context, k *pb.LocatorKey) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		"UPDATE healed_locators SET times_used=times_used+1 WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=?",
		k.PagePath, k.SemanticId, k.DomSubtreeHash)
	return &pb.Empty{}, err
}

func (s *Server) AppendAudit(_ context.Context, a *pb.AuditRow) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		"INSERT INTO healing_audit(run_id,step,semantic_id,page_path,strategy,original,healed,confidence,outcome,dom_hash,ts,identity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
		a.RunId, a.Step, a.SemanticId, a.PagePath, a.Strategy, a.Original, a.Healed, a.Confidence, a.Outcome, a.DomHash, now(), a.Identity)
	return &pb.Empty{}, err
}

func (s *Server) SaveGolden(_ context.Context, g *pb.Golden) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	mac := goldenMAC(s.goldenKey, g.PageKey, g.A11YHash, g.ScreenshotHash)
	_, err := s.db.Exec(
		"INSERT OR REPLACE INTO golden_snapshots(page_key,a11y_hash,screenshot_hash,created_at,mac) VALUES(?,?,?,?,?)",
		g.PageKey, g.A11YHash, g.ScreenshotHash, now(), mac)
	return &pb.Empty{}, err
}

func (s *Server) GetGolden(_ context.Context, k *pb.PageKey) (*pb.Golden, error) {
	g := &pb.Golden{PageKey: k.PageKey}
	var mac sql.NullString
	err := s.db.QueryRow(
		"SELECT a11y_hash,screenshot_hash,mac FROM golden_snapshots WHERE page_key=?",
		k.PageKey).Scan(&g.A11YHash, &g.ScreenshotHash, &mac)
	if err == sql.ErrNoRows {
		return &pb.Golden{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	// #24: every golden row must carry a valid HMAC. Pre-#24 rows were MAC'd once at upgrade
	// (backfillGoldenMACs), so a MISSING mac here is not "legacy" — it is a strip/inject attempt and
	// is rejected, exactly like a wrong mac. This is what makes a DB swap detectable without the key:
	// the swapped-in rows have no valid mac. Rejection -> brain maps DataLoss to a controlled exit 3.
	want := goldenMAC(s.goldenKey, k.PageKey, g.A11YHash, g.ScreenshotHash)
	if !mac.Valid || mac.String == "" || !hmac.Equal([]byte(want), []byte(mac.String)) {
		return nil, status.Errorf(codes.DataLoss,
			"golden integrity: missing or invalid MAC for page %q (tampered or DB swapped)", k.PageKey)
	}
	g.Found = true
	return g, nil
}

// RecordStep mirrors brain/store.py: a failure counts toward flakiness only while aut_sha is
// unchanged; quarantine at >=3 fails in the last 5; clear on 3 consecutive passes.
func (s *Server) RecordStep(_ context.Context, r *pb.StepResult) (*pb.Quarantine, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var last5JSON, lastSha string
	var quar int
	row := s.db.QueryRow("SELECT last5,last_aut_sha,quarantined FROM step_failures WHERE plan_id=? AND step_key=?",
		r.PlanId, r.StepKey)
	existed := true
	if err := row.Scan(&last5JSON, &lastSha, &quar); err == sql.ErrNoRows {
		existed = false
	} else if err != nil {
		return nil, err
	}
	var last5 []int
	if last5JSON != "" {
		_ = json.Unmarshal([]byte(last5JSON), &last5)
	}
	quarantined := quar != 0
	if existed && lastSha != r.AutSha { // app under test changed -> reset window
		last5 = nil
		quarantined = false
	}
	v := 0
	if r.Passed {
		v = 1
	}
	last5 = append(last5, v)
	if len(last5) > 5 {
		last5 = last5[len(last5)-5:]
	}
	fails := 0
	for _, x := range last5 {
		if x == 0 {
			fails++
		}
	}
	if fails >= 3 {
		quarantined = true
	}
	n := len(last5)
	if n >= 3 && last5[n-1] == 1 && last5[n-2] == 1 && last5[n-3] == 1 {
		quarantined = false
	}
	b, _ := json.Marshal(last5)
	qi := 0
	if quarantined {
		qi = 1
	}
	if _, err := s.db.Exec(
		"INSERT OR REPLACE INTO step_failures(plan_id,step_key,last5,last_aut_sha,quarantined) VALUES(?,?,?,?,?)",
		r.PlanId, r.StepKey, string(b), r.AutSha, qi); err != nil {
		return nil, err
	}
	return &pb.Quarantine{Quarantined: quarantined}, nil
}

func (s *Server) IsQuarantined(_ context.Context, k *pb.StepKey) (*pb.Quarantine, error) {
	var q int
	err := s.db.QueryRow("SELECT quarantined FROM step_failures WHERE plan_id=? AND step_key=?",
		k.PlanId, k.StepKey).Scan(&q)
	if err == sql.ErrNoRows {
		return &pb.Quarantine{Quarantined: false}, nil
	}
	if err != nil {
		return nil, err
	}
	return &pb.Quarantine{Quarantined: q != 0}, nil
}

func (s *Server) ClearQuarantine(_ context.Context, _ *pb.Empty) (*pb.Count, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.Exec("DELETE FROM step_failures")
	if err != nil {
		return nil, err
	}
	n, _ := res.RowsAffected()
	return &pb.Count{N: n}, nil
}

func (s *Server) AuditRows(_ context.Context, _ *pb.Empty) (*pb.AuditRowsReply, error) {
	rows, err := s.db.Query("SELECT strategy,outcome,confidence,COALESCE(identity,'') FROM healing_audit")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	reply := &pb.AuditRowsReply{}
	for rows.Next() {
		a := &pb.AuditRow{}
		// COALESCE, not a NullString: rows written before ADR-082 have identity NULL, and "no identity
		// claim" is exactly what the empty string means here — so the two collapse on purpose.
		if err := rows.Scan(&a.Strategy, &a.Outcome, &a.Confidence, &a.Identity); err != nil {
			return nil, err
		}
		reply.Rows = append(reply.Rows, a)
	}
	return reply, rows.Err()
}
