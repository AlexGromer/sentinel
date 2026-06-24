"""Offline M4 tests — .spec.ts export, HTML/JSON/Prometheus report, calibrate (no browser).

Run:  .venv/bin/python tests/test_m4_offline.py   (or: pytest tests/)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.exporter import export_spec   # noqa: E402
from brain.report import generate, _metrics  # noqa: E402
from brain.calibrate import calibrate    # noqa: E402
from brain.store import Store            # noqa: E402


def _plan():
    return {"plan_id": "p1", "target_url": "file:///s/index.html", "steps": [
        {"step_id": 1, "action_type": "navigate", "target": "file:///s/index.html"},
        {"step_id": 2, "action_type": "click", "intent": "click cta",
         "locator": {"role": "button", "name": "Get started"}},
        {"step_id": 3, "action_type": "click", "intent": "by testid", "locator": {"testid": "cta"}},
    ]}


def test_export_spec_deterministic_and_maps_locators():
    a, b = export_spec(_plan()), export_spec(_plan())
    assert a == b                                                  # deterministic
    assert "import { test, expect } from '@playwright/test';" in a
    assert "await page.goto('file:///s/index.html')" in a
    assert "page.getByRole('button', { name: 'Get started' })" in a
    assert "page.getByTestId('cta')" in a


def test_report_html_json_metrics():
    rep = {"plan_id": "p1", "mode": "replay", "exit_code": 2, "healed": 1, "failed": 0,
           "regressions": [{"page": "index.html", "kinds": ["a11y", "visual(advisory)"], "exit2": True}],
           "steps": [
               {"step_id": 1, "type": "navigate", "outcome": "ok",
                "regression": ["a11y", "visual(advisory)"]},
               {"step_id": 2, "type": "click", "outcome": "healed",
                "heal": {"strategy": "testid", "confidence": 0.95}}]}
    d = tempfile.mkdtemp()
    open(os.path.join(d, "heal-report.json"), "w").write(json.dumps(rep))
    generate(d)
    h = open(os.path.join(d, "report.html")).read()
    m = open(os.path.join(d, "metrics.prom")).read()
    assert os.path.exists(os.path.join(d, "report.json"))
    assert "<table" in h and "testid" in h and "exit" in h
    assert "sentinel_run_exit_code 2" in m
    assert 'sentinel_heal_by_strategy_total{strategy="testid"} 1' in m
    assert 'sentinel_regression_total{kind="a11y"} 1' in m


def test_calibrate_histogram_and_counts():
    st = Store(os.path.join(tempfile.mkdtemp(), "s.db"), now=lambda: 0.0)
    st.audit(run_id="r", step=1, semantic_id="s", page_path="p", strategy="testid",
             original="{}", healed="{}", confidence=0.95, outcome="auto_healed", dom_hash="h")
    st.audit(run_id="r", step=2, semantic_id="s", page_path="p", strategy="role_name",
             original="{}", healed="{}", confidence=0.7, outcome="flagged", dom_hash="h")
    c = calibrate(st)
    assert c["total_attempts"] == 2
    assert c["confidence_histogram"][">=0.85"] == 1
    assert c["confidence_histogram"]["0.60-0.85"] == 1
    assert c["by_strategy"]["testid"]["auto_healed"] == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
