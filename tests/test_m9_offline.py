"""Offline M9.1 tests — form/login/validation primitives (no browser, no network, no LLM).

Run:  .venv/bin/python tests/test_m9_offline.py     (or: pytest tests/)

A FakeEx simulates pw-executor so we can deterministically exercise the new step kinds via replay:
- secret-non-leak: a `fill` step passes `secretRef` (the env-var NAME), NEVER the value, over the wire,
  and the secret never appears in any artifact (report / heal-report.json / rec);
- assert / negative-testing exit composition (0 / 1 / quarantine-suppressed / a11y exit-2 dominance);
- determinism: plan_hash is independent of the resolved secret, the frozen steps are not mutated by
  replay (secret resolution + selectOption result stay out of the step), tampering a new field aborts (exit 3);
- heal reuse with a non-click verb: a broken `fill` locator heals via testid and the FILL verb (not click)
  is applied to the healed locator;
- exporter forward-compat + the brain/validation.py invalid-input generator (sketch).
"""
import copy
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import Store                  # noqa: E402
from brain.healing import HealingEngine        # noqa: E402
from brain.replay import run_replay            # noqa: E402
from brain.state import canonical_plan_hash    # noqa: E402
from brain.exporter import export_spec         # noqa: E402
from brain import validation                   # noqa: E402
from brain import __main__ as brain_main       # noqa: E402

SECRET = "hunter2"   # the literal that MUST NOT appear anywhere on the brain side


class FakeEx:
    """Simulated pw-executor. Records every (method, params); returns canned results.

    Knobs:  expect_returns -> browser.expect {"ok": ...};  a11y/shot -> golden hashes;
            probe -> {locator-signature: count} (default 1, i.e. the locator resolves).
    """

    def __init__(self, *, expect_returns=True, a11y='- alert "ok"', shot="shot1", probe=None):
        self.calls = []
        self.url = ""
        self._expect = expect_returns
        self._a11y = a11y
        self._shot = shot
        self._probe = probe or {}

    @staticmethod
    def _sig(loc):
        if not loc:
            return ""
        if loc.get("testid"):
            return "testid:" + loc["testid"]
        if loc.get("role"):
            return "role:%s:%s" % (loc.get("role"), loc.get("name"))
        if loc.get("label"):
            return "label:" + loc["label"]
        if loc.get("text"):
            return "text:" + loc["text"]
        return json.dumps(loc, sort_keys=True)

    def call(self, m, **p):
        self.calls.append((m, p))
        if m == "browser.navigate":
            self.url = p.get("url", "")
            return {"url": self.url, "title": "", "status": 200}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": self._a11y, "nodeCount": 1}
        if m == "browser.screenshotHash":
            return {"hash": self._shot}
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.probe":
            return {"count": self._probe.get(self._sig(p.get("locator") or {}), 1)}
        if m == "browser.expect":
            return {"ok": self._expect}
        if m == "browser.select":
            return {"selected": [p.get("value")]}
        if m in ("browser.click", "browser.fill", "browser.type", "browser.press"):
            return {"ok": True}
        if m == "browser.saveStorageState":
            return {"path": p.get("path")}
        return {}


# --- builders ----------------------------------------------------------------
def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


def _he(ex, st):
    return HealingEngine(ex, st, "r", use_llm=False)


def _frozen(steps):
    return {"plan_id": "m9", "target_url": "file:///s/login.html",
            "plan_hash": canonical_plan_hash(steps), "steps": steps}


def _nav(target="file:///s/login.html"):
    return {"step_id": 1, "action_type": "navigate", "semantic_id": "nav", "intent": "go",
            "target": target, "locator": None, "alternatives": None}


def _assert(*, expect_ok=True, condition="visible", locator=None, expected=None, sid="asrt", step_id=2):
    return {"step_id": step_id, "action_type": "assert", "semantic_id": sid, "intent": "assert",
            "locator": locator if locator is not None else {"role": "alert"},
            "condition": condition, "expected": expected, "expect_ok": expect_ok}


def _login_steps():
    """navigate -> fill(user literal) -> fill(password via secretRef) -> click(submit)."""
    return [
        _nav(),
        {"step_id": 2, "action_type": "fill", "semantic_id": "u", "intent": "user",
         "locator": {"label": "User"}, "value": "alice", "alternatives": None},
        {"step_id": 3, "action_type": "fill", "semantic_id": "pw", "intent": "pass",
         "locator": {"label": "Password"}, "secretRef": "LOGIN_PASSWORD", "alternatives": None},
        {"step_id": 4, "action_type": "click", "semantic_id": "submit", "intent": "submit",
         "locator": {"role": "button", "name": "Sign in"}, "alternatives": None},
    ]


def _find(calls, method):
    return [p for (m, p) in calls if m == method]


def _write_plan(steps):
    pf = os.path.join(tempfile.mkdtemp(), "plan.json")
    with open(pf, "w") as f:
        json.dump(_frozen(steps), f)
    return pf


def _run_main_replay(ex, *, save_state=None, pw_no_trace=None, steps=None):
    """Drive brain.__main__._run_replay (the wrapper, not the replay module) under a temp cwd so the
    relative _STORE_PATH='state/locators.db' side-effect can't pollute the repo. Returns the exit code."""
    cwd0 = os.getcwd()
    work, out = tempfile.mkdtemp(), pathlib.Path(tempfile.mkdtemp())
    plan_file = _write_plan(steps if steps is not None else _login_steps())
    for k, v in (("STORAGE_STATE_SAVE", save_state), ("PW_NO_TRACE", pw_no_trace)):
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    os.chdir(work)
    try:
        return brain_main._run_replay(ex, "r", out, "file:///s/login.html", plan_file,
                                      False, baseline=False, aut_version="", ci=False, force=False)
    finally:
        os.chdir(cwd0)
        os.environ.pop("STORAGE_STATE_SAVE", None)
        os.environ.pop("PW_NO_TRACE", None)


# --- 1-2: secret non-leak ----------------------------------------------------
def test_secret_passed_as_ref_never_value_on_wire():
    os.environ["LOGIN_PASSWORD"] = SECRET
    p, st, ex = _frozen(_login_steps()), _store(), FakeEx()
    run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    fills = _find(ex.calls, "browser.fill")
    pw = [f for f in fills if f.get("secretRef") == "LOGIN_PASSWORD"]
    assert len(pw) == 1, fills
    assert "value" not in pw[0], pw[0]                       # the secret value never crosses the wire
    user = [f for f in fills if f.get("value") == "alice"]
    assert user and "secretRef" not in user[0], fills        # non-secret uses a literal value


def test_secret_absent_from_all_artifacts():
    os.environ["LOGIN_PASSWORD"] = SECRET
    p, st, ex = _frozen(_login_steps()), _store(), FakeEx()
    d = tempfile.mkdtemp()
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], d)
    assert SECRET not in json.dumps(r), "secret leaked into the report dict"
    with open(os.path.join(d, "heal-report.json")) as f:
        assert SECRET not in f.read(), "secret leaked into heal-report.json"
    assert all(SECRET not in json.dumps(rec) for rec in r["steps"]), "secret leaked into a step rec"


# --- 3-7: assert / exit composition -----------------------------------------
def test_assert_positive_pass_exit0():
    p, st, ex = _frozen([_nav(), _assert(expect_ok=True)]), _store(), FakeEx(expect_returns=True)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0 and r["failed"] == 0, r


def test_negative_input_rejected_pass_exit0():
    # invalid input -> the UI shows an error -> the negative assertion (error visible) holds -> pass.
    steps = [_nav(),
             {"step_id": 2, "action_type": "fill", "semantic_id": "e", "intent": "bad email",
              "locator": {"label": "Email"}, "value": "notanemail", "alternatives": None},
             _assert(expect_ok=True, sid="err", step_id=3)]
    p, st, ex = _frozen(steps), _store(), FakeEx(expect_returns=True)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0, r


def test_assert_fail_exit1():
    # the UI did NOT reject the bad input (validation broken) -> assert fails -> exit 1.
    p, st, ex = _frozen([_nav(), _assert(expect_ok=True)]), _store(), FakeEx(expect_returns=False)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 1 and r["failed"] == 1, r


def test_assert_fail_quarantined_suppressed_exit0():
    p, st = _frozen([_nav(), _assert(expect_ok=True, sid="qa")]), _store()
    for _ in range(3):
        st.record_step("m9", "qa", passed=False, aut_sha="shaA")     # pre-quarantine the assert step (3/5)
    ex = FakeEx(expect_returns=False)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp(), aut_version="shaA")
    assert r["exit_code"] == 0, r                                     # failing assert suppressed
    assert any(s.get("quarantined") for s in r["steps"]), r


def test_assert_fail_and_a11y_regression_exit2_dominates():
    p, st = _frozen([_nav(), _assert(expect_ok=True)]), _store()
    base = FakeEx(expect_returns=True, a11y='- alert "clean"')
    run_replay(base, st, _he(base, st), p, p["target_url"], tempfile.mkdtemp(), baseline=True)
    drift = FakeEx(expect_returns=False, a11y='- alert "DRIFTED"')
    r = run_replay(drift, st, _he(drift, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 2, r                 # a11y regression (exit 2) dominates the failing assert
    assert r["failed"] == 1, r                    # ...both conditions are present


# --- 8-10: determinism -------------------------------------------------------
def test_plan_hash_independent_of_secret_env():
    steps = _login_steps()
    os.environ.pop("LOGIN_PASSWORD", None)
    h1 = canonical_plan_hash(steps)
    os.environ["LOGIN_PASSWORD"] = SECRET
    h2 = canonical_plan_hash(steps)
    assert h1 == h2, "plan_hash must not depend on the resolved secret"


def test_steps_unmutated_after_replay():
    os.environ["LOGIN_PASSWORD"] = SECRET
    steps = _login_steps() + [
        {"step_id": 9, "action_type": "select", "semantic_id": "sel", "intent": "pick role",
         "locator": {"label": "Role"}, "value": "admin", "alternatives": None},
        _assert(expect_ok=True, sid="a9", step_id=10)]
    p = _frozen(steps)
    before, stored = copy.deepcopy(p["steps"]), p["plan_hash"]
    st, ex = _store(), FakeEx()
    run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert p["steps"] == before, "replay mutated the frozen steps"           # selectOption result NOT in step
    assert canonical_plan_hash(p["steps"]) == stored, "plan_hash drifted after replay"


def test_tampered_assert_field_hard_abort_exit3():
    p = _frozen([_nav(), _assert(expect_ok=True)])
    p["steps"][1]["expected"] = "TAMPERED"          # a new assert field is under plan_hash -> mismatch
    st, ex = _store(), FakeEx()
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 3 and r["steps"] == [], r


# --- 11: heal reuse with a non-click verb ------------------------------------
def test_fill_heals_via_testid_applies_fill_verb():
    steps = [_nav(),
             {"step_id": 2, "action_type": "fill", "semantic_id": "u", "intent": "user",
              "locator": {"role": "textbox", "name": "User"}, "value": "alice",
              "alternatives": [{"strategy": "testid", "locator": {"testid": "user"}, "prior": 0.95}]}]
    p, st = _frozen(steps), _store()
    ex = FakeEx(probe={"role:textbox:User": 0})   # primary broken; testid alt resolves (default 1)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["healed"] == 1, r
    fills = _find(ex.calls, "browser.fill")
    assert any(f.get("locator") == {"testid": "user"} for f in fills), fills   # FILL verb on healed locator
    assert not _find(ex.calls, "browser.click"), "must not click when the step's verb is fill"


# --- 12: exporter forward-compat --------------------------------------------
def test_export_spec_maps_new_kinds_and_keeps_secret_as_env_ref():
    steps = _login_steps() + [
        # the SAME literal as the secret, but on a NON-secret field — a secretRef field must still
        # stay an env ref (load-bearing: proves the exporter keys off secretRef, not the string value).
        {"step_id": 7, "action_type": "fill", "semantic_id": "nick", "intent": "nickname",
         "locator": {"label": "Nickname"}, "value": SECRET, "alternatives": None},
        {"step_id": 8, "action_type": "type", "semantic_id": "t", "intent": "search",
         "locator": {"label": "Search"}, "text": "abc", "clear": True, "alternatives": None},
        {"step_id": 9, "action_type": "select", "semantic_id": "s", "intent": "role",
         "locator": {"label": "Role"}, "value": "admin", "alternatives": None},
        {"step_id": 10, "action_type": "press", "semantic_id": "k", "intent": "enter",
         "locator": None, "key": "Enter"},
        _assert(expect_ok=True, step_id=11),
        _assert(expect_ok=False, locator={"role": "alert"}, sid="neg", step_id=12)]
    spec = export_spec(_frozen(steps))
    assert ".fill(process.env.LOGIN_PASSWORD!)" in spec, spec     # secret -> UNQUOTED env ref
    assert ".fill('%s')" % SECRET in spec, spec                  # same string as a non-secret literal is fine
    assert ".pressSequentially('abc')" in spec, spec
    assert ".selectOption('admin')" in spec, spec
    assert "page.keyboard.press('Enter')" in spec, spec
    assert "expect(page.getByRole('alert')).toBeVisible();" in spec, spec      # positive polarity
    assert "expect(page.getByRole('alert')).not.toBeVisible();" in spec, spec  # negative polarity (expect_ok=False)


# --- 13-14: validation generator (sketch) ------------------------------------
def test_invalid_inputs_generator_by_type():
    cases = validation.invalid_inputs_for({"type": "email", "required": True, "name": "email"})
    vals = [c["value"] for c in cases]
    assert "" in vals, vals                                       # required -> empty
    assert any(v and "@" not in v for v in vals), vals            # email -> a value with no @
    for c in cases:                                               # every case carries a reject assertion
        assert c["expects"]["condition"] == "visible" and c["expects"]["expect_ok"] is True, c
    assert validation.invalid_inputs_for({"type": "number"}) == validation.invalid_inputs_for({"type": "number"})


def test_negative_steps_pairs_fill_then_assert():
    field = {"type": "email", "required": True, "name": "email", "locator": {"label": "Email"}}
    steps = validation.negative_steps(field, start_id=1)
    kinds = [s["action_type"] for s in steps]
    assert kinds[0] == "fill" and kinds[1] == "assert", kinds
    assert len(steps) % 2 == 0 and len(steps) >= 2, kinds


# --- 15-16: STORAGE_STATE_SAVE auth lifecycle (drives __main__._run_replay) --
def test_storage_state_saved_on_exit0_before_shutdown():
    os.environ["LOGIN_PASSWORD"] = SECRET
    target = os.path.join(tempfile.mkdtemp(), "sub", "state.json")   # parent dir does NOT exist yet
    ex = FakeEx(expect_returns=True)
    rc = _run_main_replay(ex, save_state=target, pw_no_trace="1")    # login-as-test: tracing off
    assert rc == 0, rc
    methods = [m for (m, _) in ex.calls]
    saves = _find(ex.calls, "browser.saveStorageState")
    assert saves and saves[0]["path"] == target, ex.calls                                  # (a) saved to path
    assert methods.index("browser.saveStorageState") < methods.index("shutdown"), methods  # (b) before shutdown
    assert os.path.isdir(os.path.dirname(target)), "parent dir not created"                # (c) mkdir parents


def test_storage_state_not_saved_on_failure():
    target = os.path.join(tempfile.mkdtemp(), "state.json")
    ex = FakeEx(expect_returns=False)                               # assert fails -> exit 1
    rc = _run_main_replay(ex, save_state=target, steps=[_nav(), _assert(expect_ok=True)])
    assert rc == 1, rc
    assert not _find(ex.calls, "browser.saveStorageState"), ex.calls   # no save on a non-zero exit
    assert not os.path.exists(target)


def test_secretref_without_pw_no_trace_aborts_exit3():
    # Fail-closed (GAP-RISK-010): a secretRef fill with tracing ON would leak into trace.zip -> abort.
    os.environ["LOGIN_PASSWORD"] = SECRET
    ex = FakeEx()
    rc = _run_main_replay(ex, save_state=None, pw_no_trace=None)    # login plan has secretRef, tracing on
    assert rc == 3, rc
    assert ex.calls == [], "must not touch the browser when aborting on the secret/tracing guard"


# --- 17: press (both arms) + type executed through replay --------------------
def test_press_arms_and_type_through_replay():
    steps = [_nav(),
             {"step_id": 2, "action_type": "press", "semantic_id": "k1", "intent": "enter",
              "locator": None, "key": "Enter"},                                    # global key
             {"step_id": 3, "action_type": "press", "semantic_id": "k2", "intent": "tab",
              "locator": {"role": "textbox", "name": "X"}, "key": "Tab"},          # scoped key
             {"step_id": 4, "action_type": "type", "semantic_id": "t", "intent": "search",
              "locator": {"label": "Search"}, "text": "abc", "clear": True, "alternatives": None}]
    p, st, ex = _frozen(steps), _store(), FakeEx()
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0 and r["failed"] == 0, r
    presses = _find(ex.calls, "browser.press")
    assert {f.get("key") for f in presses} == {"Enter", "Tab"}, presses
    enter = next(f for f in presses if f.get("key") == "Enter")
    tab = next(f for f in presses if f.get("key") == "Tab")
    assert "locator" not in enter, enter                                      # global key -> no locator
    assert tab.get("locator") == {"role": "textbox", "name": "X"}, tab        # scoped key -> locator
    probed = [pp.get("locator") for pp in _find(ex.calls, "browser.probe")]
    assert {"role": "textbox", "name": "X"} not in probed, "press must not probe/heal (contract §6)"
    types = _find(ex.calls, "browser.type")
    assert types and types[0].get("text") == "abc" and types[0].get("clear") is True, types


# --- 18: negative assert polarity (expect_ok=False) --------------------------
def test_assert_negative_polarity_pass_exit0():
    # expect_ok=False: the step passes precisely because the condition does NOT hold (FakeEx ok=False).
    p, st, ex = _frozen([_nav(), _assert(expect_ok=False)]), _store(), FakeEx(expect_returns=False)
    r = run_replay(ex, st, _he(ex, st), p, p["target_url"], tempfile.mkdtemp())
    assert r["exit_code"] == 0 and r["failed"] == 0, r


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
