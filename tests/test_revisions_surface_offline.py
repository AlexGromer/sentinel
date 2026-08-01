"""Offline gate: the revision store is READABLE (PROD-VERSIONING, PR-7).

Run:  .venv/bin/python tests/test_revisions_surface_offline.py

ADR-106 built the store and wired the WRITE: append-only history, step-level diff, a rollback that
re-appends rather than deletes. What it never had was a way to read any of it back — no subcommand,
no route, no screen. Measured before this: `grep -r 'revision|rollback' cmd/ docs/index.html` returned
nothing, and on this working tree `state/revisions/` did not exist at all, so the feature had never
run outside its own unit tests.

A revision written and unreachable is not history. It is a file.

What is pinned here is the SURFACE, not the store (the store has its own gate). Specifically:

  - the four operations work through the REAL binary, against a REAL store on disk;
  - `diff` with no arguments answers the question a person actually asks — "what changed?" — by
    comparing the last two, and REFUSES when there is no pair rather than returning an empty diff
    that reads as "nothing changed";
  - `rollback` never assumes its target. Guessing "the previous one" would make an irreversible-
    looking action depend on intent the caller never stated;
  - rollback GROWS the history rather than truncating it, so the surface cannot be used to erase.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain import revisions as R  # noqa: E402

AGENTCTL = os.path.join(REPO, "bin", "agentctl")


def ctl(root, *args, timeout=300):
    env = dict(os.environ, SENTINEL_REVISIONS_DIR=root, PYTHONPATH=REPO)
    return subprocess.run([AGENTCTL, "revisions"] + list(args), cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=timeout)


def payload(r):
    assert r.returncode == 0, (r.returncode, r.stderr[-400:])
    return json.loads(r.stdout)


def main():
    assert os.path.exists(AGENTCTL), (
        "bin/agentctl is not built, so this gate cannot run — refusing to report success over a "
        "check that did not happen (CI builds it before the suite)"
    )
    root = tempfile.mkdtemp(prefix="sentinel-revsurface-")
    try:
        v1 = {"steps": [{"semantic_id": "a", "verb": "click", "intent": "sign in"}]}
        v2 = {"steps": [{"semantic_id": "a", "verb": "click", "intent": "sign in"},
                        {"semantic_id": "b", "verb": "assert", "intent": "dashboard"}]}
        r1 = R.save_revision(root, "login-test", v1)
        r2 = R.save_revision(root, "login-test", v2)

        # 1 — LIST reaches the history the store wrote.
        d = payload(ctl(root, "list", "--test", "login-test"))
        assert [x["revision"] for x in d["revisions"]] == [r1["revision"], r2["revision"]], d
        assert d["head"] == r2["revision"], d
        print("PASS list reads the real history back, oldest first, and names the head")

        # 2 — SHOW returns the stored plan, and defaults to the head (the one a person means).
        d = payload(ctl(root, "show", "--test", "login-test"))
        assert d["plan"] == v2, d["plan"]
        d = payload(ctl(root, "show", "--test", "login-test", "--rev", r1["revision"]))
        assert d["plan"] == v1, d["plan"]
        print("PASS show returns the stored plan; bare show means the head")

        # 3 — DIFF with no arguments compares the last two. This is the one place guessing is right:
        #     there is exactly one sensible pair, and it is the question being asked.
        d = payload(ctl(root, "diff", "--test", "login-test"))
        assert d["a"] == r1["revision"] and d["b"] == r2["revision"], (d["a"], d["b"])
        added = d["diff"]["added"]
        assert len(added) == 1 and added[0]["step"]["semantic_id"] == "b", d["diff"]
        assert d["diff"]["removed"] == [], d["diff"]
        print("PASS bare diff compares the last two and names the added step")

        # 4 — and it REFUSES when there is no pair. An empty diff would read as "nothing changed",
        #     which is a different and wrong answer.
        R.save_revision(root, "lonely", v1)
        r = ctl(root, "diff", "--test", "lonely")
        assert r.returncode != 0, "a single-revision test produced a diff instead of refusing"
        print("PASS diff refuses when there is no pair, instead of answering 'nothing changed'")

        # 5 — ROLLBACK never assumes a target, and GROWS the history: the surface cannot erase.
        r = ctl(root, "rollback", "--test", "login-test")
        assert r.returncode == 2, ("rollback guessed a target", r.returncode, r.stderr[-200:])
        before = len(R.list_revisions(root, "login-test"))
        d = payload(ctl(root, "rollback", "--test", "login-test", "--rev", r1["revision"]))
        assert d["head"]["revision"] == r1["revision"], d
        after = R.list_revisions(root, "login-test")
        assert len(after) == before + 1, ("rollback truncated history instead of appending", before, len(after))
        assert after[-1]["op"] == "rollback", after[-1]
        # the intermediate revision is still there — "put it back" did not erase what it replaced.
        assert any(x["revision"] == r2["revision"] for x in after), after
        print("PASS rollback demands its target, appends, and erases nothing")

        # 6 — an unknown test or revision is a 'not found', not a crash and not an empty success.
        r = ctl(root, "list", "--test", "no-such-test")
        d = json.loads(r.stdout) if r.returncode == 0 else None
        assert r.returncode == 0 and d["revisions"] == [] and d["head"] is None, (r.returncode, r.stdout[:200])
        r = ctl(root, "show", "--test", "login-test", "--rev", "0" * 64)
        assert r.returncode == 3, ("an unknown revision did not report not-found", r.returncode)
        r = ctl(root, "bogus-op", "--test", "login-test")
        assert r.returncode == 2, r.returncode
        print("PASS unknown test -> empty history; unknown revision -> not found; bad op -> refused")

        # 7 — the HTTP surface exists for all four operations, or the hub has nothing to call.
        # The whole package, not main.go alone: ADR-109's second half moved the registrations into the
        # access.go declaration table, and a gate pinned to one file reported all four routes missing.
        apidir = os.path.join(REPO, "cmd", "control-api")
        api = "\n".join(
            open(os.path.join(apidir, f), encoding="utf-8").read()
            for f in sorted(os.listdir(apidir))
            if f.endswith(".go") and not f.endswith("_test.go"))
        for route in ("GET /v1/tests/{id}/revisions",
                      "GET /v1/tests/{id}/revisions/diff",
                      "GET /v1/tests/{id}/revisions/show",
                      "POST /v1/tests/{id}/revisions/rollback"):
            assert route in api, "route not registered: " + route
        print("PASS all four operations are registered as routes")

        print("ALL PASS (7)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
