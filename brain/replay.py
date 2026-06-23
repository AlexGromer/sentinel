"""Sentinel — minimal replay runner (M2).

Executes a frozen plan's steps in order against a (possibly drifted) target, healing broken
click locators via the HealingEngine. This is the M2 "minimal replay": NO M3 trust layer
(plan_hash hard-abort, golden baselines, structured exit codes, flake quarantine). Its purpose
is to exercise self-healing offline. See docs/M2_CONTRACT.md.

Navigate steps are rebased from the plan's original base to the replay --target's base so a plan
explored on site/ can replay against site-v2/.
"""
import hashlib
import json
import os

from .state import normalize_url


def _dom_hash(aria: str) -> str:
    """M2: page-scoped structural hash (subtree-scoping is M3)."""
    return hashlib.sha256((aria or "").encode()).hexdigest()[:16]


def run_replay(ex, store, heal, plan: dict, new_target: str, run_dir: str) -> dict:
    """Replay `plan` against `new_target`, healing broken locators. Returns the heal report."""
    steps = plan.get("steps", [])
    old_base = normalize_url(plan.get("target_url", "")).rsplit("/", 1)[0] + "/"
    new_base = normalize_url(new_target).rsplit("/", 1)[0] + "/"
    report = {"plan_id": plan.get("plan_id"), "old_base": old_base, "new_base": new_base, "steps": []}

    ex.call("initialize")
    for s in steps:
        kind = s.get("action_type")
        rec = {"step_id": s.get("step_id"), "type": kind, "intent": s.get("intent")}

        if kind == "navigate":
            tgt = (s.get("target") or "").replace(old_base, new_base)
            ex.call("browser.navigate", url=tgt)
            rec["url"] = tgt
            rec["outcome"] = "ok"
        else:  # click
            primary = s.get("locator") or {}
            page_path = normalize_url(ex.call("browser.currentUrl").get("url", ""))
            count = ex.call("browser.probe", locator=primary).get("count", 0) if primary else 0
            if count == 1:
                ex.call("browser.click", locator=primary)
                rec["outcome"] = "ok"
                rec["locator"] = primary
            else:
                # primary locator broke -> heal
                snap = ex.call("browser.snapshot")
                inter = ex.call("browser.interactives").get("elements", [])
                ctx = {"step": s.get("step_id"), "semantic_id": s.get("semantic_id"),
                       "page_path": page_path, "intent": s.get("intent"),
                       "attempted_locator": primary, "alternatives": s.get("alternatives") or [],
                       "dom_hash": _dom_hash(snap.get("ariaSnapshot", "")), "interactives": inter}
                h = heal.heal(ctx)
                if h.get("outcome") in ("auto_healed", "flagged", "cache_hit") and h.get("locator"):
                    ex.call("browser.click", locator=h["locator"])
                    rec["outcome"] = "healed"
                    rec["locator"] = h["locator"]
                    rec["heal"] = {k: h.get(k) for k in ("strategy", "confidence", "outcome")}
                else:
                    rec["outcome"] = "failed"
                    rec["heal"] = h
        report["steps"].append(rec)

    report["healed"] = sum(1 for r in report["steps"] if r["outcome"] == "healed")
    report["failed"] = sum(1 for r in report["steps"] if r["outcome"] == "failed")
    with open(os.path.join(run_dir, "heal-report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report
