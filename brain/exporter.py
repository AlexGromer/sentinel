"""Sentinel — export a frozen plan to a Playwright @playwright/test spec (M4, ADR-014).

Pure + deterministic: `export_spec(plan) -> str` (no browser, no MCP-codegen dependency).
Locator-dict -> Playwright code mirrors pw-executor's buildLocator. The emitted .spec.ts is the
handoff artifact to human-maintained suites (e.g. the qa-automation-engineer workflow).
"""


def _esc(s: object) -> str:
    return (str(s) if s is not None else "").replace("\\", "\\\\").replace("'", "\\'")


def _locator_expr(loc: dict):
    """Map a locator dict to a Playwright `page.<...>` expression (None if unmappable)."""
    if not loc:
        return None
    if "testid" in loc:
        return f"page.getByTestId('{_esc(loc['testid'])}')"
    if "role" in loc:
        name = loc.get("name")
        if name:
            return f"page.getByRole('{_esc(loc['role'])}', {{ name: '{_esc(name)}' }})"
        return f"page.getByRole('{_esc(loc['role'])}')"
    if "label" in loc:
        return f"page.getByLabel('{_esc(loc['label'])}')"
    if "text" in loc:
        return f"page.getByText('{_esc(loc['text'])}')"
    if "css" in loc:
        return f"page.locator('{_esc(loc['css'])}')"
    if "xpath" in loc:
        return f"page.locator('xpath={_esc(loc['xpath'])}')"
    return None


def _assert_expr(s: dict) -> str:
    """M9.1: map an assert step to a Playwright web-first assertion (polarity via `.not`)."""
    cond = s.get("condition")
    neg = "" if s.get("expect_ok", True) else ".not"
    expr = _locator_expr(s.get("locator")) or "page"
    table = {
        "visible": f"await expect({expr}){neg}.toBeVisible();",
        "hidden": f"await expect({expr}){neg}.toBeHidden();",
        "enabled": f"await expect({expr}){neg}.toBeEnabled();",
        "disabled": f"await expect({expr}){neg}.toBeDisabled();",
        "value_equals": f"await expect({expr}){neg}.toHaveValue('{_esc(s.get('expected'))}');",
        "text_contains": f"await expect({expr}){neg}.toContainText('{_esc(s.get('expected'))}');",
        "count_equals": f"await expect({expr}){neg}.toHaveCount({int(s.get('expected') or 0)});",
        "url_contains": f"await expect(page){neg}.toHaveURL(/{_esc(s.get('expected'))}/);",
    }
    return table.get(cond, f"// unmapped assert condition {_esc(cond)}")


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
