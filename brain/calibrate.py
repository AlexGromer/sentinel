"""Sentinel — heal calibration report (M4, ADR-008 foundation).

Reads the append-only `healing_audit` table and summarizes outcome counts by strategy + a
confidence histogram. Full precision/recall vs human-verified outcomes is wired once the human
gate lands; M4 establishes the data + reporting foundation.
"""


def calibrate(store, threshold: float = 0.85, cold_start: float = 0.90) -> dict:
    rows = store.audit_rows()
    by_strategy = {}
    hist = {"<0.60": 0, "0.60-0.85": 0, ">=0.85": 0}
    # ADR-082: the identity verdict of every re-ground, tallied. This is the first signal in the system
    # that is ABOUT whether a heal was right rather than about how confident we declared ourselves —
    # `by_strategy` counts outcomes we chose, and PRIORS are hand-written. It is still not a human
    # label, and the note below keeps saying so; but "how often does a re-ground contradict what the
    # plan froze" is a measurable quantity, and it was not measurable before.
    identity = {"verified": 0, "contradicted": 0, "unverifiable": 0}
    for strategy, outcome, confidence, ident in rows:
        d = by_strategy.setdefault(strategy or "unknown", {})
        d[outcome] = d.get(outcome, 0) + 1
        c = confidence or 0.0
        bucket = "<0.60" if c < 0.60 else ("0.60-0.85" if c < 0.85 else ">=0.85")
        hist[bucket] += 1
        if ident in identity:          # "" = no claim (a re-bind, or a row predating ADR-082)
            identity[ident] += 1
    return {
        "threshold": threshold,
        "cold_start_threshold": cold_start,
        "total_attempts": len(rows),
        "by_strategy": by_strategy,
        "confidence_histogram": hist,
        "identity": identity,
        "note": "precision/recall vs human-verified outcomes pending the human gate (ADR-008)",
    }
