// M13 (ADR-049/050): the StoreService — 5 persistence domains (runs, scenarios/tests, chats
// projection, results, metrics) on the SAME single-writer SQLite DB as the legacy PersistenceService.
// All writes take s.mu (ADR-007); reads don't. Upserts use portable `INSERT ... ON CONFLICT DO UPDATE`
// (no SQLite-only INSERT OR REPLACE) so the Postgres backend (M13-service, M11/ADR-053) reuses this SQL.
package store

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"strings"
	"time"

	pb "github.com/AlexGromer/sentinel/internal/store/pb"
)

func newStoreID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "local"
	}
	return hex.EncodeToString(b)
}

// nowRFC3339 returns s when non-empty, else the current UTC RFC3339 timestamp (default-on-write).
func nowRFC3339(s string) string {
	if s != "" {
		return s
	}
	return time.Now().UTC().Format(time.RFC3339)
}

// listCap bounds a page size to a sane range (0/oversized -> 200).
func listCap(limit int64) int64 {
	if limit <= 0 || limit > 1000 {
		return 200
	}
	return limit
}

// --- runs (persist control-API's run map; survives restart) -----------------

func (s *Server) UpsertRun(_ context.Context, r *pb.RunRecord) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO runs(run_id,conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id) DO UPDATE SET conversation_id=excluded.conversation_id,mode=excluded.mode,
		   target=excluded.target,planner=excluded.planner,state=excluded.state,exit_code=excluded.exit_code,
		   artifact_dir=excluded.artifact_dir,error=excluded.error,started_at=excluded.started_at,
		   finished_at=excluded.finished_at`,
		r.RunId, r.ConversationId, r.Mode, r.Target, r.Planner, r.State, r.ExitCode, r.ArtifactDir,
		r.Error, nowRFC3339(r.StartedAt), r.FinishedAt)
	return &pb.Empty{}, err
}

func (s *Server) GetRun(_ context.Context, id *pb.RunId) (*pb.RunRecord, error) {
	r := &pb.RunRecord{RunId: id.RunId}
	err := s.db.QueryRow(
		`SELECT conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at
		 FROM runs WHERE run_id=?`, id.RunId).Scan(
		&r.ConversationId, &r.Mode, &r.Target, &r.Planner, &r.State, &r.ExitCode, &r.ArtifactDir,
		&r.Error, &r.StartedAt, &r.FinishedAt)
	if err == sql.ErrNoRows {
		return &pb.RunRecord{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	r.Found = true
	return r, nil
}

func (s *Server) ListRuns(_ context.Context, q *pb.ListRunsReq) (*pb.RunList, error) {
	where, args := "", []any{}
	if q.State != "" {
		where, args = " WHERE state=?", append(args, q.State)
	}
	out := &pb.RunList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM runs"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT run_id,conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at
		 FROM runs`+where+` ORDER BY started_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		r := &pb.RunRecord{Found: true}
		if err := rows.Scan(&r.RunId, &r.ConversationId, &r.Mode, &r.Target, &r.Planner, &r.State,
			&r.ExitCode, &r.ArtifactDir, &r.Error, &r.StartedAt, &r.FinishedAt); err != nil {
			return nil, err
		}
		out.Runs = append(out.Runs, r)
	}
	return out, rows.Err()
}

// --- scenarios / tests (index + promote scenario -> test) -------------------

func (s *Server) SaveScenario(_ context.Context, sc *pb.Scenario) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO scenarios(scenario_id,name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name,target=excluded.target,run_mode=excluded.run_mode,
		   plan_hash=excluded.plan_hash,steps_json=excluded.steps_json,unmatched=excluded.unmatched,
		   tags=excluded.tags,source_run_id=excluded.source_run_id`,
		sc.ScenarioId, sc.Name, sc.Target, sc.RunMode, sc.PlanHash, sc.StepsJson, sc.Unmatched, sc.Tags,
		sc.SourceRunId, nowRFC3339(sc.CreatedAt))
	return &pb.Empty{}, err
}

func (s *Server) GetScenario(_ context.Context, id *pb.ScenarioId) (*pb.Scenario, error) {
	sc := &pb.Scenario{ScenarioId: id.ScenarioId}
	err := s.db.QueryRow(
		`SELECT name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at
		 FROM scenarios WHERE scenario_id=?`, id.ScenarioId).Scan(
		&sc.Name, &sc.Target, &sc.RunMode, &sc.PlanHash, &sc.StepsJson, &sc.Unmatched, &sc.Tags,
		&sc.SourceRunId, &sc.CreatedAt)
	if err == sql.ErrNoRows {
		return &pb.Scenario{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	sc.Found = true
	return sc, nil
}

func (s *Server) ListScenarios(_ context.Context, q *pb.ListScenariosReq) (*pb.ScenarioList, error) {
	where, args := "", []any{}
	if q.Target != "" {
		where, args = " WHERE target=?", append(args, q.Target)
	}
	out := &pb.ScenarioList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM scenarios"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT scenario_id,name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at
		 FROM scenarios`+where+` ORDER BY created_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		sc := &pb.Scenario{Found: true}
		if err := rows.Scan(&sc.ScenarioId, &sc.Name, &sc.Target, &sc.RunMode, &sc.PlanHash, &sc.StepsJson,
			&sc.Unmatched, &sc.Tags, &sc.SourceRunId, &sc.CreatedAt); err != nil {
			return nil, err
		}
		out.Scenarios = append(out.Scenarios, sc)
	}
	return out, rows.Err()
}

// PromoteTest freezes a scenario into a test (ADR-052: test = scenario + frozen plan_hash + golden +
// optional schedule + history). `schedule` is stored, NOT executed (no scheduler in M13).
func (s *Server) PromoteTest(_ context.Context, r *pb.PromoteReq) (*pb.TestRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var planHash string
	err := s.db.QueryRow("SELECT plan_hash FROM scenarios WHERE scenario_id=?", r.ScenarioId).Scan(&planHash)
	if err == sql.ErrNoRows {
		return &pb.TestRecord{Found: false}, nil // no such scenario to promote
	}
	if err != nil {
		return nil, err
	}
	name := r.Name
	if name == "" {
		name = r.ScenarioId
	}
	t := &pb.TestRecord{TestId: newStoreID(), ScenarioId: r.ScenarioId, PlanHash: planHash, Name: name,
		Schedule: r.Schedule, Enabled: true, CreatedAt: time.Now().UTC().Format(time.RFC3339), Found: true}
	if _, err = s.db.Exec(
		`INSERT INTO tests(test_id,scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at)
		 VALUES(?,?,?,?,?,1,'','',?)`,
		t.TestId, t.ScenarioId, t.PlanHash, t.Name, t.Schedule, t.CreatedAt); err != nil {
		return nil, err
	}
	return t, nil
}

func (s *Server) GetTest(_ context.Context, id *pb.TestId) (*pb.TestRecord, error) {
	t := &pb.TestRecord{TestId: id.TestId}
	var enabled int
	err := s.db.QueryRow(
		`SELECT scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at
		 FROM tests WHERE test_id=?`, id.TestId).Scan(
		&t.ScenarioId, &t.PlanHash, &t.Name, &t.Schedule, &enabled, &t.LastStatus, &t.LastRunId, &t.CreatedAt)
	if err == sql.ErrNoRows {
		return &pb.TestRecord{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	t.Enabled = enabled != 0
	t.Found = true
	return t, nil
}

func (s *Server) ListTests(_ context.Context, q *pb.ListTestsReq) (*pb.TestList, error) {
	out := &pb.TestList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM tests").Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT test_id,scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at
		 FROM tests ORDER BY created_at DESC LIMIT ? OFFSET ?`, listCap(q.Limit), q.Offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		t := &pb.TestRecord{Found: true}
		var enabled int
		if err := rows.Scan(&t.TestId, &t.ScenarioId, &t.PlanHash, &t.Name, &t.Schedule, &enabled,
			&t.LastStatus, &t.LastRunId, &t.CreatedAt); err != nil {
			return nil, err
		}
		t.Enabled = enabled != 0
		out.Tests = append(out.Tests, t)
	}
	return out, rows.Err()
}

// --- chats (BROWSABLE PROJECTION of conversations.db; not a duplicate) ------

func (s *Server) UpsertChat(_ context.Context, c *pb.ChatProjection) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO chats(conversation_id,last_target,turn_count,last_active,last_goal,summary,updated_at)
		 VALUES(?,?,?,?,?,?,?)
		 ON CONFLICT(conversation_id) DO UPDATE SET last_target=excluded.last_target,turn_count=excluded.turn_count,
		   last_active=excluded.last_active,last_goal=excluded.last_goal,summary=excluded.summary,updated_at=excluded.updated_at`,
		c.ConversationId, c.LastTarget, c.TurnCount, nowRFC3339(c.LastActive), c.LastGoal, c.Summary,
		time.Now().UTC().Format(time.RFC3339))
	return &pb.Empty{}, err
}

func (s *Server) GetChat(_ context.Context, id *pb.ConversationId) (*pb.ChatProjection, error) {
	c := &pb.ChatProjection{ConversationId: id.ConversationId}
	err := s.db.QueryRow(
		`SELECT last_target,turn_count,last_active,last_goal,summary FROM chats WHERE conversation_id=?`,
		id.ConversationId).Scan(&c.LastTarget, &c.TurnCount, &c.LastActive, &c.LastGoal, &c.Summary)
	if err == sql.ErrNoRows {
		return &pb.ChatProjection{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	c.Found = true
	return c, nil
}

func (s *Server) ListChats(_ context.Context, q *pb.ListChatsReq) (*pb.ChatList, error) {
	out := &pb.ChatList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT conversation_id,last_target,turn_count,last_active,last_goal,summary
		 FROM chats ORDER BY last_active DESC LIMIT ? OFFSET ?`, listCap(q.Limit), q.Offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		c := &pb.ChatProjection{Found: true}
		if err := rows.Scan(&c.ConversationId, &c.LastTarget, &c.TurnCount, &c.LastActive, &c.LastGoal, &c.Summary); err != nil {
			return nil, err
		}
		out.Chats = append(out.Chats, c)
	}
	return out, rows.Err()
}

// --- results (index heal-report.json / report.json) -------------------------

func (s *Server) SaveResult(_ context.Context, r *pb.ResultRecord) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO results(run_id,plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id) DO UPDATE SET plan_id=excluded.plan_id,mode=excluded.mode,verdict=excluded.verdict,
		   exit_code=excluded.exit_code,healed=excluded.healed,failed=excluded.failed,
		   regressions_json=excluded.regressions_json,steps_json=excluded.steps_json,coverage=excluded.coverage,
		   duration_ms=excluded.duration_ms`,
		r.RunId, r.PlanId, r.Mode, r.Verdict, r.ExitCode, r.Healed, r.Failed, r.RegressionsJson,
		r.StepsJson, r.Coverage, r.DurationMs, nowRFC3339(r.CreatedAt))
	return &pb.Empty{}, err
}

func (s *Server) GetResult(_ context.Context, id *pb.RunId) (*pb.ResultRecord, error) {
	r := &pb.ResultRecord{RunId: id.RunId}
	err := s.db.QueryRow(
		`SELECT plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at
		 FROM results WHERE run_id=?`, id.RunId).Scan(
		&r.PlanId, &r.Mode, &r.Verdict, &r.ExitCode, &r.Healed, &r.Failed, &r.RegressionsJson,
		&r.StepsJson, &r.Coverage, &r.DurationMs, &r.CreatedAt)
	if err == sql.ErrNoRows {
		return &pb.ResultRecord{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	r.Found = true
	return r, nil
}

func (s *Server) ListResults(_ context.Context, q *pb.ListResultsReq) (*pb.ResultList, error) {
	out := &pb.ResultList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM results").Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT run_id,plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at
		 FROM results ORDER BY created_at DESC LIMIT ? OFFSET ?`, listCap(q.Limit), q.Offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		r := &pb.ResultRecord{Found: true}
		if err := rows.Scan(&r.RunId, &r.PlanId, &r.Mode, &r.Verdict, &r.ExitCode, &r.Healed, &r.Failed,
			&r.RegressionsJson, &r.StepsJson, &r.Coverage, &r.DurationMs, &r.CreatedAt); err != nil {
			return nil, err
		}
		out.Results = append(out.Results, r)
	}
	return out, rows.Err()
}

// --- metrics (time-series ingested from results; trends for M15) ------------

func (s *Server) IngestMetrics(_ context.Context, b *pb.MetricsBatch) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return nil, err
	}
	stmt, err := tx.Prepare("INSERT INTO metrics(run_id,ts,name,value,labels_json) VALUES(?,?,?,?,?)")
	if err != nil {
		_ = tx.Rollback()
		return nil, err
	}
	defer stmt.Close()
	for _, p := range b.Points {
		ts := p.Ts
		if ts == 0 {
			ts = now()
		}
		if _, err := stmt.Exec(p.RunId, ts, p.Name, p.Value, p.LabelsJson); err != nil {
			_ = tx.Rollback()
			return nil, err
		}
	}
	return &pb.Empty{}, tx.Commit()
}

func (s *Server) QueryMetrics(_ context.Context, q *pb.MetricsQuery) (*pb.MetricsSeries, error) {
	var cond []string
	var args []any
	if q.Name != "" {
		cond, args = append(cond, "name=?"), append(args, q.Name)
	}
	if q.SinceTs > 0 {
		cond, args = append(cond, "ts>=?"), append(args, q.SinceTs)
	}
	if q.UntilTs > 0 {
		cond, args = append(cond, "ts<=?"), append(args, q.UntilTs)
	}
	where := ""
	if len(cond) > 0 {
		where = " WHERE " + strings.Join(cond, " AND ")
	}
	rows, err := s.db.Query(
		"SELECT run_id,ts,name,value,labels_json FROM metrics"+where+" ORDER BY ts ASC LIMIT ?",
		append(args, listCap(q.Limit))...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := &pb.MetricsSeries{}
	for rows.Next() {
		p := &pb.MetricPoint{}
		if err := rows.Scan(&p.RunId, &p.Ts, &p.Name, &p.Value, &p.LabelsJson); err != nil {
			return nil, err
		}
		out.Points = append(out.Points, p)
	}
	return out, rows.Err()
}

// Trends returns the last `window` points for a metric in chronological order (for M15 sparklines).
func (s *Server) Trends(_ context.Context, r *pb.TrendReq) (*pb.TrendReply, error) {
	window := r.Window
	if window <= 0 || window > 1000 {
		window = 50
	}
	rows, err := s.db.Query(
		"SELECT run_id,ts,value FROM metrics WHERE name=? ORDER BY ts DESC LIMIT ?", r.Metric, window)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var pts []*pb.TrendPoint
	for rows.Next() {
		p := &pb.TrendPoint{}
		if err := rows.Scan(&p.RunId, &p.Ts, &p.Value); err != nil {
			return nil, err
		}
		pts = append(pts, p)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for i, j := 0, len(pts)-1; i < j; i, j = i+1, j-1 { // newest-first DB order -> chronological
		pts[i], pts[j] = pts[j], pts[i]
	}
	return &pb.TrendReply{Points: pts}, nil
}
