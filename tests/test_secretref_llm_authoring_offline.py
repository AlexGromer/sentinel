"""Offline gate: LLM authoring can keep a secret out of the stored scenario (SEC-SCENARIO-SECRETREF).

Run:  .venv/bin/python tests/test_secretref_llm_authoring_offline.py

The leak this closes: `scenarios.steps_json[].value` stored the typed value — a password, a card
number — in the clear (docs/DB_FOREIGN_TEXT.md). The recorder path already carried a `secretRef`
(the env-var NAME instead of the value), but the LLM authoring schema had `value` and no `secretRef`,
so a model asked to author a login had no way to enter a password except literally.

What is proved here, behaviourally — through the REAL schema and the REAL grounding functions, never
by asserting anything about the source text (a mutation walks through that):

  1. the real _SCHEMA_STEPS / _SCHEMA_DRAFT now ACCEPT a secretRef step and still reject a
     wrong-typed one (jsonschema against the actual schema objects);
  2. a fill step authored with secretRef grounds to a scenario step that carries secretRef and
     stores NO literal value — end to end through ground_scenario / reconcile;
  3. secretRef + a literal value on the same fill step stores the ref and DROPS the literal (the
     leak-safe precedence, not the reverse);
  4. secretRef on a non-fill verb is REJECTED to `unmatched`, not carried and then silently dropped
     — a dropped secretRef would read as "protected" while the field was left empty;
  5. the GoalPlanner path actually offers the field: a model that returns a secretRef step has it
     survive into the authored refs.
"""
import json
import os
import sys

import jsonschema

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import budget                                                   # noqa: E402
from brain.llm import LLMResult                                            # noqa: E402
from brain.planner import GoalPlanner, _SCHEMA_STEPS, _SCHEMA_DRAFT        # noqa: E402
from brain.scenario import ground_scenario, reconcile                     # noqa: E402

_PAGE = "file:///s/login.html"


def _site_map():
    return {
        _PAGE: [
            {"semantic_id": "pw", "role": "textbox", "name": "Password", "testid": None,
             "locator": {"role": "textbox", "name": "Password"},
             "alternatives": [{"strategy": "role_name",
                               "locator": {"role": "textbox", "name": "Password"}, "prior": 0.9}],
             "page": _PAGE},
        ],
    }


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# 1 — the real schema declares secretRef as a typed, optional string, and still rejects a wrong type.
def test_schema_accepts_secretref_and_still_typechecks():
    for schema, step in (
        (_SCHEMA_STEPS, {"ref": "pw", "verb": "fill", "secretRef": "LOGIN_PASSWORD"}),
        (_SCHEMA_DRAFT, {"verb": "fill", "intent": "enter password", "secretRef": "LOGIN_PASSWORD"}),
    ):
        jsonschema.validate({"steps": [step]}, schema)  # accepts a secretRef step
        bad = dict(step); bad["secretRef"] = 123        # a number is not an env-var name
        try:
            jsonschema.validate({"steps": [bad]}, schema)
            raise AssertionError("schema accepted a non-string secretRef")
        except jsonschema.ValidationError:
            pass
    # And the property is genuinely there, not smuggled through additionalProperties: a schema that
    # never declared secretRef would still "accept" the step above, so assert it is named.
    for schema in (_SCHEMA_STEPS, _SCHEMA_DRAFT):
        props = schema["properties"]["steps"]["items"]["properties"]
        _check("secretRef" in props, "the schema item does not declare secretRef at all")


# 2 + 3 — a fill authored with secretRef grounds to a step that carries the ref and NO literal.
def test_fill_with_secretref_stores_ref_not_literal():
    # goal path
    steps, unmatched = ground_scenario(
        [{"ref": "pw", "verb": "fill", "secretRef": "LOGIN_PASSWORD"}], _site_map())
    _check(unmatched == [], f"unexpected unmatched: {unmatched}")
    step = steps[-1]
    _check(step["secretRef"] == "LOGIN_PASSWORD", f"secretRef not carried: {step}")
    _check("value" not in step, f"a literal value was stored alongside the ref: {step}")
    blob = json.dumps(steps)
    _check("LOGIN_PASSWORD" in blob and "S3cr3t" not in blob, "the env name must survive, no secret")

    # precedence: secretRef + a literal value on the same fill -> the literal is dropped, not stored.
    steps2, _ = ground_scenario(
        [{"ref": "pw", "verb": "fill", "value": "S3cr3t!", "secretRef": "LOGIN_PASSWORD"}], _site_map())
    s2 = steps2[-1]
    _check(s2.get("secretRef") == "LOGIN_PASSWORD", "secretRef must win over a co-supplied value")
    _check("S3cr3t!" not in json.dumps(steps2), "the literal password leaked despite secretRef")

    # describe path carries it too (the reconcile grounding, reached in describe mode).
    draft = [{"verb": "fill", "intent": "enter password", "secretRef": "LOGIN_PASSWORD",
              "hypothesized_target": {"role": "textbox", "name": "Password"}}]
    dsteps, dunmatched = reconcile(draft, _site_map())
    _check(dunmatched == [], f"describe unmatched: {dunmatched}")
    _check(dsteps[-1].get("secretRef") == "LOGIN_PASSWORD", f"describe dropped secretRef: {dsteps}")
    _check("value" not in dsteps[-1], f"describe stored a literal too: {dsteps}")


# 4 — secretRef on a non-fill verb is rejected, not silently dropped.
def test_secretref_on_non_fill_is_rejected_not_dropped():
    for verb in ("type", "select", "click", "press"):
        steps, unmatched = ground_scenario(
            [{"ref": "pw", "verb": verb, "secretRef": "LOGIN_PASSWORD"}], _site_map())
        _check(len(unmatched) == 1, f"{verb}: expected exactly one rejection, got {unmatched}")
        _check("secretRef" in unmatched[0]["reason"] and "fill only" in unmatched[0]["reason"],
               f"{verb}: rejection must explain why: {unmatched}")
        # The decisive half: nothing was authored, so the secret cannot sit unprotected in a step.
        _check(all("secretRef" not in s for s in steps),
               f"{verb}: a secretRef survived onto an authored step: {steps}")

    # describe path rejects it symmetrically.
    draft = [{"verb": "type", "intent": "type token", "secretRef": "TOK",
              "hypothesized_target": {"role": "textbox", "name": "Password"}}]
    dsteps, dunmatched = reconcile(draft, _site_map())
    _check(len(dunmatched) == 1 and "fill only" in dunmatched[0]["reason"],
           f"describe must reject secretRef on type: {dunmatched}")


# 5 — the GoalPlanner path actually surfaces the field: a model returning a secretRef step keeps it.
def test_goalplanner_carries_a_returned_secretref():
    class FakeBackend:
        name, model, supports_vision = "fake", "fake-model", False

        def __init__(self, reply):
            self.reply = reply

        def complete(self, prompt, *, max_tokens, temperature):
            return LLMResult(self.reply, 10, 10)

        def complete_vision(self, *a, **k):
            raise NotImplementedError

    budget.reset(plan_limit=10000, heal_limit=10000)  # give the planner a live budget to spend
    backend = FakeBackend('{"steps": [{"ref": "pw", "verb": "fill", "secretRef": "LOGIN_PASSWORD"}]}')
    flat_map = _site_map()[_PAGE]  # build_scenario takes the flat element list
    out = GoalPlanner(goal="log in", backend=backend).build_scenario(flat_map)
    refs = out.get("refs") or []
    _check(any(r.get("secretRef") == "LOGIN_PASSWORD" for r in refs),
           f"the planner dropped a secretRef the model returned: {refs}")
    # And it grounds cleanly to a protected step.
    steps, _ = ground_scenario(refs, _site_map())
    _check(steps and steps[-1].get("secretRef") == "LOGIN_PASSWORD" and "value" not in steps[-1],
           f"a planner-authored secret did not stay a ref: {steps}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok   {t.__name__}")
    print(f"\nsecretRef-llm-authoring: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
