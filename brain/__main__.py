"""Sentinel brain entrypoint — dispatches all modes.

RUN_MODE: explore | replay | baseline | clear-quarantine | export-spec | report | calibrate.
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

from .executor import log, make_executor
from .otel import setup_tracing, span
from .graph import build_graph
from .planner import HeuristicPlanner, LLMPlanner
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


def _run_explore(ex, run_id, out, target, planner_name, coverage_target, max_steps) -> int:
    """M1 autonomous walk: explore the site, converge on coverage, freeze plan.json."""
    trace_path = str((out / "trace.zip").resolve())
    base_origin = normalize_url(target).rsplit("/", 1)[0] + "/"
    planner = LLMPlanner() if planner_name == "llm" else HeuristicPlanner()
    log(f"explore: planner={planner.name} coverage_target={coverage_target} target={target}")
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
            "current_url": target, "page_model": {},
            "exploration_plan": [init], "plan_hash": "", "current_step": 1,
            "interactive_seen": [], "interactive_exercised": [], "visited_paths": [],
            "nav_frontier": [], "coverage_achieved": 0.0, "exploration_complete": False,
            "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}], "errors": [],
        }
        ckpt = str((out / "checkpoint.db").resolve())
        with _checkpointer(ckpt) as saver:
            app = build_graph(ex, planner, tx_write).compile(checkpointer=saver)
            final = app.invoke(init_state,
                               config={"recursion_limit": max(60, max_steps * 8),
                                       "configurable": {"thread_id": run_id}})
        ex.call("browser.traceStop", path=trace_path)
        ex.call("shutdown")
        steps = final.get("exploration_plan", [])
        cov, ph = final.get("coverage_achieved", 0.0), final.get("plan_hash", "")
        print("=" * 60)
        print(f"EXPLORE COMPLETE — {len(steps)} steps, coverage={cov:.2f}, plan_hash={ph[:16]}")
        for s in steps:
            print(f"  #{s['step_id']:>2} {s['action_type']:<9} {s['intent']}")
        print("=" * 60)
        plan_file = out / "plan.json"
        trace = pathlib.Path(trace_path)
        ok = plan_file.exists() and len(steps) >= 5 and trace.exists() and trace.stat().st_size > 0
        return 0 if ok else 1
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
    setup_tracing()

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
                              os.environ.get("PLANNER", "heuristic"),
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
