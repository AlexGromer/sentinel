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
    _check("css" in strategies, "the #pay-now css locator was not flagged as weak")
    _check("text" in strategies, "a getByText locator was not flagged as weak")
    for n in weak:
        _check(n["prior"] < 0.95, f"a 'weak' note carries a strong prior: {n}")


# 6 — the report totals add up and match the steps, so the 'state of your suite' summary is true.
def test_report_totals_are_consistent():
    p = _parsed()
    rep = imp.rewrite_report(p)
    tot = rep["totals"]
    _check(tot["tests"] == 2 and tot["steps"] == 12, f"totals off: {tot}")
    _check(tot["bound"] == 11, f"bound = {tot['bound']}, want 11 (the toHaveURL assert has no locator)")
    _check(tot["dropped"] == 2, f"dropped = {tot['dropped']}, want 2")
    _check(tot["weak"] >= 4, f"weak = {tot['weak']}, want >= 4")
    # per-test bound never exceeds its step count — a report that over-counts is worse than none.
    for t in rep["tests"]:
        _check(t["bound"] <= t["steps"], f"{t['name']}: bound {t['bound']} > steps {t['steps']}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok   {t.__name__}")
    print(f"\nimporter: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
