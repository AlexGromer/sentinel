"""Sentinel — interim brain-local persistence for healed locators + audit (M2).

TEMPORARY (ADR-012): the brain writes SQLite directly here. M2b moves all writes behind the
Go store-gateway over gRPC, restoring the single-writer invariant (ADR-007). The DB lives under
state/ (git-ignored). Values are opaque JSON strings (the locator dict), serialized by the caller.
"""
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
"""


class Store:
    """Thin SQLite wrapper. `now` is injectable for deterministic tests."""

    def __init__(self, path: str, now=None) -> None:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self._now = now or time.time

    def lookup(self, page_path: str, semantic_id: str, dom_subtree_hash: str):
        cur = self.db.execute(
            "SELECT strategy,value,confidence,status FROM healed_locators "
            "WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=? AND status='active'",
            (page_path, semantic_id, dom_subtree_hash))
        r = cur.fetchone()
        return {"strategy": r[0], "value": r[1], "confidence": r[2], "status": r[3]} if r else None

    def evict_stale(self, page_path: str, semantic_id: str, current_hash: str) -> None:
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

    def close(self) -> None:
        self.db.close()
