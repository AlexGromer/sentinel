"""Sentinel brain entrypoint — dispatches explore (M1) or replay+heal (M2).

Config via env (set by agentctl): TARGET_URL, RUN_ID, ARTIFACT_DIR, PW_EXECUTOR_CMD,
RUN_MODE (explore|replay), PLANNER (heuristic|llm), COVERAGE_TARGET, MAX_STEPS,
PLAN_FILE (replay), HEAL_LLM (0|1). See docs/M1_CONTRACT.md and docs/M2_CONTRACT.md.

Exit codes (M2, pre-M3): 0 = gate met, 1 = failure, 2 = bad invocation.
"""
import json
import os
import pathlib
import sys
import traceback

from langgraph.checkpoint.sqlite import SqliteSaver

from .executor import Executor, log
from .graph import build_graph
from .planner import HeuristicPlanner, LLMPlanner
from .state import normalize_url, semantic_id


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
        with SqliteSaver.from_conn_string(ckpt) as saver:
            app = build_graph(ex, planner, tx_write).compile(checkpointer=saver)
            final = app.invoke(init_state,
                               config={"recursion_limit": max(60, max_steps * 8),
                                       "configurable": {"thread_id": run_id}})
        ex.call("browser.traceStop", path=trace_path)
        ex.call("shutdown")

        steps = final.get("exploration_plan", [])
        cov = final.get("coverage_achieved", 0.0)
        ph = final.get("plan_hash", "")
        print("=" * 60)
        print(f"EXPLORE COMPLETE — {len(steps)} steps, coverage={cov:.2f}, plan_hash={ph[:16]}")
        for s in steps:
            print(f"  #{s['step_id']:>2} {s['action_type']:<9} {s['intent']}")
        print("=" * 60)
        log(f"DONE steps={len(steps)} coverage={cov:.2f} plan_hash={ph[:12]}")
        plan_file = out / "plan.json"
        trace = pathlib.Path(trace_path)
        ok = plan_file.exists() and len(steps) >= 5 and trace.exists() and trace.stat().st_size > 0
        return 0 if ok else 1
    finally:
        tx.close()


def _run_replay(ex, run_id, out, target, plan_file, use_llm) -> int:
    """M2 minimal replay: execute a frozen plan against `target`, healing broken locators."""
    from .store import Store
    from .healing import HealingEngine
    from .replay import run_replay

    if not plan_file or not pathlib.Path(plan_file).exists():
        log(f"FATAL: --plan file not found: {plan_file}")
        return 2
    plan = json.loads(pathlib.Path(plan_file).read_text())
    trace_path = str((out / "trace.zip").resolve())
    store = Store(str(pathlib.Path("state") / "locators.db"))
    heal = HealingEngine(ex, store, run_id, use_llm=use_llm)
    log(f"replay: plan={plan_file} target={target} heal_llm={use_llm}")
    try:
        report = run_replay(ex, store, heal, plan, target, str(out))
        ex.call("browser.traceStop", path=trace_path)
        ex.call("shutdown")
        print("=" * 60)
        print(f"REPLAY COMPLETE — {len(report['steps'])} steps, "
              f"healed={report['healed']}, failed={report['failed']}")
        for r in report["steps"]:
            extra = ""
            if r["outcome"] == "healed":
                extra = f" via {r['heal'].get('strategy')} (conf {r['heal'].get('confidence')})"
            print(f"  #{r['step_id']:>2} {r['type']:<9} {r['outcome']}{extra}")
        print("=" * 60)
        ok = report["failed"] == 0 and pathlib.Path(trace_path).exists()
        return 0 if ok else 1
    finally:
        store.close()


def main() -> int:
    target = os.environ.get("TARGET_URL")
    run_id = os.environ.get("RUN_ID", "local")
    artifact_dir = os.environ.get("ARTIFACT_DIR", f"./runs/{run_id}")
    pw_cmd = os.environ.get("PW_EXECUTOR_CMD")
    run_mode = os.environ.get("RUN_MODE", "explore")
    planner_name = os.environ.get("PLANNER", "heuristic")
    coverage_target = float(os.environ.get("COVERAGE_TARGET", "0.85"))
    max_steps = int(os.environ.get("MAX_STEPS", "40"))
    plan_file = os.environ.get("PLAN_FILE", "")
    use_llm = os.environ.get("HEAL_LLM", "0") == "1"
    if not target:
        log("FATAL: TARGET_URL not set")
        return 2
    if not pw_cmd:
        log("FATAL: PW_EXECUTOR_CMD not set")
        return 2

    out = pathlib.Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    log(f"run_id={run_id} mode={run_mode}")

    ex = Executor(pw_cmd)
    rc = 1
    try:
        if run_mode == "replay":
            rc = _run_replay(ex, run_id, out, target, plan_file, use_llm)
        else:
            rc = _run_explore(ex, run_id, out, target, planner_name, coverage_target, max_steps)
    except Exception:
        traceback.print_exc()
        rc = 1
    finally:
        ex.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
