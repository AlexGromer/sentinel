"""Offline gate: the Cypress dialect (PROD-IMPORT, PR-2).

Run:  .venv/bin/python tests/test_import_cypress_offline.py

Cypress states a test as ONE CHAIN — a subject command followed by actions and assertions on that
subject. Sentinel has flat steps, each carrying its own locator. So the transpile is a chain walk,
and how many steps come out DEPENDS ON THE CHAIN:

    cy.get('.receipt').should('be.visible')                    -> 1 step   (assert)
    cy.get('#pay').click().should('be.disabled')               -> 2 steps  (action, then assert)
    cy.get('.r').should('be.visible').and('have.text', '$1')   -> 2 steps  (two asserts)

The module's original design note called `.should()` "assert-over-locator = two steps". Right about
the SHAPE — the assertion is a separate step, not a modifier on the action — and wrong as a constant:
a bare `cy.get(...).should(...)` is one step, because there was no action to separate it from.
Encoding the constant would have inserted a phantom step into every assertion-only line, which is why
this gate asserts step COUNTS per line shape rather than trusting the rule of thumb.

What this pins, beyond the counts:

  - `[data-cy=…]` is a TESTID, not css. Reporting a team's stable hook as a weak css locator would
    understate their suite in the one report they came for.
  - hooks are not test-less. `beforeEach(() => cy.visit(...))` runs for EVERY test, and its body is
    prepended to each. Measured: without this the fixture's `cy.intercept` vanished and the report
    said "0 constructs dropped" about a file that had one.
  - an assertion Sentinel has no condition for is DROPPED and named — never mapped to a nearby
    condition that checks something else.
  - a `cy.<command>` with no class is named WITH ITS NAME, so a team sees which custom command did
    not survive. "unsupported" without the name is a silent drop wearing a label.

The regex that finds the subject uses a NAMED backreference on purpose, and one check here exists
solely because of it: `_Q` (`(['"])(.*?)\\1`) carries a POSITIONAL backreference, and composing it
into a pattern that already had a group renumbered `\\1` onto that other group. The subject regex then
matched the empty string, every step came out WITHOUT a locator, and nothing raised — a silent
quality collapse that only reading the transpiled output revealed.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.importer import parse_cypress_spec, rewrite_report, detect_engine  # noqa: E402

FIXTURE = os.path.join(REPO, "testdata", "import-dialects", "cypress-legacy-layout", "checkout.spec.ts")


def _steps(src):
    p = parse_cypress_spec(src, "x.cy.ts")
    return p["tests"][0]["steps"], p["tests"][0]["notes"]


def main():
    src = open(FIXTURE, encoding="utf-8").read()
    parsed = parse_cypress_spec(src, "checkout.spec.ts")
    rep = rewrite_report(parsed)

    # 1 — the whole fixture, end to end, on the SAME report shape Playwright produces.
    assert rep["totals"]["tests"] == 2, rep["totals"]
    assert rep["totals"]["steps"] == 12, rep["totals"]
    assert rep["totals"]["bound"] == 12, (
        "a step lost its locator — check the subject regex; a positional backreference composed into "
        "a larger pattern silently matches the empty string and every step comes out unbound"
    )
    assert rep["totals"]["dropped"] == 2, (
        "the cy.intercept in beforeEach was not reported — hook bodies run for every test and must "
        "be carried into each"
    )
    assert rep["totals"]["weak"] == 8, rep["totals"]
    print("PASS the fixture transpiles onto the shared report shape (2 tests, 12 steps, 12 bound)")

    # 2 — the describe name is carried, so two identically-named `it`s stay apart.
    names = [t["name"] for t in parsed["tests"]]
    assert names == ["checkout > pays with a saved card", "checkout > rejects an expired card"], names
    print("PASS the suite name is carried into the test name")

    # 3 — CHAIN ARITHMETIC. This is the rule the design note got wrong as a constant.
    one, _ = _steps("it('a', () => { cy.get('.receipt').should('be.visible'); });")
    assert len(one) == 1 and one[0]["verb"] == "assert", one
    two, _ = _steps("it('a', () => { cy.get('#pay').click().should('be.disabled'); });")
    assert [s["verb"] for s in two] == ["click", "assert"], two
    both, _ = _steps("it('a', () => { cy.get('.r').should('be.visible').and('have.text','$1'); });")
    assert [s["verb"] for s in both] == ["assert", "assert"], both
    assert both[1]["expected"] == "$1", both[1]
    print("PASS chain arithmetic — 1 / 2 / 2 steps, by the shape of the chain")

    # 4 — a test-id attribute is a TESTID, not a weak css selector.
    s, n = _steps("it('a', () => { cy.get('[data-cy=save]').click(); });")
    assert s[0]["locator"] == {"testid": "save"}, s[0]
    assert not [x for x in n if x["kind"] == "weak_locator"], "a testid was reported as weak"
    s2, n2 = _steps("it('a', () => { cy.get('.save-btn').click(); });")
    assert s2[0]["locator"] == {"css": ".save-btn"}, s2[0]
    assert [x["strategy"] for x in n2 if x["kind"] == "weak_locator"] == ["css"], n2
    print("PASS [data-cy=…] is a testid; a plain selector is css and is flagged weak")

    # 5 — negation and the url subject, which map to different conditions from the same word.
    s, _ = _steps("it('a', () => { cy.get('.r').should('not.exist'); });")
    assert s[0]["condition"] == "visible" and s[0]["expect_ok"] is False, s[0]
    s, _ = _steps("it('a', () => { cy.url().should('include', '/dashboard'); });")
    assert s[0]["condition"] == "url_contains" and s[0]["expected"] == "/dashboard", s[0]
    assert "locator" not in s[0], "a url assertion must not carry an element locator"
    print("PASS negation -> expect_ok False; cy.url() -> url_contains with no locator")

    # 6 — a secret read through Cypress.env stays a REF, never a literal in the plan.
    s, _ = _steps("it('a', () => { cy.get('#pw').type(Cypress.env('LOGIN_PASSWORD')); });")
    assert s[0].get("secretRef") == "LOGIN_PASSWORD", s[0]
    assert "text" not in s[0], "the secret was also written as a literal"
    print("PASS Cypress.env(...) becomes a secretRef, not a literal")

    # 6b — a value the transpiler cannot resolve must NOT become a literal. The link regex stops the
    #      argument at the first ")", so an expression arrives truncated; writing it through would
    #      type a fragment of the SOURCE CODE into the application under test.
    s, n = _steps("it('a', () => { cy.get('#pw').type(user.password); });")
    assert "text" not in s[0] and "secretRef" not in s[0], (
        "an unresolvable expression was written into the plan as a literal value: %r" % s[0]
    )
    assert s[0]["locator"] == {"css": "#pw"}, "the step lost its target as well as its value"
    assert any(x["kind"] == "dropped" and "user.password" in x["construct"] for x in n), (
        "the unresolved value was swallowed instead of reported — the step now silently does nothing"
    )
    print("PASS an unresolvable value is reported, and never becomes a literal")

    # 6c — cy.contains() selects by TEXT, not by css. Both are weak, so a totals-only assertion cannot
    #      tell them apart; the locator itself has to be checked or a mis-mapping hides in the count.
    s, _ = _steps("it('a', () => { cy.contains('Pay now').click(); });")
    assert s[0]["locator"] == {"text": "Pay now"}, s[0]
    print("PASS cy.contains() binds by text, not css")

    # 7 — constructs with no equivalent are NAMED, each with its consequence.
    _, n = _steps("it('a', () => { cy.wait(2000); });")
    assert any(x["construct"] == "cy.wait" for x in n if x["kind"] == "dropped"), n
    _, n = _steps("it('a', () => { cy.login('admin'); });")
    dropped = [x for x in n if x["kind"] == "dropped"]
    assert dropped and "cy.login()" in dropped[0]["construct"], (
        "a custom command was dropped without naming it — 'unsupported' without the name is a silent "
        "drop wearing a label"
    )
    _, n = _steps("it('a', () => { cy.get('.x').should('have.attr','href','/y'); });")
    assert any("have.attr" in x.get("construct", "") for x in n if x["kind"] == "dropped"), (
        "an assertion with no Sentinel condition was mapped to something else instead of reported"
    )
    _, n = _steps("it('a', () => { cy.get('tr').first().click(); });")
    assert any(x["construct"] == ".first()" for x in n if x["kind"] == "dropped"), n
    print("PASS cy.wait / a custom command / an unmappable assertion / .first() are each named")

    # 8 — the hook body reaches EVERY test, and only the tests that follow it.
    src2 = ("describe('s', () => {\n"
            "  beforeEach(() => { cy.visit('/a'); });\n"
            "  it('one', () => { cy.get('.x').click(); });\n"
            "  it('two', () => { cy.get('.y').click(); });\n"
            "});\n")
    p2 = parse_cypress_spec(src2, "s.cy.ts")
    for t in p2["tests"]:
        assert t["steps"][0]["verb"] == "navigate" and t["steps"][0]["target"] == "/a", t["steps"]
    # ...and the copies are independent: appending to one test must not reach the other.
    p2["tests"][0]["steps"].append({"verb": "click"})
    assert len(p2["tests"][1]["steps"]) == 2, "the tests share one steps list"
    print("PASS beforeEach reaches every test, as independent copies")

    # 9 — detection agrees with the parser: the file this dialect handles is recognised as its own.
    assert detect_engine(src) == "cypress", detect_engine(src)
    print("PASS the fixture is detected as cypress")

    print("ALL PASS (11)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
