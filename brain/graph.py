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
from langgraph.types import interrupt

from . import runcontrol
from .otel import span
from .state import RunState, normalize_url, semantic_id, canonical_plan_hash


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


def _elements_from_interactives(elements: list, path: str) -> list:
    """Build element descriptors (semantic_id + primary locator + L1–L6 alternatives + role + page) from
    pw-executor `browser.interactives`.

    M9.2b (ADR-028): generalized beyond buttons to **input/select/link** so a login/form/billing scenario
    can ground. Coverage still uses the button subset (`role == "button"`) so pure-explore convergence and
    `plan_hash` are unchanged — button `semantic_id`s are identical to the old button-only cataloguer.

    semantic_id anchors on testid (stable across DOM drift) when present, else the accessible name. The
    primary locator is role+name (human-natural, drift-fragile); stabler testid/label/text are healing
    alternatives ordered by strategy prior (testid 0.95 > role_name 0.90 > label 0.88 > text 0.80).
    """
    out = []
    for e in elements:
        tag = (e.get("tag") or "").lower()
        erole = (e.get("role") or "").lower()
        if tag == "button" or erole == "button":
            role = "button"
        elif tag == "a" or erole == "link":
            role = "link"
        elif tag == "select" or erole in ("combobox", "listbox"):
            role = "combobox"
        elif erole in ("checkbox", "radio", "switch"):
            role = erole
        elif tag in ("input", "textarea") or erole == "textbox":
            role = "textbox"
        elif erole:
            role = erole
        else:
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
            alts.append({"strategy": "role_name", "locator": {"role": role, "name": name}, "prior": 0.90})
        if role != "button" and e.get("label"):    # buttons stay byte-identical to the old cataloguer (plan_hash)
            alts.append({"strategy": "label", "locator": {"label": e["label"]}, "prior": 0.88})
        if text and text != name:
            alts.append({"strategy": "text_role", "locator": {"text": text}, "prior": 0.80})
        primary = {"role": role, "name": name} if name else (alts[0]["locator"] if alts else None)
        out.append({"semantic_id": semantic_id(path, role, anchor), "role": role, "name": name,
                    "testid": testid, "locator": primary, "alternatives": alts, "page": path})
    return out


def _user_turns(messages: list) -> list:
    """M9.10 (ADR-048): pull user-turn text out of the messages channel for refine context. Entries are
    BaseMessage objects (after add_messages coercion) or plain dicts — duck-typed on `.type`/`.content`
    so the brain needs no langchain_core import. BaseMessage `.type` is 'human'; a dict uses 'user'."""
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            role, content = m.get("role"), m.get("content")
        else:
            role, content = getattr(m, "type", None), getattr(m, "content", None)
        if role in ("human", "user") and content:
            out.append(content)
    return out


def build_graph(ex, planner, tx_write, scenario_head=None, rc=None):
    """Build and return an uncompiled StateGraph. Caller compiles it with a checkpointer.

    M9.2b (ADR-028): when `scenario_head` (a GoalPlanner or DescribePlanner) is wired, a `scenario` node
    runs once after the explore converges — it authors a grounded scenario over the COMPLETE site map.
    Pure explore (scenario_head=None) routes straight through scenario as a no-op to report.

    M9.8 F4 (ADR-054): `rc` is the RunControl client (orchestrator link). Defaults to make_client()
    (no-op unless ORCH_ADDR is set) — injectable so the offline takeover test drives interrupt/resume
    with a fake orchestrator. Production behaviour is unchanged when rc is left None."""
    rc = rc if rc is not None else runcontrol.make_client()  # M8: token deltas to the Go orchestrator (no-op if ORCH_ADDR unset)

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
        elements = _elements_from_interactives(ex.call("browser.interactives").get("elements", []), path)
        buttons = [e for e in elements if e["role"] == "button"]   # coverage/candidates: button subset (unchanged)
        seen = list(dict.fromkeys(list(state.get("interactive_seen", []))
                                  + [b["semantic_id"] for b in buttons]))
        # M9.2b (ADR-028): accumulate the site-wide element map (superset of buttons) for the scenario head.
        site_map = dict(state.get("site_map") or {})
        have = {el["semantic_id"] for el in site_map.get(path, [])}
        site_map[path] = list(site_map.get(path, [])) + [el for el in elements if el["semantic_id"] not in have]
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
                "coverage_achieved": coverage, "page_model": pm, "site_map": site_map}

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
        if rc.report(state.get("run_id", ""), "plan", tok.get("prompt"),
                     tok.get("completion")) == runcontrol.ABORT:
            log("plan: orchestrator budget abort -> converging")
            return {"exploration_complete": True}
        return {"exploration_plan": list(state.get("exploration_plan", [])) + [planned],
                "_pending": planned}

    def act(state: RunState) -> dict:
        """Execute the pending action via pw-executor; mark the element exercised."""
        p = state.get("_pending")
        if not p:
            return {"_last_ok": False}
        try:
            at = p["action_type"]
            if at == "navigate":
                ex.call("browser.navigate", url=p["target"])
            elif at == "click":
                ex.call("browser.click", locator=p["locator"])
            elif at in ("fill", "type", "select"):
                # M9.1 forward-compat: the explorer emits only click/navigate today; frozen/authored
                # plans run through act reuse replay's verb dispatch (single source of truth).
                from .replay import _act
                _act(ex, at, p.get("locator") or {}, p)
            elif at == "press":
                if p.get("locator"):
                    ex.call("browser.press", locator=p["locator"], key=p.get("key"))
                else:
                    ex.call("browser.press", key=p.get("key"))
            elif at == "assert":
                from .replay import _expect_params
                ex.call("browser.expect", **_expect_params(p))
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
        """LangGraph persists at each superstep boundary.

        M9.8 F4 (ADR-054): operator-takeover gate. A 0-token poll to the orchestrator; if a takeover is
        pending, ARM it (a state latch) — the actual pause runs in the dedicated `takeover` node next
        superstep. The decision is latched into STATE (not re-derived from the volatile poll) so the
        interrupting node's interrupt() is reached identically on the resume re-run.

        abort > takeover: if the orchestrator ABORTS (budget breach / external Abort) while or after a
        takeover, converge immediately instead of resuming the walk. This node is re-entered on resume
        (bypassing plan()'s own abort check), so it must honour abort here too. No-op / no arm when no
        orchestrator is wired (poll() -> "continue"), so the standalone/offline path is byte-identical."""
        verb = rc.poll(state.get("run_id", ""), "checkpoint")
        if verb == runcontrol.ABORT:
            log("checkpoint: orchestrator abort -> converging (abort > takeover)")
            return {"exploration_complete": True, "_takeover_armed": False}
        if verb == runcontrol.TAKEOVER:
            log("checkpoint: operator takeover pending -> arming pause")
            return {"_takeover_armed": True}
        return {}

    def takeover(state: RunState) -> dict:
        """M9.8 F4 (ADR-054): paused for an operator takeover. interrupt() yields the live browser to the
        human (CDP, M9-LIVE) and persists the partial run; app.invoke() returns with `__interrupt__`. On
        the orchestrator's Return the brain resumes this thread (Command(resume=...)), re-enters here where
        interrupt() now RETURNS the resume payload, clears the arm, and records the return. The interrupt()
        is UNCONDITIONAL — the decision was latched by checkpoint, so this node re-runs cleanly on resume.
        Edge back to checkpoint re-polls (handles a not-yet-propagated Return) before the run continues."""
        payload = interrupt({"reason": "operator_takeover", "run_id": state.get("run_id", "")})
        log(f"takeover: resumed from operator takeover -> {payload!r}")
        return {"_takeover_armed": False,
                "takeover_returns": list(state.get("takeover_returns", [])) + [payload]}

    def scenario(state: RunState) -> dict:
        """M9.2b (ADR-028): phase-2 head — author a grounded scenario over the COMPLETE site map.
        No-op unless `scenario_head` is wired (goal/describe mode). Appends grounded steps to the plan;
        records `scenario_unmatched` (refs/draft steps that couldn't bind to a real element).

        M9.10 (ADR-048): also the RESUME entrypoint for multi-turn chat (conditional edge from START on a
        warm thread). It re-authors over the PERSISTED site_map using the prior conversation turns as
        refine context, then records an assistant summary so the next turn inherits the thread. `prior`
        is empty for one-shot goal/describe (no messages) ⇒ that path stays byte-identical."""
        if scenario_head is None:
            return {}
        from .scenario import flatten_site_map, ground_scenario, reconcile
        site_map = state.get("site_map") or {}
        base_id = len(state.get("exploration_plan", []))
        # M9.10: prior user turns (all but the current — which IS this turn's goal/describe) = refine context.
        prior = _user_turns(state.get("messages"))[:-1]
        if scenario_head.name == "goal":
            out = scenario_head.build_scenario(flatten_site_map(site_map), state.get("goal"), history=prior)
            steps, unmatched = ground_scenario(out.get("refs", []), site_map, start_id=base_id + 1)
        else:  # describe: LLM draft -> deterministic reconcile against the real map
            out = scenario_head.draft(history=prior)
            steps, unmatched = reconcile(out.get("draft", []), site_map, start_id=base_id + 1)
        tok = out.get("tokens") or {}
        tx_write({"step": "scenario", "planner": scenario_head.name, "model": scenario_head.model,
                  "decision": "scenario", "reason": f"{len(steps)} grounded, {len(unmatched)} unmatched",
                  "prompt_tokens": tok.get("prompt"), "completion_tokens": tok.get("completion")})
        rc.report(state.get("run_id", ""), "plan", tok.get("prompt"), tok.get("completion"))
        # M9.10: record an assistant summary into the conversation thread for the next turn's context.
        summary = {"role": "assistant",
                   "content": f"authored {len(steps)} grounded step(s), {len(unmatched)} unmatched"}
        return {"exploration_plan": list(state.get("exploration_plan", [])) + steps,
                "scenario_steps": steps, "scenario_unmatched": unmatched, "phase": "scenario",
                "messages": [summary]}

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
        return "scenario" if state.get("exploration_complete") else "act"

    def route_verify(state: RunState) -> str:
        return "checkpoint" if state.get("_verify_ok", True) else "heal"

    def route_checkpoint(state: RunState) -> str:
        # M9.8 F4 (ADR-054): abort during a takeover converges (abort > takeover); an armed takeover
        # diverts to the pause node before the run continues.
        if state.get("exploration_complete"):
            return "scenario"
        if state.get("_takeover_armed"):
            return "takeover"
        return "scenario" if state.get("current_step", 0) >= state.get("max_steps", 40) else "perceive"

    def route_entry(state: RunState) -> str:
        """M9.10 (ADR-048): conditional entry. A RESUMED multi-turn thread carries a persisted `site_map`
        AND prior `messages` → skip the browser explore, go straight to re-author (`scenario`). A cold
        turn-1 / one-shot run has an empty site_map → the full `perceive`-walk. Pure explore is unchanged
        (site_map starts {} ⇒ always `perceive`)."""
        return "scenario" if (state.get("site_map") and state.get("messages")) else "perceive"

    def _traced(node_name, fn):
        """Wrap a node in a per-node OTel span (M8, ADR-021); no-op when tracing isn't configured."""
        def wrapped(state):
            with span(f"node.{node_name}"):
                return fn(state)
        return wrapped

    b = StateGraph(RunState)
    for name, fn in [("perceive", perceive), ("ground", ground), ("plan", plan),
                     ("act", act), ("verify", verify), ("heal", heal),
                     ("checkpoint", checkpoint), ("takeover", takeover),
                     ("scenario", scenario), ("report", report)]:
        b.add_node(name, _traced(name, fn))
    # M9.10 (ADR-048): conditional entry — resume a warm multi-turn thread straight into `scenario`,
    # else the normal cold/one-shot `perceive` walk. (Was an unconditional START->perceive edge.)
    b.add_conditional_edges(START, route_entry, {"perceive": "perceive", "scenario": "scenario"})
    b.add_edge("perceive", "ground")
    b.add_edge("ground", "plan")
    b.add_conditional_edges("plan", route_plan, {"act": "act", "scenario": "scenario"})
    b.add_edge("act", "verify")
    b.add_conditional_edges("verify", route_verify, {"checkpoint": "checkpoint", "heal": "heal"})
    b.add_edge("heal", "checkpoint")
    # M9.8 F4 (ADR-054): an armed takeover routes to the pause node, which loops back to checkpoint after
    # the operator returns (re-poll handles a not-yet-propagated Return) before the run continues.
    b.add_conditional_edges("checkpoint", route_checkpoint,
                            {"perceive": "perceive", "scenario": "scenario", "takeover": "takeover"})
    b.add_edge("takeover", "checkpoint")
    b.add_edge("scenario", "report")  # M9.2b: scenario node (no-op in pure explore) -> report
    b.add_edge("report", END)
    return b
