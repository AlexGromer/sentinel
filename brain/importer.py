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

# Locator prior by strategy — mirrors the healing corpus: a stable hook (testid) or an accessible
# (role, name) pair is strong; visible text or a label is medium (text drifts with copy); a css/xpath
# path is weak (it breaks on the first restructure). The report flags anything below STRONG so a team
# sees which of its tests rest on a locator that will rot.
_PRIOR = {"testid": 0.95, "role_name": 0.95, "label": 0.85, "text": 0.85, "css": 0.80, "xpath": 0.80}
_STRONG = 0.95

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
            if loc and strat and _PRIOR.get(strat, 1.0) < _STRONG:
                cur["notes"].append({"kind": "weak_locator", "strategy": strat,
                                     "prior": _PRIOR[strat], "line": line,
                                     "why": "assert bound by %s (prior %.2f) — drifts with copy/markup"
                                            % (strat, _PRIOR[strat])})
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
            if loc is None:
                cur["notes"].append({"kind": "unmatched", "line": line,
                                     "why": "no locator constructor recognised — step imported without a target"})
            elif strat and _PRIOR.get(strat, 1.0) < _STRONG:
                cur["notes"].append({"kind": "weak_locator", "strategy": strat,
                                     "prior": _PRIOR[strat], "line": line,
                                     "why": "%s bound by %s (prior %.2f) — prefer a testid" % (verb, strat, _PRIOR[strat])})
            continue

    return {"tests": tests, "source": source}


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
