"""Sentinel brain — negative-input generator (M9.1 sketch, ADR-026).

Builds INVALID field inputs by type so that explore/goal/replay can assert the UI *rejected* them
(GAP-M9-10 negative testing — "validation actually works"). Pure + deterministic, **no I/O**.

This is a **sketch for M9.1**: a small, type-driven set of invalid values + a default rejection
assertion. The full engine (input masks, numeric bounds, regex/schema-driven generation, locale) is
**M9.2**.

A `field` is a plain dict describing one form field, e.g.::

    {"type": "email", "name": "email", "locator": {"label": "Email"},
     "required": True, "maxlength": 64, "pattern": r"\\d{3}",
     "error_locator": {"role": "alert"}}        # optional; default = role=alert

`invalid_inputs_for(field)` -> a deterministic list of cases::

    {"value": <invalid string>, "reason": <why invalid>,
     "expects": {"condition": "visible", "locator": {...}, "expect_ok": True}}

`expects` is the assert spec proving the UI rejected the input (an error became visible). The helpers
`fill_step` / `assert_step` / `negative_steps` assemble FROZEN plan steps (under plan_hash) from a
field + case — the assert's `condition/expected/expect_ok` live in the step, never resolved at replay.
"""


def _reject_spec(field: dict) -> dict:
    """Default 'the UI rejected the input' assertion: a validation error became visible.

    `role=alert` is the ARIA convention for validation messages; a field can override via
    `error_locator`. (Cross-app 'submit blocked' is also expressible as `url_contains <form>`,
    chosen by the caller; the sketch defaults to the visible-error signal.)
    """
    return {"condition": "visible",
            "locator": field.get("error_locator") or {"role": "alert"},
            "expect_ok": True}


def invalid_inputs_for(field: dict) -> list:
    """Return invalid inputs for `field` by type/constraint. Deterministic; order is stable."""
    ftype = (field.get("type") or "text").lower()
    expects = _reject_spec(field)
    cases: list = []
    seen: set = set()

    def add(value: str, reason: str) -> None:
        if value in seen:
            return
        seen.add(value)
        cases.append({"value": value, "reason": reason, "expects": expects})

    # constraint-driven (independent of type)
    if field.get("required"):
        add("", "required field left empty")
    maxlen = field.get("maxlength")
    if isinstance(maxlen, int) and maxlen > 0:
        add("x" * (maxlen + 1), f"exceeds maxlength {maxlen}")
    if field.get("pattern"):
        add("∅invalid∅", f"violates pattern {field['pattern']!r}")

    # type-driven
    if ftype == "email":
        add("notanemail", "missing @ and domain")
        add("a@b", "missing TLD")
    elif ftype == "number":
        add("abc", "non-numeric")
    elif ftype in ("tel", "phone"):
        add("not-a-phone", "non-numeric phone")
    elif ftype == "url":
        add("notaurl", "not a URL")
    elif ftype == "date":
        add("31/31/2026", "impossible date")

    return cases


def fill_step(field: dict, value: str, step_id: int) -> dict:
    """A frozen `fill` step entering `value` into `field` (literal, non-secret — these are test inputs)."""
    return {"step_id": step_id, "action_type": "fill",
            "intent": f"enter invalid {field.get('name', field.get('type', 'field'))}={value!r}",
            "semantic_id": f"neg-fill-{field.get('name', step_id)}-{step_id}",
            "locator": field.get("locator"), "value": value,
            "alternatives": field.get("alternatives"), "is_milestone": False}


def assert_step(expects: dict, step_id: int, intent: str = "UI rejected invalid input") -> dict:
    """A frozen `assert` step proving the UI rejected the previous input."""
    return {"step_id": step_id, "action_type": "assert", "intent": intent,
            "semantic_id": f"neg-assert-{step_id}",
            "locator": expects.get("locator"), "condition": expects.get("condition"),
            "expected": expects.get("expected"), "expect_ok": expects.get("expect_ok", True),
            "is_milestone": False}


def negative_steps(field: dict, start_id: int = 1) -> list:
    """Convenience: flatten a field's invalid cases into [fill, assert, fill, assert, ...] steps."""
    steps: list = []
    sid = start_id
    for case in invalid_inputs_for(field):
        steps.append(fill_step(field, case["value"], sid)); sid += 1
        steps.append(assert_step(case["expects"], sid, f"reject: {case['reason']}")); sid += 1
    return steps
