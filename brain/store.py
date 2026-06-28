"""Sentinel — persistence for healing (M2) + trust layer (M3).

Two interchangeable implementations behind one method interface (ADR-015):
- LocalStore  — direct SQLite (interim brain-local; used by the offline test suite and as the
                no-gateway fallback).
- GrpcStore   — thin client to the Go store-gateway over gRPC (production single-writer, ADR-007).

`make_store(local_path)` returns GrpcStore when STORE_ADDR is set, else LocalStore. healing.py /
replay.py / calibrate.py call the same methods on either. `Store` aliases LocalStore (tests import it).
"""
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import sqlite3
import time

# #23 (THREAT_MODEL ❷): gRPC metadata key carrying the per-run store token. Must match the Go
# gateway's store.StoreTokenMDKey. Keys are lowercase per the gRPC/HTTP-2 convention.
STORE_TOKEN_MD_KEY = "x-sentinel-store-token"


class GoldenIntegrityError(Exception):
    """#24 (THREAT_MODEL ❷ / STRIDE-T): a golden_snapshots row failed its HMAC — the row was
    tampered with or the DB was swapped. replay.py converts this to a controlled exit 3."""


def _load_or_create_key(path: str) -> bytes:
    """Return the HMAC key at path, minting a fresh 32-byte key (0600) on first use. The key lives
    beside locators.db; a peer who swaps the DB without the key cannot forge valid MACs (#24)."""
    try:
        with open(path, "rb") as fh:
            b = fh.read()
        if len(b) >= 16:
            return b
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:  # lost a race — read the winner's key
        with open(path, "rb") as fh:
            return fh.read()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _golden_mac(key: bytes, page_key: str, a11y_hash: str, screenshot_hash: str) -> str:
    """HMAC-SHA256 over the integrity-bearing golden fields. Byte-identical to the Go gateway's
    goldenMAC (created_at excluded so Go/Python float formatting can't diverge)."""
    msg = b"\x1f".join(s.encode("utf-8") for s in (page_key, a11y_hash, screenshot_hash))
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


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
  page_key TEXT PRIMARY KEY, a11y_hash TEXT, screenshot_hash TEXT, created_at REAL, mac TEXT
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
        self._ensure_golden_mac_column()  # #24: migrate pre-integrity DBs (no mac column)
        self.db.commit()
        self._now = now or time.time
        key_path = os.path.join(os.path.dirname(path) or ".", "golden.key")
        key_existed = os.path.exists(key_path)
        self._golden_key = _load_or_create_key(key_path)
        # #24: on first key creation (fresh install or upgrade over a pre-#24 DB) MAC the existing
        # rows once (trust-on-first-use); thereafter every read requires a valid MAC, so a NULL/
        # stripped mac is rejected as tampering rather than silently trusted (closes the strip oracle).
        if not key_existed:
            self._backfill_golden_macs()

    def _ensure_golden_mac_column(self) -> None:
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(golden_snapshots)").fetchall()]
        if "mac" not in cols:
            self.db.execute("ALTER TABLE golden_snapshots ADD COLUMN mac TEXT")

    def _backfill_golden_macs(self) -> None:
        rows = self.db.execute(
            "SELECT page_key,a11y_hash,screenshot_hash FROM golden_snapshots "
            "WHERE mac IS NULL OR mac=''").fetchall()
        for pk, a11y, shot in rows:
            self.db.execute("UPDATE golden_snapshots SET mac=? WHERE page_key=?",
                            (_golden_mac(self._golden_key, pk, a11y, shot), pk))
        if rows:
            self.db.commit()

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
        mac = _golden_mac(self._golden_key, page_key, a11y_hash, screenshot_hash)
        self.db.execute(
            "INSERT OR REPLACE INTO golden_snapshots(page_key,a11y_hash,screenshot_hash,created_at,mac) "
            "VALUES(?,?,?,?,?)", (page_key, a11y_hash, screenshot_hash, self._now(), mac))
        self.db.commit()

    def get_golden(self, page_key):
        r = self.db.execute(
            "SELECT a11y_hash,screenshot_hash,mac FROM golden_snapshots WHERE page_key=?",
            (page_key,)).fetchone()
        if not r:
            return None
        a11y, shot, mac = r[0], r[1], r[2]
        # #24: every row must carry a valid MAC (pre-#24 rows were MAC'd once at upgrade). A missing
        # or wrong MAC => stripped / tampered / DB swapped -> fail closed (replay maps this to exit 3).
        want = _golden_mac(self._golden_key, page_key, a11y, shot)
        if not mac or not hmac.compare_digest(want, mac):
            raise GoldenIntegrityError(
                f"golden integrity: missing or invalid MAC for page {page_key!r} (tampered or DB swapped)")
        return {"a11y_hash": a11y, "screenshot_hash": shot}

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


def _trace_interceptor():
    """gRPC client interceptor that injects the current W3C trace context into call metadata
    (M8, ADR-021) so the Go store-gateway's server spans join the brain's trace. No-op when tracing
    isn't configured (inject_context returns an empty carrier)."""
    import collections
    import grpc

    from .otel import inject_context

    class _Details(collections.namedtuple(
            "_Details", ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"))):
        pass

    class _Interceptor(grpc.UnaryUnaryClientInterceptor):
        def intercept_unary_unary(self, continuation, details, request):
            carrier = inject_context({})
            if carrier:
                md = list(details.metadata or []) + [(k.lower(), v) for k, v in carrier.items()]
                details = _Details(details.method, details.timeout, md, details.credentials,
                                   details.wait_for_ready, details.compression)
            return continuation(details, request)

    return _Interceptor()


def _token_interceptor(token: str):
    """gRPC client interceptor that attaches the per-run store token (#23) to every call's metadata
    so the Go store-gateway's TokenAuthInterceptor admits it. Mirrors _trace_interceptor's shape."""
    import collections
    import grpc

    class _Details(collections.namedtuple(
            "_Details", ("method", "timeout", "metadata", "credentials", "wait_for_ready", "compression"))):
        pass

    class _Interceptor(grpc.UnaryUnaryClientInterceptor):
        def intercept_unary_unary(self, continuation, details, request):
            md = list(details.metadata or []) + [(STORE_TOKEN_MD_KEY, token)]
            details = _Details(details.method, details.timeout, md, details.credentials,
                               details.wait_for_ready, details.compression)
            return continuation(details, request)

    return _Interceptor()


class GrpcStore:
    """Thin gRPC client to the Go store-gateway. Same method interface as LocalStore (ADR-015)."""

    def __init__(self, addr: str) -> None:
        import grpc
        from .pb import persistence_pb2 as pbmsg, persistence_pb2_grpc as pbgrpc
        self._pb = pbmsg
        base = grpc.insecure_channel(f"unix:{addr}")
        interceptors = [_trace_interceptor()]            # M8: W3C trace propagation
        token = os.environ.get("STORE_TOKEN", "")
        if token:
            interceptors.append(_token_interceptor(token))  # #23: authN to the gateway
        self._ch = grpc.intercept_channel(base, *interceptors)
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
        import grpc
        try:
            g = self._stub.GetGolden(self._pb.PageKey(page_key=page_key))
        except grpc.RpcError as e:  # #24: the gateway rejects a tampered golden with DATA_LOSS
            if e.code() == grpc.StatusCode.DATA_LOSS:
                raise GoldenIntegrityError(e.details() or "golden integrity: MAC mismatch") from None
            raise
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
