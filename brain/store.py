"""Sentinel — interim brain-local persistence (SQLite) for M2 healing + M3 trust layer.

TEMPORARY (ADR-012): the brain writes SQLite directly. M2b moves all writes behind the Go
store-gateway over gRPC, restoring the single-writer invariant (ADR-007). The DB lives under
state/ (git-ignored). Locator values are opaque JSON strings serialized by the caller.

Tables:
- healed_locators / healing_audit  — M2 self-healing (see brain/healing.py)
- golden_snapshots                 — M3 dual a11y+screenshot baselines (page-keyed), ADR-006/013
- step_failures                    — M3 AUT-SHA-gated flake quarantine
"""
import json
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


class Store:
    """Thin SQLite wrapper. `now` is injectable for deterministic tests."""

    def __init__(self, path: str, now=None) -> None:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self._now = now or time.time

    # ---- M2: healed locators -------------------------------------------------
    def lookup(self, page_path, semantic_id, dom_subtree_hash):
        cur = self.db.execute(
            "SELECT strategy,value,confidence,status FROM healed_locators "
            "WHERE page_path=? AND semantic_id=? AND dom_subtree_hash=? AND status='active'",
            (page_path, semantic_id, dom_subtree_hash))
        r = cur.fetchone()
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

    # ---- M3: golden baselines (immutable except via `baseline update`) -------
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

    # ---- M3: AUT-SHA-gated flake quarantine ---------------------------------
    def record_step(self, plan_id, step_key, passed: bool, aut_sha: str) -> bool:
        """Record a step outcome; return whether the step is now quarantined.

        A failure counts toward flakiness only while the AUT sha is unchanged (an app change
        resets the window). Quarantine at >=3 failures in the last 5; clear on 3 straight passes.
        """
        row = self.db.execute(
            "SELECT last5,last_aut_sha,quarantined FROM step_failures WHERE plan_id=? AND step_key=?",
            (plan_id, step_key)).fetchone()
        last5 = json.loads(row[0]) if row and row[0] else []
        quarantined = bool(row[2]) if row else False
        if row and row[1] != aut_sha:   # app under test changed -> reset the flake window
            last5 = []
            quarantined = False
        last5 = (last5 + [1 if passed else 0])[-5:]
        fails = sum(1 for x in last5 if x == 0)
        if fails >= 3:
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
