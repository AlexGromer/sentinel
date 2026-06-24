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


def export_spec(plan: dict) -> str:
    """Render a deterministic Playwright test from the plan's steps."""
    plan_id = plan.get("plan_id", "plan")
    target = plan.get("target_url", "")
    lines = ["import { test, expect } from '@playwright/test';", "",
             f"test('sentinel: {_esc(plan_id)}', async ({{ page }}) => {{"]
    for s in plan.get("steps", []):
        sid = s.get("step_id")
        if s.get("action_type") == "navigate":
            lines.append(f"  await page.goto('{_esc(s.get('target') or target)}');  // step {sid}")
        else:
            expr = _locator_expr(s.get("locator"))
            if expr:
                lines.append(f"  await {expr}.first().click();  // step {sid}: {_esc(s.get('intent'))}")
            else:
                lines.append(f"  // step {sid}: unmapped locator ({_esc(s.get('intent'))})")
    lines += ["});", ""]
    return "\n".join(lines)
