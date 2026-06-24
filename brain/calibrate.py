"""Sentinel — heal calibration report (M4, ADR-008 foundation).

Reads the append-only `healing_audit` table and summarizes outcome counts by strategy + a
confidence histogram. Full precision/recall vs human-verified outcomes is wired once the human
gate lands; M4 establishes the data + reporting foundation.
"""


def calibrate(store, threshold: float = 0.85, cold_start: float = 0.90) -> dict:
    rows = store.db.execute(
        "SELECT strategy, outcome, confidence FROM healing_audit").fetchall()
    by_strategy = {}
    hist = {"<0.60": 0, "0.60-0.85": 0, ">=0.85": 0}
    for strategy, outcome, confidence in rows:
        d = by_strategy.setdefault(strategy or "unknown", {})
        d[outcome] = d.get(outcome, 0) + 1
        c = confidence or 0.0
        bucket = "<0.60" if c < 0.60 else ("0.60-0.85" if c < 0.85 else ">=0.85")
        hist[bucket] += 1
    return {
        "threshold": threshold,
        "cold_start_threshold": cold_start,
        "total_attempts": len(rows),
        "by_strategy": by_strategy,
        "confidence_histogram": hist,
        "note": "precision/recall vs human-verified outcomes pending the human gate (ADR-008)",
    }
