"""Offline gates for ADR-108c — the map gate.

Run:  .venv/bin/python tests/test_map_gate_offline.py

Alex's directive: after exploring, the tool analyses the map ITSELF, reports what it found, and asks
permission before authoring a test over it.

Three things have to hold, and the second is the one a gate usually gets wrong:

1. the report is about what a person DECIDES on, not a count of elements;
2. an unanswered gate WAITS — a gate whose default is "proceed" is decoration — and a timeout is a
   REFUSAL, because treating silence as consent inverts the gate's meaning exactly when it matters;
3. it does not hang a run that nobody is watching: with no orchestrator wired there is nobody to ask,
   so the report is emitted and the wait is skipped. Otherwise CI, cron and the air-gapped bundle stop
   working the day this ships.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.graph import summarise_site_map, await_map_decision  # noqa: E402


class FakeRC:
    """Stands in for the orchestrator client. `answers` is popped one per poll, so a test can make the
    gate wait for N polls before the person answers."""

    def __init__(self, answers, wired=True):
        self.answers, self.polls, self.wired = list(answers), 0, wired

    def map_decision(self, run_id):
        self.polls += 1
        return self.answers.pop(0) if self.answers else ""

    # The graph also reports token deltas and polls for takeover; neither is what this fixture is
    # about, so both answer "carry on" rather than being left to raise deep inside a node.
    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running"):
        return "continue"

    def poll(self, run_id, node="checkpoint"):
        return "continue"

    def close(self):
        pass


def _env(**kw):
    saved = dict(os.environ)
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
    return saved


def test_the_report_is_about_what_a_person_decides_on():
    """A count of elements is not a report. What decides the answer is whether anything looks
    destructive or looks like a login — the two ways an autonomous run does damage or silently
    achieves nothing."""
    m = {
        "/login": [{"role": "textbox", "name": "Password"}, {"role": "button", "name": "Sign in"}],
        "/admin": [{"role": "button", "name": "Delete account"}, {"role": "link", "name": "Home"}],
    }
    s = summarise_site_map(m)
    assert s["pages"] == 2 and s["interactives"] == 4, s
    assert s["form_fields"] == 1, s
    assert [r["name"] for r in s["looks_destructive"]] == ["Delete account"], s
    assert {r["name"] for r in s["looks_like_auth"]} == {"Password", "Sign in"}, s
    # Kinds are ordered by how many there are: a person scanning the report reads the shape of the app
    # from the top of the list, not by counting.
    assert list(s["kinds"])[0] == "button", s["kinds"]
    assert summarise_site_map({})["pages"] == 0
    assert summarise_site_map(None)["interactives"] == 0


def test_an_unanswered_gate_waits_then_refuses():
    """The default is WAIT, and the timeout is a REFUSAL.

    Both halves matter. A gate that proceeded on an empty answer would be decoration; one that
    approved on timeout would give permission nobody granted, at the exact moment nobody was there.
    """
    rc = FakeRC([])                       # nobody ever answers
    saved = _env(SENTINEL_MAP_GATE_TIMEOUT="1", SENTINEL_MAP_GATE=None)
    try:
        t0 = time.monotonic()
        d = await_map_decision(rc, "r1", {"pages": 1, "interactives": 2})
        waited = time.monotonic() - t0
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert d == "reject", f"an unanswered gate must refuse, got {d!r}"
    assert waited >= 1.0, f"it did not actually wait ({waited:.2f}s) — the gate is decoration"
    assert rc.polls >= 1, "it never asked"


def test_a_late_answer_is_honoured():
    """The person answers on the third poll: the gate must still be listening."""
    rc = FakeRC(["", "", "approve"])
    saved = _env(SENTINEL_MAP_GATE_TIMEOUT="10", SENTINEL_MAP_GATE=None)
    try:
        assert await_map_decision(rc, "r1", {"pages": 1}) == "approve"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert rc.polls == 3, rc.polls


def test_a_refusal_is_carried_through():
    rc = FakeRC(["reject"])
    saved = _env(SENTINEL_MAP_GATE_TIMEOUT="10", SENTINEL_MAP_GATE=None)
    try:
        assert await_map_decision(rc, "r1", {"pages": 1}) == "reject"
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_it_does_not_hang_a_run_nobody_is_watching():
    """With no orchestrator there is NOBODY who can answer. A headless run — CI, cron, the air-gapped
    bundle — must still complete; only the waiting is skipped, and the report is still produced."""
    rc = FakeRC([], wired=False)
    saved = _env(SENTINEL_MAP_GATE_TIMEOUT="30", SENTINEL_MAP_GATE=None)
    try:
        t0 = time.monotonic()
        d = await_map_decision(rc, "r1", {"pages": 3, "interactives": 9})
        waited = time.monotonic() - t0
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert d == "skipped", d
    assert waited < 1.0, f"it waited {waited:.2f}s for an orchestrator that does not exist"
    assert rc.polls == 0, "it polled a client that cannot answer"


def test_the_opt_out_is_explicit():
    rc = FakeRC(["approve"], wired=True)
    saved = _env(SENTINEL_MAP_GATE="0", SENTINEL_MAP_GATE_TIMEOUT="30")
    try:
        assert await_map_decision(rc, "r1", {"pages": 1}) == "skipped"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert rc.polls == 0, "SENTINEL_MAP_GATE=0 must not even ask"


def test_the_gate_blocks_the_scenario_node_itself():
    """The interception is at the TOP of the scenario node, so nothing is authored before the answer.

    Asserted by driving the real node: a rejecting orchestrator must produce NO scenario steps, and an
    approving one must produce the same steps as before the gate existed.
    """
    from brain.graph import build_graph
    from brain.planner import HeuristicPlanner
    from tests.test_r2_multiturn_offline import WalkEx, QueuedFakeBackend, _explore_init, _reply, _USER_SID
    from langgraph.checkpoint.memory import MemorySaver
    from brain import budget

    def run_with(rc, timeout="5", gate="off-is-none"):
        budget.reset(plan_limit=10**6, heal_limit=10**6)
        # `gate` is explicit rather than inherited: the helper clears SENTINEL_MAP_GATE by default, so a
        # caller that set it OUTSIDE would have it wiped — which is exactly what made the first version
        # of the control run compare a gated run against another gated run.
        saved = _env(SENTINEL_MAP_GATE_TIMEOUT=timeout,
                     SENTINEL_MAP_GATE=(None if gate == "off-is-none" else gate))
        try:
            from brain.planner import GoalPlanner
            head = GoalPlanner("fill the user field")
            head._backend = QueuedFakeBackend([_reply([{"ref": _USER_SID, "verb": "fill", "value": "bob"}])])
            app = build_graph(WalkEx(), HeuristicPlanner(), lambda r: None,
                              scenario_head=head, rc=rc).compile(checkpointer=MemorySaver())
            return app.invoke(_explore_init(goal="fill the user field"),
                              config={"recursion_limit": 200, "configurable": {"thread_id": "map-gate"}})
        finally:
            os.environ.clear()
            os.environ.update(saved)
            budget.reset()

    rejected = run_with(FakeRC(["reject"]))
    assert rejected.get("phase") == "map_rejected", rejected.get("phase")
    assert not rejected.get("scenario_steps"), \
        f"a refused map still authored {len(rejected.get('scenario_steps') or [])} step(s)"

    # The control is the SAME run with the gate switched off, not an absolute step count: what is being
    # asserted is that approving changes nothing about the outcome, and a hard-coded number would fail
    # for any reason the fixture's authoring changes — which is not what this test is about.
    approved = run_with(FakeRC(["approve"]))
    ungated = run_with(FakeRC([]), gate="0")
    assert approved.get("phase") == ungated.get("phase"), \
        f"approving changed the outcome: {approved.get('phase')} vs ungated {ungated.get('phase')}"
    assert len(approved.get("scenario_steps") or []) == len(ungated.get("scenario_steps") or []), \
        (f"an approved map authored {len(approved.get('scenario_steps') or [])} step(s) but the same run "
         f"with the gate off authored {len(ungated.get('scenario_steps') or [])}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
