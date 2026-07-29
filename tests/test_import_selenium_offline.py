"""Offline gate: the Selenium dialect — four bindings, one model (PROD-IMPORT, PR-3).

Run:  .venv/bin/python tests/test_import_selenium_offline.py

Selenium is the SAME WebDriver API in Python, Java, JS/TS and C#. The semantics are identical and only
the surface spelling differs (`find_element` / `findElement` / `FindElement`), so this is one walker
plus a token table per language — not four parsers. This gate exists partly to keep that claim honest:
if the model were really four models wearing one name, the same assertions could not hold across
bindings.

THE Selenium mismatch class, and it is the same in every binding: `WebDriverWait(...).until(...)` is
an EXPLICIT wait, and Sentinel waits implicitly before every action. The wait disappears. Nothing
fails when it does — the test still passes — which is exactly why it must be REPORTED rather than
absorbed: an invisible change of meaning is the worst import outcome.

And the finding a migrating team most needs, which the report must say loudly rather than apologise
for: Selenium has NO semantic locator. Everything it offers is structural (`By.ID`, `By.CSS_SELECTOR`,
`By.XPATH`) or text. So an imported Selenium suite comes out almost entirely WEAK. That is the
diagnosis of their suite, not a shortcoming of the transpile.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.importer import parse_selenium_spec, rewrite_report, detect_engine  # noqa: E402

PY_FIXTURE = os.path.join(REPO, "testdata", "import-dialects", "selenium-python", "test_checkout.py")

JS_SRC = """
const {Builder, By, until} = require('selenium-webdriver');
it('signs in', async function () {
  await driver.get('https://shop.example.com/login');
  await driver.findElement(By.id('username')).sendKeys('qa_admin');
  await driver.findElement(By.name('password')).sendKeys(process.env.LOGIN_PASSWORD);
  await driver.wait(until.elementLocated(By.css('#sign-in')), 10000);
  await driver.findElement(By.css('#sign-in')).click();
});
"""


def main():
    py_src = open(PY_FIXTURE, encoding="utf-8").read()
    assert detect_engine(py_src) == "selenium", detect_engine(py_src)
    assert detect_engine(JS_SRC) == "selenium", detect_engine(JS_SRC)
    print("PASS both bindings are detected as selenium")

    # 1 — the Python fixture, end to end, on the shared report shape.
    p = parse_selenium_spec(py_src, "test_checkout.py")
    rep = rewrite_report(p)
    assert rep["totals"]["tests"] == 2, rep["totals"]
    assert rep["totals"]["steps"] == 8, rep["totals"]
    assert rep["totals"]["bound"] == 8, "a step lost its locator"
    assert rep["totals"]["dropped"] == 1, "the WebDriverWait was not reported"
    print("PASS the python fixture transpiles (2 tests, 8 steps, 8 bound)")

    # 2 — THE mismatch class, named with its consequence rather than absorbed.
    wait = [n for t in p["tests"] for n in t["notes"]
            if n["kind"] == "dropped" and n["construct"] == "WebDriverWait"]
    assert len(wait) == 1, wait
    assert "EXPLICIT" in wait[0]["why"] and "implicitly" in wait[0]["why"], wait[0]["why"]
    print("PASS WebDriverWait is reported as an explicit wait that disappears")

    # 3 — the diagnosis a migrating team needs: Selenium has no semantic locator, so the suite is
    #     structurally weak. 6 of 8 steps here; the two exceptions are the navigates.
    assert rep["totals"]["weak"] == 6, rep["totals"]
    strategies = sorted({n["strategy"] for t in p["tests"] for n in t["notes"]
                         if n["kind"] == "weak_locator"})
    assert strategies == ["css", "text_role", "xpath"], strategies
    print("PASS the report says the suite is structurally weak, per strategy")

    # 4 — By.<strategy> mapping, each to the locator Sentinel actually has.
    steps = {s["verb"]: s for t in p["tests"] for s in t["steps"]}
    by_loc = [s.get("locator") for t in p["tests"] for s in t["steps"] if s.get("locator")]
    assert {"css": "#username"} in by_loc, by_loc          # By.ID -> css #id
    assert {"css": '[name="password"]'} in by_loc, by_loc  # By.NAME -> attribute selector
    assert {"xpath": "//tr[@data-invoice='4471']//button"} in by_loc, by_loc
    assert {"text": "Pay now"} in by_loc, by_loc           # By.LINK_TEXT -> text
    print("PASS By.ID/NAME/XPATH/LINK_TEXT each map to a real Sentinel locator")

    # 5 — a secret from the environment stays a REF; a literal stays a literal; and neither is the
    #     spliced garbage a line-wide match produces. This is the check that caught the real defect:
    #     `find_element(By.ID, "username").send_keys("qa")` yielded value=`username").send_keys("qa`,
    #     i.e. the LOCATOR's argument welded onto the value, ready to be typed into the application.
    fills = [s for t in p["tests"] for s in t["steps"] if s["verb"] == "fill"]
    assert fills[0].get("value") == "qa_admin", fills[0]
    assert fills[1].get("secretRef") == "LOGIN_PASSWORD" and "value" not in fills[1], fills[1]
    sel = [s for t in p["tests"] for s in t["steps"] if s["verb"] == "select"][0]
    assert sel.get("value") == "Visa ****4242", sel
    print("PASS values are read from the call, not the line; env secrets stay refs")

    # 6 — THE SAME ASSERTIONS ACROSS A SECOND BINDING. If the model were four models, this fails.
    j = parse_selenium_spec(JS_SRC, "login.spec.js")
    js_steps = j["tests"][0]["steps"]
    assert [s["verb"] for s in js_steps] == ["navigate", "fill", "fill", "click"], js_steps
    assert js_steps[1]["locator"] == {"css": "#username"}, js_steps[1]
    assert js_steps[2].get("secretRef") == "LOGIN_PASSWORD", js_steps[2]
    assert js_steps[3]["locator"] == {"css": "#sign-in"}, (
        "By.css (the JS shorthand for cssSelector) lost its locator — a missing alias costs the step "
        "its target silently"
    )
    assert any(n["construct"] == "WebDriverWait" for n in j["tests"][0]["notes"]
               if n["kind"] == "dropped"), j["tests"][0]["notes"]
    print("PASS the JS/TS binding yields the same steps, locators, secret and dropped wait")

    # 7 — a Page Object indirection cannot be resolved per line, and says so instead of binding to
    #     something wrong or vanishing.
    po = parse_selenium_spec(
        "def test_x():\n"
        "    @FindBy(id = \"pay\")\n"
        "    private WebElement payButton;\n", "PageTest.py")
    notes = po["tests"][0]["notes"] if po["tests"] else []
    assert any(n["kind"] == "unmatched" and "@FindBy" in n["why"] for n in notes), notes
    print("PASS an @FindBy Page Object locator is named as unresolvable, not dropped")

    print("ALL PASS (7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
