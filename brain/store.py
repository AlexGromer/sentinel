"""Sentinel — persistence for healing (M2) + trust layer (M3).

Two interchangeable implementations behind one method interface (ADR-015):
- LocalStore  — direct SQLite (interim brain-local; used by the offline test suite and as the
                no-gateway fallback).
- GrpcStore   — thin client to the Go store-gateway over gRPC (production single-writer, ADR-007).

`make_store(local_path)` returns GrpcStore when STORE_ADDR is set, else LocalStore. healing.py /
replay.py / calibrate.py call the same methods on either. `Store` aliases LocalStore (tests import it).
"""
import json
import os
import pathlib
import sqlite3
import time

_SCHEMA = """
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
);
"""


class LocalStore:
    """Direct-SQLite implementation (interim; tests + no-gateway fallback). `now` injectable."""

    def __init__(self, path: str, now=None) -> None:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self._now = now or time.time

    def lookup(self, page_path, semantic_id, dom_subtree_hash):
        r = self.db.execute(
            "SELECT strategy,value,confidence,status FROM healed_locators "
            "WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=? AND status='active'",
            (page_path, semantic_id, dom_subtree_hash)).fetchone()
        return {"strategy": r[0], "value": r[1], "confidence": r[2], "status": r[3]} if r else None

    def evict_stale(self, page_path, semantic_id, current_hash) -> None:
        self.db.execute(
            "UPDATE healed_locators SET status='deprecated' "
            "WHERE page_path=? AND semantic_id=? AND dom_subtree_hash!=? AND status='active'",
            (page_path, semantic_id, current_hash))
        self.db.commit()

    def save_locator(self, page_path, semantic_id, strategy, value, confidence,
                     dom_subtree_hash, status="active") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO healed_locators"
            "(page_path,semantic_id,strategy,value,confidence,dom_subtree_hash,status,times_used,created_at) "
            "VALUES(?,?,?,?,?,?,?,"
            "COALESCE((SELECT times_used FROM healed_locators WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=?),0),?)",
            (page_path, semantic_id, strategy, value, confidence, dom_subtree_hash, status,
             page_path, semantic_id, dom_subtree_hash, self._now()))
        self.db.commit()

    def bump_used(self, page_path, semantic_id, dom_subtree_hash) -> None:
        self.db.execute(
            "UPDATE healed_locators SET times_used=times_used+1 "
            "WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=?",
            (page_path, semantic_id, dom_subtree_hash))
        self.db.commit()

    def audit(self, **row) -> None:
        row = {**row, "ts": self._now()}
        self.db.execute(
            "INSERT INTO healing_audit"
            "(run_id,step,semantic_id,page_path,strategy,original,healed,confidence,outcome,dom_hash,ts) "
            "VALUES(:run_id,:step,:semantic_id,:page_path,:strategy,:original,:healed,:confidence,:outcome,:dom_hash,:ts)",
            row)
        self.db.commit()

    def audit_rows(self):
        return list(self.db.execute("SELECT strategy,outcome,confidence FROM healing_audit").fetchall())

    def save_golden(self, page_key, a11y_hash, screenshot_hash) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO golden_snapshots(page_key,a11y_hash,screenshot_hash,created_at) "
            "VALUES(?,?,?,?)", (page_key, a11y_hash, screenshot_hash, self._now()))
        self.db.commit()

    def get_golden(self, page_key):
        r = self.db.execute(
            "SELECT a11y_hash,screenshot_hash FROM golden_snapshots WHERE page_key=?",
            (page_key,)).fetchone()
        return {"a11y_hash": r[0], "screenshot_hash": r[1]} if r else None

    def record_step(self, plan_id, step_key, passed: bool, aut_sha: str) -> bool:
        row = self.db.execute(
            "SELECT last5,last_aut_sha,quarantined FROM step_failures WHERE plan_id=? AND step_key=?",
            (plan_id, step_key)).fetchone()
        last5 = json.loads(row[0]) if row and row[0] else []
        quarantined = bool(row[2]) if row else False
        if row and row[1] != aut_sha:
            last5, quarantined = [], False
        last5 = (last5 + [1 if passed else 0])[-5:]
        if sum(1 for x in last5 if x == 0) >= 3:
            quarantined = True
        if len(last5) >= 3 and last5[-3:] == [1, 1, 1]:
            quarantined = False
        self.db.execute(
            "INSERT OR REPLACE INTO step_failures(plan_id,step_key,last5,last_aut_sha,quarantined) "
            "VALUES(?,?,?,?,?)", (plan_id, step_key, json.dumps(last5), aut_sha, int(quarantined)))
        self.db.commit()
        return quarantined

    def is_quarantined(self, plan_id, step_key) -> bool:
        r = self.db.execute(
            "SELECT quarantined FROM step_failures WHERE plan_id=? AND step_key=?",
            (plan_id, step_key)).fetchone()
        return bool(r[0]) if r else False

    def clear_quarantine(self) -> int:
        cur = self.db.execute("DELETE FROM step_failures")
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.close()


class GrpcStore:
    """Thin gRPC client to the Go store-gateway. Same method interface as LocalStore (ADR-015)."""

    def __init__(self, addr: str) -> None:
        import grpc
        from .pb import persistence_pb2 as pbmsg, persistence_pb2_grpc as pbgrpc
        self._pb = pbmsg
        self._ch = grpc.insecure_channel(f"unix:{addr}")
        self._stub = pbgrpc.PersistenceServiceStub(self._ch)

    def lookup(self, page_path, semantic_id, dom_subtree_hash):
        r = self._stub.Lookup(self._pb.LocatorKey(
            page_path=page_path, semantic_id=semantic_id, dom_subtree_hash=dom_subtree_hash))
        return None if not r.found else {
            "strategy": r.strategy, "value": r.value, "confidence": r.confidence, "status": r.status}

    def evict_stale(self, page_path, semantic_id, current_hash) -> None:
        self._stub.EvictStale(self._pb.EvictRequest(
            page_path=page_path, semantic_id=semantic_id, current_hash=current_hash))

    def save_locator(self, page_path, semantic_id, strategy, value, confidence,
                     dom_subtree_hash, status="active") -> None:
        self._stub.SaveLocator(self._pb.LocatorRecord(
            page_path=page_path, semantic_id=semantic_id, strategy=strategy, value=value,
            confidence=confidence, dom_subtree_hash=dom_subtree_hash, status=status))

    def bump_used(self, page_path, semantic_id, dom_subtree_hash) -> None:
        self._stub.BumpUsed(self._pb.LocatorKey(
            page_path=page_path, semantic_id=semantic_id, dom_subtree_hash=dom_subtree_hash))

    def audit(self, **row) -> None:
        self._stub.AppendAudit(self._pb.AuditRow(
            run_id=row.get("run_id", ""), step=int(row.get("step") or 0),
            semantic_id=row.get("semantic_id", ""), page_path=row.get("page_path", ""),
            strategy=row.get("strategy", ""), original=row.get("original", ""),
            healed=row.get("healed", ""), confidence=float(row.get("confidence") or 0.0),
            outcome=row.get("outcome", ""), dom_hash=row.get("dom_hash", "")))

    def audit_rows(self):
        return [(r.strategy, r.outcome, r.confidence)
                for r in self._stub.AuditRows(self._pb.Empty()).rows]

    def save_golden(self, page_key, a11y_hash, screenshot_hash) -> None:
        self._stub.SaveGolden(self._pb.Golden(
            page_key=page_key, a11y_hash=a11y_hash, screenshot_hash=screenshot_hash))

    def get_golden(self, page_key):
        g = self._stub.GetGolden(self._pb.PageKey(page_key=page_key))
        return None if not g.found else {"a11y_hash": g.a11y_hash, "screenshot_hash": g.screenshot_hash}

    def record_step(self, plan_id, step_key, passed: bool, aut_sha: str) -> bool:
        return self._stub.RecordStep(self._pb.StepResult(
            plan_id=plan_id, step_key=step_key, passed=passed, aut_sha=aut_sha)).quarantined

    def is_quarantined(self, plan_id, step_key) -> bool:
        return self._stub.IsQuarantined(self._pb.StepKey(plan_id=plan_id, step_key=step_key)).quarantined

    def clear_quarantine(self) -> int:
        return self._stub.ClearQuarantine(self._pb.Empty()).n

    def close(self) -> None:
        self._ch.close()


def make_store(local_path: str):
    """GrpcStore when STORE_ADDR is set (Go store-gateway running), else LocalStore (ADR-015)."""
    addr = os.environ.get("STORE_ADDR")
    return GrpcStore(addr) if addr else LocalStore(local_path)


# Backward-compatible alias: the offline test suite and existing imports use `Store`.
Store = LocalStore
