"""Offline gate: append-only test revisions, step diff, and history-preserving rollback (PROD-VERSIONING).

Run:  .venv/bin/python tests/test_revisions_offline.py

The claims this pins are the ones plan_hash alone could not make: there is a HISTORY (not just "same
or not"), a step-level answer to "how did it differ", and a rollback that PUTS IT BACK without erasing
what was in between. Behavioural, against the real store on a temp dir; `now` is injected so ordering
is deterministic without touching the wall clock.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.revisions as R  # noqa: E402
from brain.state import canonical_plan_hash  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _check(c, m):
    if not c:
        raise AssertionError(m)


P1 = {"steps": [{"semantic_id": "a", "verb": "fill", "value": "x"},
                {"semantic_id": "b", "verb": "click"}]}
P2 = {"steps": [{"semantic_id": "a", "verb": "fill", "value": "CHANGED"},
                {"semantic_id": "c", "verb": "press", "key": "Enter"}]}


# 1 — the revision id IS the plan_hash, and saves are append-only + idempotent.
def test_append_only_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        clk = _Clock()
        r1 = R.save_revision(d, "login", P1, clk)
        _check(r1["revision"] == canonical_plan_hash(P1["steps"]), "revision id is not the plan_hash")
        _check(r1["new"] is True and r1["parent"] is None, f"first save wrong: {r1}")
        # re-saving the identical plan does not grow the history (a no-change re-run is not a revision).
        r1b = R.save_revision(d, "login", P1, clk)
        _check(r1b["new"] is False, "an identical re-save created a new revision")
        _check(len(R.list_revisions(d, "login")) == 1, "history grew on an idempotent save")
        r2 = R.save_revision(d, "login", P2, clk)
        _check(r2["new"] is True and r2["parent"] == r1["revision"], f"second save wrong: {r2}")
        _check(len(R.list_revisions(d, "login")) == 2, "history did not record the second revision")
        # the older revision body is still readable — nothing was overwritten.
        _check(R.get_plan(d, "login", r1["revision"]) == P1, "an older revision was lost")


# 2 — the diff names exactly what changed, per step.
def test_step_diff_is_precise():
    with tempfile.TemporaryDirectory() as d:
        clk = _Clock()
        a = R.save_revision(d, "login", P1, clk)["revision"]
        b = R.save_revision(d, "login", P2, clk)["revision"]
        diff = R.diff_revisions(d, "login", a, b)
        _check([x["key"] for x in diff["added"]] == [["sid", "c"]], f"added wrong: {diff['added']}")
        _check([x["key"] for x in diff["removed"]] == [["sid", "b"]], f"removed wrong: {diff['removed']}")
        _check(len(diff["changed"]) == 1, f"changed count wrong: {diff['changed']}")
        ch = diff["changed"][0]
        _check(ch["key"] == ["sid", "a"] and ch["fields"] == ["value"], f"changed step wrong: {ch}")
        _check(ch["before"]["value"] == "x" and ch["after"]["value"] == "CHANGED",
               f"before/after wrong: {ch}")


# 3 — rollback restores the head WITHOUT deleting the history in between.
def test_rollback_preserves_history():
    with tempfile.TemporaryDirectory() as d:
        clk = _Clock()
        a = R.save_revision(d, "login", P1, clk)["revision"]
        R.save_revision(d, "login", P2, clk)
        _check(R.head(d, "login") != a, "head should be the second revision before rollback")
        rb = R.rollback(d, "login", a, clk)
        _check(rb["op"] == "rollback", "rollback did not record itself as a rollback op")
        _check(R.head(d, "login") == a, "rollback did not restore the target as head")
        # append-only: the history GREW (save, save, rollback) — nothing was deleted.
        hist = R.list_revisions(d, "login")
        _check(len(hist) == 3, f"rollback did not preserve history: {len(hist)} entries, want 3")
        _check(hist[-1]["op"] == "rollback" and hist[-1]["revision"] == a, "last entry is not the rollback")


# 4 — rollback to an id that was never saved is an error, not a silent no-op that loses the request.
def test_rollback_unknown_revision_raises():
    with tempfile.TemporaryDirectory() as d:
        R.save_revision(d, "login", P1, _Clock())
        try:
            R.rollback(d, "login", "deadbeef" * 8)
            raise AssertionError("rollback to an unknown revision did not raise")
        except ValueError:
            pass


# 5 — a scenario_id that tries to traverse out of the store is refused before any write.
def test_scenario_id_cannot_traverse():
    with tempfile.TemporaryDirectory() as d:
        for bad in ["../evil", "a/b", "..", ""]:
            try:
                R.save_revision(d, bad, P1, _Clock())
                raise AssertionError(f"a traversal-shaped scenario_id was accepted: {bad!r}")
            except ValueError:
                pass


# 6 — the LIVE wire: freezing a scenario records a revision when SENTINEL_TEST_ID is set, and records
#     NOTHING when it is not (an ad-hoc run is not a versioned test). This is what keeps the store from
#     being a library nobody calls — the chats-projection lesson.
def test_freeze_scenario_records_a_revision_when_named():
    import pathlib
    from brain.__main__ import _write_scenario

    steps = [{"semantic_id": "a", "verb": "fill", "value": "x"}]
    with tempfile.TemporaryDirectory() as d:
        revdir = os.path.join(d, "revs")
        os.environ["SENTINEL_REVISIONS_DIR"] = revdir
        try:
            # named test -> a revision is recorded under that id.
            os.environ["SENTINEL_TEST_ID"] = "login-test"
            out = pathlib.Path(d) / "run1"
            out.mkdir()
            _write_scenario(out, "run1", "http://x", steps, [], False)
            hist = R.list_revisions(revdir, "login-test")
            _check(len(hist) == 1, f"a named scenario freeze recorded no revision: {hist}")

            # ad-hoc run (no id) -> nothing recorded.
            os.environ.pop("SENTINEL_TEST_ID", None)
            out2 = pathlib.Path(d) / "run2"
            out2.mkdir()
            _write_scenario(out2, "run2", "http://x", steps, [], False)
            _check(R.list_revisions(revdir, "login-test") == hist,
                   "an unnamed run added a revision — ad-hoc runs must not be versioned")
        finally:
            os.environ.pop("SENTINEL_TEST_ID", None)
            os.environ.pop("SENTINEL_REVISIONS_DIR", None)


# 7 — a crafted revision id cannot become a path: get_plan/rollback validate it is a 64-hex hash
#     before it is used as a filename, so "../../etc/x" resolves to None, not a traversal.
def test_revision_id_cannot_traverse():
    with tempfile.TemporaryDirectory() as d:
        R.save_revision(d, "login", P1, _Clock())
        for bad in ["../../etc/passwd", "..", "a/b", "deadbeef", "g" * 64, "A" * 64, ""]:
            _check(R.get_plan(d, "login", bad) is None, f"a malformed revision id was resolved: {bad!r}")
            try:
                R.rollback(d, "login", bad)
                raise AssertionError(f"rollback accepted a malformed revision id: {bad!r}")
            except ValueError:
                pass
        # a real 64-hex revision still works (the validation does not break the happy path).
        rev = R.head(d, "login")
        _check(len(rev) == 64 and R.get_plan(d, "login", rev) == P1, "a valid revision stopped resolving")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok   {t.__name__}")
    print(f"\nrevisions: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
