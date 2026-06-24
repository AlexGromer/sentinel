// Package store implements the Sentinel PersistenceService (M2b-1, ADR-015): the sole SQLite
// writer, exposed over gRPC. It replaces brain/store.py's local SQLite (restoring ADR-007).
// SQL mirrors brain/store.py 1:1 so behavior is identical. Pure-Go driver (modernc.org/sqlite).
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"sync"
	"time"

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
  original TEXT, healed TEXT, confidence REAL, outcome TEXT, dom_hash TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS golden_snapshots (
  page_key TEXT PRIMARY KEY, a11y_hash TEXT, screenshot_hash TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS step_failures (
  plan_id TEXT, step_key TEXT, last5 TEXT, last_aut_sha TEXT, quarantined INTEGER DEFAULT 0,
  PRIMARY KEY (plan_id, step_key)
);`

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// Server is the SQLite-backed PersistenceService. Writes are serialized (single-writer).
type Server struct {
	pb.UnimplementedPersistenceServiceServer
	db *sql.DB
	mu sync.Mutex
}

func New(path string) (*Server, error) {
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
	return &Server{db: db}, nil
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
		"INSERT INTO healing_audit(run_id,step,semantic_id,page_path,strategy,original,healed,confidence,outcome,dom_hash,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
		a.RunId, a.Step, a.SemanticId, a.PagePath, a.Strategy, a.Original, a.Healed, a.Confidence, a.Outcome, a.DomHash, now())
	return &pb.Empty{}, err
}

func (s *Server) SaveGolden(_ context.Context, g *pb.Golden) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		"INSERT OR REPLACE INTO golden_snapshots(page_key,a11y_hash,screenshot_hash,created_at) VALUES(?,?,?,?)",
		g.PageKey, g.A11YHash, g.ScreenshotHash, now())
	return &pb.Empty{}, err
}

func (s *Server) GetGolden(_ context.Context, k *pb.PageKey) (*pb.Golden, error) {
	g := &pb.Golden{PageKey: k.PageKey}
	err := s.db.QueryRow(
		"SELECT a11y_hash,screenshot_hash FROM golden_snapshots WHERE page_key=?",
		k.PageKey).Scan(&g.A11YHash, &g.ScreenshotHash)
	if err == sql.ErrNoRows {
		return &pb.Golden{Found: false}, nil
	}
	if err != nil {
		return nil, err
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
	rows, err := s.db.Query("SELECT strategy,outcome,confidence FROM healing_audit")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	reply := &pb.AuditRowsReply{}
	for rows.Next() {
		a := &pb.AuditRow{}
		if err := rows.Scan(&a.Strategy, &a.Outcome, &a.Confidence); err != nil {
			return nil, err
		}
		reply.Rows = append(reply.Rows, a)
	}
	return reply, rows.Err()
}
