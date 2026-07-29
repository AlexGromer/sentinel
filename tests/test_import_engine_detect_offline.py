"""Offline gate: import must never report success over a file it did not read (PROD-IMPORT).

Run:  .venv/bin/python tests/test_import_engine_detect_offline.py

THE DEFECT THIS PINS, measured with the real binary before the fix:

    $ agentctl import --from ./cypress/integration
    imported 0 test(s), 0 step(s): 0 bound, 0 by a weak locator, 0 construct(s) dropped, 0 unmatched
    $ echo $?
    0
    $ jq .engine import-report.json
    "playwright"

Cypress <= 9's default layout is cypress/integration/**/*.spec.ts. That glob was the ONLY thing the
importer walked, and `engine` was a hardcoded string, so a Cypress suite was handed to the Playwright
parser, matched no `test(` (Cypress writes `it(`), and came back as a successful import of zero tests.
A green run over a suite that silently vanished — the exact outcome brain/importer.py's docstring
promises can never happen.

Three things are pinned, and they are independent — each has its own way of regressing:

  1. the engine is decided by CONTENT, not by extension (detect_engine);
  2. a file that is walked but yields no tests is NAMED in the report with its engine and a reason;
  3. the run EXITS NON-ZERO when anything was skipped. Naming it in a JSON file nobody opens while
     still exiting 0 would leave CI green — the failure mode is the exit code, not the prose.

Point 3 is a deliberate CONTRACT CHANGE: a mixed directory that used to come back green now comes
back red. That is the point — it was green by dropping files.

This gate runs the REAL _run_import over REAL fixture directories on a temp artifact dir. It does not
stub the parser: a stub would answer with whatever this file's author believed, which is how a gate
comes to pin its own assumptions instead of the product's behaviour.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.importer import detect_engine, parse_spec  # noqa: E402
from brain.__main__ import _run_import  # noqa: E402

FIXTURES = os.path.join(REPO, "testdata", "import")

PLAYWRIGHT_SRC = open(os.path.join(FIXTURES, "playwright-login.spec.ts"), encoding="utf-8").read()
CYPRESS_SRC = open(
    os.path.join(REPO, "testdata", "import-dialects", "cypress-legacy-layout", "checkout.spec.ts"), encoding="utf-8"
).read()

SELENIUM_PY = '''
import os
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def test_login():
    driver = webdriver.Chrome()
    driver.get("https://shop.example.com/login")
    driver.find_element(By.ID, "username").send_keys("qa_admin")
    driver.find_element(By.NAME, "password").send_keys(os.environ["LOGIN_PASSWORD"])
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#sign-in")))
    driver.find_element(By.CSS_SELECTOR, "#sign-in").click()
'''

NOT_A_TEST = "export const helpers = { formatMoney: (n) => `$${n}` };\n"

# Detected as Playwright — it imports @playwright/test and drives `page` — yet yields NO test: every
# case is `test.skip(`, which the parser does not (and should not) treat as a test. A realistic file
# to meet in a real suite, and the one that exercises the "right dialect, nothing parsed" branch:
# without it that branch has no coverage, which is how a mutation removing it survives.
PLAYWRIGHT_NO_TESTS = '''
import { test, expect } from '@playwright/test';

test.skip('login is quarantined until the new IdP lands', async ({ page }) => {
  await page.goto('https://shop.example.com/login');
  await page.getByRole('button', { name: 'Sign in' }).click();
});
'''


def _run(files, name):
    """Run the REAL importer over a temp directory holding `files` -> (exit code, report)."""
    src = tempfile.mkdtemp(prefix="sentinel-imp-src-")
    out = tempfile.mkdtemp(prefix="sentinel-imp-out-")
    try:
        for rel, body in files.items():
            p = pathlib.Path(src, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        rc = _run_import(pathlib.Path(out), src)
        rp = pathlib.Path(out, "import-report.json")
        report = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else None
        return rc, report
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def main():
    # 1 — detection is by CONTENT. The Cypress fixture is deliberately named *.spec.ts: if detection
    #     ever falls back to the extension, this is the assertion that breaks.
    assert detect_engine(PLAYWRIGHT_SRC) == "playwright", detect_engine(PLAYWRIGHT_SRC)
    assert detect_engine(CYPRESS_SRC) == "cypress", (
        "a Cypress suite named *.spec.ts was not recognised as Cypress — this is the exact input that "
        "used to be parsed as Playwright and reported as a successful import of zero tests"
    )
    assert detect_engine(SELENIUM_PY) == "selenium", detect_engine(SELENIUM_PY)
    assert detect_engine(NOT_A_TEST) == "unknown", detect_engine(NOT_A_TEST)
    print("PASS detect_engine — decided by content, not by filename")

    # 2 — an engine with no parser yet returns (None, engine): named, not silently empty. Cypress
    #     HAS a parser now, so Selenium carries this case; the shape being pinned is the contract,
    #     not which dialect happens to be missing this week.
    # Every named engine now HAS a parser, so `unknown` carries the no-parser contract — which is
    # where it belongs permanently: a file whose engine we cannot name can never have one.
    parsed, engine = parse_spec(NOT_A_TEST, "helpers.spec.ts")
    assert engine == "unknown" and parsed is None, (parsed, engine)
    for src, want in ((PLAYWRIGHT_SRC, "playwright"), (CYPRESS_SRC, "cypress"), (SELENIUM_PY, "selenium")):
        parsed, engine = parse_spec(src, "x")
        assert engine == want and parsed and parsed["tests"], f"the {want} path regressed"
    print("PASS parse_spec — a dialect without a parser is reported, never returned as empty")

    # 3 — THE REGRESSION: a Cypress file under the legacy layout must not come back green.
    rc, report = _run({"e2e/helpers.spec.ts": NOT_A_TEST}, "no-parser")
    assert rc != 0, "import exited 0 over a file it could not read — the original defect, restored"
    assert report["totals"]["tests"] == 0, report["totals"]
    assert [s["source"] for s in report["skipped"]] == ["e2e/helpers.spec.ts"]
    assert report["skipped"][0]["engine"] == "unknown", report["skipped"][0]
    assert report["engines"] == [], (
        "engines must report what was actually imported; nothing was, so it must be empty — the "
        "hardcoded 'playwright' is what made the report claim a successful Playwright import"
    )
    # And the original input of the defect — a Cypress suite under the *.spec.ts legacy layout — is
    # now IMPORTED rather than skipped, under its own engine. Same file, opposite outcome: that is
    # what adding the dialect had to change, and pinning it here keeps the two facts in one place.
    rc2, rep2 = _run({"cypress/integration/checkout.spec.ts": CYPRESS_SRC}, "cypress-now-parsed")
    assert rc2 == 0 and rep2["engines"] == ["cypress"], (rc2, rep2.get("engines"))
    assert rep2["totals"]["tests"] == 2 and rep2["skipped"] == [], rep2["totals"]
    print("PASS an unrecognised file is named and RED; Cypress is imported as cypress")

    # 4 — mixed directory: the readable file still imports fully, the unreadable ones are named, and
    #     the run is red because part of the suite did not make it.
    rc, report = _run({
        "e2e/login.spec.ts": PLAYWRIGHT_SRC,
        "cypress/integration/checkout.spec.ts": CYPRESS_SRC,
        "selenium/test_login.py": SELENIUM_PY,
        "e2e/helpers.spec.ts": NOT_A_TEST,
    }, "mixed")
    assert rc != 0, "a directory with unreadable files must not be green"
    assert report["engines"] == ["cypress", "playwright", "selenium"], report["engines"]
    assert report["totals"]["tests"] == 5, report["totals"]   # 2 playwright + 2 cypress + 1 selenium
    assert report["totals"]["skipped"] == 1, report["totals"]
    by_src = {s["source"]: s for s in report["skipped"]}
    assert by_src["e2e/helpers.spec.ts"]["engine"] == "unknown"
    # every skipped entry must carry a REASON — "skipped" alone tells a team nothing.
    for s in report["skipped"]:
        assert s.get("why"), s
    print("PASS mixed suite — playwright+cypress imported, the rest named as skipped, run RED")

    # 4b — the OTHER skip reason, which has its own branch: the dialect was right and the parser ran,
    #      and still nothing came out. Saying "engine detected but no parser" there would be a lie,
    #      and saying nothing at all is the original defect. It must be named with its own reason.
    rc, report = _run({"e2e/quarantined.spec.ts": PLAYWRIGHT_NO_TESTS}, "parsed-to-nothing")
    assert rc != 0, (
        "a .spec.ts recognised as Playwright that yielded no test came back green — the file is "
        "either misnamed or the parser failed on it, and both are findings about the suite"
    )
    assert report["totals"]["tests"] == 0 and report["engines"] == [], report
    only = report["skipped"][0]
    assert only["source"] == "e2e/quarantined.spec.ts", only
    assert only["engine"] == "playwright", (
        "the file was reported under the wrong engine — it IS Playwright, it just has no test()"
    )
    assert "no test was parsed" in only["why"], only["why"]
    print("PASS a recognised file that parses to nothing is named with its OWN reason, and is RED")

    # 5 — the negative control that keeps 3 and 4 from being satisfied by "always fail": an
    #     all-readable directory is still GREEN and still imports everything.
    rc, report = _run({"e2e/login.spec.ts": PLAYWRIGHT_SRC}, "clean")
    assert rc == 0, "a fully readable suite must still exit 0 — the gate is not 'fail always'"
    assert report["totals"]["skipped"] == 0 and report["skipped"] == []
    assert report["totals"]["tests"] == 2, report["totals"]
    assert report["engines"] == ["playwright"], report["engines"]
    print("PASS negative control — a clean suite is still green and fully imported")

    # 6 — the walk must actually reach the other engines' default layouts. Before this, only
    #     *.spec.ts was walked, so a Cypress >=10 (*.cy.ts) or Selenium (test_*.py) suite was not
    #     skipped — it was INVISIBLE, and "no specs found" is a different lie from "imported 0".
    rc, report = _run({"cypress/e2e/checkout.cy.ts": CYPRESS_SRC}, "cy10")
    assert rc == 0 and report["engines"] == ["cypress"] and report["totals"]["tests"] == 2, (rc, report["totals"])
    rc, report = _run({"tests/test_login.py": SELENIUM_PY}, "selenium")
    assert rc == 0 and report["engines"] == ["selenium"] and report["totals"]["tests"] == 1, (rc, report["totals"])
    print("PASS the walk reaches Cypress >=10 and Selenium layouts, and both import")

    print("ALL PASS (8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
