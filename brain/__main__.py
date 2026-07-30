"""Sentinel brain entrypoint — dispatches all modes.

RUN_MODE: explore | replay | baseline | clear-quarantine | export-spec | import | revisions | report | calibrate | mcp-server | chat.
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
from .eventlog import log
from .executor import make_executor
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


def _write_scenario(out, run_id, target, scenario_steps, unmatched, is_describe, author_model=None) -> int:
    """M9.2b (ADR-028): freeze scenario.json (standalone, renumbered from 1) + reconcile-report.json
    (describe). Exit: describe with any unmatched -> 1; zero grounded steps -> 1; else 0.

    `models`/`tokens` ride along because a scenario is the artifact people HAND EACH OTHER — it is the
    deliverable of a goal/describe run — and one that cannot say which model authored it, at what token
    cost, is not reproducible by whoever receives it. It carried neither until now."""
    from .state import canonical_plan_hash
    from . import budget
    sc = [{**s, "step_id": i + 1} for i, s in enumerate(scenario_steps)]
    obj = {"plan_id": f"{run_id}-scenario", "plan_hash": canonical_plan_hash(sc), "target_url": target,
           "run_mode": "scenario", "mode": ("describe" if is_describe else "goal"),
           "unmatched": len(unmatched), "steps": sc,
           "models": {"author": author_model}, "tokens": budget.tracker().summary()}
    with open(out / "scenario.json", "w") as f:
        json.dump(obj, f, indent=2)
    # PROD-VERSIONING (ADR-106): when this scenario is a NAMED test (SENTINEL_TEST_ID set — the CI or
    # operator re-authoring "the login test"), append it as a revision so its history, diff and
    # rollback exist. An ad-hoc explore run has no stable test identity and is deliberately NOT
    # versioned — recording it under a run-scoped id would be noise, not history. The store is
    # file-based under state/revisions (authoritative, air-gap-friendly; no network service).
    test_id = os.environ.get("SENTINEL_TEST_ID", "").strip()
    if test_id:
        try:
            from . import revisions
            root = os.environ.get("SENTINEL_REVISIONS_DIR") or os.path.join("state", "revisions")
            rev = revisions.save_revision(root, test_id, obj)
            log("test.revision_saved", test_id=test_id, revision=rev["revision"][:12], new=rev["new"])
            print(f"REVISION — {test_id} @ {rev['revision'][:12]} ({'new' if rev['new'] else 'unchanged'})")
        except Exception as e:  # versioning must never fail the authoring run
            log("test.revision_save_failed", test_id=test_id, error=e)
    if is_describe:
        with open(out / "reconcile-report.json", "w") as f:
            json.dump({"target_url": target, "grounded": len(sc), "unmatched": unmatched}, f, indent=2)
    log("test.scenario_authored", grounded=len(sc), unmatched=len(unmatched))
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
        log("hitl.takeover_paused")
        deadline = None if timeout <= 0 else time.monotonic() + timeout
        while rc.poll(run_id, "checkpoint") == runcontrol.TAKEOVER:
            if deadline is not None and time.monotonic() > deadline:
                log("hitl.takeover_timeout")
                break
            time.sleep(0.5)
        final = app.invoke(Command(resume={"returned": True}), config=cfg)
    return final


def _run_explore(ex, run_id, out, target, coverage_target, max_steps) -> int:
    """M1 autonomous walk: explore the site, converge on coverage, freeze plan.json.

    M9.2b (ADR-028): goal/describe modes run a deterministic heuristic walk (phase 1) + a one-shot
    scenario head (phase 2) that authors a grounded scenario.json over the complete site map."""
    from . import budget  # M15.1: isolate per-run token totals — the mcp-server reuses one process across runs
    budget.tracker().reset()
    trace_path = str((out / "trace.zip").resolve())
    base_origin = normalize_url(target).rsplit("/", 1)[0] + "/"
    goal = os.environ.get("GOAL", "").strip()            # M9.2a goal-mode
    describe = os.environ.get("DESCRIBE", "").strip()    # M9.2b describe-mode
    if goal and describe:
        log("fatal.goal_describe_conflict")
        return 3
    from .planner import HeuristicPlanner, GoalPlanner, DescribePlanner
    if goal:
        planner, scenario_head = HeuristicPlanner(), GoalPlanner(goal)
    elif describe:
        planner, scenario_head = HeuristicPlanner(), DescribePlanner(describe)
    else:
        planner, scenario_head = make_planner(), None    # pure explore (heuristic|llm)
    log("run.explore_config", planner=planner.name, target=target,
        scenario=getattr(scenario_head, "name", None), goal=goal, describe=describe,
        coverage_target=coverage_target)
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
        # ADR-099: this file is DELETED when the run ends (see the finally below). Measured on a dev
        # box: 284 of them, 570 MB — 94% of everything runs/ held, and nothing pruned them, because
        # `sweepTraces` and `sweepLogs` prune traces and logs and no sweeper owns the run directory.
        # It is safe to delete because it is unresumable BY CONSTRUCTION: the thread is keyed by a
        # run_id that is unique per run, which is exactly why multi-turn chat keeps its own shared
        # store (`_conversations_store_path`) instead of reusing this one.
        rc = runcontrol.make_client()  # M8/M9.8 F4: shared by the graph's checkpoint gate + the resume loop
        cfg = {"recursion_limit": max(60, max_steps * 8), "configurable": {"thread_id": run_id}}
        try:
            with _checkpointer(ckpt) as saver:
                app = build_graph(ex, planner, tx_write, scenario_head=scenario_head, rc=rc).compile(checkpointer=saver)
                final = app.invoke(init_state, config=cfg)
                # M9.8 F4 (ADR-054): if the run paused for an operator takeover, await Return and resume.
                final = _resume_through_takeovers(app, final, cfg, rc, run_id)
        finally:
            _discard_checkpoint(ckpt)
        # ADR-084: explore's trace holds the same live application DOM a replay's does, so the same
        # rule applies. The exit code is not known yet here, so the decision is made below, right
        # before it is computed.
        steps = final.get("exploration_plan", [])
        cov, ph = final.get("coverage_achieved", 0.0), final.get("plan_hash", "")
        scenario_steps = final.get("scenario_steps", [])
        scenario_unmatched = final.get("scenario_unmatched", [])
        log("test.explore_complete", steps=len(steps), coverage=f"{cov:.2f}")
        print("=" * 60)
        print(f"EXPLORE COMPLETE — {len(steps)} steps, coverage={cov:.2f}, plan_hash={ph[:16]}")
        for s in steps:
            print(f"  #{s['step_id']:>2} {s['action_type']:<9} {s['intent']}")
        print("=" * 60)
        if scenario_head is not None:    # M9.2b: goal/describe -> scenario.json is the deliverable
            return _write_scenario(out, run_id, target, scenario_steps, scenario_unmatched, bool(describe),
                                   author_model=getattr(scenario_head, "model", None))
        plan_file = out / "plan.json"
        # `trace.exists()` used to be part of this criterion. It asserted a BY-PRODUCT rather than the
        # result — a trace file proves the browser ran, which `len(steps) >= 5` already proves better —
        # and after ADR-084 a clean explore deliberately leaves no trace at all, so keeping it would
        # have made every successful explore report failure.
        ok = plan_file.exists() and len(steps) >= 5
        _stop_trace(ex, trace_path, 0 if ok else 1)
        ex.call("shutdown")
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
        # ADR-109: the projection is written HERE, by the brain, which is why the owner has to travel as
        # a run var. control-api resolves who asked for the run but never touches this row — so without
        # SENTINEL_OWNER every conversation landed unowned, and "each person has their own chats" was
        # true of the schema and false of the data.
        projector.upsert_chat(conversation_id=conversation_id, last_target=target,
                              turn_count=len(turns), last_goal=(turns[-1] if turns else ""),
                              summary=_rolling_summary(turns),
                              owner=os.environ.get("SENTINEL_OWNER", ""))
    finally:
        projector.close()


def _run_chat(run_id, out, conversation_id, target, coverage_target, max_steps) -> int:
    """M9.10 (ADR-048): stateful multi-turn authoring. One brain process per turn; conversation memory is
    the shared checkpointer keyed by thread_id=conversation_id (state/conversations.db or CHECKPOINT_DSN).

    Turn-1 (COLD — no thread state) explores + authors WITH a browser. Turn-N (WARM — a persisted
    site_map) RESUMES straight into the `scenario` node (conditional entry, brain/graph.py:route_entry)
    and re-authors over the persisted map using the prior conversation as refine context — NO browser.
    The deliverable each turn is scenario.json (renumbered from 1)."""
    from . import budget  # M15.1: isolate per-run token totals (server reuses the process across turns)
    budget.tracker().reset()
    goal = os.environ.get("GOAL", "").strip()
    describe = os.environ.get("DESCRIBE", "").strip()
    # ADR-108a: MESSAGE is this turn's text; GOAL/DESCRIBE declare the conversation's objective. They
    # used to be the same thing — every turn arrived as GOAL — so a follow-up was indistinguishable
    # from a new objective and "one goal per conversation" could not be said, never mind enforced.
    message = os.environ.get("MESSAGE", "").strip()
    if goal and describe:
        log("fatal.goal_describe_conflict")
        return 3
    if not goal and not describe and not message:
        log("fatal.chat_no_intent")
        return 3
    from .planner import HeuristicPlanner, GoalPlanner, DescribePlanner
    planner = HeuristicPlanner()    # the explore walk stays deterministic; authoring is the scenario head
    # scenario_head and the turn's message are resolved AFTER the thread is peeked: on a warm turn the
    # objective comes from the pinned chat_intent, not from this request, so neither can be chosen yet.
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
                # Peeked with NO scenario head: this compile only reads state, and choosing a head here
                # would mean choosing it from a request that may not carry the objective at all.
                snap = build_graph(_NoBrowser(), planner, tx_write,
                                   scenario_head=None, rc=rc).compile(checkpointer=saver).get_state(cfg)

                # ---- ADR-108a: the objective belongs to the CONVERSATION ----
                pinned = (snap.values.get("chat_intent") if snap and snap.values else None) or None
                if pinned:
                    # A request may RESTATE the objective (idempotent) but never replace it. Rejected
                    # before any browser or model work, so the refusal costs nothing and says why.
                    if goal and (pinned.get("kind") != "goal" or pinned.get("text") != goal):
                        log("fatal.chat_goal_changed", pinned_kind=pinned.get("kind"),
                            pinned=pinned.get("text"), requested_kind="goal", requested=goal)
                        return 3
                    if describe and (pinned.get("kind") != "describe" or pinned.get("text") != describe):
                        log("fatal.chat_goal_changed", pinned_kind=pinned.get("kind"),
                            pinned=pinned.get("text"), requested_kind="describe", requested=describe)
                        return 3
                    kind, objective = pinned.get("kind", "goal"), pinned.get("text", "")
                else:
                    # First turn — or a conversation started before chat_intent existed, which pins on
                    # the first turn that carries an objective rather than being refused retroactively.
                    if not goal and not describe:
                        log("fatal.chat_no_objective")
                        return 3
                    kind, objective = ("goal", goal) if goal else ("describe", describe)
                intent = {"kind": kind, "text": objective}
                # The turn's instruction is its MESSAGE; a turn that sends none is restating the
                # objective, which is what every turn did before the two were separated.
                turn_text = message or objective
                scenario_head = GoalPlanner(turn_text) if kind == "goal" else DescribePlanner(turn_text)
                user_msg = {"role": "user", "content": turn_text}
                # The authoring head reads state["goal"]/["describe"], so the TURN's text goes there and
                # the objective stays in chat_intent. That keeps authoring byte-identical to before for a
                # turn whose message equals its objective — i.e. every turn that exists today.
                turn_goal = turn_text if kind == "goal" else ""
                turn_describe = turn_text if kind == "describe" else ""

                has_map = bool(snap and snap.values and snap.values.get("site_map"))
                # GAP-M9-19: a warm refine reuses the PERSISTED site map without re-checking the target.
                # SENTINEL_REFINE_REVERIFY=1 forces a re-explore (cold path, with the browser) so a stale
                # map is refreshed — the opt-in staleness mitigation. (Auto-detection needs a live a11y-hash
                # probe on the warm turn, which has no browser → that half is M9-LIVE.)
                reverify = os.environ.get("SENTINEL_REFINE_REVERIFY") == "1"
                if has_map and reverify:
                    log("run.chat_reverify")
                warm = has_map and not reverify
                if warm:
                    log("run.chat_resume", conversation_id=conversation_id)
                    # Compiled with the head chosen from the PINNED objective, which is why the peek
                    # above used none: this is the first point at which the right head is known.
                    warm_app = build_graph(_NoBrowser(), planner, tx_write,
                                           scenario_head=scenario_head, rc=rc).compile(checkpointer=saver)
                    final = warm_app.invoke({"messages": [user_msg], "goal": turn_goal,
                                             "describe": turn_describe, "chat_intent": intent,
                                             "run_id": run_id, "artifact_dir": str(out)}, config=cfg)
                else:
                    if not target:
                        log("fatal.chat_no_target")
                        return 2
                    log("run.chat_cold", conversation_id=conversation_id, target=target)
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
                            "goal": turn_goal, "describe": turn_describe, "messages": [user_msg],
                            "chat_intent": intent,   # ADR-108a: pinned here, on the first turn, for good
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
                        # ADR-084: a cold chat turn is an explore; same rule, and its exit code is
                        # the scenario write below, so keep the trace only when nothing was authored.
                        _stop_trace(ex, trace_path, 0 if final.get("scenario_steps") else 1)
                        ex.call("shutdown")
                    finally:
                        ex.close()
                scenario_steps = final.get("scenario_steps", [])
                scenario_unmatched = final.get("scenario_unmatched", [])
                eff_target = target or final.get("target_url", "")
                _project_chat(conversation_id, eff_target, final)  # M13: browsable chats projection (best-effort)
                log("test.chat_turn_complete", conversation=conversation_id, steps=len(scenario_steps))
                print("=" * 60)
                print(f"CHAT TURN COMPLETE — conversation={conversation_id}, "
                      f"{len(scenario_steps)} grounded, {len(scenario_unmatched)} unmatched")
                print("=" * 60)
                return _write_scenario(out, run_id, eff_target, scenario_steps,
                                       scenario_unmatched, bool(describe),
                                       author_model=getattr(scenario_head, "model", None))
        finally:
            tx.close()


def _discard_checkpoint(ckpt: str) -> None:
    """Delete the per-run LangGraph checkpoint (ADR-099).

    It cannot be resumed: the thread is keyed by a run_id unique to this run, which is precisely why
    multi-turn chat keeps its own shared store rather than reusing this one. So once the graph has
    finished, the file is pure residue — and it was the biggest residue we had. Measured on a dev box
    before this change: 284 files, 570 MB, 94% of everything under runs/, pruned by nothing, because
    the two sweepers that exist own traces and logs and no sweeper owned the run directory.

    In a `finally`, so a crashed run leaves no more behind than a clean one — a failure is exactly when
    an operator is least likely to go looking for stray files.

    Swallows its own errors: teardown must not turn a finished run into a crash, and a checkpoint left
    behind is wasted disk, not a wrong answer. It is still SAID, because "the tool quietly used more
    disk than it admitted" is the shape of the problem being fixed.
    """
    import glob
    removed = 0
    # SQLite may leave -wal/-shm beside the database; deleting the .db alone would leave the pair.
    for path in (ckpt, ckpt + "-wal", ckpt + "-shm"):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            log("system.checkpoint_kept", path=path, error=e)
            return
    if removed:
        log("system.checkpoint_discarded", path=ckpt)


def _stop_trace(ex, trace_path: str, exit_code: int) -> None:
    """Stop tracing, keeping the artifact only when `_keep_trace` says the run is worth a post-mortem.

    Swallows its own errors: teardown must not turn a finished run into a crash, and every call site
    is already past the point where the result is decided."""
    try:
        if _keep_trace(exit_code):
            ex.call("browser.traceStop", path=trace_path)
            _redact_trace(trace_path)
        else:
            ex.call("browser.traceStop")
    except Exception as e:
        log("system.trace_stop_error", error=e)


def _redact_trace(trace_path: str) -> None:
    """Strip typed values and credentials from a kept trace (ADR-098). FAILS CLOSED.

    Runs here, immediately after the archive exists, rather than as part of report generation: the
    report is built later, and a replay started directly (`python -m brain`) never reaches it at all.
    A redaction that depends on how the run was launched is the worst property a security control can
    have.

    ⚠ THE WINDOW IS REAL AND IS NOT CLOSED. Playwright writes the zip itself and offers no hook
    between "bytes hit the disk" and "we can read them", so the raw archive exists for the duration of
    one subprocess. ADR-084 could avoid its window — discarding is a supported option — and this one
    cannot. Saying so is the honest half.

    On ANY failure the trace is DELETED. A trace that could not be redacted is not a degraded
    artifact, it is a leak; keeping it because the cleanup failed would invert the point of the
    cleanup. The run's verdict is untouched either way — this is teardown, and the result is already
    decided.
    """
    import shutil
    import subprocess

    if os.environ.get("SENTINEL_TRACE_RAW") == "1":
        # Opt-in escape hatch for diagnosing the tool itself. Announced every time, because a mode
        # that silently keeps credentials is exactly the thing this function exists to prevent.
        log("system.trace_raw_kept", path=trace_path)
        return
    tool = os.environ.get("SENTINEL_AGENTCTL") or shutil.which("agentctl") or "bin/agentctl"
    try:
        r = subprocess.run([tool, "redact-trace", "--trace", trace_path],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            log("system.trace_redacted", path=trace_path, detail=(r.stdout or "").strip()[:200])
            return
        reason = (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200]
    except Exception as e:                                   # tool missing, timeout, unreadable
        reason = f"{type(e).__name__}: {e}"[:200]
    try:
        os.remove(trace_path)
        log("system.trace_discarded_unredacted", path=trace_path, error=reason)
    except OSError as e:
        # The one case worse than a leak is a leak nobody is told about.
        log("system.trace_leak", path=trace_path, error=f"{reason}; and removal failed: {e}")


def _keep_trace(exit_code: int) -> bool:
    """ADR-084: keep `trace.zip` only when the run did NOT finish clean.

    The trace is the best post-mortem tool we have — and on a GREEN run there is no post-mortem to
    perform, while the file still holds the tested application's live DOM (`input.value` included) and
    request bodies, unredactable because Playwright has no mask API. Keeping it by default meant every
    passing CI run left a copy of someone's application state on disk for the sake of a diagnosis
    nobody was going to make.

    `exit_code != 0` rather than "a step failed": a golden regression exits 2 without any step
    failing, and that is exactly a case a human will want to look at frame by frame.

    `SENTINEL_TRACE_ALWAYS=1` restores the old behaviour for someone debugging a run that passes but
    behaves oddly. `PW_NO_TRACE=1` still wins over both — it means the trace was never recorded.
    """
    return exit_code != 0 or os.environ.get("SENTINEL_TRACE_ALWAYS") == "1"


def _run_replay(ex, run_id, out, target, plan_file, use_llm, *, baseline, aut_version, ci, force) -> int:
    """M2/M3 replay or baseline-capture. Returns the structured exit code from the trust layer."""
    from .store import make_store
    from .healing import HealingEngine
    from .replay import run_replay
    from . import budget  # M15.1: isolate per-run token totals (server reuses the process across runs)
    budget.tracker().reset()

    if not plan_file or not pathlib.Path(plan_file).exists():
        log("fatal.plan_missing", path=plan_file)
        return 3
    try:
        plan = json.loads(pathlib.Path(plan_file).read_text())
    except Exception as e:
        log("fatal.plan_unparseable", error=e)
        return 3
    if not target:
        target = plan.get("target_url", "")
    if ci and force:
        log("fatal.force_replay_in_ci")
        return 3
    # M9.1/GAP-RISK-010: fail closed — a secretRef fill must never run while tracing is on (it would
    # leak the credential into trace.zip). The login-as-test workflow sets PW_NO_TRACE=1.
    if os.environ.get("PW_NO_TRACE") != "1" and any(
            s.get("secretRef") is not None for s in plan.get("steps", [])):
        log("fatal.secret_would_leak_to_trace")
        return 3
    trace_path = str((out / "trace.zip").resolve())
    store = make_store(_STORE_PATH)
    log("run.store_mode",
        store="grpc@" + os.environ["STORE_ADDR"] if os.environ.get("STORE_ADDR") else "local")
    heal = HealingEngine(ex, store, run_id, use_llm=use_llm,
                         use_visual=os.environ.get("HEAL_VISUAL") == "1")
    log("run.replay_config", kind="baseline" if baseline else "replay", plan=plan_file,
        target=target, aut=aut_version or "-", ci=ci)
    try:
        report = run_replay(ex, store, heal, plan, target, str(out),
                            baseline=baseline, aut_version=aut_version, ci=ci, force=force, run_id=run_id)
        # M9.1 (ADR-026): persist auth after a successful login-as-test run (before traceStop/shutdown).
        save_state = os.environ.get("STORAGE_STATE_SAVE")
        if save_state and report.get("exit_code") == 0:
            try:
                pathlib.Path(save_state).parent.mkdir(parents=True, exist_ok=True)
                ex.call("browser.saveStorageState", path=save_state)
                log("system.storage_state_saved", path=save_state)
            except Exception as e:
                log("system.storage_state_error", error=e)
        try:
            # ADR-084: keep the artifact only when the run is worth a post-mortem; otherwise the
            # executor discards the buffered trace and nothing reaches the disk.
            _stop_trace(ex, trace_path, int(report.get("exit_code", 1)))
            ex.call("shutdown")
        except Exception:
            pass
        code = report.get("exit_code", 1)
        head = "BASELINE" if baseline else "REPLAY"
        if report.get("reason"):
            log("test.aborted", reason=report["reason"])
        log("test.summary", kind=head, steps=len(report["steps"]), healed=report.get("healed", 0),
            failed=report.get("failed", 0), regressions=len(report.get("regressions", [])))
        # One event per step, so "which step went wrong" is a filterable record rather than a line of
        # prose. The human console keeps its compact table too — a CLI operator should not have to read
        # structured records to see the outcome at a glance.
        print("=" * 60)
        print(f"{head} COMPLETE — {len(report['steps'])} steps, healed={report.get('healed', 0)}, "
              f"failed={report.get('failed', 0)}, regressions={len(report.get('regressions', []))}, "
              f"exit={code}")
        for r in report["steps"]:
            sid, stype, outcome = r["step_id"], r["type"], r["outcome"]
            if r.get("regression"):
                log("test.step_regression", step=sid, type=stype, what=",".join(r["regression"]))
            elif outcome == "healed":
                log("test.step_healed", step=sid, type=stype,
                    strategy=(r.get("heal") or {}).get("strategy"),
                    confidence=(r.get("heal") or {}).get("confidence"))
            elif outcome in ("failed", "fail", "error"):
                log("test.step_failed", step=sid, type=stype)
            else:
                log("test.step_passed", step=sid, type=stype)
            if r.get("quarantined"):
                log("test.step_quarantined", step=sid, type=stype)
            extra = ""
            if outcome == "healed":
                extra = f" via {(r.get('heal') or {}).get('strategy')} (conf {(r.get('heal') or {}).get('confidence')})"
            if r.get("regression"):
                extra += f"  [GOLDEN REGRESSION: {','.join(r['regression'])}]"
            if r.get("quarantined"):
                extra += "  [quarantined]"
            print(f"  #{sid:>2} {stype:<9} {outcome}{extra}")
        print("=" * 60)
        return code
    finally:
        store.close()


def _revisions_root():
    return os.environ.get("SENTINEL_REVISIONS_DIR") or os.path.join("state", "revisions")


def _run_revisions(out, op, test_id, rev_a, rev_b) -> int:
    """PROD-VERSIONING: the READ surface over brain/revisions (no browser, no network).

    The store has been complete since ADR-106 — append-only history, step-level diff, rollback that
    re-appends rather than deletes — and nothing could read it back. A revision written and
    unreachable is not history; it is a file. So this is deliberately thin: it exposes the existing
    functions and adds no policy of its own.

    Output is JSON on stdout AND an artifact, because both callers are real: a person reading a
    terminal, and control-api relaying it to the hub.
    """
    from . import revisions as R
    root = _revisions_root()
    tid = (test_id or "").strip()
    if not tid:
        log("fatal.revisions_no_test_id")
        return 2
    try:
        if op == "list":
            hist = R.list_revisions(root, tid)
            payload = {"test_id": tid, "head": R.head(root, tid), "revisions": hist}
        elif op == "show":
            rev = rev_a or R.head(root, tid)
            if not rev:
                log("fatal.revisions_unknown", test_id=tid, revision=str(rev_a))
                return 3
            plan = R.get_plan(root, tid, rev)
            if plan is None:
                log("fatal.revisions_unknown", test_id=tid, revision=rev)
                return 3
            payload = {"test_id": tid, "revision": rev, "plan": plan}
        elif op == "diff":
            hist = R.list_revisions(root, tid)
            # Defaulting to "the last two" is the question a person actually asks — "what changed?" —
            # and it is the one moment where guessing is right, because there is exactly one sensible
            # pair. With fewer than two revisions there is no pair, and that is said rather than
            # answered with an empty diff that reads as "nothing changed".
            a = rev_a or (hist[-2]["revision"] if len(hist) > 1 else None)
            b = rev_b or (hist[-1]["revision"] if hist else None)
            if not a or not b:
                log("fatal.revisions_not_enough", test_id=tid, have=len(hist))
                return 3
            payload = {"test_id": tid, "a": a, "b": b, "diff": R.diff_revisions(root, tid, a, b)}
        elif op == "rollback":
            if not rev_a:
                log("fatal.revisions_no_target", test_id=tid)
                return 2
            payload = {"test_id": tid, "rolled_back_to": rev_a,
                       "head": R.rollback(root, tid, rev_a)}
        else:
            log("fatal.revisions_bad_op", op=str(op))
            return 2
    except ValueError as e:
        log("fatal.revisions_unknown", test_id=tid, revision=str(rev_a), error=e)
        return 3
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    (out / "revisions.json").write_text(body, encoding="utf-8")
    print(body)
    return 0


def _run_export_spec(out, plan_file, spec_out) -> int:
    """M4: emit a Playwright .spec.ts from a frozen plan (no browser)."""
    from .exporter import export_spec
    if not plan_file or not pathlib.Path(plan_file).exists():
        log("fatal.plan_missing_export", path=plan_file)
        return 3
    plan = json.loads(pathlib.Path(plan_file).read_text())
    dest = spec_out or str(out / "exported.spec.ts")
    pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(dest).write_text(export_spec(plan))
    print(f"exported Playwright spec -> {dest}")
    return 0


def _run_import(out, import_dir) -> int:
    """PROD-IMPORT (ADR-105): transpile a directory of existing tests into Sentinel steps + a rewrite
    report (no browser, no LLM, no network). Channel 1 — the filesystem path — because in CI the repo
    is already checked out; the other channels (UI upload, git clone, chat paste) feed the same code.

    Writes import-report.json (the 'state of your suite' diagnosis) and imported-scenarios.json (the
    transpiled steps) to `out`. Grounding against a live explore map is a separate pass the caller runs
    when a map exists; this step is the deterministic transpile + honest report.
    """
    from .importer import parse_spec, detect_engine, rewrite_report, ground_imported
    if not import_dir or not pathlib.Path(import_dir).is_dir():
        log("fatal.import_dir_missing", path=import_dir)
        return 3
    # The extension is only a PREFILTER for which files to open — the engine is decided by content
    # (importer.detect_engine). These globs cover the default layouts of the engines a team actually
    # arrives with: @playwright/test, Cypress (<=9 `cypress/integration/**/*.spec.ts`, >=10
    # `**/*.cy.ts`), and Selenium's four language bindings.
    patterns = ("*.spec.ts", "*.spec.js", "*.cy.ts", "*.cy.js",
                "test_*.py", "*_test.py",
                "*Test.java", "*Tests.java", "*Test.cs", "*Tests.cs")
    specs = sorted({p for pat in patterns for p in pathlib.Path(import_dir).rglob(pat)})
    if not specs:
        log("fatal.import_no_specs", path=import_dir)
        return 3
    # PROD-IMPORT: an optional explore map grounds the imported steps against the real app — "does this
    # step still bind to an element the app has?". Passed as a file so import stays browser-less; a live
    # explore that produces the map is the caller's separate step.
    site_map = None
    map_file = os.environ.get("IMPORT_MAP", "")
    if map_file:
        try:
            site_map = json.loads(pathlib.Path(map_file).read_text(encoding="utf-8"))
        except Exception as e:
            log("fatal.import_map_invalid", path=map_file, error=e)
            return 3
    all_tests, reports, groundings, skipped = [], [], [], []
    for spec in specs:
        rel = str(spec.relative_to(import_dir))
        src = spec.read_text(encoding="utf-8", errors="replace")
        parsed, engine = parse_spec(src, rel)
        if parsed is None:
            # DETECTED but no parser for that dialect yet. Named with its engine — "we saw a Cypress
            # suite and cannot read it" is a useful answer; silence is not.
            skipped.append({"source": rel, "engine": engine,
                            "why": "engine detected but no parser for this dialect yet"
                                   if engine != "unknown"
                                   else "no test engine recognised in the file's content"})
            continue
        if not parsed["tests"]:
            # Parsed by the right dialect and still yielded nothing. Either it is not a test file
            # despite its name, or the parser failed on it. Both are findings about the suite, and
            # both used to be reported as success.
            skipped.append({"source": rel, "engine": engine,
                            "why": "recognised as %s but no test was parsed out of it" % engine})
            continue
        all_tests.extend(parsed["tests"])
        r = rewrite_report(parsed)
        r["engine"] = engine
        reports.append(r)
        if site_map is not None:
            groundings.append({"source": parsed["source"], **ground_imported(parsed, site_map)})
    # one aggregate report across the suite — the totals a team sees on first contact.
    # `engines` is what was actually SEEN, never a constant: the hardcoded "playwright" is what let a
    # Cypress suite be reported as a successfully imported Playwright one.
    agg = {"engines": sorted({r["engine"] for r in reports}),
           "sources": [r["source"] for r in reports],
           "skipped": skipped,
           "totals": {k: sum(r["totals"][k] for r in reports)
                      for k in ("tests", "steps", "bound", "weak", "dropped", "unmatched")},
           "reports": reports}
    agg["totals"]["skipped"] = len(skipped)
    if site_map is not None:
        agg["grounded"] = True
        agg["grounding_totals"] = {k: sum(g["totals"][k] for g in groundings)
                                   for k in ("bound", "unmatched", "unverifiable", "no_locator")}
        agg["groundings"] = groundings
    (out / "import-report.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2))
    (out / "imported-scenarios.json").write_text(json.dumps({"tests": all_tests}, ensure_ascii=False, indent=2))
    t = agg["totals"]
    msg = (f"imported {t['tests']} test(s), {t['steps']} step(s): {t['bound']} bound, "
           f"{t['weak']} by a weak locator, {t['dropped']} construct(s) dropped, "
           f"{t['unmatched']} unmatched")
    if site_map is not None:
        gt = agg["grounding_totals"]
        msg += (f"; grounded vs the app: {gt['bound']} bind, {gt['unmatched']} reference a gone element, "
                f"{gt['unverifiable']} unverifiable (css/xpath)")
    print(msg + f" -> {out}/import-report.json")
    if skipped:
        # CONTRACT CHANGE, deliberate: a file we could not read is a FINDING about the suite, which is
        # the product of import — so it exits 1 ("the test found a problem"), never 0. A mixed
        # directory that used to come back green did so by reporting success over files it had
        # silently dropped, which is the defect this replaces. Named per file, on stderr so it is
        # visible even when stdout is being captured.
        print("SKIPPED %d file(s) — NOT imported:" % len(skipped), file=sys.stderr)
        for s in skipped:
            print("  %s — engine=%s, %s" % (s["source"], s["engine"], s["why"]), file=sys.stderr)
        log("import.files_skipped", count=len(skipped),
            sources=", ".join(s["source"] for s in skipped))
        return 1
    return 0


def _run_report(run_dir) -> int:
    """M4: generate report.html + report.json + metrics.prom from a run's heal-report.json."""
    from .report import generate
    if not (pathlib.Path(run_dir) / "heal-report.json").exists():
        log("fatal.heal_report_missing", dir=run_dir)
        return 3
    generate(run_dir)
    gw = os.environ.get("PROM_PUSHGATEWAY")
    if gw:
        from .report import push_metrics
        try:
            rep = json.loads((pathlib.Path(run_dir) / "heal-report.json").read_text())
            push_metrics(rep, gw)
            log("system.metrics_pushed", gateway=gw)
        except Exception as e:
            log("system.metrics_push_error", error=e)
    # ADR-073: name junit.xml too. A CLI that writes a file it does not mention is how an operator
    # concludes the feature is missing.
    print(f"report -> {run_dir}/report.html, report.json, metrics.prom, junit.xml")
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
            log("run.config_applied", path=run_config)
        except Exception as e:
            log("fatal.run_config_invalid", path=run_config, error=e)
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
    if run_mode == "import":
        return _run_import(out, os.environ.get("IMPORT_DIR", ""))
    if run_mode == "revisions":
        return _run_revisions(out, os.environ.get("REV_OP", ""), os.environ.get("SENTINEL_TEST_ID", ""),
                              os.environ.get("REV_A", ""), os.environ.get("REV_B", ""))

    # --- browser modes -------------------------------------------------------
    target = os.environ.get("TARGET_URL")
    pw_cmd = os.environ.get("PW_EXECUTOR_CMD")
    if not pw_cmd:
        log("fatal.executor_cmd_unset")
        return 3
    if run_mode == "mcp-server":
        # M7 (ADR-020): expose the brain as an MCP server; the host drives + supplies the model.
        from .server import run_mcp_server
        return run_mcp_server(out, run_id)
    if run_mode == "chat":
        # M9.10 (ADR-048): stateful multi-turn authoring. Dispatched BEFORE make_executor — a warm
        # refine turn must not spawn a browser; _run_chat creates the executor lazily on a cold turn only.
        conversation_id = os.environ.get("SENTINEL_CONVERSATION_ID", "").strip()
        if not conversation_id:
            log("fatal.chat_no_conversation_id")
            return 2
        return _run_chat(run_id, out, conversation_id, target,
                         float(os.environ.get("COVERAGE_TARGET", "0.85")),
                         int(os.environ.get("MAX_STEPS", "40")))
    if run_mode == "explore" and not target:
        log("fatal.target_unset")
        return 2

    log("run.config", run_id=run_id, mode=run_mode)
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
    except Exception as e:
        # ADR-087: an unhandled exception in OUR code used to exit 1, and exit 1 means "Тест нашёл
        # проблему … это результат работы, а не поломка инструмента". So every internal crash was
        # presented to the user as a finding about THEIR application — the exact confusion principle 8
        # exists to prevent, and the one the product is least able to afford.
        traceback.print_exc()
        log("fatal.internal_error", error=e)
        rc = 4
    finally:
        ex.close()
        _run_span.__exit__(None, None, None)
    return rc


if __name__ == "__main__":
    sys.exit(main())
