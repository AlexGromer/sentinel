// M13 (ADR-049/050): the StoreService — 5 persistence domains (runs, scenarios/tests, chats
// projection, results, metrics) on the SAME single-writer SQLite DB as the legacy PersistenceService.
// M11.5 PR-5 (ADR-062) adds a 6th: config — the SERVICE tier of the tiered config (ADR-049).
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

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AlexGromer/sentinel/internal/configguard"
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
		`INSERT INTO runs(run_id,conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at,owner)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id) DO UPDATE SET conversation_id=excluded.conversation_id,mode=excluded.mode,
		   target=excluded.target,planner=excluded.planner,state=excluded.state,exit_code=excluded.exit_code,
		   artifact_dir=excluded.artifact_dir,error=excluded.error,started_at=excluded.started_at,
		   finished_at=excluded.finished_at,owner=excluded.owner`,
		r.RunId, r.ConversationId, r.Mode, r.Target, r.Planner, r.State, r.ExitCode, r.ArtifactDir,
		r.Error, nowRFC3339(r.StartedAt), r.FinishedAt, r.Owner)
	return &pb.Empty{}, err
}

func (s *Server) GetRun(_ context.Context, id *pb.RunId) (*pb.RunRecord, error) {
	r := &pb.RunRecord{RunId: id.RunId}
	err := s.db.QueryRow(
		`SELECT conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at,
		        COALESCE(owner,'')
		 FROM runs WHERE run_id=?`, id.RunId).Scan(
		&r.ConversationId, &r.Mode, &r.Target, &r.Planner, &r.State, &r.ExitCode, &r.ArtifactDir,
		&r.Error, &r.StartedAt, &r.FinishedAt, &r.Owner)
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
	where, args := ownerWhere(q.Owner)
	if q.State != "" {
		where, args = and(where, "state=?"), append(args, q.State)
	}
	out := &pb.RunList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM runs"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT run_id,conversation_id,mode,target,planner,state,exit_code,artifact_dir,error,started_at,finished_at,
		        COALESCE(owner,'')
		 FROM runs`+where+` ORDER BY started_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		r := &pb.RunRecord{Found: true}
		if err := rows.Scan(&r.RunId, &r.ConversationId, &r.Mode, &r.Target, &r.Planner, &r.State,
			&r.ExitCode, &r.ArtifactDir, &r.Error, &r.StartedAt, &r.FinishedAt, &r.Owner); err != nil {
			return nil, err
		}
		out.Runs = append(out.Runs, r)
	}
	return out, rows.Err()
}

// ownerWhere starts a WHERE clause scoped to one account, or an unscoped one when owner is "".
//
// ADR-109 makes identity OPT-IN: an empty owner means "every row", which is what a machine token gets
// and what a deployment with no accounts always gets. A store with no subjects must behave exactly as
// it did before subjects existed, or adding identity would break the single-team install open-core is
// for. Rows written before the column existed carry NULL, which COALESCE reads as "" — so an unowned
// row belongs to nobody rather than accidentally matching the first account created.
func ownerWhere(owner string) (string, []any) {
	if owner == "" {
		return "", []any{}
	}
	return " WHERE COALESCE(owner,'')=?", []any{owner}
}

// and appends a condition to a clause that may or may not have started one yet.
func and(where, cond string) string {
	if where == "" {
		return " WHERE " + cond
	}
	return where + " AND " + cond
}

// --- scenarios / tests (index + promote scenario -> test) -------------------

func (s *Server) SaveScenario(_ context.Context, sc *pb.Scenario) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO scenarios(scenario_id,name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at,owner)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name,target=excluded.target,run_mode=excluded.run_mode,
		   plan_hash=excluded.plan_hash,steps_json=excluded.steps_json,unmatched=excluded.unmatched,
		   tags=excluded.tags,source_run_id=excluded.source_run_id,owner=excluded.owner`,
		sc.ScenarioId, sc.Name, sc.Target, sc.RunMode, sc.PlanHash, sc.StepsJson, sc.Unmatched, sc.Tags,
		sc.SourceRunId, nowRFC3339(sc.CreatedAt), sc.Owner)
	return &pb.Empty{}, err
}

func (s *Server) GetScenario(_ context.Context, id *pb.ScenarioId) (*pb.Scenario, error) {
	sc := &pb.Scenario{ScenarioId: id.ScenarioId}
	err := s.db.QueryRow(
		`SELECT name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at,COALESCE(owner,'')
		 FROM scenarios WHERE scenario_id=?`, id.ScenarioId).Scan(
		&sc.Name, &sc.Target, &sc.RunMode, &sc.PlanHash, &sc.StepsJson, &sc.Unmatched, &sc.Tags,
		&sc.SourceRunId, &sc.CreatedAt, &sc.Owner)
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
		`SELECT scenario_id,name,target,run_mode,plan_hash,steps_json,unmatched,tags,source_run_id,created_at,
		        COALESCE(owner,'')
		 FROM scenarios`+where+` ORDER BY created_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		sc := &pb.Scenario{Found: true}
		if err := rows.Scan(&sc.ScenarioId, &sc.Name, &sc.Target, &sc.RunMode, &sc.PlanHash, &sc.StepsJson,
			&sc.Unmatched, &sc.Tags, &sc.SourceRunId, &sc.CreatedAt, &sc.Owner); err != nil {
			return nil, err
		}
		out.Scenarios = append(out.Scenarios, sc)
	}
	return out, rows.Err()
}

// DeleteScenario removes a scenario (M14 wave W3: library management). Idempotent — deleting a
// missing scenario_id is success, not an error, so a UI double-click/retry never surfaces a fault.
func (s *Server) DeleteScenario(_ context.Context, id *pb.ScenarioId) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("DELETE FROM scenarios WHERE scenario_id=?", id.ScenarioId)
	return &pb.Empty{}, err
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
		Schedule: r.Schedule, Enabled: true, CreatedAt: time.Now().UTC().Format(time.RFC3339), Found: true,
		Owner: r.Owner}
	if _, err = s.db.Exec(
		`INSERT INTO tests(test_id,scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at,owner)
		 VALUES(?,?,?,?,?,1,'','',?,?)`,
		t.TestId, t.ScenarioId, t.PlanHash, t.Name, t.Schedule, t.CreatedAt, t.Owner); err != nil {
		return nil, err
	}
	return t, nil
}

func (s *Server) GetTest(_ context.Context, id *pb.TestId) (*pb.TestRecord, error) {
	t := &pb.TestRecord{TestId: id.TestId}
	var enabled int
	err := s.db.QueryRow(
		`SELECT scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at,COALESCE(owner,'')
		 FROM tests WHERE test_id=?`, id.TestId).Scan(
		&t.ScenarioId, &t.PlanHash, &t.Name, &t.Schedule, &enabled, &t.LastStatus, &t.LastRunId, &t.CreatedAt,
		&t.Owner)
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
	where, args := ownerWhere(q.Owner)
	if err := s.db.QueryRow("SELECT COUNT(*) FROM tests"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT test_id,scenario_id,plan_hash,name,schedule,enabled,last_status,last_run_id,created_at,
		        COALESCE(owner,'')
		 FROM tests`+where+` ORDER BY created_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		t := &pb.TestRecord{Found: true}
		var enabled int
		if err := rows.Scan(&t.TestId, &t.ScenarioId, &t.PlanHash, &t.Name, &t.Schedule, &enabled,
			&t.LastStatus, &t.LastRunId, &t.CreatedAt, &t.Owner); err != nil {
			return nil, err
		}
		t.Enabled = enabled != 0
		out.Tests = append(out.Tests, t)
	}
	return out, rows.Err()
}

// DeleteTest removes a test (M14 wave W3: library management). Idempotent, like DeleteScenario.
func (s *Server) DeleteTest(_ context.Context, id *pb.TestId) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("DELETE FROM tests WHERE test_id=?", id.TestId)
	return &pb.Empty{}, err
}

// --- chats (BROWSABLE PROJECTION of conversations.db; not a duplicate) ------

func (s *Server) UpsertChat(_ context.Context, c *pb.ChatProjection) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO chats(conversation_id,last_target,turn_count,last_active,last_goal,summary,updated_at,owner)
		 VALUES(?,?,?,?,?,?,?,?)
		 ON CONFLICT(conversation_id) DO UPDATE SET last_target=excluded.last_target,turn_count=excluded.turn_count,
		   last_active=excluded.last_active,last_goal=excluded.last_goal,summary=excluded.summary,updated_at=excluded.updated_at,owner=excluded.owner`,
		c.ConversationId, c.LastTarget, c.TurnCount, nowRFC3339(c.LastActive), c.LastGoal, c.Summary,
		time.Now().UTC().Format(time.RFC3339), c.Owner)
	return &pb.Empty{}, err
}

func (s *Server) GetChat(_ context.Context, id *pb.ConversationId) (*pb.ChatProjection, error) {
	c := &pb.ChatProjection{ConversationId: id.ConversationId}
	err := s.db.QueryRow(
		`SELECT last_target,turn_count,last_active,last_goal,summary,COALESCE(owner,'') FROM chats WHERE conversation_id=?`,
		id.ConversationId).Scan(&c.LastTarget, &c.TurnCount, &c.LastActive, &c.LastGoal, &c.Summary, &c.Owner)
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
	where, args := ownerWhere(q.Owner)
	if err := s.db.QueryRow("SELECT COUNT(*) FROM chats"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT conversation_id,last_target,turn_count,last_active,last_goal,summary,COALESCE(owner,'')
		 FROM chats`+where+` ORDER BY last_active DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		c := &pb.ChatProjection{Found: true}
		if err := rows.Scan(&c.ConversationId, &c.LastTarget, &c.TurnCount, &c.LastActive, &c.LastGoal,
			&c.Summary, &c.Owner); err != nil {
			return nil, err
		}
		out.Chats = append(out.Chats, c)
	}
	return out, rows.Err()
}

// DeleteChat removes a chat projection row (M14 wave W3: conversation management). Idempotent, like
// DeleteScenario/DeleteTest. This deletes only the browsable projection row — the underlying
// conversation thread (state/conversations.db) is untouched (chats here is a projection, not a duplicate).
func (s *Server) DeleteChat(_ context.Context, id *pb.ConversationId) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("DELETE FROM chats WHERE conversation_id=?", id.ConversationId)
	return &pb.Empty{}, err
}

// --- results (index heal-report.json / report.json) -------------------------

func (s *Server) SaveResult(_ context.Context, r *pb.ResultRecord) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO results(run_id,plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at,owner)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id) DO UPDATE SET plan_id=excluded.plan_id,mode=excluded.mode,verdict=excluded.verdict,
		   exit_code=excluded.exit_code,healed=excluded.healed,failed=excluded.failed,
		   regressions_json=excluded.regressions_json,steps_json=excluded.steps_json,coverage=excluded.coverage,
		   duration_ms=excluded.duration_ms,owner=excluded.owner`,
		r.RunId, r.PlanId, r.Mode, r.Verdict, r.ExitCode, r.Healed, r.Failed, r.RegressionsJson,
		r.StepsJson, r.Coverage, r.DurationMs, nowRFC3339(r.CreatedAt), r.Owner)
	return &pb.Empty{}, err
}

func (s *Server) GetResult(_ context.Context, id *pb.RunId) (*pb.ResultRecord, error) {
	r := &pb.ResultRecord{RunId: id.RunId}
	err := s.db.QueryRow(
		`SELECT plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at,
		        COALESCE(owner,'')
		 FROM results WHERE run_id=?`, id.RunId).Scan(
		&r.PlanId, &r.Mode, &r.Verdict, &r.ExitCode, &r.Healed, &r.Failed, &r.RegressionsJson,
		&r.StepsJson, &r.Coverage, &r.DurationMs, &r.CreatedAt, &r.Owner)
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
	where, args := ownerWhere(q.Owner)
	if err := s.db.QueryRow("SELECT COUNT(*) FROM results"+where, args...).Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT run_id,plan_id,mode,verdict,exit_code,healed,failed,regressions_json,steps_json,coverage,duration_ms,created_at,
		        COALESCE(owner,'')
		 FROM results`+where+` ORDER BY created_at DESC LIMIT ? OFFSET ?`,
		append(args, listCap(q.Limit), q.Offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		r := &pb.ResultRecord{Found: true}
		if err := rows.Scan(&r.RunId, &r.PlanId, &r.Mode, &r.Verdict, &r.ExitCode, &r.Healed, &r.Failed,
			&r.RegressionsJson, &r.StepsJson, &r.Coverage, &r.DurationMs, &r.CreatedAt, &r.Owner); err != nil {
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

// --- config (M11.5 PR-5, ADR-062: the service tier of the tiered config) -----
//
// A key -> JSON-document store. The standalone tier keeps its file loader (brain/runconfig.py,
// unchanged); this domain is what a SERVICE deployment reads at start and the setup wizard writes.
//
// The secret guard is enforced HERE, not only in the control-API, because this socket is reachable by
// any same-UID process holding STORE_TOKEN — an HTTP-only check would be a suggestion, not a boundary.
// The rule itself lives in internal/configguard so both enforcement points share one definition.

// PutConfig upserts a config document, REFUSING (InvalidArgument) rather than silently stripping a
// document that is not a JSON object or that carries a secret-shaped member name at any depth.
func (s *Server) PutConfig(_ context.Context, r *pb.ConfigRecord) (*pb.Empty, error) {
	if r.Key == "" {
		return nil, status.Error(codes.InvalidArgument, "config: key is required")
	}
	if err := configguard.Validate(r.ValueJson); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO config(key,value_json,updated_at) VALUES(?,?,?)
		 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`,
		r.Key, r.ValueJson, nowRFC3339(r.UpdatedAt))
	return &pb.Empty{}, err
}

func (s *Server) GetConfig(_ context.Context, k *pb.ConfigKey) (*pb.ConfigRecord, error) {
	r := &pb.ConfigRecord{Key: k.Key}
	err := s.db.QueryRow(`SELECT value_json,updated_at FROM config WHERE key=?`, k.Key).
		Scan(&r.ValueJson, &r.UpdatedAt)
	if err == sql.ErrNoRows {
		return &pb.ConfigRecord{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	r.Found = true
	return r, nil
}

func (s *Server) ListConfig(_ context.Context, _ *pb.Empty) (*pb.ConfigList, error) {
	rows, err := s.db.Query(`SELECT key,value_json,updated_at FROM config ORDER BY key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := &pb.ConfigList{}
	for rows.Next() {
		r := &pb.ConfigRecord{Found: true}
		if err := rows.Scan(&r.Key, &r.ValueJson, &r.UpdatedAt); err != nil {
			return nil, err
		}
		out.Items = append(out.Items, r)
	}
	return out, rows.Err()
}

// DeleteConfig is idempotent — deleting a missing key is success, like DeleteScenario/DeleteTest/DeleteChat.
func (s *Server) DeleteConfig(_ context.Context, k *pb.ConfigKey) (*pb.Empty, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(`DELETE FROM config WHERE key=?`, k.Key)
	return &pb.Empty{}, err
}
