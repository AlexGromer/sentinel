"""Sentinel — replay runner + M3 trust layer.

Executes a frozen plan against a (possibly drifted) target, self-healing broken click locators
(M2) and enforcing the M3 trust layer: plan_hash hard-abort, dual golden baselines (a11y +
screenshot), AUT-SHA-gated flake quarantine, and structured exit codes. See docs/M3_CONTRACT.md.

Modes:
- replay  : verify plan integrity, execute+heal, golden-diff, return exit 0/1/2/3.
- baseline: replay a trusted plan and CAPTURE goldens (the only golden mutation path; ADR-006).

Exit codes: 0 pass · 1 step failure (non-quarantined) · 2 golden regression (non-quarantined) ·
3 integrity hard-abort — plan_hash mismatch (nothing executed) OR golden HMAC mismatch (#24).

ADR-013: heal and golden-diff coexist — a healed step still executes AND its page is still
golden-diffed. M3 note: quarantine suppresses a step's contribution to exit 1; golden regressions
(exit 2) always count (a real app change must not be hidden by a flaky-locator quarantine).
"""
import hashlib
import json
import os

from . import agui
from .eventlog import log
from .state import normalize_url, canonical_plan_hash
from .store import GoldenIntegrityError


def _emit(event_type: str, run_id: str, **data) -> None:
    """Best-effort AG-UI emission for the replay path (M14 tail 2; docs/M14_CONTRACT.md §2/§7).

    The graph modes emit via graph.py's `_agui`; replay cannot import that (graph.py imports FROM
    replay — a cycle), so this is the replay-local twin: additive stdout only, UNCONDITIONAL, and
    swallowed on failure so it can never break a replay. A run_id of "" (no wiring / older caller)
    is still emitted — the control-API keys by run_id but an empty one is harmless observability."""
    try:
        agui.emit(event_type, run_id, **data)
    except Exception as e:
        log("system.agui_emit_failed", error=e)


def _a11y_hash(aria: str) -> str:
    return hashlib.sha256((aria or "").encode()).hexdigest()


def _env_flag(name: str) -> bool:
    """Truthy-string env flag (1/true/yes/on, case-insensitive). Unset/empty -> False."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Non-negative int from env; a missing, unparseable or negative value falls back to `default`.

    Falling back rather than raising is deliberate: a typo in a CI variable must not turn a passing
    replay into a crash. `0` is a legitimate explicit value and means "off"."""
    try:
        v = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return v if v >= 0 else default


def _basename(url: str) -> str:
    p = normalize_url(url)
    return p.rsplit("/", 1)[-1] or p


def _app_faults(ex) -> dict:
    """ADR-072: ask the EXECUTOR for its tally of the application's own faults.

    Why the executor and not the log: those lines go to stderr, and `brain/executor.py` inherits the
    executor's stderr rather than piping it — so the brain never saw them. The only process that parsed
    them was control-api's log sink, and by the time it has counted, the run has already exited with a
    verdict. Asking the emitter is what makes the number available INSIDE the run.

    Fail-open by construction: an older executor without the method, or a dead one, yields an empty
    tally. A run must not fail because we could not count how badly the application behaved."""
    try:
        r = ex.call("browser.appFaults") or {}
    except Exception as e:
        log("system.app_faults_unavailable", error=e)
        return {}
    counts = {k: int(v) for k, v in (r.get("counts") or {}).items() if v}
    if not counts:
        return {}
    # `errors` is the subset a build would reasonably gate on: the page threw, or it logged an error, or
    # a request failed / answered 4xx-5xx. A console WARNING and a dialog are reported but not counted as
    # errors — gating on warnings would make the feature unusable on any real application.
    err_codes = ("app.js_error", "app.console_error", "app.request_failed", "app.http_error")
    return {"counts": counts,
            "total": sum(counts.values()),
            "errors": sum(counts.get(c, 0) for c in err_codes),
            "capped": bool(r.get("capped")),
            "cap": r.get("cap")}


def _drift_entry(step: dict, attempted: dict, h: dict, page_path: str) -> dict:
    """One row of the drift report (ADR-071): what changed, where, and by which CLASS.

    The class is the whole point, because the two kinds are different news and `healed` conflated them:

      rebind   — the healed strategy was FROZEN WITH THE PLAN (`alternatives[]`). Same element, another
                 key: the testid changed but role+name still finds it. This is repairing the TEST, and it
                 is what self-healing exists for.
      reground — the strategy was NOT in the frozen plan, so it was produced at heal time from the page as
                 it is NOW (`css` from the LLM re-ground, `visual` from set-of-marks). It may well be the
                 right element — and it may be a different element that merely looks right. Nothing in the
                 pipeline verifies identity, so this class must be readable at a glance rather than
                 averaged into a count.

    Deriving the class from membership in `alternatives[]` rather than from a hardcoded strategy list is
    deliberate: the real question is "was this key frozen with the plan?", and an authored or imported plan
    may legitimately carry a `css` alternative — in which case using it IS a rebind."""
    frozen = {a.get("strategy") for a in (step.get("alternatives") or []) if isinstance(a, dict)}
    strategy = h.get("strategy")
    return {"step": step.get("step_id"),
            "semantic_id": step.get("semantic_id"),
            # The human handle: what the reader recognises on screen. Falls back through the fields a
            # step actually carries, because `intent` is optional on imported plans.
            "name": step.get("intent") or step.get("name") or step.get("semantic_id") or "",
            "page": page_path,
            "kind": "rebind" if strategy in frozen else "reground",
            "strategy": strategy,
            "confidence": h.get("confidence"),
            "outcome": h.get("outcome"),      # auto_healed | flagged | cache_hit
            "from": attempted,                # the locator the frozen plan asked for
            "to": h.get("locator")}           # the locator that actually worked


def _write(report: dict, run_dir: str) -> None:
    name = "baseline-report.json" if report.get("mode") == "baseline" else "heal-report.json"
    with open(os.path.join(run_dir, name), "w") as f:
        json.dump(report, f, indent=2)


# M9.1 (ADR-026): locator-bearing interaction verbs share the probe -> heal -> act path with click.
LOCATOR_VERBS = ("click", "fill", "type", "select")


def _act(ex, kind: str, locator: dict, s: dict) -> None:
    """Apply the step's verb to `locator` via the matching pw-executor tool.

    Secrets stay as `secretRef` (the env-var NAME) and are resolved ONLY inside pw-executor — the
    literal value is never read, returned, or recorded here. `s` is read ONLY (plan_hash stability).
    """
    if kind == "click":
        ex.call("browser.click", locator=locator)
    elif kind == "fill":
        if s.get("secretRef") is not None:
            ex.call("browser.fill", locator=locator, secretRef=s["secretRef"])
        else:
            ex.call("browser.fill", locator=locator, value=s.get("value", ""))
    elif kind == "type":
        ex.call("browser.type", locator=locator, text=s.get("text", ""), clear=bool(s.get("clear", False)))
    elif kind == "select":
        ex.call("browser.select", locator=locator, value=s.get("value"))
    else:
        raise ValueError(f"_act: unsupported verb {kind!r}")


def _expect_params(s: dict) -> dict:
    """Build browser.expect kwargs from an assert step (read-only)."""
    p = {"condition": s.get("condition")}
    if s.get("locator") is not None:
        p["locator"] = s["locator"]
    if s.get("expected") is not None:
        p["expected"] = s["expected"]
    return p


def run_replay(ex, store, heal, plan: dict, new_target: str, run_dir: str, *,
               baseline: bool = False, aut_version: str = "", ci: bool = False,
               force: bool = False, run_id: str = "") -> dict:
    """Replay `plan` against `new_target`. Returns the report incl. `exit_code`.

    M14 tail 2 (docs/M14_CONTRACT.md §7): this path now emits AG-UI events (run.started · step.progress ·
    heal · verdict) so a replay/baseline run drives the co-pilot's rich timeline instead of a raw log view,
    and counts consecutive heal failures to emit the auto-HITL `hitl_needed` signal at
    SENTINEL_AUTO_HITL_THRESHOLD — mirroring the graph-mode checkpoint node. The *signal* is wired here;
    the live auto-PAUSE (a human takeover mid-replay) rides on the co-pilot takeover machinery, which is
    M9-LIVE — replay has no interrupt/resume today, and graph-mode defers its live auto-pause to M9-LIVE too."""
    steps = plan.get("steps", [])
    plan_id = plan.get("plan_id") or "plan"
    stored = plan.get("plan_hash", "")
    computed = canonical_plan_hash(steps)
    mode = "baseline" if baseline else "replay"
    report = {"plan_id": plan_id, "mode": mode,
              "steps": [], "regressions": [], "healed": 0, "failed": 0,
              # ADR-071: UI drift is a FIRST-CLASS outcome, not a counter. `healed` alone said "something
              # was repaired N times" and left the reader to guess whether the interface moved under the
              # test or a flake was absorbed. `drift` names what changed, where, and by which class.
              "drift": {"rebind": 0, "reground": 0, "elements": []}}
    _emit("run.started", run_id, mode=mode, target=new_target, planner="replay")

    # --- plan integrity hard-abort (ADR-006) -----------------------------------
    if stored and computed != stored and not force:
        report["exit_code"] = 3
        report["reason"] = f"plan_hash mismatch stored={stored[:12]} computed={computed[:12]}"
        _emit("verdict", run_id, verdict="integrity", exit_code=3, healed=0, failed=0)
        _write(report, run_dir)
        return report

    old_base = normalize_url(plan.get("target_url", "")).rsplit("/", 1)[0] + "/"
    new_base = normalize_url(new_target).rsplit("/", 1)[0] + "/"
    report["old_base"], report["new_base"] = old_base, new_base

    # GAP-RISK-009: visual (screenshot) golden regressions are ADVISORY by default. A deployment
    # that has PROVEN screenshot bytes are stable across browser processes can opt the visual diff
    # into gating exit 2 (like a11y) via SENTINEL_VISUAL_AUTHORITATIVE=1. Default-on awaits the
    # real-browser byte-stability proof (M9-LIVE). Read once per run so tests can toggle per-call.
    visual_authoritative = _env_flag("SENTINEL_VISUAL_AUTHORITATIVE")

    ex.call("initialize")
    checked = set()
    failures = 0
    regressions = 0
    # M14 tail 2: auto-HITL signal. `consecutive_heal_failures` counts CONSECUTIVE real (non-quarantined)
    # failures — the same quantity graph-mode tracks (it increments on every failure via heal-routing).
    # threshold=0 (default) disables the emit. See the per-step update after the quarantine block.
    consecutive_heal_failures = 0
    try:
        hitl_threshold = int(os.environ.get("SENTINEL_AUTO_HITL_THRESHOLD", "0"))
    except ValueError:
        # A malformed operator env must not crash the whole replay before a single step runs (this parse
        # sits at the top of the function, unlike graph-mode's mid-run checkpoint) — treat garbage as off.
        log("hitl.threshold_invalid")
        hitl_threshold = 0
    total = len(steps)

    for idx, s in enumerate(steps):
        kind = s.get("action_type")
        step_key = s.get("semantic_id") or str(s.get("step_id"))
        rec = {"step_id": s.get("step_id"), "type": kind, "intent": s.get("intent")}
        passed = True
        _emit("step.progress", run_id, n=idx + 1, total=total, desc=f"{kind}: {s.get('intent') or step_key}")

        if kind == "navigate":
            tgt = (s.get("target") or "").replace(old_base, new_base)
            try:
                ex.call("browser.navigate", url=tgt)
                rec["outcome"], rec["url"] = "ok", tgt
            except Exception as e:
                rec["outcome"], rec["error"], passed = "failed", str(e), False
        elif kind == "assert":
            # M9.1: non-throwing assert; the step passes iff observed ok == expected polarity.
            # No probe/heal — a zero-count locator may be the very point of the assertion.
            expect_ok = bool(s.get("expect_ok", True))
            try:
                res = ex.call("browser.expect", **_expect_params(s))
                ok = bool(res.get("ok", False))
                passed = (ok == expect_ok)
                rec["outcome"] = "ok" if passed else "failed"
                rec["assert"] = {"condition": s.get("condition"), "expect_ok": expect_ok, "observed": ok}
                if res.get("actual") is not None:
                    rec["assert"]["actual"] = res["actual"]
            except Exception as e:
                rec["outcome"], rec["error"], passed = "failed", str(e), False
        elif kind == "press":
            # M9.1: key press; heal only applies to locator-bearing verbs, a global key has none.
            try:
                if s.get("locator"):
                    ex.call("browser.press", locator=s["locator"], key=s.get("key"))
                else:
                    ex.call("browser.press", key=s.get("key"))
                rec["outcome"], rec["key"] = "ok", s.get("key")
            except Exception as e:
                rec["outcome"], rec["error"], passed = "failed", str(e), False
        elif kind in LOCATOR_VERBS:  # click/fill/type/select: probe -> heal -> act(verb)
            primary = s.get("locator") or {}
            page_path = normalize_url(ex.call("browser.currentUrl").get("url", ""))
            count = ex.call("browser.probe", locator=primary).get("count", 0) if primary else 0
            if count == 1:
                try:
                    _act(ex, kind, primary, s)
                    rec["outcome"], rec["locator"] = "ok", primary
                except Exception as e:
                    rec["outcome"], rec["error"], passed = "failed", str(e), False
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
                        _act(ex, kind, h["locator"], s)   # apply the step's VERB to the healed locator
                        rec["outcome"], rec["locator"] = "healed", h["locator"]
                        rec["heal"] = {k: h.get(k) for k in ("strategy", "confidence", "outcome")}
                        # ADR-071: record WHAT drifted, WHERE and HOW — not just that a heal happened.
                        d = _drift_entry(s, primary, h, page_path)
                        rec["heal"]["kind"] = d["kind"]
                        rec["heal"]["from"], rec["heal"]["to"] = d["from"], d["to"]
                        report["drift"][d["kind"]] += 1
                        report["drift"]["elements"].append(d)
                        # Two literal calls rather than one with a computed code: the catalogue gate
                        # cannot vouch for a code it does not see as a literal, and it is right not to —
                        # a dynamically built code silently reverts to raw English in the UI.
                        _dargs = {"element": d["name"], "step": d["step"], "strategy": d["strategy"],
                                  "confidence": round(float(d["confidence"] or 0), 2)}
                        if d["kind"] == "rebind":
                            log("heal.drift_rebind", **_dargs)
                        else:
                            log("heal.drift_reground", **_dargs)
                        report["healed"] += 1
                        _emit("heal", run_id, step=s.get("step_id"), strategy=h.get("strategy"),
                              confidence=h.get("confidence"), ok=True)
                    except Exception as e:
                        rec["outcome"], rec["error"], rec["heal"], passed = "failed", str(e), h, False
                        _emit("heal", run_id, step=s.get("step_id"), strategy=h.get("strategy"),
                              confidence=h.get("confidence"), ok=False)
                else:
                    rec["outcome"], rec["heal"], passed = "failed", h, False
                    _emit("heal", run_id, step=s.get("step_id"), strategy=h.get("strategy"),
                          confidence=h.get("confidence"), ok=False)
        else:
            rec["outcome"], rec["error"], passed = "failed", f"unknown action_type: {kind}", False

        # --- flake quarantine accounting (suppresses exit-1 contribution) ------
        quarantined = store.record_step(plan_id, step_key, passed, aut_version)
        if not passed:
            if quarantined:
                rec["quarantined"] = True
            else:
                failures += 1

        # --- auto-HITL signal (M14 tail 2) -------------------------------------
        # Tracks CONSECUTIVE real failures (any kind — a broken navigate/assert is the agent stuck just
        # like a heal miss), matching graph-mode's `consecutive_heal_failures` (which increments on every
        # failure via heal-routing, resets on a pass). A passed step OR a quarantined flake resets it (a
        # known-flaky step is not "stuck"); a real, non-quarantined failure extends it. On threshold, emit
        # hitl_needed — the co-pilot shows the "take control" banner. Actually pausing replay is M9-LIVE.
        if passed or quarantined:
            consecutive_heal_failures = 0
        else:
            consecutive_heal_failures += 1
            if hitl_threshold > 0 and consecutive_heal_failures >= hitl_threshold:
                _emit("hitl_needed", run_id, reason="consecutive_heal_failures",
                      count=consecutive_heal_failures)

        # --- golden capture / diff: once per page, at FIRST landing -------------
        # Symmetry: baseline AND replay both snapshot a page on first arrival, so the compared
        # states match (a button clicked later must not shift the golden). a11y-hash always drives
        # exit 2 (deterministic). screenshot-hash regression is ADVISORY by default (cross-process
        # byte-stable screenshots aren't guaranteed — GAP-RISK-009); it gates exit 2 only when
        # SENTINEL_VISUAL_AUTHORITATIVE=1 (visual_authoritative).
        pkey = _basename(ex.call("browser.currentUrl").get("url", ""))
        if pkey not in checked:
            checked.add(pkey)
            a11y = _a11y_hash(ex.call("browser.snapshot").get("ariaSnapshot", ""))
            shot = ex.call("browser.screenshotHash").get("hash", "")
            if baseline:
                store.save_golden(pkey, a11y, shot)
                rec["golden"] = "saved:" + pkey
            else:
                try:
                    g = store.get_golden(pkey)
                except GoldenIntegrityError as e:
                    # #24: a tampered/forged golden is an integrity failure — hard-abort like a
                    # plan_hash mismatch (exit 3), never silently trust the forged baseline.
                    report["exit_code"] = 3
                    report["reason"] = str(e)
                    # This early return bypasses the normal `report["failed"] = failures` at the tail, so
                    # set it HERE too — otherwise the persisted heal-report.json says failed=0 while the
                    # AG-UI verdict (below) says failed=N for the same aborted run (they must agree).
                    report["failed"] = failures
                    _emit("verdict", run_id, verdict="integrity", exit_code=3,
                          healed=report["healed"], failed=report["failed"])
                    _write(report, run_dir)
                    return report
                if g:
                    a_diff = g["a11y_hash"] != a11y
                    s_diff = g["screenshot_hash"] != shot
                    if a_diff or s_diff:
                        visual_label = "visual" if visual_authoritative else "visual(advisory)"
                        kinds = (["a11y"] if a_diff else []) + ([visual_label] if s_diff else [])
                        gate = a_diff or (s_diff and visual_authoritative)
                        report["regressions"].append({"page": pkey, "kinds": kinds, "exit2": gate})
                        rec["regression"] = kinds
                        if gate:
                            regressions += 1   # a11y always; visual only when authoritative (GAP-RISK-009)

        report["steps"].append(rec)

    report["failed"] = failures
    # ADR-071: drift becomes a verdict STATE always, and a red build only when asked.
    #
    # Why the state is unconditional: a run that needed five heals is not the same event as a run that
    # needed none, and until now the two were indistinguishable at the verdict — `exit 0`, both. The
    # reader has to be able to see "passed, but the interface moved" without opening a report.
    #
    # Why the exit code stays 0 by default: healing exists so that a cosmetic DOM change does not stop a
    # pipeline. Making drift red out of the box would turn every renamed testid into a broken build and
    # teach teams to switch the feature off — the opposite of the goal.
    #
    # Why exit 1 and not a new code when the threshold IS crossed: the code catalogue (0/1/2/3/-1) is a
    # published contract, and a sixth value would break consumers that switch on it. Of the existing
    # codes, 1 — "the test found a problem" — is the honest one: the test did find something, namely that
    # the interface changed. Exit 2 was rejected because its label says "golden/visual regression", and
    # a re-bound locator is not that; reusing it would make the label lie.
    drift_total = report["drift"]["rebind"] + report["drift"]["reground"]
    fail_on = _env_int("SENTINEL_FAIL_ON_HEAL", 0)   # 0 = off; N = fail when drifted elements >= N
    drift_fails = bool(fail_on) and drift_total >= fail_on
    # ADR-072: the application's own faults reach the verdict. Before this, a run could report exit 0
    # "PASSED" while the page threw exceptions and answered 5xx for the whole run — the events existed,
    # in a log file nobody opens when the build is green.
    faults = _app_faults(ex)
    if faults:
        report["app_faults"] = faults
        log("app.faults_summary", total=faults["total"], errors=faults["errors"])
    app_fail_on = _env_int("SENTINEL_FAIL_ON_APP_ERRORS", 0)   # 0 = off (default): report, do not gate
    app_fails = bool(app_fail_on) and faults.get("errors", 0) >= app_fail_on
    if app_fails:
        report["app_faults"]["failed_build"] = True
        report["app_faults"]["threshold"] = app_fail_on
    if baseline:
        report["exit_code"] = 0
    else:
        report["exit_code"] = 2 if regressions else (1 if (failures or drift_fails or app_fails) else 0)
    if drift_fails:
        report["drift"]["failed_build"] = True
        report["drift"]["threshold"] = fail_on
    from . import budget  # M15.1: per-run token totals (heal LLM) -> persistResult ingests tokens_* + cost_usd
    report["tokens"] = budget.tracker().summary()
    report["models"] = {"heal": getattr(getattr(heal, "_backend", None), "model", None)}
    # M14 tail 2: the REAL structured exit code (0/1/2/3), unlike graph-mode's best-effort verdict.
    _verdict = {0: "pass", 1: "problem", 2: "regression", 3: "integrity"}.get(report["exit_code"], "problem")
    # ADR-071: a pass that needed healing is its own state. Reported on the verdict frame, so the co-pilot
    # timeline and any AG-UI consumer see it without reading heal-report.json.
    # Precedence when a run passes but is not clean: the APPLICATION misbehaving outranks our own test
    # having drifted. A tester who sees both wants the app fault first — it is their bug, whereas drift
    # is our maintenance. Both remain visible in the report either way.
    if _verdict == "pass" and faults.get("errors"):
        _verdict = "pass_with_app_faults"
    elif _verdict == "pass" and drift_total:
        _verdict = "pass_with_drift"
    # And when a threshold is what reddened the build, SAY WHICH. Plain "problem" sends the reader looking
    # for a failed step that does not exist — a support burden this feature would otherwise create itself.
    # Only when nothing else failed: a real step failure keeps the generic word, because then the step IS
    # the story.
    elif _verdict == "problem" and not failures:
        if app_fails:
            _verdict = "problem_app_faults"
        elif drift_fails:
            _verdict = "problem_drift"
    # NOTE (PR-2): `ResultRecord.verdict` in the store is derived INDEPENDENTLY on the Go side from the
    # exit code (`cmd/control-api/main.go::verdictEnum` -> pass|problem|regression|integrity), so the
    # store still sees only the coarse four. Carrying these states across is part of the surfaces PR.
    report["verdict"] = _verdict
    if drift_total:
        log("heal.drift_summary", total=drift_total, rebind=report["drift"]["rebind"],
            reground=report["drift"]["reground"])
    _emit("verdict", run_id, verdict=_verdict, exit_code=report["exit_code"],
          healed=report["healed"], failed=failures,
          drift=drift_total, rebind=report["drift"]["rebind"],
          reground=report["drift"]["reground"],
          app_faults=faults.get("total", 0), app_errors=faults.get("errors", 0))
    _write(report, run_dir)
    return report
