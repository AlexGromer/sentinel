"""Import someone else's test suite into Sentinel's step format, and REPORT the rewrite (PROD-IMPORT).

The product could start from zero or from its own recorder, but not "take my Playwright project,
make sense of it, extend it, fix it" — the most common entry for a team that already has tests. This
is the transpiler for that entry. The channels that feed it (a filesystem path, a UI upload, a git
clone, a chat paste) are thin; THIS is the mechanism.

Two things matter more than "the tests ran":

1. NOTHING IS DROPPED SILENTLY. A construct the source engine has and Sentinel does not — an explicit
   `waitForTimeout`, a `route()` network stub, a custom command — changes or loses meaning when
   rewritten, and the team must be TOLD, per construct, what happened. A test that quietly changes
   semantics is worse than one that fails to import.

2. THE REPORT IS THE PRODUCT. The value of entering someone's project is the first honest diagnosis
   of their suite: which steps bound to a real locator, which bound only by a WEAK one (text instead
   of testid — prior 0.80 vs 0.95), and which constructs had no equivalent and what became of them.
   Grounding against a live explore map (scenario.reconcile) — "does this element still exist?" — is a
   second pass the caller runs when a map is available; this module produces the steps and the
   rewrite report deterministically, with no LLM and no network.

Scope today: Playwright (@playwright/test), the dominant engine and the backlog's channel-1 priority.
Cypress and Selenium are separate source dialects added on top of the same report shape; their
mismatch classes (Cypress `.should()` = assert-over-locator = two steps; `cy.intercept` = no class;
Selenium `WebDriverWait` = explicit wait that DISAPPEARS under Playwright's implicit waiting) are
named here so adding them is filling a table, not reopening the design.
"""
import re

# The locator-strategy vocabulary and priors come from the ONE place that owns them (brain/strategies,
# ADR-083) — an importer that kept its own copy is exactly how that vocabulary drifted before, and the
# offline gate forbids a private copy. A locator is WEAK when its strategy is one that drifts: text
# moves with copy, css/xpath break on the first restructure. testid/role_name/label are stable enough
# not to warn on. The prior reported for each is the canonical one, so an imported suite is ranked by
# the same numbers a heal would use.
from .strategies import PRIORS, STRATEGY_BY_LOCATOR_KEY, canonical, TEXT_ROLE, CSS, XPATH  # noqa: E402

_WEAK_STRATEGIES = frozenset({TEXT_ROLE, CSS, XPATH})


def _strategy_of(locator):
    """The canonical strategy name for a parsed locator dict, via the shared locator-key map."""
    for key in ("testid", "role", "label", "text", "css", "xpath"):
        if key in locator:
            return STRATEGY_BY_LOCATOR_KEY.get(key)
    return None

# A quoted argument that may contain the OTHER quote inside it — a css/frame selector like
# 'iframe[name="stripe"]' has double quotes within single quotes. `(['"])(.*?)\1` matches the opening
# quote and the matching close, so the value (group 2) keeps its inner quotes instead of being
# truncated at the first one (measured: the naive [^'"]+ dropped the frame scope).
_Q = r"""(['"])(.*?)\1"""
# Playwright locator constructors -> our locator dict + strategy label. group(2) is the argument.
_LOC_PATTERNS = [
    (re.compile(r"getByTestId\(\s*%s" % _Q), lambda m: ({"testid": m.group(2)}, "testid")),
    (re.compile(r"getByRole\(\s*%s\s*,\s*\{\s*name:\s*%s" % (_Q, _Q)),
     lambda m: ({"role": m.group(2), "name": m.group(4)}, "role_name")),
    (re.compile(r"getByRole\(\s*%s" % _Q), lambda m: ({"role": m.group(2)}, "role")),
    (re.compile(r"getByLabel\(\s*%s" % _Q), lambda m: ({"label": m.group(2)}, "label")),
    (re.compile(r"getByText\(\s*%s" % _Q), lambda m: ({"text": m.group(2)}, "text")),
    (re.compile(r"getByPlaceholder\(\s*%s" % _Q), lambda m: ({"label": m.group(2)}, "label")),
    (re.compile(r"locator\(\s*(['\"])xpath=(.*?)\1"), lambda m: ({"xpath": m.group(2)}, "xpath")),
    (re.compile(r"locator\(\s*%s" % _Q), lambda m: ({"css": m.group(2)}, "css")),
]

# No end-of-line anchor: a generated spec often carries a trailing `// comment`, and anchoring to $
# silently dropped every action line that had one (measured against the fixture — exactly the
# silent-drop this module exists to prevent).
_ACTION_RE = re.compile(r"\.(click|fill|type|press|selectOption|check|uncheck)\(\s*([^)]*)\)")
_TEST_RE = re.compile(r"""\btest\s*\(\s*['"](?P<name>[^'"]+)['"]""")
_GOTO_RE = re.compile(r"""\.goto\(\s*['"](?P<url>[^'"]+)['"]""")
# constructs Sentinel does not have an equivalent for — named, never dropped.
_NO_EQUIV = {
    "waitForTimeout": "explicit sleep — Sentinel waits implicitly (Playwright auto-wait); the fixed delay is DROPPED, and a test relying on it changes timing",
    "waitForSelector": "explicit wait — becomes implicit; the wait itself is DROPPED (the following step auto-waits)",
    "route": "network stub — Sentinel has no request-interception class; the stub is DROPPED and the run hits the real network",
    "intercept": "network stub (Cypress form) — no equivalent; DROPPED",
    "waitForLoadState": "explicit load wait — implicit under Sentinel; DROPPED",
    "setViewportSize": "viewport control — no per-step equivalent; DROPPED",
    "addInitScript": "page init script — no equivalent; DROPPED",
}
_ASSERT_RE = re.compile(
    r"expect\(\s*(?P<subj>.*?)\s*\)\s*(?P<neg>\.not)?\.(?P<matcher>toBeVisible|toBeHidden|toBeEnabled|"
    r"toBeDisabled|toHaveValue|toContainText|toHaveText|toHaveCount|toHaveURL)\(\s*(?P<arg>[^)]*)\)")
_ASSERT_COND = {
    "toBeVisible": "visible", "toBeHidden": "hidden", "toBeEnabled": "enabled", "toBeDisabled": "disabled",
    "toHaveValue": "value_equals", "toContainText": "text_contains", "toHaveText": "text_contains",
    "toHaveCount": "count_equals", "toHaveURL": "url_contains",
}


# --- engine detection -------------------------------------------------------------------------
# By CONTENT, never by extension. The extension cannot decide this: Cypress <=9's default layout is
# cypress/integration/**/*.spec.ts, so a Cypress suite arrives under the very glob that used to be
# treated as proof of Playwright — the file was then parsed by the Playwright parser, yielded nothing
# (Cypress uses it(), the parser looks for test()), and the run reported success over a suite that had
# silently vanished. Measured with the real binary before this existed.
#
# Scored rather than first-match: a real file carries several signals, and picking the first token
# seen makes the answer depend on line order. Ties and zero evidence are `unknown` — which is a
# reportable outcome here, not a default to fall through on.
_ENGINE_SIGNALS = {
    "playwright": [
        r"@playwright/test",
        r"\bpage\.(goto|getBy|locator|frameLocator|click|fill)\(",
        r"\btest\s*\(\s*['\"].*?['\"]\s*,\s*async",
        r"\bexpect\s*\(\s*page",
    ],
    "cypress": [
        r"\bcy\.[a-zA-Z]",
        r"types=[\"']cypress[\"']",
        r"\bCypress\.",
        r"\.should\(\s*['\"]",
    ],
    "selenium": [
        r"\bwebdriver\b",
        r"\bBy\.[A-Za-z_]",
        r"\bWebDriverWait\b",
        r"\b(find_element|findElement|FindElement)s?\b",
        r"\bExpectedConditions\b|\bEC\.[a-z_]",
        r"\bFindsBy\b|@FindBy\b",
    ],
}


def detect_engine(src):
    """Which test engine wrote this file? -> playwright | cypress | selenium | unknown.

    Returns the engine with the most distinct signals present. `unknown` on a tie or no evidence: a
    guess here would put the file through the wrong parser and produce exactly the silent
    zero-test success this function exists to prevent.
    """
    scores = {
        engine: sum(1 for rx in pats if re.search(rx, src))
        for engine, pats in _ENGINE_SIGNALS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "unknown"
    winners = [e for e, n in scores.items() if n == best]
    return winners[0] if len(winners) == 1 else "unknown"


# Dialects with a parser today. An engine that is DETECTED but absent here is reported by name with
# "no parser yet" — a different and far more useful message than silence.
PARSERS = {}


def parse_spec(src, source="<spec>", engine=None):
    """Transpile a spec of ANY supported engine. Returns (parsed, engine).

    `parsed` is None when the engine has no parser — the caller must report the file, never drop it.
    """
    engine = engine or detect_engine(src)
    fn = PARSERS.get(engine)
    if fn is None:
        return None, engine
    return fn(src, source), engine


def _parse_locator(expr):
    """Return (locator_dict, strategy) for the FIRST locator constructor in a Playwright expression,
    or (None, None). A frameLocator(...) scope becomes our `frame` axis (ADR-095), preserved rather
    than dropped so an imported step reaches a control inside an iframe."""
    frame = None
    fm = re.search(r"frameLocator\(\s*%s" % _Q, expr)
    if fm:
        frame = fm.group(2)
    for rx, build in _LOC_PATTERNS:
        m = rx.search(expr)
        if m:
            loc, strat = build(m)
            if frame:
                loc = {**loc, "frame": frame}
            return loc, strat
    return None, None


def _unquote(arg):
    m = re.match(r"""\s*['"](.*)['"]\s*$""", arg)
    return m.group(1) if m else arg.strip()


# Locator modifiers and chains that CHANGE WHICH ELEMENT a step targets. The parser keeps the first
# locator tier; anything that narrowed or re-scoped it is not representable and must be REPORTED, never
# dropped in silence — a step that quietly targets a different element is the worst import outcome
# (the module's whole promise). Detected by token, with the consequence spelled out.
_MODIFIER_RE = re.compile(r"\.(first|last|nth|filter)\(")
_LOC_CTOR_RE = re.compile(r"(getBy[A-Za-z]+|\blocator)\(")


def _modifier_notes(line):
    notes = []
    for m in _MODIFIER_RE.finditer(line):
        tok = m.group(1)
        why = {"nth": "picks one element by position; Sentinel binds by identity, so the position is "
                      "DROPPED and the step may target a different match",
               "first": "picks the first of several matches; the position is DROPPED — the step may "
                        "target a different element",
               "last": "picks the last of several matches; the position is DROPPED — the step may "
                       "target a different element",
               "filter": "narrows the match by content; the filter is not representable and is DROPPED "
                         "— the step may bind more broadly than the source test intended"}[tok]
        notes.append({"kind": "dropped", "construct": "." + tok + "()", "why": why, "line": line})
    # a chained/scoped locator (more than one constructor) — only the first tier survives.
    if len(_LOC_CTOR_RE.findall(line)) > 1:
        notes.append({"kind": "dropped", "construct": "chained-locator",
                      "why": "a chained/scoped locator (e.g. row.getByRole(...)); only the first tier is "
                             "kept and the rest is DROPPED, which changes which element the step targets",
                      "line": line})
    return notes


def parse_playwright_spec(src, source="<spec>"):
    """Transpile @playwright/test source into Sentinel steps + a rewrite report.

    Returns {"tests": [{"name", "steps": [...], "notes": [...]}], "source": source}. `steps` are in
    the shape scenario.reconcile / _verb_step consume ({verb, locator, value|secretRef|expected|...}).
    `notes` records every rewrite decision — WEAK locators, DROPPED constructs, semantic changes —
    because the point of import is the diagnosis, not a silent best-effort.
    """
    tests = []
    cur = None
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue

        tm = _TEST_RE.search(line)
        if tm:
            cur = {"name": tm.group("name"), "steps": [], "notes": []}
            tests.append(cur)
            continue
        if cur is None:
            continue  # imports / top-level scaffolding before the first test()

        # constructs with no Sentinel equivalent — reported, never dropped in silence.
        for token, why in _NO_EQUIV.items():
            if re.search(r"\.%s\(" % re.escape(token), line) or ("cy.%s(" % token) in line:
                cur["notes"].append({"kind": "dropped", "construct": token, "why": why, "line": line})

        gm = _GOTO_RE.search(line)
        if gm:
            cur["steps"].append({"verb": "navigate", "target": gm.group("url"),
                                 "intent": "navigate to %s" % gm.group("url")})
            continue

        am = _ASSERT_RE.search(line)
        if am:
            loc, strat = _parse_locator(am.group("subj"))
            cond = _ASSERT_COND[am.group("matcher")]
            step = {"verb": "assert", "condition": cond,
                    "expect_ok": am.group("neg") is None,
                    "intent": "assert %s" % cond}
            if loc:
                step["locator"] = loc
            if am.group("arg").strip():
                arg = am.group("arg").strip()
                # toHaveURL(/dashboard/) carries a regex literal, not a string — strip the // delimiters
                # so the stored `expected` is the pattern, matching how url_contains is authored natively.
                rx = re.match(r"^/(.*)/[a-z]*$", arg)
                step["expected"] = rx.group(1) if rx else _unquote(arg)
            cur["steps"].append(step)
            cur["notes"].extend(_modifier_notes(line))
            cstrat = _strategy_of(loc) if loc else None
            if cstrat in _WEAK_STRATEGIES:
                cur["notes"].append({"kind": "weak_locator", "strategy": cstrat,
                                     "prior": PRIORS[cstrat], "line": line,
                                     "why": "assert bound by %s (prior %.2f) — drifts with copy/markup"
                                            % (cstrat, PRIORS[cstrat])})
            continue

        acm = _ACTION_RE.search(line)
        if acm:
            verb_map = {"click": "click", "fill": "fill", "type": "type", "press": "press",
                        "selectOption": "select", "check": "click", "uncheck": "click"}
            pw_verb, arg = acm.group(1), acm.group(2)
            verb = verb_map[pw_verb]
            loc, strat = _parse_locator(line)
            step = {"verb": verb, "intent": "%s%s" % (verb, " " + strat if strat else "")}
            if loc:
                step["locator"] = loc
            # a secret referenced by env — process.env.NAME! — stays a ref, never a literal (M9.1).
            secret = re.search(r"process\.env\.([A-Z0-9_]+)", arg)
            if verb == "fill" and secret:
                step["secretRef"] = secret.group(1)
            elif verb in ("fill", "type"):
                step["value" if verb == "fill" else "text"] = _unquote(arg)
            elif verb == "select":
                step["value"] = _unquote(arg)
            elif verb == "press":
                step["key"] = _unquote(arg)
            cur["steps"].append(step)
            cur["notes"].extend(_modifier_notes(line))
            if loc is None:
                cur["notes"].append({"kind": "unmatched", "line": line,
                                     "why": "no locator constructor recognised — step imported without a target"})
            else:
                cstrat = _strategy_of(loc)
                if cstrat in _WEAK_STRATEGIES:
                    cur["notes"].append({"kind": "weak_locator", "strategy": cstrat,
                                         "prior": PRIORS[cstrat], "line": line,
                                         "why": "%s bound by %s (prior %.2f) — prefer a testid"
                                                % (verb, cstrat, PRIORS[cstrat])})
            continue

    return {"tests": tests, "source": source}


PARSERS["playwright"] = parse_playwright_spec


# --- Cypress ------------------------------------------------------------------------------------
# Cypress states a test as ONE CHAIN: a subject command (cy.get / cy.contains / cy.url) followed by
# actions and assertions on that subject. Sentinel has no chain — it has flat steps, each carrying its
# own locator. So the transpile is a chain WALK, and how many steps come out depends on the chain:
#
#   cy.get('.receipt').should('be.visible')                 -> 1 step  (assert)
#   cy.get('#pay').click().should('be.disabled')            -> 2 steps (action, then assert)
#   cy.get('.r').should('be.visible').and('have.text','$1')  -> 2 steps (two asserts)
#
# The module's original note called `.should()` "assert-over-locator = two steps". That is right about
# the shape (the assertion is a SEPARATE step from the action, not a modifier on it) and wrong as a
# constant: a bare `cy.get(...).should(...)` is one step, because there was no action to separate it
# from. Encoding the constant would have inserted a phantom step into every assertion-only line.
_CY_IT_RE = re.compile(r"\b(?:it|specify)\s*\(\s*['\"](?P<name>[^'\"]+)['\"]")
_CY_DESCRIBE_RE = re.compile(r"\b(?:describe|context)\s*\(\s*['\"](?P<name>[^'\"]+)['\"]")
_CY_VISIT_RE = re.compile(r"\bcy\.visit\(\s*%s" % _Q)
# NAMED backreference, deliberately — not `_Q`. `_Q` is `(['"])(.*?)\1`, and `\1` is POSITIONAL: the
# moment it is composed into a pattern that already has a group, it renumbers and points at that other
# group instead of the quote. Here it silently began matching the empty string, so every subject
# parsed with NO selector and every step came out locator-less — a quiet quality collapse that no
# exception reports and that only reading the transpiled output revealed.
_CY_SUBJECT_RE = re.compile(r"\bcy\.(?P<cmd>get|contains|url)\(\s*(?:(?P<q>['\"])(?P<sel>.*?)(?P=q))?")
# every `.action(args)` / `.should(args)` / `.and(args)` link, in source order.
_CY_LINK_RE = re.compile(
    r"\.(click|type|clear|select|check|uncheck|should|and|first|last|eq|find|within|then|trigger)"
    r"\(\s*([^)]*)\)")
# A test-id attribute selector is a TESTID, not css. Reporting `[data-cy=save]` as a weak css locator
# would understate the suite: the team did use a stable hook, and the diagnosis is what they came for.
_CY_TESTID_RE = re.compile(r"^\[\s*data-(?:cy|test|testid|test-id)\s*=\s*['\"]?([^'\"\]]+)['\"]?\s*\]$")
# Cypress assertion vocabulary -> Sentinel's closed condition set. Anything absent here has no
# equivalent and is REPORTED, never quietly turned into the nearest-looking condition.
_CY_SHOULD = {
    "be.visible": ("visible", True), "not.be.visible": ("visible", False),
    "be.hidden": ("hidden", True), "not.be.hidden": ("hidden", False),
    "exist": ("visible", True), "not.exist": ("visible", False),
    "be.enabled": ("enabled", True), "not.be.enabled": ("enabled", False),
    "be.disabled": ("disabled", True), "not.be.disabled": ("disabled", False),
    "have.text": ("text_contains", True), "not.have.text": ("text_contains", False),
    "contain": ("text_contains", True), "not.contain": ("text_contains", False),
    "contain.text": ("text_contains", True), "not.contain.text": ("text_contains", False),
    "include": ("text_contains", True), "not.include": ("text_contains", False),
    "have.value": ("value_equals", True), "not.have.value": ("value_equals", False),
    "have.length": ("count_equals", True), "not.have.length": ("count_equals", False),
}
_CY_NO_EQUIV_LINK = {
    "first": "picks the first match; Sentinel binds by identity, so the position is DROPPED and the "
             "step may target a different element",
    "last": "picks the last match; the position is DROPPED — the step may target a different element",
    "eq": "picks a match by index; the position is DROPPED — the step may target a different element",
    "find": "re-scopes the search inside the subject; only the first tier is kept, so the step may "
            "bind more broadly than the source test intended",
    "within": "scopes the following commands to the subject; the scope is DROPPED",
    "then": "arbitrary JavaScript over the subject; nothing in it is transpiled",
    "trigger": "raw DOM event; Sentinel has no equivalent verb — DROPPED",
}
_CY_ACTION_VERB = {"click": "click", "type": "type", "select": "select",
                   "check": "click", "uncheck": "click", "clear": "clear"}


def _cy_value(args):
    """What a Cypress .type()/.select() argument really is -> (kind, value).

    kind is 'secret' (Cypress.env('NAME') — stays a REF, never a literal, M9.1), 'literal' (a quoted
    string), or 'unresolved'. The distinction is load-bearing: the link regex stops the argument at
    the first `)`, so `Cypress.env('PW')` arrives truncated, and treating whatever arrived as a
    literal would have typed the source fragment `Cypress.env('PW'` into the application under test.
    """
    a = args.strip()
    secret = re.search(r"Cypress\.env\(\s*['\"]([A-Za-z0-9_]+)['\"]", a)
    if secret:
        return "secret", secret.group(1)
    m = re.match(r"""^(['"])(.*)\1$""", a)
    if m:
        return "literal", m.group(2)
    return "unresolved", a


def _cy_locator(kind, arg):
    """(locator, strategy) for a Cypress subject command."""
    if kind == "url" or arg is None:
        return None, None
    if kind == "contains":
        return {"text": arg}, "text"
    m = _CY_TESTID_RE.match(arg.strip())
    if m:
        return {"testid": m.group(1)}, "testid"
    return {"css": arg}, "css"


def parse_cypress_spec(src, source="<spec>"):
    """Transpile a Cypress suite into the SAME `parsed` shape parse_playwright_spec produces.

    One dialect, one report: `rewrite_report` and `ground_imported` are untouched by this — the whole
    point of the contract is that a second engine fills a table rather than forking the pipeline.
    """
    tests, cur, suite = [], None, ""
    # `beforeEach(() => cy.visit('/billing'))` is the idiomatic Cypress setup, and it runs for EVERY
    # test in the block. Parsed into a test-less accumulator and prepended to each `it` that follows:
    # ignoring hooks drops the navigation every test depends on, and — measured on the fixture — it
    # also dropped a `cy.intercept` that sat there, so the report said "0 constructs dropped" about a
    # file that had one. Hook body = from the hook opener to the next it/describe/hook, which is right
    # for ordinary formatting and stated here rather than assumed.
    hook = {"name": "<hook>", "steps": [], "notes": []}
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("*"):
            continue

        if re.search(r"\b(afterEach|after)\s*\(", line):
            # Teardown has no place in a linear plan; named once rather than parsed into steps that
            # would run in the wrong order.
            hook["notes"].append({"kind": "dropped", "construct": "afterEach()",
                                  "why": "teardown hook — Sentinel plans are linear and have no "
                                         "teardown phase; its body is DROPPED", "line": line})
            cur = None
            continue
        hm = re.search(r"\b(beforeEach|before)\s*\(", line)
        if hm:
            cur = hook
            line = line[hm.end():]     # fall through: the body may be on this same line

        dm = _CY_DESCRIBE_RE.search(line)
        if dm and not _CY_IT_RE.search(line):
            # Only the NEAREST preceding describe is carried, not the full nesting path — enough to
            # keep two identically-named `it`s apart, and said plainly rather than implied.
            suite = dm.group("name")
            cur = None
            continue
        im = _CY_IT_RE.search(line)
        if im:
            name = "%s > %s" % (suite, im.group("name")) if suite else im.group("name")
            # copies, so a later hook line cannot retroactively mutate an already-emitted test.
            cur = {"name": name, "steps": list(hook["steps"]), "notes": list(hook["notes"])}
            tests.append(cur)
            # A one-line test — `it('a', () => { cy.get(...).click(); });` — is ordinary in Cypress
            # and in every hand-written example. Skipping the rest of the line after the opener
            # parsed such a file to ZERO steps, which under PR-1's rule reports as "recognised but
            # nothing parsed": loud, but wrong. Continue on the REMAINDER instead of dropping it.
            line = line[im.end():]
        if cur is None or not line:
            continue

        vm = _CY_VISIT_RE.search(line)
        if vm:
            cur["steps"].append({"verb": "navigate", "target": vm.group(2),
                                 "intent": "navigate to %s" % vm.group(2)})
            continue

        # `cy.intercept` / `cy.wait` and any command with no Sentinel class — named with consequence.
        for token, why in _NO_EQUIV.items():
            if ("cy.%s(" % token) in line or re.search(r"\.%s\(" % re.escape(token), line):
                cur["notes"].append({"kind": "dropped", "construct": token, "why": why, "line": line})
        if re.search(r"\bcy\.wait\(", line):
            cur["notes"].append({"kind": "dropped", "construct": "cy.wait",
                                 "why": "explicit wait (a fixed delay, or waiting on an intercept "
                                        "alias) — Sentinel waits implicitly; the wait is DROPPED and "
                                        "a test relying on its timing changes meaning",
                                 "line": line})

        sm = _CY_SUBJECT_RE.search(line)
        if not sm:
            # A cy.* command we have no class for at all. Named with its own name so a team can see
            # exactly which custom command did not survive — "unsupported" without the name is the
            # silent drop wearing a label.
            unknown = re.search(r"\bcy\.([a-zA-Z][a-zA-Z0-9_]*)\(", line)
            if unknown and unknown.group(1) not in ("wait", "intercept", "visit"):
                cur["notes"].append({
                    "kind": "dropped", "construct": "cy.%s()" % unknown.group(1),
                    "why": "no Sentinel equivalent for this Cypress command (a built-in or a custom "
                           "command defined in support/) — DROPPED",
                    "line": line})
            continue

        kind = sm.group("cmd")
        loc, strat = _cy_locator(kind, sm.group("sel"))
        emitted = False
        for lm in _CY_LINK_RE.finditer(line):
            link, args = lm.group(1), lm.group(2).strip()
            if link in _CY_NO_EQUIV_LINK:
                cur["notes"].append({"kind": "dropped", "construct": "." + link + "()",
                                     "why": _CY_NO_EQUIV_LINK[link], "line": line})
                continue
            if link in _CY_ACTION_VERB:
                verb = _CY_ACTION_VERB[link]
                step = {"verb": verb, "intent": "%s%s" % (verb, " " + strat if strat else "")}
                if loc:
                    step["locator"] = loc
                if verb in ("type", "select"):
                    kindv, val = _cy_value(args)
                    if kindv == "secret":
                        step["secretRef"] = val
                    elif kindv == "literal":
                        step["text" if verb == "type" else "value"] = val
                    else:
                        # An expression we cannot resolve — a fixture reference, a variable, an alias.
                        # It must NOT become a literal: writing `Cypress.env('PW'` into the plan would
                        # type a fragment of the source code into the application. Reported instead.
                        cur["notes"].append({
                            "kind": "dropped", "construct": "%s(%s)" % (link, val),
                            "why": "the value is an expression this transpiler cannot resolve "
                                   "(a variable, a fixture or an alias); the step keeps its target "
                                   "but has NO value — supply one before replaying",
                            "line": line})
                cur["steps"].append(step)
                emitted = True
                continue
            if link in ("should", "and"):
                parts = [p.strip() for p in args.split(",", 1)]
                cond_raw = _unquote(parts[0]) if parts else ""
                mapped = _CY_SHOULD.get(cond_raw)
                if mapped is None:
                    cur["notes"].append({
                        "kind": "dropped", "construct": ".should('%s')" % cond_raw,
                        "why": "Sentinel has no condition for this Cypress assertion; the assertion "
                               "is DROPPED rather than mapped to a nearby one that checks something "
                               "else", "line": line})
                    continue
                cond, ok = mapped
                if kind == "url":
                    cond = "url_contains"
                step = {"verb": "assert", "condition": cond, "expect_ok": ok,
                        "intent": "assert %s" % cond}
                if loc:
                    step["locator"] = loc
                if len(parts) > 1:
                    step["expected"] = _unquote(parts[1])
                cur["steps"].append(step)
                emitted = True

        if emitted:
            cstrat = _strategy_of(loc) if loc else None
            if cstrat in _WEAK_STRATEGIES:
                cur["notes"].append({"kind": "weak_locator", "strategy": cstrat,
                                     "prior": PRIORS[cstrat], "line": line,
                                     "why": "bound by %s (prior %.2f) — Cypress selects by css/text "
                                            "by default, so this is what the source test relied on"
                                            % (cstrat, PRIORS[cstrat])})
        elif loc is not None:
            cur["notes"].append({"kind": "unmatched", "line": line,
                                 "why": "a subject was selected but nothing was done with it that "
                                        "Sentinel can represent"})

    return {"tests": tests, "source": source}


PARSERS["cypress"] = parse_cypress_spec


# --- Selenium -----------------------------------------------------------------------------------
# FOUR language bindings, ONE model. Selenium is the same WebDriver API in Python, Java, JS/TS and C#;
# the semantics are identical and only the surface spelling differs. So this is one walker plus a
# token table per language, not four parsers — and the mismatch classes (below) are the same in all
# four, which is the real reason the split would have been arbitrary.
_SEL_LANG = {
    "python": {"find": r"find_element(?:s)?", "send": r"send_keys",
               "select": r"select_by_(?:visible_text|value|index)",
               "env": r"os\.environ\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]",
               "click": r"\.click\(\s*\)", "nav": r"\b(?:driver|browser)\s*\.\s*get\(",
               "test": r"\bdef\s+(?P<name>test_[A-Za-z0-9_]+)", "anno": None},
    "js":     {"find": r"findElement(?:s)?", "send": r"sendKeys",
               "select": r"selectByVisibleText", "env": r"process\.env\.([A-Za-z0-9_]+)",
               "click": r"\.click\(\s*\)", "nav": r"\b(?:driver|browser)\s*\.\s*get\(",
               "test": r"\b(?:it|test)\s*\(\s*['\"](?P<name>[^'\"]+)['\"]", "anno": None},
    # Java and C# name a test with an ANNOTATION on the line before the method, so the test name is
    # not on the line that identifies it. `anno` marks the annotation; `test` then matches the next
    # method signature. Matching bare `void x()` without the annotation would turn every helper into
    # a test.
    "java":   {"find": r"findElement(?:s)?", "send": r"sendKeys",
               "select": r"selectBy(?:VisibleText|Value|Index)",
               "env": r"System\.getenv\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)",
               "click": r"\.click\(\s*\)", "nav": r"\bdriver\s*\.\s*get\(",
               "test": r"\bvoid\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(", "anno": r"@Test\b"},
    "csharp": {"find": r"FindElement(?:s)?", "send": r"SendKeys",
               "select": r"SelectBy(?:Text|Value|Index)",
               "env": r"Environment\.GetEnvironmentVariable\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*\)",
               "click": r"\.Click\(\s*\)", "nav": r"\bNavigate\(\s*\)\s*\.\s*GoToUrl\(",
               "test": r"\bvoid\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
               "anno": r"\[(?:Test|Fact|TestMethod)\]"},
}
# Page Object field declarations. Java's @FindBy and C#'s [FindsBy] put the locator on an ANNOTATION
# and the element in a FIELD, so the locator and its use are never on the same line — and often not
# even in the same file. Within one file they can be joined, and are; across files they cannot, and
# the step says so by name instead of binding to something else.
_SEL_FINDBY_JAVA = re.compile(
    r"@FindBy\s*\(\s*(?P<how>id|name|css|className|xpath|linkText|partialLinkText|tagName)\s*=\s*"
    r"(?P<q>['\"])(?P<val>.*?)(?P=q)", re.I)
_SEL_FINDBY_CS = re.compile(
    r"\[\s*FindsBy\s*\(\s*How\s*=\s*How\.(?P<how>Id|Name|CssSelector|ClassName|XPath|LinkText|"
    r"PartialLinkText|TagName)\s*,\s*Using\s*=\s*(?P<q>['\"])(?P<val>.*?)(?P=q)", re.I)
_SEL_FIELD_RE = re.compile(
    r"\b(?:private|public|protected|internal)?\s*(?:readonly\s+)?I?WebElement\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)")
# `css` is Java's @FindBy spelling of a css selector; the By.<x> table keys on CSS_SELECTOR.
_SEL_HOW_ALIAS = {"css": "CSS_SELECTOR", "cssselector": "CSS_SELECTOR", "id": "ID", "name": "NAME",
                  "classname": "CLASS_NAME", "xpath": "XPATH", "linktext": "LINK_TEXT",
                  "partiallinktext": "PARTIAL_LINK_TEXT", "tagname": "TAG_NAME"}


def _sel_page_object_fields(src):
    """{fieldName: (locator, strategy)} for every @FindBy / [FindsBy] declared IN THIS FILE.

    The annotation and the field are on consecutive lines, so this is a two-line join; a field whose
    annotation lives in another file simply will not be here, which is exactly the case the walker
    then reports as unresolvable rather than guessing at.
    """
    out, pending = {}, None
    for raw in src.splitlines():
        line = raw.strip()
        m = _SEL_FINDBY_JAVA.search(line) or _SEL_FINDBY_CS.search(line)
        if m:
            how = _SEL_HOW_ALIAS.get(m.group("how").lower())
            build = _SEL_BY.get(how)
            pending = build(m.group("val")) if build else None
            continue
        if pending:
            f = _SEL_FIELD_RE.search(line)
            if f:
                out[f.group("field")] = pending
            pending = None
    return out
# By.<X> -> our locator. Selenium has no semantic locator at all: everything it offers is structural
# or text. That is not a transpiler shortcoming to apologise for — it is the DIAGNOSIS, and the report
# says it loudly, because "your suite is bound almost entirely by css/xpath" is what a team comes for.
_SEL_BY = {
    "ID": lambda v: ({"css": "#" + v}, "css"),
    "NAME": lambda v: ({"css": "[name=\"%s\"]" % v}, "css"),
    "CSS_SELECTOR": lambda v: ({"css": v}, "css"),
    "XPATH": lambda v: ({"xpath": v}, "xpath"),
    "CLASS_NAME": lambda v: ({"css": "." + v}, "css"),
    "TAG_NAME": lambda v: ({"css": v}, "css"),
    "LINK_TEXT": lambda v: ({"text": v}, "text"),
    "PARTIAL_LINK_TEXT": lambda v: ({"text": v}, "text"),
}
_SEL_BY_RE = re.compile(
    r"\bBy\.(?P<by>ID|NAME|CSS_SELECTOR|XPATH|CLASS_NAME|TAG_NAME|LINK_TEXT|PARTIAL_LINK_TEXT|"
    # JS/TS and C# spell the same strategies differently, and JS additionally shortens
    # cssSelector to `css` — a missing alias silently costs the step its locator.
    r"cssSelector|css|id|name|xpath|className|tagName|linkText|partialLinkText)"
    r"\s*[(,]\s*(?P<q>['\"])(?P<val>.*?)(?P=q)", re.I)
_SEL_BY_ALIAS = {"id": "ID", "name": "NAME", "cssselector": "CSS_SELECTOR", "css": "CSS_SELECTOR", "xpath": "XPATH",
                 "classname": "CLASS_NAME", "tagname": "TAG_NAME", "linktext": "LINK_TEXT",
                 "partiallinktext": "PARTIAL_LINK_TEXT"}
_SEL_TEST_RE = re.compile(
    r"\b(?:def\s+(?P<py>test_[A-Za-z0-9_]+)|it\s*\(\s*['\"](?P<js>[^'\"]+)['\"])")
_SEL_GET_RE = re.compile(r"\b(?:driver|browser)\s*\.\s*get\(\s*(?P<q>['\"])(?P<url>.*?)(?P=q)")


def _sel_locator(line):
    m = _SEL_BY_RE.search(line)
    if not m:
        return None, None
    by = m.group("by")
    by = _SEL_BY_ALIAS.get(by.lower(), by.upper())
    build = _SEL_BY.get(by)
    return build(m.group("val")) if build else (None, None)


def detect_selenium_lang(src):
    """Which binding wrote this Selenium file? The four share the API, not the spelling."""
    if re.search(r"\busing OpenQA\.Selenium|\[(?:Test|Fact|TestMethod)\]|IWebDriver|IWebElement", src):
        return "csharp"
    if re.search(r"\bimport org\.openqa\.selenium|@Test\b|\bpublic\s+class\b|WebElement\b", src):
        return "java"
    if re.search(r"\brequire\(|from ['\"]selenium-webdriver|await driver", src):
        return "js"
    return "python"


def parse_selenium_spec(src, source="<spec>", lang=None):
    """Transpile a Selenium suite (any of the supported bindings) into the shared `parsed` shape."""
    lang = lang or detect_selenium_lang(src)
    tok = _SEL_LANG[lang]
    # Page Object fields declared in THIS file — resolvable; anything else is reported by name.
    fields = _sel_page_object_fields(src) if tok["anno"] else {}
    tests, cur, armed = [], None, tok["anno"] is None
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        if tok["anno"] and re.search(tok["anno"], line):
            armed = True          # the NEXT method signature is a test
            continue
        tm = re.search(tok["test"], line)
        if tm:
            if armed:
                cur = {"name": tm.group("name"), "steps": [], "notes": []}
                tests.append(cur)
                if tok["anno"]:
                    armed = False     # one annotation arms exactly one method
            else:
                # A method signature WITHOUT the arming annotation ends the previous test. Without
                # this, a plain helper's body was appended to whichever test came before it — the
                # imported test then contained steps the source test never ran.
                cur = None
            continue
        if cur is None:
            continue

        # THE Selenium mismatch class, and it is the same in all four bindings: an EXPLICIT wait
        # disappears under implicit waiting. The test still passes, so nothing announces the loss —
        # which is exactly why it has to be reported rather than silently absorbed.
        if re.search(r"\bWebDriverWait\b|\bdriver\.wait\(|\.implicitly_wait\(|implicitlyWait", line):
            cur["notes"].append({
                "kind": "dropped", "construct": "WebDriverWait",
                "why": "an EXPLICIT wait. Sentinel (via Playwright) waits implicitly before every "
                       "action, so the wait itself is DROPPED — the step that followed it still "
                       "waits, but any timeout or polling interval the test chose is gone",
                "line": line})
            # a wait line often also carries the locator of the thing waited for; nothing to do.
            continue

        gm = re.search(r"(?:%s)\s*(['\"])(?P<url>.*?)\1" % tok["nav"], line)
        if gm:
            cur["steps"].append({"verb": "navigate", "target": gm.group("url"),
                                 "intent": "navigate to %s" % gm.group("url")})
            continue

        loc, strat = _sel_locator(line)
        verb = None
        if re.search(tok["click"], line):
            verb = "click"
        elif re.search(r"\.%s\(" % tok["send"], line):
            verb = "fill"
        elif re.search(tok["select"], line):
            verb = "select"

        if verb is None:
            continue

        # PAGE OBJECT. Java/C# put the locator on an annotation and the element in a field, so the
        # line that ACTS carries only the field name. When the annotation is in this file, the two are
        # joined and the step binds normally — refusing to resolve what is plainly in front of us
        # would understate the suite. When it is not, the step is reported BY FIELD NAME, so a reader
        # knows which Page Object to look in, rather than being told "unresolvable" about nothing.
        if loc is None and tok["anno"]:
            recv = re.match(r"(?:this\s*\.\s*)?(?P<f>[A-Za-z_][A-Za-z0-9_]*)\s*\.", line)
            if recv:
                f = recv.group("f")
                if f in fields:
                    loc, strat = fields[f]
                elif f not in ("driver", "wait", "Assert", "assertThat", "System", "new"):
                    cur["notes"].append({
                        "kind": "unmatched", "line": line,
                        "why": "the element `%s` comes from a Page Object whose @FindBy/[FindsBy] "
                               "declaration is not in this file; a per-file transpiler cannot follow "
                               "that reference, so the step is imported WITHOUT a target" % f})

        step = {"verb": verb, "intent": "%s%s" % (verb, " " + strat if strat else "")}
        if loc:
            step["locator"] = loc
        if verb in ("fill", "select"):
            # The value is the argument of the SEND/SELECT call, and it has to be read from that
            # call — not from the line. A line is `find_element(By.ID, "username").send_keys("qa")`,
            # so a lazy "first quoted thing" match yields `username").send_keys("qa`: the LOCATOR's
            # argument spliced onto the value, which would then be typed into the application.
            call = tok["send"] if verb == "fill" else tok["select"]
            secret = re.search(tok["env"], line)
            vm = re.search(r"(?:%s)\(\s*(['\"])(.*?)\1\s*\)" % call, line)
            if secret:
                step["secretRef"] = secret.group(1)
            elif vm:
                step["value"] = vm.group(2)
            else:
                cur["notes"].append({
                    "kind": "dropped", "construct": "%s(...)" % verb,
                    "why": "the value is an expression this transpiler cannot resolve (a variable, a "
                           "fixture or a data-driven parameter); the step keeps its target but has NO "
                           "value — supply one before replaying",
                    "line": line})
        cur["steps"].append(step)
        if loc is None:
            # ...unless the Page Object branch above already said WHY, by field name. Two notes for
            # one cause reads as two problems.
            if not (cur["notes"] and cur["notes"][-1].get("line") == line
                    and cur["notes"][-1]["kind"] == "unmatched"):
                cur["notes"].append({"kind": "unmatched", "line": line,
                                     "why": "no By.<strategy> locator on this line — the element was "
                                            "most likely held in a variable, which a per-line parser "
                                            "cannot follow"})
        else:
            cstrat = _strategy_of(loc)
            if cstrat in _WEAK_STRATEGIES:
                cur["notes"].append({"kind": "weak_locator", "strategy": cstrat,
                                     "prior": PRIORS[cstrat], "line": line,
                                     "why": "bound by %s (prior %.2f) — Selenium has no semantic "
                                            "locator, so a suite written with it is structurally "
                                            "weak; this is the diagnosis, not a transpile fault"
                                            % (cstrat, PRIORS[cstrat])})
    return {"tests": tests, "source": source}


PARSERS["selenium"] = parse_selenium_spec


def ground_imported(parsed, site_map):
    """Ground the transpiled steps against a real explore map (PROD-IMPORT). This is the second half of
    the diagnosis the item promises: "does this step still bind to an element the app actually has?"

    An imported locator is checked against the semantic map the explorer produced:
      - bound        : an element matches (by testid / role+name / label / text) — the control exists,
                       and the match names the real semantic_id it grounds to.
      - unmatched    : a SEMANTIC locator (testid/role+name/label/text) that matches nothing — the
                       element the source test targeted is gone or renamed. This is the finding a team
                       most needs: a test that references something the app no longer has.
      - unverifiable : a css/xpath locator — the explore map is a SEMANTIC/a11y model with no DOM
                       paths, so a structural selector cannot be checked against it here. Said plainly
                       rather than guessed: it will only be known at replay against the live DOM.
      - no_locator   : navigate / url-assert steps — nothing to ground.

    Returns a per-test grounding report + totals. Reuses scenario._match for role+name/name/text so the
    binding rule is the SAME conservative one authoring uses (a >1 match is ambiguous -> not bound),
    never a second, looser copy.
    """
    from .scenario import flatten_site_map, _match
    flat = flatten_site_map(site_map)

    def _bind(loc):
        if not loc:
            return ("no_locator", None, None)
        if "css" in loc or "xpath" in loc:
            return ("unverifiable", None, "css" if "css" in loc else "xpath")
        if "testid" in loc:
            hits = [e for e in flat if e.get("testid") == loc["testid"]]
            return ("bound", hits[0]["semantic_id"], "testid") if len(hits) == 1 else ("unmatched", None, "testid")
        if "label" in loc:  # a label targets a field by its accessible name
            m = _match({"name": loc["label"]}, flat)
            return ("bound", m["semantic_id"], "label") if m else ("unmatched", None, "label")
        if "role" in loc:
            m = _match({"role": loc.get("role"), "name": loc.get("name")}, flat)
            return ("bound", m["semantic_id"], "role_name") if m else ("unmatched", None, "role_name")
        if "text" in loc:
            m = _match({"text": loc["text"]}, flat)
            return ("bound", m["semantic_id"], "text") if m else ("unmatched", None, "text")
        return ("unverifiable", None, None)

    out = {"tests": [], "totals": {"bound": 0, "unmatched": 0, "unverifiable": 0, "no_locator": 0}}
    for t in parsed["tests"]:
        steps = []
        for i, s in enumerate(t["steps"]):
            status, sid, via = _bind(s.get("locator"))
            steps.append({"index": i, "verb": s["verb"], "status": status,
                          "semantic_id": sid, "via": via})
            out["totals"][status] += 1
        out["tests"].append({"name": t["name"], "steps": steps})
    return out


def rewrite_report(parsed):
    """The 'состояние вашего тестового хозяйства' summary, in the was->now shape of PROD-HEAL-VERDICT.
    Per source test: how many steps, how many bound, how many by a weak locator, and every dropped or
    changed construct named. This is what a team sees on first contact — the honest diagnosis."""
    out = {"source": parsed["source"], "tests": [], "totals": {
        "tests": 0, "steps": 0, "bound": 0, "weak": 0, "dropped": 0, "unmatched": 0}}
    for t in parsed["tests"]:
        steps = t["steps"]
        bound = sum(1 for s in steps if s.get("locator") or s["verb"] == "navigate")
        weak = sum(1 for n in t["notes"] if n["kind"] == "weak_locator")
        dropped = [n for n in t["notes"] if n["kind"] == "dropped"]
        unmatched = [n for n in t["notes"] if n["kind"] == "unmatched"]
        out["tests"].append({
            "name": t["name"], "steps": len(steps), "bound": bound,
            "weak_locators": weak, "dropped": dropped, "unmatched": unmatched, "notes": t["notes"]})
        out["totals"]["tests"] += 1
        out["totals"]["steps"] += len(steps)
        out["totals"]["bound"] += bound
        out["totals"]["weak"] += weak
        out["totals"]["dropped"] += len(dropped)
        out["totals"]["unmatched"] += len(unmatched)
    return out
