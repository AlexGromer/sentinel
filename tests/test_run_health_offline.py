"""HEALTH-001 — a run refuses to start when its mode needs a component that is not there.

The case that made this necessary, in full: `goal` mode with no model reachable. `make_backend`
returns None, the planner falls back to the heuristic, THE GOAL IS IGNORED, and the run finishes. Not a crash. Every
artefact looks right, and the only way to notice is to read the plan and realise it has nothing to do
with what was asked.

The exit code varies with the fixture — measured at 1 where the heuristic grounds nothing and 0 where
it grounds something — which is worse than a consistent lie: half the time it also looks like a pass.
So the property asserted below is that the run does not START, not that it fails in some particular
way afterwards.

TWO PROPERTIES, and the second is the one that keeps this gate honest.

  1. A run that needs something missing REFUSES, with exit 3 and a named reason.
  2. A run that does NOT need it still runs. This is not a formality: a gate that demanded everything
     would break CI (no key, no store, no orchestrator) and the air-gapped bundle on the day it
     landed, and a gate that breaks those gets switched off. So `explore` without a model, and
     `replay` without a store, are asserted to still work — the refusal has to be narrow to survive.

WHY EXIT 3 AND NOT A NEW CODE: docs/DETERMINISM.md and brain/events.json's exit_codes table both
define 3 as integrity / bad invocation, and `fatal.executor_cmd_unset` already uses it for exactly
this shape — "the configuration does not add up, a human is needed". A new code would have meant a
new contract for a case the existing one already describes.

The requirement logic is tested directly rather than only through subprocesses: `requirements()` is
where the product decision lives ("replay does not need an LLM"), and a subprocess test can only
show that one combination behaved, not that the rule is the rule.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain import health  # noqa: E402


def _clean_env(**overrides: str) -> "dict[str, str]":
    """An environment with every model credential removed, plus whatever the case sets.

    Built by subtraction rather than from scratch: PATH, HOME and the venv have to survive, and a
    hand-built environment that omits one of them fails for a reason that has nothing to do with the
    property under test.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_BACKEND", "LLM_MODEL",
                        "LLM_BASE_URL", "LLM_API_KEY", "LLM_BACKEND_PLANNER", "LLM_MODEL_PLANNER",
                        "LLM_BASE_URL_PLANNER", "LLM_API_KEY_PLANNER", "STORE_ADDR", "ORCH_ADDR",
                        "GOAL", "DESCRIBE", "SENTINEL_HEALTH_SKIP"}}
    env.update(overrides)
    return env


def _run(env_overrides: "dict[str, str]", args: "list[str]") -> "tuple[int, str]":
    agentctl = REPO / "bin" / "agentctl"
    if not agentctl.exists():
        return -1, "agentctl not built"
    out = tempfile.mkdtemp(prefix="health-gate-")
    env = _clean_env(PW_NO_TRACE="1", BRAIN_PYTHON=str(REPO / ".venv" / "bin" / "python"),
                     **env_overrides)
    p = subprocess.run([str(agentctl), "run", "--target",
                        f"file://{REPO}/testdata/site/index.html", "--artifact-dir", out] + args,
                       env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout + p.stderr


def _skip(reason: str) -> None:
    print(f"     SKIP — {reason}")


# ---------------------------------------------------------------- the requirement rule itself

def test_a_mode_requires_only_what_it_actually_needs():
    """The product decision, asserted directly.

    Each of these is a choice somebody could reverse by accident while "tightening" the gate, and
    each reversal breaks a shipped path: replay without a store is the documented CI and air-gapped
    default; explore without a model is the deterministic heuristic walk the whole golden mechanism
    rests on.
    """
    keep = {k: os.environ.get(k) for k in ("STORE_ADDR", "ORCH_ADDR", "SENTINEL_MAP_GATE")}
    for k in keep:
        os.environ.pop(k, None)
    try:
        assert health.LLM not in health.requirements("explore", has_objective=False), (
            "plain explore was made to require a model — that is the deterministic heuristic walk "
            "the golden mechanism depends on, and CI has no key")
        assert health.LLM in health.requirements("explore", has_objective=True), (
            "a goal-directed run was allowed to start without a model — the exact failure this "
            "exists to stop: the goal is ignored and the run finishes on the heuristic walk")
        assert health.LLM not in health.requirements("replay", has_objective=False), (
            "replay was made to require a model — L1-L6 healing is offline by design and heal-LLM "
            "is explicitly opt-in")
        assert health.STORE not in health.requirements("replay", has_objective=False), (
            "replay was made to require a store even when none was declared — LocalStore is the "
            "documented default for CI and the air-gapped bundle")
        assert health.requirements("report", has_objective=False) == set(), (
            "a no-browser mode was made to require components it never touches")

        # A DECLARED store must answer; an undeclared one means LocalStore.
        os.environ["STORE_ADDR"] = "unix:/tmp/nope.sock"
        assert health.STORE in health.requirements("replay", has_objective=False), (
            "a store was declared via STORE_ADDR and the run did not require it to answer — that is "
            "an operator who believes they have persistence and does not")
        os.environ.pop("STORE_ADDR")

        # The orchestrator is required only when the map gate is ON and an address was declared.
        os.environ["ORCH_ADDR"] = "unix:/tmp/nope.sock"
        assert health.ORCHESTRATOR in health.requirements("explore", has_objective=True)
        os.environ["SENTINEL_MAP_GATE"] = "0"
        assert health.ORCHESTRATOR not in health.requirements("explore", has_objective=True), (
            "the orchestrator was required with the map gate switched off — that would break every "
            "unattended run, which is what the switch is for")
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_the_llm_check_agrees_with_the_backend_it_mirrors():
    """`_llm_configured` copies make_backend's branches, and a copy is a liability.

    If llm.py changes what counts as configured and this does not, the gate starts refusing runs
    that would have worked — or, worse, passing runs that will silently degrade. Asserted against
    the real make_backend rather than against a second copy of the reasoning.
    """
    from brain import llm

    cases = [
        ({}, False),
        ({"ANTHROPIC_API_KEY": "x"}, True),
        ({"LLM_BACKEND": "openai"}, False),                                    # no model
        ({"LLM_BACKEND": "openai", "LLM_MODEL": "m"}, False),                  # no key, no base_url
        ({"LLM_BACKEND": "openai", "LLM_MODEL": "m", "LLM_BASE_URL": "http://x/v1"}, True),
        ({"LLM_BACKEND": "openai", "LLM_MODEL": "m", "OPENAI_API_KEY": "k"}, True),
        ({"LLM_BACKEND": "nonsense"}, False),
    ]
    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_BACKEND", "LLM_MODEL", "LLM_BASE_URL",
              "LLM_API_KEY")}
    try:
        for overrides, expected in cases:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(overrides)
            got = health._llm_configured()
            assert got is expected, (
                f"_llm_configured() said {got} for {overrides}, expected {expected}")
            # And the thing it mirrors agrees. make_backend returning a backend is the ground truth;
            # this is what catches llm.py moving underneath the mirror.
            real = llm.make_backend("planner") is not None
            assert real is expected, (
                f"make_backend said {real} for {overrides} while this gate expects {expected} — "
                f"llm.py and brain/health.py have drifted apart, and the mirror is now wrong")
    finally:
        for k in saved:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------- end to end, through agentctl

def test_a_goal_run_without_a_model_refuses_with_exit_3_and_says_why():
    code, out = _run({}, ["--goal", "log in and check the dashboard"])
    if code == -1:
        return _skip(out)
    assert code == 3, (
        f"a goal run with no model exited {code}, not 3.\n\n"
        f"  Any code but 3 means the run STARTED. That is the defect, and the exit code it happens "
        f"to reach is incidental: the goal is silently ignored, the heuristic walks the site, and "
        f"whether that ends 0 or 1 depends only on whether it grounded anything — measured as 1 on "
        f"this fixture and 0 on richer ones. Neither is an answer to what was asked.\n{out[-600:]}")
    assert "fatal.llm_required_unreachable" in out, (
        f"the refusal did not name the catalogued reason:\n{out[-600:]}")


def test_a_plain_explore_without_a_model_still_runs():
    """The half that keeps the gate narrow enough to survive.

    If this ever fails, the gate has been tightened into something CI and the air-gapped bundle
    cannot use, and the next step after that is somebody switching it off entirely.
    """
    code, out = _run({}, ["--planner", "heuristic"])
    if code == -1:
        return _skip(out)
    assert code == 0, (
        f"a plain heuristic explore with no model exited {code} — the deterministic walk must not "
        f"need one.\n{out[-600:]}")
    assert "EXPLORE COMPLETE" in out


def test_the_escape_hatch_works_and_announces_itself():
    """An override that goes quiet is indistinguishable from a gate that was never there.

    The hatch has to exist — a false positive would otherwise block all work with no recourse short
    of editing code — but a run that used it must carry that fact, which is why the skip is a
    `degrades: true` event rather than a comment in a log.
    """
    code, out = _run({"SENTINEL_HEALTH_SKIP": "llm"}, ["--goal", "log in"])
    if code == -1:
        return _skip(out)
    assert code != 3 or "fatal.llm_required_unreachable" not in out, (
        f"SENTINEL_HEALTH_SKIP=llm did not skip the LLM check:\n{out[-600:]}")
    assert "system.health_check_skipped" in out, (
        f"the skip was honoured SILENTLY — a bypassed check must announce itself:\n{out[-600:]}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} run-health tests passed")
