"""Sentinel brain entrypoint — dispatches all modes.

RUN_MODE: explore | replay | baseline | clear-quarantine | export-spec | report | calibrate | mcp-server | chat.
Config via env (set by agentctl). See docs/M1–M4_CONTRACT.md.
Exit codes (M3): 0 pass · 1 step failure · 2 golden regression · 3 plan integrity / bad invocation.
"""
import contextlib
import json
import os
import pathlib
import sys
import traceback

from langgraph.checkpoint.sqlite import SqliteSaver

from . import runcontrol
from .executor import log, make_executor
from .otel import setup_tracing, span
from .graph import build_graph
from .planner import make_planner
from .state import normalize_url, semantic_id

_STORE_PATH = str(pathlib.Path("state") / "locators.db")


@contextlib.contextmanager
def _checkpointer(ckpt_path: str):
    """LangGraph checkpointer (M5-3): Postgres when CHECKPOINT_DSN is set (K3s multi-runner) — a
    near drop-in for the per-run SQLite file otherwise. Postgres needs a one-time setup()."""
    dsn = os.environ.get("CHECKPOINT_DSN")
    if dsn:
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield saver
    else:
        with SqliteSaver.from_conn_string(ckpt_path) as saver:
            yield saver


def _write_scenario(out, run_id, target, scenario_steps, unmatched, is_describe) -> int:
    """M9.2b (ADR-028): freeze scenario.json (standalone, renumbered from 1) + reconcile-report.json
    (describe). Exit: describe with any unmatched -> 1; zero grounded steps -> 1; else 0."""
    from .state import canonical_plan_hash
    sc = [{**s, "step_id": i + 1} for i, s in enumerate(scenario_steps)]
    obj = {"plan_id": f"{run_id}-scenario", "plan_hash": canonical_plan_hash(sc), "target_url": target,
           "run_mode": "scenario", "mode": ("describe" if is_describe else "goal"),
           "unmatched": len(unmatched), "steps": sc}
    with open(out / "scenario.json", "w") as f:
        json.dump(obj, f, indent=2)
    if is_describe:
        with open(out / "reconcile-report.json", "w") as f:
            json.dump({"target_url": target, "grounded": len(sc), "unmatched": unmatched}, f, indent=2)
    print(f"SCENARIO — {len(sc)} grounded steps, {len(unmatched)} unmatched -> {out}/scenario.json"
          + (" + reconcile-report.json" if is_describe else ""))
    if is_describe and unmatched:
        return 1
    return 0 if sc else 1


def _resume_through_takeovers(app, final, cfg, rc, run_id):
    """M9.8 F4 (ADR-054): drive a graph through any operator takeovers.

    If the graph paused for a takeover, app.invoke() returns with `__interrupt__` (brain/graph.py:
    checkpoint). Wait for the orchestrator's Return — poll() drops from "takeover" back to "continue" —
    then resume the SAME checkpointer thread with Command(resume=...). Loop until the graph completes
    (no more interrupts) or the takeover exceeds SENTINEL_TAKEOVER_TIMEOUT seconds (default 1800; 0 =
    wait indefinitely). A pure no-op on the common path (nothing interrupted) and whenever no
    orchestrator is wired (poll() is a no-op → "continue" → `final` never carries `__interrupt__`)."""
    import time
    from langgraph.types import Command
    try:
        timeout = float(os.environ.get("SENTINEL_TAKEOVER_TIMEOUT", "1800"))
    except ValueError:
        timeout = 1800.0
    while final.get("__interrupt__"):
        log("explore: paused for operator takeover; awaiting return (Command(resume) on Return)")
        deadline = None if timeout <= 0 else time.monotonic() + timeout
        while rc.poll(run_id, "checkpoint") == runcontrol.TAKEOVER:
            if deadline is not None and time.monotonic() > deadline:
                log("explore: takeover exceeded SENTINEL_TAKEOVER_TIMEOUT -> resuming anyway")
                break
            time.sleep(0.5)
        final = app.invoke(Command(resume={"returned": True}), config=cfg)
    return final


def _run_explore(ex, run_id, out, target, coverage_target, max_steps) -> int:
    """M1 autonomous walk: explore the site, converge on coverage, freeze plan.json.

    M9.2b (ADR-028): goal/describe modes run a deterministic heuristic walk (phase 1) + a one-shot
    scenario head (phase 2) that authors a grounded scenario.json over the complete site map."""
    trace_path = str((out / "trace.zip").resolve())
    base_origin = normalize_url(target).rsplit("/", 1)[0] + "/"
    goal = os.environ.get("GOAL", "").strip()            # M9.2a goal-mode
    describe = os.environ.get("DESCRIBE", "").strip()    # M9.2b describe-mode
    if goal and describe:
        log("FATAL: GOAL and DESCRIBE are mutually exclusive -> exit 3")
        return 3
    from .planner import HeuristicPlanner, GoalPlanner, DescribePlanner
    if goal:
        planner, scenario_head = HeuristicPlanner(), GoalPlanner(goal)
    elif describe:
        planner, scenario_head = HeuristicPlanner(), DescribePlanner(describe)
    else:
        planner, scenario_head = make_planner(), None    # pure explore (heuristic|llm)
    log(f"explore: planner={planner.name} scenario={getattr(scenario_head, 'name', None)} "
        f"goal={goal!r} describe={describe!r} coverage_target={coverage_target} target={target}")
    tx = open(out / "llm-transcript.jsonl", "w")

    def tx_write(rec: dict) -> None:
        tx.write(json.dumps(rec) + "\n")
        tx.flush()

    try:
        ex.call("initialize")
        ex.call("browser.navigate", url=target)
        init = {"step_id": 1, "intent": f"navigate to target {target}",
                "semantic_id": semantic_id(normalize_url(target), "navigate", ""),
                "action_type": "navigate", "target": normalize_url(target),
                "locator": None, "alternatives": None, "is_milestone": True}
        init_state = {
            "run_id": run_id, "run_mode": "explore", "target_url": target, "base_origin": base_origin,
            "coverage_target": coverage_target, "max_steps": max_steps, "artifact_dir": str(out),
            "goal": goal, "describe": describe,
            "site_map": {}, "phase": "explore", "scenario_steps": [], "scenario_unmatched": [],
            "current_url": target, "page_model": {},
            "exploration_plan": [init], "plan_hash": "", "current_step": 1,
            "interactive_seen": [], "interactive_exercised": [], "visited_paths": [],
            "nav_frontier": [], "coverage_achieved": 0.0, "exploration_complete": False,
            "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}], "errors": [],
        }
        ckpt = str((out / "checkpoint.db").resolve())
        rc = runcontrol.make_client()  # M8/M9.8 F4: shared by the graph's checkpoint gate + the resume loop
        cfg = {"recursion_limit": max(60, max_steps * 8), "configurable": {"thread_id": run_id}}
        with _checkpointer(ckpt) as saver:
            app = build_graph(ex, planner, tx_write, scenario_head=scenario_head, rc=rc).compile(checkpointer=saver)
            final = app.invoke(init_state, config=cfg)
            # M9.8 F4 (ADR-054): if the run paused for an operator takeover, await Return and resume.
            final = _resume_through_takeovers(app, final, cfg, rc, run_id)
        ex.call("browser.traceStop", path=trace_path)
        ex.call("shutdown")
        steps = final.get("exploration_plan", [])
        cov, ph = final.get("coverage_achieved", 0.0), final.get("plan_hash", "")
        scenario_steps = final.get("scenario_steps", [])
        scenario_unmatched = final.get("scenario_unmatched", [])
        print("=" * 60)
        print(f"EXPLORE COMPLETE — {len(steps)} steps, coverage={cov:.2f}, plan_hash={ph[:16]}")
        for s in steps:
            print(f"  #{s['step_id']:>2} {s['action_type']:<9} {s['intent']}")
        print("=" * 60)
        if scenario_head is not None:    # M9.2b: goal/describe -> scenario.json is the deliverable
            return _write_scenario(out, run_id, target, scenario_steps, scenario_unmatched, bool(describe))
        plan_file = out / "plan.json"
        trace = pathlib.Path(trace_path)
        ok = plan_file.exists() and len(steps) >= 5 and trace.exists() and trace.stat().st_size > 0
        return 0 if ok else 1
    finally:
        tx.close()


def _conversations_store_path() -> str:
    """M9.10 (ADR-048): the SHARED, NON-ephemeral checkpoint store for multi-turn chat threads — distinct
    from the per-run ARTIFACT_DIR/checkpoint.db (that one is keyed by a unique run_id, so it can't be
    resumed). CHECKPOINT_DSN (Postgres) overrides this in `_checkpointer`; otherwise it is SQLite at
    SENTINEL_CONVERSATIONS_DB (an override for tests / relocation) or the air-gapped default
    state/conversations.db. The thread (keyed by thread_id=conversation_id) is NOT deleted at turn end."""
    override = os.environ.get("SENTINEL_CONVERSATIONS_DB", "").strip()
    if override:
        pathlib.Path(override).parent.mkdir(parents=True, exist_ok=True)
        return override
    pathlib.Path("state").mkdir(parents=True, exist_ok=True)
    return str((pathlib.Path("state") / "conversations.db").resolve())


class _NoBrowser:
    """M9.10: a guard executor for the warm refine path. Turn-N resumes straight into the `scenario` node
    (conditional entry), which never drives the browser — so this is never called. If a warm turn DOES try
    to reach a browser node, fail loudly rather than silently spawn/hang."""

    def call(self, method, **kwargs):
        raise RuntimeError(f"refine turn must not drive the browser (called {method!r})")

    def close(self):
        pass


def _project_chat(conversation_id: str, target: str, final: dict) -> None:
    """M13 (ADR-050): emit the browsable `chats` projection to the store-gateway (best-effort). Reads the
    accumulated conversation from the final graph state; a no-op when STORE_ADDR is unset (offline). This
    is an index, NOT a duplicate of the checkpointer thread (which stays the source of truth)."""
    from .store import make_chat_projector
    from .graph import _user_turns, _rolling_summary
    projector = make_chat_projector()
    if not projector:
        return
    try:
        turns = _user_turns(final.get("messages"))
        projector.upsert_chat(conversation_id=conversation_id, last_target=target,
                              turn_count=len(turns), last_goal=(turns[-1] if turns else ""),
                              summary=_rolling_summary(turns))
    finally:
        projector.close()


def _run_chat(run_id, out, conversation_id, target, coverage_target, max_steps) -> int:
    """M9.10 (ADR-048): stateful multi-turn authoring. One brain process per turn; conversation memory is
    the shared checkpointer keyed by thread_id=conversation_id (state/conversations.db or CHECKPOINT_DSN).

    Turn-1 (COLD — no thread state) explores + authors WITH a browser. Turn-N (WARM — a persisted
    site_map) RESUMES straight into the `scenario` node (conditional entry, brain/graph.py:route_entry)
    and re-authors over the persisted map using the prior conversation as refine context — NO browser.
    The deliverable each turn is scenario.json (renumbered from 1)."""
    goal = os.environ.get("GOAL", "").strip()
    describe = os.environ.get("DESCRIBE", "").strip()
    if goal and describe:
        log("FATAL: GOAL and DESCRIBE are mutually exclusive -> exit 3")
        return 3
    if not goal and not describe:
        log("FATAL: chat mode needs GOAL or DESCRIBE -> exit 3")
        return 3
    from .planner import HeuristicPlanner, GoalPlanner, DescribePlanner
    scenario_head = GoalPlanner(goal) if goal else DescribePlanner(describe)
    planner = HeuristicPlanner()    # the explore walk stays deterministic; authoring is the scenario head
    user_msg = {"role": "user", "content": goal or describe}
    cfg = {"recursion_limit": max(60, max_steps * 8), "configurable": {"thread_id": conversation_id}}
    rc = runcontrol.make_client()  # M9.8 F4: shared by the graph's checkpoint gate + the cold-turn resume loop
    tx = open(out / "llm-transcript.jsonl", "w")

    def tx_write(rec: dict) -> None:
        tx.write(json.dumps(rec) + "\n")
        tx.flush()

    with span("sentinel.run", run_id=run_id, mode="chat", conversation_id=conversation_id,
              store=("postgres" if os.environ.get("CHECKPOINT_DSN") else "sqlite")):
        try:
            with _checkpointer(_conversations_store_path()) as saver:
                # Peek the thread WITHOUT a browser: a warm turn already has a persisted site_map.
                # get_state always returns a StateSnapshot — on a brand-new thread its .values is {} (not
                # None), so the guard + .get keep a cold turn-1 from being mistaken for a resume.
                probe = build_graph(_NoBrowser(), planner, tx_write,
                                    scenario_head=scenario_head, rc=rc).compile(checkpointer=saver)
                snap = probe.get_state(cfg)
                warm = bool(snap and snap.values and snap.values.get("site_map"))
                if warm:
                    log(f"chat: RESUME conversation={conversation_id} "
                        f"(warm — refine over persisted site map, no browser)")
                    final = probe.invoke({"messages": [user_msg], "goal": goal, "describe": describe,
                                          "run_id": run_id, "artifact_dir": str(out)}, config=cfg)
                else:
                    if not target:
                        log("FATAL: chat cold-start needs TARGET_URL -> exit 2")
                        return 2
                    log(f"chat: COLD conversation={conversation_id} (explore + author) target={target}")
                    trace_path = str((out / "trace.zip").resolve())
                    base_origin = normalize_url(target).rsplit("/", 1)[0] + "/"
                    ex = make_executor(os.environ["PW_EXECUTOR_CMD"])   # only the cold turn spawns a browser
                    try:
                        ex.call("initialize")
                        ex.call("browser.navigate", url=target)
                        init = {
                            "run_id": run_id, "run_mode": "chat", "target_url": target,
                            "base_origin": base_origin, "coverage_target": coverage_target,
                            "max_steps": max_steps, "artifact_dir": str(out),
                            "goal": goal, "describe": describe, "messages": [user_msg],
                            "site_map": {}, "phase": "explore", "scenario_steps": [],
                            "scenario_unmatched": [], "current_url": target, "page_model": {},
                            "exploration_plan": [{"step_id": 1, "intent": f"navigate to target {target}",
                                                  "semantic_id": semantic_id(normalize_url(target), "navigate", ""),
                                                  "action_type": "navigate", "target": normalize_url(target),
                                                  "locator": None, "alternatives": None, "is_milestone": True}],
                            "plan_hash": "", "current_step": 1, "interactive_seen": [],
                            "interactive_exercised": [], "visited_paths": [], "nav_frontier": [],
                            "coverage_achieved": 0.0, "exploration_complete": False,
                            "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}], "errors": [],
                        }
                        app = build_graph(ex, planner, tx_write,
                                          scenario_head=scenario_head, rc=rc).compile(checkpointer=saver)
                        final = app.invoke(init, config=cfg)
                        # M9.8 F4 (ADR-054): a takeover during the cold turn pauses here too — await Return
                        # and resume BEFORE tearing down the browser (mirror _run_explore).
                        final = _resume_through_takeovers(app, final, cfg, rc, run_id)
                        ex.call("browser.traceStop", path=trace_path)
                        ex.call("shutdown")
                    finally:
                        ex.close()
                scenario_steps = final.get("scenario_steps", [])
                scenario_unmatched = final.get("scenario_unmatched", [])
                eff_target = target or final.get("target_url", "")
                _project_chat(conversation_id, eff_target, final)  # M13: browsable chats projection (best-effort)
                print("=" * 60)
                print(f"CHAT TURN COMPLETE — conversation={conversation_id}, "
                      f"{len(scenario_steps)} grounded, {len(scenario_unmatched)} unmatched")
                print("=" * 60)
                return _write_scenario(out, run_id, eff_target, scenario_steps,
                                       scenario_unmatched, bool(describe))
        finally:
            tx.close()


def _run_replay(ex, run_id, out, target, plan_file, use_llm, *, baseline, aut_version, ci, force) -> int:
    """M2/M3 replay or baseline-capture. Returns the structured exit code from the trust layer."""
    from .store import make_store
    from .healing import HealingEngine
    from .replay import run_replay

    if not plan_file or not pathlib.Path(plan_file).exists():
        log(f"FATAL: --plan file not found: {plan_file}")
        return 2
    try:
        plan = json.loads(pathlib.Path(plan_file).read_text())
    except Exception as e:
        log(f"PLAN INTEGRITY: cannot parse plan ({e}) -> exit 3")
        return 3
    if not target:
        target = plan.get("target_url", "")
    if ci and force:
        log("FATAL: --force-replay is not allowed under --ci")
        return 3
    # M9.1/GAP-RISK-010: fail closed — a secretRef fill must never run while tracing is on (it would
    # leak the credential into trace.zip). The login-as-test workflow sets PW_NO_TRACE=1.
    if os.environ.get("PW_NO_TRACE") != "1" and any(
            s.get("secretRef") is not None for s in plan.get("steps", [])):
        log("FATAL: plan has a secretRef fill but PW_NO_TRACE != '1' "
            "(would leak the secret into trace.zip) -> exit 3")
        return 3
    trace_path = str((out / "trace.zip").resolve())
    store = make_store(_STORE_PATH)
    log(f"store={'grpc@' + os.environ['STORE_ADDR'] if os.environ.get('STORE_ADDR') else 'local'}")
    heal = HealingEngine(ex, store, run_id, use_llm=use_llm,
                         use_visual=os.environ.get("HEAL_VISUAL") == "1")
    log(f"{'baseline' if baseline else 'replay'}: plan={plan_file} target={target} "
        f"aut={aut_version or '-'} ci={ci}")
    try:
        report = run_replay(ex, store, heal, plan, target, str(out),
                            baseline=baseline, aut_version=aut_version, ci=ci, force=force)
        # M9.1 (ADR-026): persist auth after a successful login-as-test run (before traceStop/shutdown).
        save_state = os.environ.get("STORAGE_STATE_SAVE")
        if save_state and report.get("exit_code") == 0:
            try:
                pathlib.Path(save_state).parent.mkdir(parents=True, exist_ok=True)
                ex.call("browser.saveStorageState", path=save_state)
                log(f"storageState saved -> {save_state}")
            except Exception as e:
                log("saveStorageState error:", e)
        try:
            ex.call("browser.traceStop", path=trace_path)
            ex.call("shutdown")
        except Exception:
            pass
        code = report.get("exit_code", 1)
        print("=" * 60)
        if report.get("reason"):
            print(f"ABORT — {report['reason']}")
        head = "BASELINE" if baseline else "REPLAY"
        print(f"{head} COMPLETE — {len(report['steps'])} steps, healed={report.get('healed', 0)}, "
              f"failed={report.get('failed', 0)}, regressions={len(report.get('regressions', []))}, "
              f"exit={code}")
        for r in report["steps"]:
            extra = ""
            if r["outcome"] == "healed":
                extra = f" via {r['heal'].get('strategy')} (conf {r['heal'].get('confidence')})"
            if r.get("regression"):
                extra += f"  [GOLDEN REGRESSION: {','.join(r['regression'])}]"
            if r.get("quarantined"):
                extra += "  [quarantined]"
            print(f"  #{r['step_id']:>2} {r['type']:<9} {r['outcome']}{extra}")
        print("=" * 60)
        return code
    finally:
        store.close()


def _run_export_spec(out, plan_file, spec_out) -> int:
    """M4: emit a Playwright .spec.ts from a frozen plan (no browser)."""
    from .exporter import export_spec
    if not plan_file or not pathlib.Path(plan_file).exists():
        log(f"FATAL: --plan not found: {plan_file}")
        return 2
    plan = json.loads(pathlib.Path(plan_file).read_text())
    dest = spec_out or str(out / "exported.spec.ts")
    pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(dest).write_text(export_spec(plan))
    print(f"exported Playwright spec -> {dest}")
    return 0


def _run_report(run_dir) -> int:
    """M4: generate report.html + report.json + metrics.prom from a run's heal-report.json."""
    from .report import generate
    if not (pathlib.Path(run_dir) / "heal-report.json").exists():
        log(f"FATAL: heal-report.json not found in {run_dir}")
        return 2
    generate(run_dir)
    gw = os.environ.get("PROM_PUSHGATEWAY")
    if gw:
        from .report import push_metrics
        try:
            rep = json.loads((pathlib.Path(run_dir) / "heal-report.json").read_text())
            push_metrics(rep, gw)
            log(f"metrics pushed -> {gw}")
        except Exception as e:
            log("pushgateway error:", e)
    print(f"report -> {run_dir}/report.html, report.json, metrics.prom")
    return 0


def _run_calibrate() -> int:
    """M4: summarize healing_audit (outcome counts + confidence histogram)."""
    from .store import make_store
    from .calibrate import calibrate
    st = make_store(_STORE_PATH)
    try:
        c = calibrate(st)
        pathlib.Path("state").mkdir(parents=True, exist_ok=True)
        pathlib.Path("state/calibration.json").write_text(json.dumps(c, indent=2))
        print(json.dumps(c, indent=2))
        return 0
    finally:
        st.close()


def _run_clear_quarantine() -> int:
    from .store import make_store
    st = make_store(_STORE_PATH)
    try:
        print(f"cleared {st.clear_quarantine()} step-failure record(s)")
        return 0
    finally:
        st.close()


def main() -> int:
    run_mode = os.environ.get("RUN_MODE", "explore")
    run_id = os.environ.get("RUN_ID", "local")
    out = pathlib.Path(os.environ.get("ARTIFACT_DIR", f"./runs/{run_id}"))
    out.mkdir(parents=True, exist_ok=True)
    # #26 (THREAT_MODEL ❹): trace.zip under the run dir captures AUT DOM/screenshots (possible PII).
    # Restrict the dir to the owner so other local users can't read it. agentctl also sets 0700 up
    # front; this covers brain-direct runs (MCP server / tests) where agentctl isn't in the path.
    try:
        os.chmod(out, 0o700)
    except OSError:
        pass
    setup_tracing()
    # M9.2a (ADR-027): a RunConfig YAML may supply mode/goal/planner/budgets (precedence flag > file > default).
    run_config = os.environ.get("RUN_CONFIG")
    if run_config:
        from .runconfig import load_run_config, apply_run_config
        try:
            apply_run_config(load_run_config(run_config))
            log(f"run-config applied: {run_config}")
        except Exception as e:
            log(f"FATAL: bad --run-config {run_config}: {e}")
            return 3

    # --- no-browser modes (M3/M4) --------------------------------------------
    if run_mode == "clear-quarantine":
        return _run_clear_quarantine()
    if run_mode == "export-spec":
        return _run_export_spec(out, os.environ.get("PLAN_FILE", ""), os.environ.get("SPEC_OUT", ""))
    if run_mode == "report":
        return _run_report(os.environ.get("REPORT_DIR", str(out)))
    if run_mode == "calibrate":
        return _run_calibrate()

    # --- browser modes -------------------------------------------------------
    target = os.environ.get("TARGET_URL")
    pw_cmd = os.environ.get("PW_EXECUTOR_CMD")
    if not pw_cmd:
        log("FATAL: PW_EXECUTOR_CMD not set")
        return 2
    if run_mode == "mcp-server":
        # M7 (ADR-020): expose the brain as an MCP server; the host drives + supplies the model.
        from .server import run_mcp_server
        return run_mcp_server(out, run_id)
    if run_mode == "chat":
        # M9.10 (ADR-048): stateful multi-turn authoring. Dispatched BEFORE make_executor — a warm
        # refine turn must not spawn a browser; _run_chat creates the executor lazily on a cold turn only.
        conversation_id = os.environ.get("SENTINEL_CONVERSATION_ID", "").strip()
        if not conversation_id:
            log("FATAL: chat mode requires SENTINEL_CONVERSATION_ID -> exit 2")
            return 2
        return _run_chat(run_id, out, conversation_id, target,
                         float(os.environ.get("COVERAGE_TARGET", "0.85")),
                         int(os.environ.get("MAX_STEPS", "40")))
    if run_mode == "explore" and not target:
        log("FATAL: TARGET_URL not set")
        return 2

    log(f"run_id={run_id} mode={run_mode} transport={os.environ.get('MCP_TRANSPORT', 'jsonrpc')}")
    ex = make_executor(pw_cmd)
    rc = 1
    _run_span = span("sentinel.run", run_id=run_id, mode=run_mode,
                     transport=os.environ.get("MCP_TRANSPORT", "jsonrpc"),
                     store=("grpc" if os.environ.get("STORE_ADDR") else "local"))
    _run_span.__enter__()
    try:
        if run_mode in ("replay", "baseline"):
            rc = _run_replay(
                ex, run_id, out, target or "",
                os.environ.get("PLAN_FILE", ""),
                os.environ.get("HEAL_LLM", "0") == "1",
                baseline=(run_mode == "baseline"),
                aut_version=os.environ.get("AUT_VERSION", ""),
                ci=os.environ.get("CI", "0") == "1",
                force=os.environ.get("FORCE_REPLAY", "0") == "1")
        else:
            rc = _run_explore(ex, run_id, out, target,
                              float(os.environ.get("COVERAGE_TARGET", "0.85")),
                              int(os.environ.get("MAX_STEPS", "40")))
    except Exception:
        traceback.print_exc()
        rc = 1
    finally:
        ex.close()
        _run_span.__exit__(None, None, None)
    return rc


if __name__ == "__main__":
    sys.exit(main())
