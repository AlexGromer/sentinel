"""Offline gate: the identity verdict survives the run (ADR-082, PR-2).

Run:  .venv/bin/python tests/test_heal_identity_store_offline.py

ADR-082 made a re-ground say whether it is the same element. That statement was visible in the log
and in `heal-report.json` and nowhere else — it did not reach the audit table, so it existed only for
as long as the terminal scrollback did, and `[PROD-HEAL-CALIBRATE]` needs exactly this column to have
any dataset at all.

What this pins:
  * the verdict reaches `healing_audit`, per outcome band, and comes back out;
  * a store written BEFORE this column keeps working — the idempotent ALTER is the only migration
    mechanism this SQLite has, and `CREATE TABLE IF NOT EXISTS` does nothing to an existing table;
  * "no claim" (a re-bind, or a pre-ADR-082 row) is the empty string on the way out, never a crash
    and never a fabricated "unverifiable";
  * both hand-maintained copies of the DDL — Python and Go — declare the same column, since nothing
    else compares them;
  * a reader of report.html and junit.xml sees it.
"""
import os
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.calibrate import calibrate                     # noqa: E402
from brain.healing import CONTRADICTED, UNVERIFIABLE, VERIFIED, HealingEngine  # noqa: E402
from brain.junit import to_junit                          # noqa: E402
from brain.report import _html as render_html             # noqa: E402
from brain.store import LocalStore                        # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "file:///s/app.html"
FROZEN = {"role": "button", "name": "Pay now"}
ALTS = [{"strategy": "role_name", "locator": FROZEN, "prior": 0.90}]
LIVE = [{"role": "button", "name": "Confirm payment", "testid": "confirm-v2"}]


class Ex:
    def __init__(self, resolves):
        self.resolves = resolves

    def call(self, m, **p):
        if m == "browser.probe":
            return {"count": 1 if p.get("locator") in self.resolves else 0}
        return {}


class Backend:
    model, supports_vision = "fake", False

    def complete(self, prompt, **kw):
        return type("R", (), {"text": '{"index": 0}', "data": {"index": 0},
                              "usage": {}, "prompt_tokens": 0, "completion_tokens": 0})()


def _store(path=None):
    return LocalStore(path or os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)


def _ctx(frozen=FROZEN, alts=ALTS):
    return {"step": 1, "semantic_id": "sid-pay", "page_path": PAGE, "intent": "click 'Pay now'",
            "attempted_locator": frozen, "alternatives": list(alts), "dom_hash": "d",
            "interactives": LIVE, "probe_count": 0}


def _identities(store):
    return [r[3] for r in store.audit_rows()]


# --- the verdict reaches the table -----------------------------------------------------------------
def test_a_contradicted_reground_is_recorded_with_its_verdict():
    st = _store()
    HealingEngine(Ex([{"testid": "confirm-v2"}]), st, "r", use_llm=True, backend=Backend()).heal(_ctx())
    rows = st.audit_rows()
    assert rows, "nothing was audited — the rest of this test would be vacuous"
    assert [r[0] for r in rows] == ["llm_pick"], rows
    assert _identities(st) == [CONTRADICTED], rows


def test_a_rebind_records_no_claim_rather_than_a_fabricated_one():
    """The negative control. `unverifiable` here would be a lie in the other direction: the plan froze
    the key, so identity was never in question."""
    st = _store()
    HealingEngine(Ex([FROZEN]), st, "r", use_llm=False).heal(_ctx())
    assert _identities(st) == [""], st.audit_rows()


def test_the_verdict_is_recorded_in_every_outcome_band():
    """A heal that is REFUSED is exactly the row a calibration set needs most, and `needs_review` takes
    a different path through the gate than the two that apply."""
    st = _store()
    eng = HealingEngine(Ex([]), st, "r", use_llm=True, backend=Backend())   # candidate probes to 0
    r = eng.heal(_ctx())
    assert r["outcome"] == "needs_review", r
    assert _identities(st) == [CONTRADICTED], st.audit_rows()


def test_an_unverifiable_reground_is_recorded_as_such():
    st = _store()
    HealingEngine(Ex([{"testid": "confirm-v2"}]), st, "r", use_llm=True,
                  backend=Backend()).heal(_ctx(frozen={"testid": "pay"}))
    assert _identities(st) == [UNVERIFIABLE], st.audit_rows()


# --- migration -------------------------------------------------------------------------------------
def test_a_store_written_before_this_column_still_works():
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so without the ALTER
    every INSERT naming `identity` would fail against an upgraded DB — and the heal path swallows
    store errors nowhere. This builds the OLD 11-column table by hand and opens it with today's code."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "s.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE healing_audit (run_id TEXT, step INTEGER, semantic_id TEXT, "
                "page_path TEXT, strategy TEXT, original TEXT, healed TEXT, confidence REAL, "
                "outcome TEXT, dom_hash TEXT, ts REAL)")
    old.execute("INSERT INTO healing_audit(run_id,strategy,outcome,confidence) VALUES('old','css',"
                "'needs_review',0.585)")
    old.commit()
    cols = [r[1] for r in old.execute("PRAGMA table_info(healing_audit)").fetchall()]
    assert "identity" not in cols, "the fixture must start WITHOUT the column, or it proves nothing"
    old.close()

    st = _store(path)
    HealingEngine(Ex([{"testid": "confirm-v2"}]), st, "r", use_llm=True, backend=Backend()).heal(_ctx())
    rows = st.audit_rows()
    assert len(rows) == 2, rows                      # the pre-existing row survived
    assert sorted(_identities(st)) == ["", CONTRADICTED], rows   # old row = no claim, not a guess


def test_the_migration_is_idempotent():
    """Opened twice, it must not try to add the column again — SQLite errors on a duplicate ALTER, and
    that would turn every second start of an upgraded store into a crash."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "s.db")
    _store(path)
    st = _store(path)                                 # second open over the migrated DB
    HealingEngine(Ex([{"testid": "confirm-v2"}]), st, "r", use_llm=True, backend=Backend()).heal(_ctx())
    assert _identities(st) == [CONTRADICTED], st.audit_rows()


# --- the two hand-maintained copies of the DDL agree -----------------------------------------------
def test_python_and_go_declare_the_same_audit_columns():
    """The schema is duplicated verbatim in two languages with no parity gate, so the copies can drift
    silently — and a Go store missing the column would reject every write from a Python brain that has
    it. Compared as text because there is no running Go store here to interrogate."""
    import re
    go = open(os.path.join(REPO, "internal/store/server.go")).read()
    py = open(os.path.join(REPO, "brain/store.py")).read()

    def audit_cols(src):
        m = re.search(r"CREATE TABLE IF NOT EXISTS healing_audit \((.*?)\)\s*;", src, re.S)
        assert m, "healing_audit DDL not found — the parity check would pass vacuously"
        return sorted(c.split()[0] for c in m.group(1).replace("\n", " ").split(",") if c.split())

    gc, pc = audit_cols(go), audit_cols(py)
    assert "identity" in pc, pc
    assert gc == pc, (gc, pc)


# --- it reaches the reader -------------------------------------------------------------------------
def _report(identity):
    return {"mode": "replay", "verdict": "pass_with_drift", "steps": [
        {"step_id": 1, "action_type": "click", "intent": "click 'Pay now'", "outcome": "healed",
         "heal": {"strategy": "llm_pick", "confidence": 0.81, "outcome": "flagged"}}],
        "drift": {"rebind": 0, "reground": 1, "elements": [
            {"step": 1, "kind": "reground", "name": "click 'Pay now'", "page": PAGE,
             "strategy": "llm_pick", "confidence": 0.81, "outcome": "flagged",
             "identity": identity, "from": FROZEN, "to": {"testid": "confirm-v2"}}]}}


def test_report_html_names_the_identity_outcome():
    html = render_html(_report(CONTRADICTED))
    assert "<th>identity</th>" in html, "the drift table gained no identity column"
    assert f">{CONTRADICTED}<" in html, html[:400]
    # And the negative control: a verified re-ground must be shown, and not dressed as a problem.
    ok = render_html(_report(VERIFIED))
    assert f">{VERIFIED}<" in ok, ok[ok.index("<tbody>"):][:400]
    assert f"unverified'>{VERIFIED}<" not in ok


def test_a_rebind_row_shows_a_dash_rather_than_an_alarm_or_a_blank():
    """The row that makes up the majority of a drifted run. A mutation proved this needed asserting:
    rendering a re-bind through the warning branch broke nothing, because every check above drives a
    re-ground. An empty cell would read as missing data and a warning colour would invent a doubt —
    the plan froze that key, so the honest cell is a dash."""
    rep = _report(CONTRADICTED)
    row = rep["drift"]["elements"][0]
    row.update({"kind": "rebind", "strategy": "role_name", "identity": None})
    rep["drift"].update({"rebind": 1, "reground": 0})
    html = render_html(rep)
    # Match the rendered CELL, not the word anywhere in the document: `verified` is a substring of the
    # `unverified` CSS class that the very same row carries in its "accepted as" cell, so a bare
    # `word in html` is true regardless of what the identity column says. Two mutations passed through
    # that hole before this comment existed.
    assert "<td>&mdash;</td>" in html, html[html.index("<tbody>"):][:600]
    for word in (CONTRADICTED, VERIFIED, UNVERIFIABLE):
        assert f">{word}<" not in html, (word, html[html.index("<tbody>"):][:600])


def test_junit_names_a_contradiction_in_words():
    xml = to_junit(_report(CONTRADICTED))
    text = ET.tostring(ET.fromstring(xml), encoding="unicode")
    assert "identity: contradicted" in text, text[:500]
    assert "IDENTITY CONTRADICTED" in text, text[:500]
    clean = ET.tostring(ET.fromstring(to_junit(_report(VERIFIED))), encoding="unicode")
    assert "identity: verified" in clean and "IDENTITY CONTRADICTED" not in clean, clean[:500]


def test_calibrate_counts_the_identity_verdicts():
    """The point of persisting it: `agentctl calibrate` can now report how often a re-ground disagreed
    with what the plan froze — the first quantity in this system that is about whether a heal was
    RIGHT rather than about how confident we declared ourselves."""
    st = _store()
    for frozen in (FROZEN, {"testid": "pay"}):
        HealingEngine(Ex([{"testid": "confirm-v2"}]), st, "r", use_llm=True,
                      backend=Backend()).heal(_ctx(frozen=frozen))
    HealingEngine(Ex([FROZEN]), st, "r", use_llm=False).heal(_ctx())      # a re-bind: no claim
    c = calibrate(st)
    assert c["total_attempts"] == 3, c
    assert c["identity"] == {"verified": 0, "contradicted": 1, "unverifiable": 1}, c["identity"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} identity-persistence tests passed")
