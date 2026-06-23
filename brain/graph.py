"""Sentinel brain — the LangGraph StateGraph (explore loop).

Nodes (9): perceive, ground, plan, act, verify, heal (STUB), checkpoint, report (+ START/END).
The graph autonomously explores a site, converges on a measurable coverage target (ADR-010),
and freezes plan.json / plan_hash. See ../docs/M1_CONTRACT.md and ../docs/STATE_MACHINE.md.

M2 change: each interactive element captures an ordered L1–L6 `alternatives` list (testid /
role+name / text), and the frozen click step records `locator` (primary) + `alternatives` so the
replay path (brain/replay.py) can self-heal a broken locator. The explore graph's `heal` node
stays a stub; real healing happens in replay (HealingEngine).

Coverage model: the "clickable" set is buttons; links drive navigation via the frontier.
Coverage = exercised buttons / seen buttons. Nodes are closures over the injected `ex`
(pw-executor client), `planner`, and `tx_write` (transcript sink).
"""
import json
import os
import sys

from langgraph.graph import StateGraph, START, END

from .state import RunState, normalize_url, semantic_id, canonical_plan_hash


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


def _buttons_from_interactives(elements: list, path: str) -> list:
    """Build button descriptors (semantic_id + primary locator + L1–L6 alternatives) from
    pw-executor `browser.interactives`.

    semantic_id anchors on testid (stable across DOM drift) when present, else the accessible
    name — so the same logical element keeps its id even if its name changes. The primary
    locator is role+name (the human-natural, drift-fragile locator); the stabler testid/text are
    kept as healing alternatives, ordered by strategy prior (testid 0.95 > role_name 0.90 > text).
    """
    out = []
    for e in elements:
        if e.get("tag") != "button" and e.get("role") != "button":
            continue
        name = (e.get("name") or "").strip()
        testid = e.get("testid")
        text = (e.get("text") or "").strip()
        anchor = testid or name or text
        if not anchor:
            continue
        alts = []
        if testid:
            alts.append({"strategy": "testid", "locator": {"testid": testid}, "prior": 0.95})
        if name:
            alts.append({"strategy": "role_name", "locator": {"role": "button", "name": name}, "prior": 0.90})
        if text and text != name:
            alts.append({"strategy": "text_role", "locator": {"text": text}, "prior": 0.80})
        primary = {"role": "button", "name": name} if name else (alts[0]["locator"] if alts else None)
        out.append({"semantic_id": semantic_id(path, "button", anchor), "name": name,
                    "testid": testid, "locator": primary, "alternatives": alts})
    return out


def build_graph(ex, planner, tx_write):
    """Build and return an uncompiled StateGraph. Caller compiles it with a checkpointer."""

    def perceive(state: RunState) -> dict:
        """Snapshot the current page (URL + accessibility tree). No LLM."""
        cur = ex.call("browser.currentUrl")
        snap = ex.call("browser.snapshot")
        return {"current_url": cur.get("url", ""),
                "page_model": {"url": cur.get("url", ""), "title": cur.get("title", ""),
                               "aria": snap.get("ariaSnapshot", ""),
                               "nodeCount": snap.get("nodeCount", 0)}}

    def ground(state: RunState) -> dict:
        """Catalogue buttons (with healing alternatives), grow the frontier, recompute coverage."""
        pm = dict(state.get("page_model") or {})
        path = normalize_url(pm.get("url", ""))
        buttons = _buttons_from_interactives(
            ex.call("browser.interactives").get("elements", []), path)
        seen = list(dict.fromkeys(list(state.get("interactive_seen", []))
                                  + [b["semantic_id"] for b in buttons]))
        links = ex.call("browser.links").get("links", [])
        origin = state.get("base_origin", "")
        visited = set(state.get("visited_paths", []))
        frontier = list(state.get("nav_frontier", []))
        for l in links:
            nu = normalize_url(l.get("href", ""))
            if nu and nu.startswith(origin) and nu not in visited and nu not in frontier and nu != path:
                frontier.append(nu)
        visited_paths = list(dict.fromkeys(list(state.get("visited_paths", [])) + [path]))
        frontier = [f for f in frontier if f != path]
        exercised = set(state.get("interactive_exercised", []))
        total = len(seen)
        done_n = len([s for s in seen if s in exercised])
        coverage = (done_n / total) if total else 0.0
        pm["buttons"] = buttons
        return {"interactive_seen": seen, "nav_frontier": frontier, "visited_paths": visited_paths,
                "coverage_achieved": coverage, "page_model": pm}

    def plan(state: RunState) -> dict:
        """Assemble candidates, enforce convergence, ask the planner for the next action."""
        pm = state.get("page_model") or {}
        exercised = set(state.get("interactive_exercised", []))
        candidates = []
        for b in pm.get("buttons", []):
            if b["semantic_id"] not in exercised:
                candidates.append({"kind": "click", "semantic_id": b["semantic_id"],
                                   "role": "button", "name": b["name"], "target": None,
                                   "intent": f"click button '{b['name']}'",
                                   "locator": b["locator"], "alternatives": b["alternatives"]})
        for nu in state.get("nav_frontier", []):
            candidates.append({"kind": "navigate", "semantic_id": semantic_id(nu, "navigate", ""),
                               "role": None, "name": None, "target": nu, "alternatives": None,
                               "locator": None, "intent": f"navigate to {nu}"})
        step = state.get("current_step", 0)
        frontier_empty = len(state.get("nav_frontier", [])) == 0
        cov_ok = state.get("coverage_achieved", 0.0) >= state.get("coverage_target", 0.85)
        if step >= state.get("max_steps", 40) or not candidates or (cov_ok and frontier_empty):
            reason = ("max_steps" if step >= state.get("max_steps", 40)
                      else "converged" if (cov_ok and frontier_empty) else "no_candidates")
            tx_write({"step": step, "planner": planner.name, "model": planner.model,
                      "decision": "done", "reason": reason,
                      "prompt_tokens": None, "completion_tokens": None})
            return {"exploration_complete": True}
        decision = planner.propose(dict(state), candidates)
        if decision.get("done") or not decision.get("action"):
            tx_write({"step": step, "planner": planner.name, "model": planner.model,
                      "decision": "done", "reason": decision.get("reason", ""),
                      "prompt_tokens": None, "completion_tokens": None})
            return {"exploration_complete": True}
        a = decision["action"]
        sid = step + 1
        planned = {"step_id": sid, "intent": a["intent"], "semantic_id": a["semantic_id"],
                   "action_type": a["kind"], "target": a.get("target"),
                   "locator": (a.get("locator") if a["kind"] == "click" else None),
                   "alternatives": (a.get("alternatives") if a["kind"] == "click" else None),
                   "is_milestone": False}
        tok = decision.get("tokens") or {}
        tx_write({"step": sid, "planner": planner.name, "model": planner.model,
                  "decision": a["intent"], "reason": decision.get("reason", ""),
                  "prompt_tokens": tok.get("prompt"), "completion_tokens": tok.get("completion")})
        return {"exploration_plan": list(state.get("exploration_plan", [])) + [planned],
                "_pending": planned}

    def act(state: RunState) -> dict:
        """Execute the pending action via pw-executor; mark the element exercised."""
        p = state.get("_pending")
        if not p:
            return {"_last_ok": False}
        try:
            if p["action_type"] == "navigate":
                ex.call("browser.navigate", url=p["target"])
            else:
                ex.call("browser.click", locator=p["locator"])
        except Exception as e:
            return {"errors": list(state.get("errors", [])) + [f"act#{p['step_id']}: {e}"],
                    "_last_ok": False, "current_step": p["step_id"]}
        exercised = list(state.get("interactive_exercised", []))
        if p["action_type"] == "click":
            exercised = list(dict.fromkeys(exercised + [p["semantic_id"]]))
        execs = list(state.get("executed_actions", [])) + [
            {"step_id": p["step_id"], "type": p["action_type"], "ok": True}]
        return {"interactive_exercised": exercised, "executed_actions": execs,
                "current_step": p["step_id"], "_last_ok": True}

    def verify(state: RunState) -> dict:
        """Explore-mode verify: trust act's result. Replay-mode healing lives in brain/replay.py."""
        return {"_verify_ok": bool(state.get("_last_ok", True))}

    def heal(state: RunState) -> dict:
        """STUB in the explore graph (explore discovers, it does not heal). See brain/replay.py."""
        log("heal node: explore-mode stub (real healing is in replay)")
        return {}

    def checkpoint(state: RunState) -> dict:
        """LangGraph's checkpointer persists at each superstep boundary; nothing explicit here."""
        return {}

    def report(state: RunState) -> dict:
        """Freeze plan.json with a deterministic plan_hash over the ordered steps."""
        steps = list(state.get("exploration_plan", []))
        ph = canonical_plan_hash(steps)
        plan_obj = {"plan_id": state.get("run_id"), "plan_hash": ph,
                    "target_url": state.get("target_url"), "run_mode": state.get("run_mode"),
                    "coverage_target": state.get("coverage_target"),
                    "coverage_achieved": round(state.get("coverage_achieved", 0.0), 4),
                    "interactive_seen": len(state.get("interactive_seen", [])),
                    "interactive_exercised": len(state.get("interactive_exercised", [])),
                    "steps": steps}
        with open(os.path.join(state.get("artifact_dir", "."), "plan.json"), "w") as f:
            json.dump(plan_obj, f, indent=2)
        return {"plan_hash": ph}

    def route_plan(state: RunState) -> str:
        return "report" if state.get("exploration_complete") else "act"

    def route_verify(state: RunState) -> str:
        return "checkpoint" if state.get("_verify_ok", True) else "heal"

    def route_checkpoint(state: RunState) -> str:
        return "report" if state.get("current_step", 0) >= state.get("max_steps", 40) else "perceive"

    b = StateGraph(RunState)
    for name, fn in [("perceive", perceive), ("ground", ground), ("plan", plan),
                     ("act", act), ("verify", verify), ("heal", heal),
                     ("checkpoint", checkpoint), ("report", report)]:
        b.add_node(name, fn)
    b.add_edge(START, "perceive")
    b.add_edge("perceive", "ground")
    b.add_edge("ground", "plan")
    b.add_conditional_edges("plan", route_plan, {"act": "act", "report": "report"})
    b.add_edge("act", "verify")
    b.add_conditional_edges("verify", route_verify, {"checkpoint": "checkpoint", "heal": "heal"})
    b.add_edge("heal", "checkpoint")
    b.add_conditional_edges("checkpoint", route_checkpoint, {"perceive": "perceive", "report": "report"})
    b.add_edge("report", END)
    return b
