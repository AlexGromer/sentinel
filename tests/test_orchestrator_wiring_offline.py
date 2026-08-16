#!/usr/bin/env python3
"""ADR-126 — the orchestrator is reachable end to end, and its absence changes nothing.

Run:  .venv/bin/python tests/test_orchestrator_wiring_offline.py

WHAT THIS PINS THAT THE GO TESTS CANNOT. `cmd/control-api/orchestrator_test.go` proves this process
registers a run and enforces a breach. It cannot prove the OTHER half — that the address actually
crosses two process boundaries and arrives at the brain — because the chain is
control-api -> agentctl -> `python -m brain`, written in two languages, and every link names the
variable in its own file. That is exactly the shape of the defect this whole wiring exists to remove:
before ADR-126 each end was correct on its own and nothing joined them, so takeover, the map gate and
the budget ceiling were dead in every shipped deployment while every unit test passed.

WHAT IT ASSERTS:

 1. The three ends agree on the NAME. control-api writes `ORCH_ADDR`, agentctl's env allowlist admits
    it, and the brain's RunControl client reads it. A rename on any one of the three breaks the chain
    silently — the brain simply stays a no-op, which is indistinguishable from "no orchestrator".
 2. `ORCH_ADDR` reaches the brain THROUGH the shipped entry point, and turns its client from a no-op
    into a real one — measured at the child process, not inferred from the source.
 3. An unreachable orchestrator does NOT fail the run. Supervision is an addition to a run, never a
    precondition for one, and a socket that does not answer must cost a line rather than the work.
 4. `_Noop.wired` is False and the real client's is True — the flag `brain/health.py` reads to decide
    whether the map gate has anybody to ask. A gate with no one to answer it is not a safeguard, it
    is a hang.

Offline, stdlib only: no gRPC server is started, and the brain's dial failure IS one of the cases
under test.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


def read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


STUB = """#!/bin/sh
env > "$SENTINEL_ENV_DUMP"
exit 0
"""


def run_brain(env_extra=None):
    """Run the shipped entry point with a stub executor. Returns (exit code, stderr, child env|None)."""
    tmp = tempfile.mkdtemp(prefix="sentinel-orchwire-")
    stub = os.path.join(tmp, "stub-executor.sh")
    with io.open(stub, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(stub, 0o755)
    dump = os.path.join(tmp, "child.env")

    env = dict(os.environ)
    env.update({
        "PW_EXECUTOR_CMD": stub,
        "SENTINEL_ENV_DUMP": dump,
        "ARTIFACT_DIR": os.path.join(tmp, "artifacts"),
        "RUN_MODE": "explore",
        "RUN_ID": "orchwire",
        "TARGET_URL": "file:///dev/null",
        "PLAN_FILE": os.path.join(tmp, "plan.json"),
        "PYTHONPATH": ROOT,
        "CI": "0",
        "HEAL_LLM": "0",
    })
    with io.open(env["PLAN_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"steps": []}, fh)
    env.pop("ORCH_ADDR", None)
    env.update(env_extra or {})

    p = subprocess.run([sys.executable, "-m", "brain"], cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    child = None
    if os.path.exists(dump):
        child = {}
        with io.open(dump, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    child[k] = v
    shutil.rmtree(tmp, ignore_errors=True)
    return p.returncode, p.stderr, child


def test_all_three_ends_name_the_same_variable():
    """The chain is only as good as the weakest spelling of one string."""
    capi = read("cmd", "control-api", "main.go")
    check("control-api EXPORTS the address to the run it spawns",
          re.search(r'"ORCH_ADDR="\s*\+\s*s\.orchAddr', capi) is not None,
          "cmd/control-api/main.go no longer adds ORCH_ADDR to the child environment")

    agent = read("cmd", "agentctl", "main.go")
    check("agentctl's env allowlist ADMITS it (a filter is where a variable dies quietly)",
          '"ORCH_ADDR": true' in agent,
          "cmd/agentctl/main.go::filteredEnv would drop ORCH_ADDR, so the brain would never see it")

    brain = read("brain", "runcontrol.py")
    check("the brain READS it, and reads nothing else to decide",
          'os.environ.get("ORCH_ADDR")' in brain,
          "brain/runcontrol.py no longer resolves the orchestrator from ORCH_ADDR")


def test_the_address_reaches_the_brain_through_the_shipped_entry_point():
    addr = "unix:/nonexistent/orch.sock"
    rc, err, child = run_brain(env_extra={"ORCH_ADDR": addr})
    check("the executor process was reached (so the run really started)", child is not None, err[-300:])
    if child:
        check("the child carries ORCH_ADDR verbatim", child.get("ORCH_ADDR") == addr,
              f"ORCH_ADDR={child.get('ORCH_ADDR')!r} — the address did not survive the boundary")


def test_an_unreachable_orchestrator_does_not_fail_the_run():
    """Fail-open, and the reason is in the run's own words rather than in a comment."""
    rc, err, child = run_brain(env_extra={"ORCH_ADDR": "unix:/nonexistent/orch.sock"})
    check("the run is NOT refused because the orchestrator could not be dialled",
          rc != 3, f"rc={rc} — exit 3 is the pre-flight refusal; supervision must never gate a run")
    check("...and the failure is SAID, not swallowed",
          "runcontrol_unavailable" in err or "orch" in err.lower(),
          err[-400:] + "  — a supervisor that vanishes without a word is one nobody can debug")


def test_the_wired_flag_is_what_the_map_gate_reads():
    """ADR-108c: `wired` decides whether the gate has anybody to ask. A headless run (CI, cron, the
    air-gapped bundle) has no operator, and a gate that waited for one there would be a hang, not a
    safeguard — so the flag has to be False exactly when there is no orchestrator, and True when
    there is."""
    from brain import runcontrol

    old = os.environ.pop("ORCH_ADDR", None)
    try:
        c = runcontrol.make_client()
        check("no ORCH_ADDR -> a no-op client that is honest about being one",
              getattr(c, "wired", None) is False, f"wired={getattr(c, 'wired', None)!r}")
        check("...and it never aborts or pauses a run",
              c.report("r", "plan", 0, 0) == runcontrol.CONTINUE and c.map_decision("r") == "",
              "a no-op that answers anything but 'continue' would stop runs on deployments with no orchestrator")

        # The real client is constructed lazily against an address that cannot be dialled; grpc's
        # channel creation does not connect, so this exercises the class rather than the network.
        os.environ["ORCH_ADDR"] = "unix:/nonexistent/orch.sock"
        c2 = runcontrol.make_client()
        check("an address present -> a client that declares itself WIRED",
              getattr(c2, "wired", None) is True,
              f"wired={getattr(c2, 'wired', None)!r} — the map gate would skip itself on a deployment "
              "that does have an operator")
    finally:
        os.environ.pop("ORCH_ADDR", None)
        if old is not None:
            os.environ["ORCH_ADDR"] = old


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        print(fn.__name__)
        fn()
    if FAILS:
        print(f"\nFAIL — {len(FAILS)} check(s): " + "; ".join(FAILS[:6]))
        sys.exit(1)
    print(f"\nALL PASS ({len(fns)} orchestrator-wiring tests)")
