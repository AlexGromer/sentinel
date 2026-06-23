"""Sentinel — replay runner + M3 trust layer.

Executes a frozen plan against a (possibly drifted) target, self-healing broken click locators
(M2) and enforcing the M3 trust layer: plan_hash hard-abort, dual golden baselines (a11y +
screenshot), AUT-SHA-gated flake quarantine, and structured exit codes. See docs/M3_CONTRACT.md.

Modes:
- replay  : verify plan integrity, execute+heal, golden-diff, return exit 0/1/2/3.
- baseline: replay a trusted plan and CAPTURE goldens (the only golden mutation path; ADR-006).

Exit codes: 0 pass · 1 step failure (non-quarantined) · 2 golden regression (non-quarantined) ·
3 plan integrity (plan_hash mismatch) — hard-abort, nothing executed.

ADR-013: heal and golden-diff coexist — a healed step still executes AND its page is still
golden-diffed. M3 note: quarantine suppresses a step's contribution to exit 1; golden regressions
(exit 2) always count (a real app change must not be hidden by a flaky-locator quarantine).
"""
import hashlib
import json
import os

from .state import normalize_url, canonical_plan_hash


def _a11y_hash(aria: str) -> str:
    return hashlib.sha256((aria or "").encode()).hexdigest()


def _basename(url: str) -> str:
    p = normalize_url(url)
    return p.rsplit("/", 1)[-1] or p


def _write(report: dict, run_dir: str) -> None:
    name = "baseline-report.json" if report.get("mode") == "baseline" else "heal-report.json"
    with open(os.path.join(run_dir, name), "w") as f:
        json.dump(report, f, indent=2)


def run_replay(ex, store, heal, plan: dict, new_target: str, run_dir: str, *,
               baseline: bool = False, aut_version: str = "", ci: bool = False,
               force: bool = False) -> dict:
    """Replay `plan` against `new_target`. Returns the report incl. `exit_code`."""
    steps = plan.get("steps", [])
    plan_id = plan.get("plan_id") or "plan"
    stored = plan.get("plan_hash", "")
    computed = canonical_plan_hash(steps)
    report = {"plan_id": plan_id, "mode": "baseline" if baseline else "replay",
              "steps": [], "regressions": [], "healed": 0, "failed": 0}

    # --- plan integrity hard-abort (ADR-006) -----------------------------------
    if stored and computed != stored and not force:
        report["exit_code"] = 3
        report["reason"] = f"plan_hash mismatch stored={stored[:12]} computed={computed[:12]}"
        _write(report, run_dir)
        return report

    old_base = normalize_url(plan.get("target_url", "")).rsplit("/", 1)[0] + "/"
    new_base = normalize_url(new_target).rsplit("/", 1)[0] + "/"
    report["old_base"], report["new_base"] = old_base, new_base

    ex.call("initialize")
    checked = set()
    failures = 0
    regressions = 0

    for s in steps:
        kind = s.get("action_type")
        step_key = s.get("semantic_id") or str(s.get("step_id"))
        rec = {"step_id": s.get("step_id"), "type": kind, "intent": s.get("intent")}
        passed = True

        if kind == "navigate":
            tgt = (s.get("target") or "").replace(old_base, new_base)
            try:
                ex.call("browser.navigate", url=tgt)
                rec["outcome"], rec["url"] = "ok", tgt
            except Exception as e:
                rec["outcome"], rec["error"], passed = "failed", str(e), False
        else:  # click
            primary = s.get("locator") or {}
            page_path = normalize_url(ex.call("browser.currentUrl").get("url", ""))
            count = ex.call("browser.probe", locator=primary).get("count", 0) if primary else 0
            if count == 1:
                ex.call("browser.click", locator=primary)
                rec["outcome"], rec["locator"] = "ok", primary
            else:
                snap = ex.call("browser.snapshot")
                inter = ex.call("browser.interactives").get("elements", [])
                ctx = {"step": s.get("step_id"), "semantic_id": s.get("semantic_id"),
                       "page_path": page_path, "intent": s.get("intent"),
                       "attempted_locator": primary, "alternatives": s.get("alternatives") or [],
                       "dom_hash": _a11y_hash(snap.get("ariaSnapshot", ""))[:16], "interactives": inter}
                h = heal.heal(ctx)
                if h.get("outcome") in ("auto_healed", "flagged", "cache_hit") and h.get("locator"):
                    try:
                        ex.call("browser.click", locator=h["locator"])
                        rec["outcome"], rec["locator"] = "healed", h["locator"]
                        rec["heal"] = {k: h.get(k) for k in ("strategy", "confidence", "outcome")}
                        report["healed"] += 1
                    except Exception as e:
                        rec["outcome"], rec["error"], rec["heal"], passed = "failed", str(e), h, False
                else:
                    rec["outcome"], rec["heal"], passed = "failed", h, False

        # --- flake quarantine accounting (suppresses exit-1 contribution) ------
        quarantined = store.record_step(plan_id, step_key, passed, aut_version)
        if not passed:
            if quarantined:
                rec["quarantined"] = True
            else:
                failures += 1

        # --- golden capture / diff: once per page, at FIRST landing -------------
        # Symmetry: baseline AND replay both snapshot a page on first arrival, so the compared
        # states match (a button clicked later must not shift the golden). a11y-hash drives exit 2
        # (deterministic); screenshot-hash regression is ADVISORY in M3 (cross-process byte-stable
        # screenshots aren't guaranteed — GAP-RISK-009).
        pkey = _basename(ex.call("browser.currentUrl").get("url", ""))
        if pkey not in checked:
            checked.add(pkey)
            a11y = _a11y_hash(ex.call("browser.snapshot").get("ariaSnapshot", ""))
            shot = ex.call("browser.screenshotHash").get("hash", "")
            if baseline:
                store.save_golden(pkey, a11y, shot)
                rec["golden"] = "saved:" + pkey
            else:
                g = store.get_golden(pkey)
                if g:
                    a_diff = g["a11y_hash"] != a11y
                    s_diff = g["screenshot_hash"] != shot
                    if a_diff or s_diff:
                        kinds = (["a11y"] if a_diff else []) + (["visual(advisory)"] if s_diff else [])
                        report["regressions"].append({"page": pkey, "kinds": kinds, "exit2": a_diff})
                        rec["regression"] = kinds
                        if a_diff:
                            regressions += 1   # only a11y regressions gate exit 2 in M3

        report["steps"].append(rec)

    report["failed"] = failures
    if baseline:
        report["exit_code"] = 0
    else:
        report["exit_code"] = 2 if regressions else (1 if failures else 0)
    _write(report, run_dir)
    return report
