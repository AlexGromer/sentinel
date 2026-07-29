"""Offline gate: import someone else's Playwright suite, and diagnose it honestly (PROD-IMPORT).

Run:  .venv/bin/python tests/test_importer_offline.py

Transpiling another engine's tests is only half the value; the other half — the half that sells the
product — is the REPORT: what bound, what bound only by a weak locator, and what construct had no
equivalent and became of it. The one thing that must never happen is a SILENT change of meaning, so
these gates check the report as hard as the steps.

Behavioural, against a real .spec.ts fixture — never an assertion about the importer's source.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.importer as imp  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "testdata", "import", "playwright-login.spec.ts")


def _parsed():
    with open(FIXTURE, encoding="utf-8") as fh:
        return imp.parse_playwright_spec(fh.read(), "playwright-login.spec.ts")


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# 1 — every test boundary and every action/assert becomes a step; nothing is dropped to reach a
#     smaller, tidier plan.
def test_all_steps_transpiled():
    p = _parsed()
    _check(len(p["tests"]) == 2, f"expected 2 tests, got {len(p['tests'])}")
    by = {t["name"]: t for t in p["tests"]}
    login = by["login and reach the dashboard"]
    pay = by["pay an invoice"]
    _check(len(login["steps"]) == 6, f"login: {len(login['steps'])} steps, want 6")
    _check(len(pay["steps"]) == 6, f"pay: {len(pay['steps'])} steps, want 6 — a comment-trailed line was dropped?")
    verbs = [s["verb"] for s in login["steps"]]
    _check(verbs == ["navigate", "fill", "fill", "click", "assert", "assert"],
           f"login verbs = {verbs}")


# 2 — a secret entered via process.env.NAME stays a secretRef, never a literal (M9.1 / SEC-SCENARIO).
def test_secret_stays_a_ref():
    p = _parsed()
    login = [t for t in p["tests"] if t["name"].startswith("login")][0]
    pw_step = [s for s in login["steps"] if s.get("secretRef")]
    _check(len(pw_step) == 1, "the password fill did not become a secretRef")
    _check(pw_step[0]["secretRef"] == "LOGIN_PASSWORD", f"wrong secretRef: {pw_step[0]}")
    # and no step carries the env NAME as a literal value
    for t in p["tests"]:
        for s in t["steps"]:
            _check(s.get("value") != "LOGIN_PASSWORD" and "process.env" not in str(s.get("value", "")),
                   f"a secret leaked into a literal value: {s}")


# 3 — a frame scope survives the rewrite (ADR-095), or an imported step silently misses the control.
def test_frame_is_preserved():
    p = _parsed()
    pay = [t for t in p["tests"] if t["name"] == "pay an invoice"][0]
    card = [s for s in pay["steps"] if s.get("locator", {}).get("label") == "Card number"][0]
    _check(card["locator"].get("frame") == 'iframe[name="stripe"]',
           f"the frame scope was dropped: {card['locator']}")


# 4 — a construct Sentinel has no equivalent for is NAMED, not dropped in silence.
def test_no_equivalent_constructs_are_reported():
    p = _parsed()
    dropped = {n["construct"] for t in p["tests"] for n in t["notes"] if n["kind"] == "dropped"}
    _check("route" in dropped, "the network stub (route) was dropped without a note")
    _check("waitForTimeout" in dropped, "the explicit sleep (waitForTimeout) was dropped without a note")
    # each dropped note explains the CONSEQUENCE, not just the name.
    for t in p["tests"]:
        for n in t["notes"]:
            if n["kind"] == "dropped":
                _check(len(n.get("why", "")) > 20, f"dropped note has no explanation: {n}")


# 5 — a weak locator (text/css/label instead of testid/role+name) is flagged with its prior, so the
#     team sees which tests rest on a locator that will rot.
def test_weak_locators_are_flagged():
    p = _parsed()
    weak = [n for t in p["tests"] for n in t["notes"] if n["kind"] == "weak_locator"]
    strategies = {n["strategy"] for n in weak}
    # canonical strategy names from brain/strategies (single source): css and text_role are drift-prone.
    _check("css" in strategies, "the #pay-now css locator was not flagged as weak")
    _check("text_role" in strategies, "a getByText locator was not flagged as weak")
    # a stable label (0.88) is NOT flagged weak — only the drift-prone strategies are.
    _check("label" not in strategies, "a getByLabel locator (prior 0.88) was wrongly flagged weak")
    for n in weak:
        _check(n["prior"] < 0.85, f"a 'weak' note carries a non-weak prior: {n}")


# 6 — the report totals add up and match the steps, so the 'state of your suite' summary is true.
def test_report_totals_are_consistent():
    p = _parsed()
    rep = imp.rewrite_report(p)
    tot = rep["totals"]
    _check(tot["tests"] == 2 and tot["steps"] == 12, f"totals off: {tot}")
    _check(tot["bound"] == 11, f"bound = {tot['bound']}, want 11 (the toHaveURL assert has no locator)")
    _check(tot["dropped"] == 2, f"dropped = {tot['dropped']}, want 2")
    _check(tot["weak"] == 3, f"weak = {tot['weak']}, want 3 (text_role x2 + css x1; label is not weak)")
    # per-test bound never exceeds its step count — a report that over-counts is worse than none.
    for t in rep["tests"]:
        _check(t["bound"] <= t["steps"], f"{t['name']}: bound {t['bound']} > steps {t['steps']}")


# 7 — the filesystem channel (channel 1) end to end: the brain's _run_import walks the dir, writes the
#     aggregate report and the transpiled scenarios, and exits 0. This is what `agentctl import --from`
#     drives — the market-entry path (in CI the repo is already checked out).
def test_fs_channel_writes_report_and_scenarios():
    import json as _json
    import pathlib
    import tempfile
    from brain.__main__ import _run_import

    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d)
        rc = _run_import(out, os.path.join(REPO, "testdata", "import"))
        _check(rc == 0, f"_run_import exit={rc}, want 0")
        rep = _json.loads((out / "import-report.json").read_text())
        _check(rep["engine"] == "playwright", "report does not name the source engine")
        _check(rep["totals"]["tests"] == 2 and rep["totals"]["steps"] == 12,
               f"aggregate totals wrong: {rep['totals']}")
        _check(rep["totals"]["dropped"] == 2, "the aggregate lost the dropped-construct count")
        scen = _json.loads((out / "imported-scenarios.json").read_text())
        _check(len(scen["tests"]) == 2, "the transpiled scenarios were not written")
        # a bad dir is a clean exit 3, not a crash.
        _check(_run_import(out, os.path.join(d, "nope")) == 3, "a missing import dir must exit 3")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok   {t.__name__}")
    print(f"\nimporter: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
