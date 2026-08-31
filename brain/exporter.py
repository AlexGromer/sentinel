"""Sentinel — export a frozen plan to a Playwright @playwright/test spec (M4, ADR-014).

Pure + deterministic: `export_spec(plan) -> str` (no browser, no MCP-codegen dependency).
Locator-dict -> Playwright code mirrors pw-executor's buildLocator. The emitted .spec.ts is the
handoff artifact to human-maintained suites (e.g. the qa-automation-engineer workflow).
"""


def _esc(s: object) -> str:
    return (str(s) if s is not None else "").replace("\\", "\\\\").replace("'", "\\'")


# Characters that mean something inside a JS regex literal, plus `/` which ENDS one.
_RE_META = set("\\^$.|?*+()[]{}/")


def _esc_re(s: object) -> str:
    """Escape a literal for use inside a JS regex literal `/…/` (ADR-138).

    `url_contains` is the one condition rendered as a regex, and the executor treats its `expected` as
    a LITERAL substring (`u.href.includes(want)`) — so escaping here is not merely safe, it is what
    makes the exported test agree with the executor. It became load-bearing when routes started
    reaching `expected`: a route is full of metacharacters, and `/app#/b` alone would close the
    literal after one character and emit a .spec.ts that does not parse.
    """
    return "".join("\\" + c if c in _RE_META else c for c in (str(s) if s is not None else ""))


def _locator_expr(loc: dict):
    """Map a locator dict to a Playwright `page.<...>` expression (None if unmappable).

    ADR-095: a `frame` scope becomes a `frameLocator(...)` root, exactly as it does in the executor's
    `buildLocator`. The six tiers below are untouched — they run against the root instead of against
    `page`, which is the whole point of treating a frame as an axis rather than as a strategy. An
    exported test that silently dropped the frame would compile, run, and fail on a control the
    original plan reaches.
    """
    if not loc:
        return None
    root = f"page.frameLocator('{_esc(loc['frame'])}')" if loc.get("frame") else "page"
    if "testid" in loc:
        return f"{root}.getByTestId('{_esc(loc['testid'])}')"
    if "role" in loc:
        name = loc.get("name")
        if name:
            return f"{root}.getByRole('{_esc(loc['role'])}', {{ name: '{_esc(name)}' }})"
        return f"{root}.getByRole('{_esc(loc['role'])}')"
    if "label" in loc:
        return f"{root}.getByLabel('{_esc(loc['label'])}')"
    if "text" in loc:
        return f"{root}.getByText('{_esc(loc['text'])}')"
    if "css" in loc:
        return f"{root}.locator('{_esc(loc['css'])}')"
    if "xpath" in loc:
        return f"{root}.locator('xpath={_esc(loc['xpath'])}')"
    return None


def _assert_expr(s: dict) -> str:
    """M9.1: map an assert step to a Playwright web-first assertion (polarity via `.not`).

    ⚠ BRANCHES, NOT A DICT LITERAL, and that is a bug fix rather than a style choice. The previous
    form built every entry EAGERLY, so `count_equals`'s `int(expected)` ran for every condition —
    exporting any assert whose `expected` is not a number (`text_contains 'Welcome'`, and now
    `url_contains '/app?tab=orders'`) raised ValueError and produced no artifact at all. Nothing
    caught it because no test exported a non-numeric assert; routes made it reachable on the ordinary
    path, which is how it was found.
    """
    cond = s.get("condition")
    neg = "" if s.get("expect_ok", True) else ".not"
    expr = _locator_expr(s.get("locator")) or "page"
    if cond == "visible":
        return f"await expect({expr}){neg}.toBeVisible();"
    if cond == "hidden":
        return f"await expect({expr}){neg}.toBeHidden();"
    if cond == "enabled":
        return f"await expect({expr}){neg}.toBeEnabled();"
    if cond == "disabled":
        return f"await expect({expr}){neg}.toBeDisabled();"
    if cond == "value_equals":
        return f"await expect({expr}){neg}.toHaveValue('{_esc(s.get('expected'))}');"
    if cond == "text_contains":
        return f"await expect({expr}){neg}.toContainText('{_esc(s.get('expected'))}');"
    if cond == "count_equals":
        try:
            want = int(s.get("expected") or 0)
        except (TypeError, ValueError):
            return f"// count_equals with a non-numeric expected {_esc(s.get('expected'))}"
        return f"await expect({expr}){neg}.toHaveCount({want});"
    if cond == "url_contains":
        return f"await expect(page){neg}.toHaveURL(/{_esc_re(s.get('expected'))}/);"
    return f"// unmapped assert condition {_esc(cond)}"


def export_spec(plan: dict) -> str:
    """Render a deterministic Playwright test from the plan's steps."""
    plan_id = plan.get("plan_id", "plan")
    target = plan.get("target_url", "")
    lines = ["import { test, expect } from '@playwright/test';", "",
             f"test('sentinel: {_esc(plan_id)}', async ({{ page }}) => {{"]
    for s in plan.get("steps", []):
        sid = s.get("step_id")
        kind = s.get("action_type")
        intent = _esc(s.get("intent"))
        if kind == "navigate":
            lines.append(f"  await page.goto('{_esc(s.get('target') or target)}');  // step {sid}")
            continue
        if kind == "assert":
            lines.append(f"  {_assert_expr(s)}  // step {sid}: {intent}")
            continue
        if kind == "press" and not s.get("locator"):
            lines.append(f"  await page.keyboard.press('{_esc(s.get('key'))}');  // step {sid}: {intent}")
            continue
        expr = _locator_expr(s.get("locator"))
        if not expr:
            lines.append(f"  // step {sid}: unmapped locator ({intent})")
            continue
        loc = f"{expr}.first()"
        if kind == "click":
            lines.append(f"  await {loc}.click();  // step {sid}: {intent}")
        elif kind == "fill":
            if s.get("secretRef") is not None:        # secret -> env ref, never a literal
                # Invariant (ADR-026 / GAP-RISK-010): a secret fill is carried as `secretRef`
                # (the env-var NAME) and is mutually exclusive with a literal `value` (see
                # scenario._verb_step). Assert it here so a secret value can never be emitted
                # into the exported spec, even if an upstream caller violates the schema.
                if s.get("value") is not None:
                    raise ValueError(
                        f"step {sid}: fill carries both secretRef and a literal value "
                        "— refusing to export (would leak the secret into the .spec.ts)")
                lines.append(f"  await {loc}.fill(process.env.{s['secretRef']}!);  // step {sid}: {intent} (secret)")
            else:
                lines.append(f"  await {loc}.fill('{_esc(s.get('value'))}');  // step {sid}: {intent}")
        elif kind == "type":
            if s.get("clear"):
                lines.append(f"  await {loc}.fill('');")
            lines.append(f"  await {loc}.pressSequentially('{_esc(s.get('text'))}');  // step {sid}: {intent}")
        elif kind == "select":
            lines.append(f"  await {loc}.selectOption('{_esc(s.get('value'))}');  // step {sid}: {intent}")
        elif kind == "press":
            lines.append(f"  await {loc}.press('{_esc(s.get('key'))}');  // step {sid}: {intent}")
        else:
            lines.append(f"  // step {sid}: unmapped action {_esc(kind)} ({intent})")
    lines += ["});", ""]
    return "\n".join(lines)
