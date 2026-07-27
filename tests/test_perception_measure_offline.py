"""Offline gate: the tool measures how much of a page it can SEE, and says when that is not all of it.

Run:  .venv/bin/python tests/test_perception_measure_offline.py

`GAP-RISK-005` promised a `completeness_ratio` and a gated visual fallback. Neither existed —
`grep -rni completeness` over *.py/*.ts/*.go returned zero — while the constraints table listed it as
the mitigation for a11y blind spots. So the product reported coverage of 1.00 over pages it perceived
in part, and nothing anywhere said the denominator was incomplete.

Coverage answers "how much of what we saw did we exercise". This answers the prior question: "how much
was there to see". The two multiply, and only one of them was ever measured.

⚠ SCOPE (ADR-093). Everything below runs against a STUB executor, so it pins the PLUMBING and nothing
else: that the number reaches the plan, once per page, as a degradation, and that an absent RPC
degrades to "not measured". It cannot tell whether the number is TRUE — and it did not: the value it
stubbed was itself wrong for a year of commits, because the audit measured a re-implementation of
perception instead of perception. Whether the number is true is pinned in
`tests/test_perception_engine_offline.py`, which drives the real executor against a real browser.
Keep the two apart; a stub that grows opinions about reality is how this went wrong.

What this pins:
  * the ratio and its BREAKDOWN reach the plan, so a reader can act on it rather than just feel bad;
  * a partly-visible page is announced — as a degradation, so it reaches the verdict;
  * it is announced ONCE PER PAGE, not once per step (a live run printed it five times before);
  * an older executor without the RPC degrades to "not measured", never to a flattering number;
  * `worst_ratio`, not the average — an average hides the one half-seen screen behind nine good ones.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import eventlog                                   # noqa: E402
from brain.graph import _perception_audit                    # noqa: E402

FULL = {"seen": 9, "total": 9, "ratio": 1.0,
        "unseen": {"outside_selector": 0, "iframe": 0},
        "opaque": {"canvas": 0, "shadow_roots_closed": 0, "frames_unreachable": 0}}
# ADR-093: these numbers used to be `seen 15 / 23` with `shadow_dom: 8`, copied from what the audit
# reported for `l5.html` — and both the count and the zone were wrong, because the audit was
# measuring a re-implementation of perception rather than perception. `l5` is in fact fully seen.
# The stub now carries `l8-blindspots.html`, a fixture built so that a partly-seen page EXISTS, and
# the real numbers are pinned against the real executor in `test_perception_engine_offline.py`.
#
# The lesson this file keeps: a stub answers with whatever its author believed, so a stubbed gate can
# only pin PLUMBING (does the number reach the plan, once per page, as a degradation). It could never
# have caught the wrong number, and nothing here should be read as if it had.
PARTIAL = {"seen": 4, "total": 9, "ratio": 0.444,
           "unseen": {"outside_selector": 5, "iframe": 0},
           "opaque": {"canvas": 1, "shadow_roots_closed": 1, "frames_unreachable": 0}}


class Ex:
    def __init__(self, audit=None, raises=False):
        self.audit, self.raises, self.calls = audit, raises, 0

    def call(self, m, **p):
        if m == "browser.perceptionAudit":
            self.calls += 1
            if self.raises:
                raise RuntimeError("unknown method: browser.perceptionAudit")
            return self.audit
        return {}


def test_a_fully_visible_page_says_nothing():
    """The negative control, and it comes first: a mechanism that warns about every page is noise, and
    noise is how a real warning gets ignored."""
    eventlog.reset_degradations()
    a = _perception_audit(Ex(FULL), "file:///p")
    assert a["ratio"] == 1.0, a
    assert eventlog.degradations() == [], eventlog.degradations()


def test_a_partly_visible_page_is_announced_and_reaches_the_verdict():
    eventlog.reset_degradations()
    a = _perception_audit(Ex(PARTIAL), "file:///p")
    assert a["seen"] == 4 and a["total"] == 9, a
    assert "perception.partial" in eventlog.degradations(), eventlog.degradations()


def test_the_breakdown_survives_not_just_the_number():
    """A ratio alone is unactionable. "5 controls sit outside our selector" tells a person what to do,
    and it is what the interface will show."""
    a = _perception_audit(Ex(PARTIAL), "file:///p")
    assert a["unseen"]["outside_selector"] == 5, a
    for key in ("outside_selector", "iframe"):
        assert key in a["unseen"], (key, a)
    for key in ("canvas", "shadow_roots_closed", "frames_unreachable"):
        assert key in a["opaque"], (key, a)


def test_an_older_executor_degrades_to_not_measured_never_to_fine():
    """Fail-open on the MEASUREMENT: a run must not break because a number is unavailable. But the
    absence is recorded as `ratio: None` — a missing key would read as "fine" in every consumer, which
    is the failure this whole change exists to remove."""
    eventlog.reset_degradations()
    a = _perception_audit(Ex(raises=True), "file:///p")
    assert a["ratio"] is None, a
    assert a.get("reason"), "the absence must say WHY, or it reads as a measurement of zero"
    assert "perception.partial" not in eventlog.degradations(), \
        "an unavailable measurement is not the same claim as a partly-visible page"


def test_the_plan_carries_the_worst_page_not_the_average():
    """Asserted against the source: building a full plan needs a browser. The choice matters — an
    average hides the one screen we half-see behind nine we see fully, and it is the half-seen screen
    that makes the plan incomplete."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "brain" / "graph.py").read_text()
    i = src.index('plan_obj["perception"]')
    window = src[max(0, i - 700):i + 300]
    assert "worst_ratio" in window, window[-300:]
    assert "min(" in window, "the worst page is a min over ratios, not a mean"
    assert "sum(" not in window.split("worst_ratio")[0][-400:], "an average would hide the worst page"


def test_it_is_measured_once_per_page_not_once_per_step():
    """`ground` runs on every step of the walk. A live run against l5.html printed the same finding
    five times before this guard — a finding repeated until it becomes wallpaper is one nobody reads.
    Asserted on the source, because the loop it guards is the graph's."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "brain" / "graph.py").read_text()
    i = src.index("perception[path] = _perception_audit")
    before = src[max(0, i - 400):i]
    assert "if path not in perception" in before, before[-200:]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} perception-measurement tests passed")
