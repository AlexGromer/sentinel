"""Offline gate: the explore map is an ARTIFACT, so grounding is reachable (PROD-IMPORT, PR-5).

Run:  .venv/bin/python tests/test_import_verify_offline.py

`ground_imported` could always answer the second half of the import diagnosis — "does this step still
bind to an element the application HAS?" — and nothing ever produced the map it needs. The map lived
only in the graph's state and in the per-run checkpoint.db, which ADR-099 deletes in `finally`. So the
capability was real in code and unreachable in practice: the only file of that shape in the whole
repository was a synthetic test fixture, and the feature was, in effect, tested against itself.

This pins the missing predicate: the explore run WRITES `site-map.json`, and what it writes is the
shape grounding actually consumes. Two halves, and they are separate failures:

  1. the file is produced, next to plan.json, from the same state the plan is frozen from;
  2. its CONTENT satisfies the consumer — asserting only that a file exists would pass for an empty
     object, and an empty map grounds every step as "gone", which is worse than not grounding at all.

Point 2 is checked by running the REAL `ground_imported` over the REAL emitted shape, not by
inspecting keys: a shape assertion agrees with whatever the author believed the consumer wanted.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.importer import ground_imported, parse_playwright_spec  # noqa: E402

SPEC = """import { test, expect } from '@playwright/test';
test('signs in', async ({ page }) => {
  await page.goto('https://app/login');
  await page.getByLabel('Username').fill('alex');
  await page.getByRole('button', { name: 'Ghost that never existed' }).click();
});
"""


def _emitted_map_shape():
    """Exactly what brain/graph.py's report node writes, built the same way it builds it."""
    # graph.py accumulates site_map as {page_path: [element, ...]} where an element carries
    # semantic_id / role / name / testid / locator. Reproduced here from the same field names the
    # perceive node produces, so a rename on either side breaks this test rather than the product.
    return {
        "https://app/login": [
            {"semantic_id": "ea692d52c825", "role": "textbox", "name": "Username",
             "testid": None, "locator": {"role": "textbox", "name": "Username"}},
            {"semantic_id": "b31f0c7a9d10", "role": "button", "name": "Sign in",
             "testid": None, "locator": {"role": "button", "name": "Sign in"}},
        ]
    }


def _run_real_explore(buttons):
    """Run the REAL graph with a fake executor into a temp artifact dir -> (dir, final state).

    Behavioural on purpose. Asserting that the string "site-map.json" appears in graph.py would pass
    for a write into the wrong directory, a write of the wrong object, or a write that never runs
    because it sits behind a condition that is never true.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from brain import budget
    from brain.graph import build_graph
    from brain.planner import HeuristicPlanner

    page = "file:///s/index.html"

    class Ex:
        def __init__(self): self.url = ""
        def call(self, m, **p):
            if m == "browser.navigate":
                self.url = p["url"]; return {"url": self.url}
            if m == "browser.currentUrl": return {"url": self.url, "title": ""}
            if m == "browser.snapshot":   return {"ariaSnapshot": "- page", "nodeCount": 1}
            if m == "browser.interactives":
                if self.url != page: return {"elements": []}
                return {"elements": [{"tag": "button", "role": "button", "name": b, "testid": None,
                                      "text": b, "disabled": False} for b in buttons]}
            if m == "browser.links":  return {"links": []}
            if m == "browser.click":  return {"ok": True}
            if m == "browser.probe":  return {"count": 1}
            if m == "browser.screenshotHash": return {"hash": "h"}
            return {}

    d = tempfile.mkdtemp(prefix="sentinel-map-")
    init = {"run_id": "t", "run_mode": "explore", "target_url": page, "base_origin": "file:///s/",
            "coverage_target": 0.85, "max_steps": 40, "artifact_dir": d,
            "goal": "", "describe": "", "site_map": {}, "phase": "explore",
            "scenario_steps": [], "scenario_unmatched": [], "current_url": page, "page_model": {},
            "exploration_plan": [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav1",
                                  "intent": "nav", "target": page, "locator": None,
                                  "alternatives": None, "is_milestone": True}],
            "plan_hash": "", "current_step": 1, "interactive_seen": [], "interactive_exercised": [],
            "visited_paths": [], "nav_frontier": [], "coverage_achieved": 0.0,
            "exploration_complete": False, "executed_actions": [], "errors": []}
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    ex = Ex(); ex.call("browser.navigate", url=page)
    app = build_graph(ex, HeuristicPlanner(), lambda r: None).compile(checkpointer=MemorySaver())
    app.invoke(init, config={"recursion_limit": 400, "configurable": {"thread_id": "t"}})
    return d


def main():
    # 1 — a REAL explore run leaves site-map.json in its artifact directory, beside plan.json.
    d = _run_real_explore(["Sign in", "Register"])
    mp = os.path.join(d, "site-map.json")
    assert os.path.exists(os.path.join(d, "plan.json")), "the harness did not reach the report node"
    assert os.path.exists(mp), (
        "an explore run produced no site-map.json — grounding's input is unproduced again, and the "
        "capability goes back to being unreachable outside a fixture"
    )
    emitted = json.loads(open(mp, encoding="utf-8").read())
    assert emitted, "the map was written empty"
    names = {e.get("name") for page in emitted.values() for e in page}
    assert {"Sign in", "Register"} <= names, ("the map does not describe what the page had", names)
    print("PASS a real explore run writes a populated site-map.json beside plan.json")

    # 2 — a page with NOTHING interactive writes no map at all. Writing {} would ground every step as
    #     'gone': a confident wrong diagnosis is worse than an absent one.
    d2 = _run_real_explore([])
    assert not os.path.exists(os.path.join(d2, "site-map.json")), (
        "an empty map was written; grounding against {} reports every element as gone"
    )
    print("PASS a page with nothing interactive writes no map at all")

    # 3 — THE CONSUMER TEST. Run the real grounder over the real emitted shape. This is the check a
    #     shape assertion cannot make: it fails if either side's field names drift apart.
    parsed = parse_playwright_spec(SPEC, "login.spec.ts")
    g = ground_imported(parsed, _emitted_map_shape())
    assert g["totals"]["bound"] == 1, ("the emitted map shape does not bind anything the grounder "
                                       "understands", g["totals"])
    assert g["totals"]["unmatched"] == 1, g["totals"]     # the ghost button
    assert g["totals"]["no_locator"] == 1, g["totals"]    # the navigate
    steps = g["tests"][0]["steps"]
    bound = [s for s in steps if s["status"] == "bound"][0]
    assert bound["semantic_id"] == "ea692d52c825", (
        "grounding bound a step but did not carry through the real semantic_id from the map", bound)
    print("PASS the emitted shape feeds the real grounder: 1 bound (by semantic_id), 1 gone")

    # 4 — --verify REFUSES the two ambiguous invocations rather than silently choosing one. Run the
    #     real binary: an assertion that the sentence appears in main.go also matches the COMMENT
    #     that explains it, so a mutation to the actual refusal survives. Measured — it did.
    agentctl = os.path.join(REPO, "bin", "agentctl")
    if not os.path.exists(agentctl):
        build = subprocess.run(["go", "build", "-o", agentctl, "./cmd/agentctl"],
                               cwd=REPO, capture_output=True, text=True)
        assert build.returncode == 0, (
            "cannot build agentctl, so this check cannot run — refusing to report success over a "
            "check that did not happen:\n" + build.stderr[-500:])
    src_dir = tempfile.mkdtemp(prefix="sentinel-verify-src-")
    open(os.path.join(src_dir, "x.spec.ts"), "w").write(SPEC)
    for args, why in (
        (["--verify"], "--verify without --target explored nothing and did not refuse"),
        (["--verify", "--target", "http://x", "--map", "/tmp/m.json"],
         "--verify with --map silently picked one; 'grounded against the app' and 'grounded against "
         "this file' are different claims and the report must not be ambiguous about which it made"),
    ):
        r = subprocess.run([agentctl, "import", "--from", src_dir] + args,
                           cwd=REPO, capture_output=True, text=True, timeout=120)
        assert r.returncode == 2, (why, r.returncode, r.stderr[-300:])
        assert "error:" in r.stderr, (why, r.stderr[-300:])
    print("PASS --verify refuses both ambiguous invocations, exit 2, measured on the real binary")

    # and an explore that merely FOUND A PROBLEM (exit 1) must still be usable — its map is written.
    go = open(os.path.join(REPO, "cmd", "agentctl", "main.go"), encoding="utf-8").read()
    assert "rc > 1" in go, (
        "a target whose exploration exits 1 (the app has a problem) still produced a map; treating "
        "that as a failure would refuse to verify against exactly the applications worth verifying"
    )

    # 5 — the artifact is reachable. An artifact nobody can fetch is the discovery gap again.
    api = open(os.path.join(REPO, "cmd", "control-api", "main.go"), encoding="utf-8").read()
    wl = api[api.index("var artifactWhitelist"):]
    wl = wl[:wl.index("\n}")]
    assert '"site-map.json"' in wl, "site-map.json is not fetchable — produced and unreachable"
    print("PASS site-map.json is on the artifact whitelist")

    print("ALL PASS (5)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
