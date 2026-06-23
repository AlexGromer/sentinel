"""Sentinel M1 brain entrypoint — autonomous walk.

Wires the pw-executor client, the selected planner, and the LangGraph StateGraph; performs
the initial navigation to the target; runs the explore loop to convergence; and writes
plan.json + llm-transcript.jsonl + trace.zip into ARTIFACT_DIR.

Config via env (set by agentctl): TARGET_URL, RUN_ID, ARTIFACT_DIR, PW_EXECUTOR_CMD,
PLANNER (heuristic|llm), COVERAGE_TARGET, MAX_STEPS. See ../docs/M1_CONTRACT.md.
Exit 0 only if the M1 gate holds (plan.json with >=5 steps + trace.zip present).
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


def main() -> int:
    target = os.environ.get("TARGET_URL")
    run_id = os.environ.get("RUN_ID", "local")
    artifact_dir = os.environ.get("ARTIFACT_DIR", f"./runs/{run_id}")
    pw_cmd = os.environ.get("PW_EXECUTOR_CMD")
    planner_name = os.environ.get("PLANNER", "heuristic")
    coverage_target = float(os.environ.get("COVERAGE_TARGET", "0.85"))
    max_steps = int(os.environ.get("MAX_STEPS", "40"))
    if not target:
        log("FATAL: TARGET_URL not set")
        return 2
    if not pw_cmd:
        log("FATAL: PW_EXECUTOR_CMD not set")
        return 2

    out = pathlib.Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace_path = str((out / "trace.zip").resolve())
    base_origin = normalize_url(target).rsplit("/", 1)[0] + "/"
    planner = LLMPlanner() if planner_name == "llm" else HeuristicPlanner()
    log(f"run_id={run_id} planner={planner.name} coverage_target={coverage_target} target={target}")

    ex = Executor(pw_cmd)
    tx = open(out / "llm-transcript.jsonl", "w")

    def tx_write(rec: dict) -> None:
        tx.write(json.dumps(rec) + "\n")
        tx.flush()

    rc = 1
    try:
        ex.call("initialize")
        ex.call("browser.navigate", url=target)
        # Record the initial navigation as step 1 so plan.json is self-contained.
        init = {"step_id": 1, "intent": f"navigate to target {target}",
                "semantic_id": semantic_id(normalize_url(target), "navigate", ""),
                "action_type": "navigate", "target": normalize_url(target),
                "locator": None, "is_milestone": True}
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
            final = app.invoke(
                init_state,
                config={"recursion_limit": max(60, max_steps * 8),
                        "configurable": {"thread_id": run_id}},
            )
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
        log(f"DONE steps={len(steps)} coverage={cov:.2f} plan_hash={ph[:12]} "
            f"seen={len(final.get('interactive_seen', []))} "
            f"exercised={len(final.get('interactive_exercised', []))}")
        if final.get("errors"):
            log("errors:", final["errors"])

        plan_file = out / "plan.json"
        trace = pathlib.Path(trace_path)
        ok = (plan_file.exists() and len(steps) >= 5
              and trace.exists() and trace.stat().st_size > 0)
        rc = 0 if ok else 1
        if not ok:
            log(f"GATE NOT MET: plan.json={plan_file.exists()} steps={len(steps)} "
                f"trace={trace.exists()}")
    except Exception:
        traceback.print_exc()
        rc = 1
    finally:
        tx.close()
        ex.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
