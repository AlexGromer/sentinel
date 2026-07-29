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
