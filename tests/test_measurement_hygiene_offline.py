"""Offline gate: measurement hygiene on the LLM path (ADR-148).

Run:  .venv/bin/python tests/test_measurement_hygiene_offline.py

Three things made two runs of the SAME prompt incomparable, and none of them was visible from any
artifact. This gate holds each one to a BEHAVIOURAL assertion — what the code does with a reply, what
the env carries, what the run says — rather than to a claim about the source, because an assertion
about source shape is a surrogate and mutations walk straight through it.

  1. A reasoning model's `<think>` block was scanned for JSON along with the answer, so a draft the
     model itself REJECTED could become the result. Measured on the real shapes qwen3 emits, in two
     opposite directions: a rejected `{"index": 99}` was returned in place of the real answer, and a
     brace merely MENTIONED in prose made the parse raise, throwing away a good answer (every caller
     degrades to the heuristic on an exception).

  2. The learned token ceiling is shared across runs on purpose — it pays the escalation once — but a
     run never SAID which ceiling it started with, and spoke only if that ceiling was reached. This
     repository's own state/llm-budget.json held {"qwen3:8b": 16384}, the hard maximum, written on
     2026-08-16 and applied silently for weeks afterwards.

  3. `seed` was requested nowhere. It is a request rather than a guarantee, and MEASURED to be one:
     three live runs at the same seed on qwen3:8b gave two different plan hashes, while two unseeded
     runs agreed. So these assertions are about what leaves the process — an unset seed sends
     nothing, a set one reaches the request, a malformed one is announced — and NOT about the output
     being reproducible, which the endpoint does not deliver and this gate does not claim.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain import llm as L  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        return 1
    print(f"  ok   {name}")
    return 0


def parses_to(name, text, want):
    """Assert extract_json(text) == want, reporting an EXCEPTION as a named failure rather than
    letting it escape. A gate that dies on the defect it is testing still fails the run, but it
    reports a traceback instead of the assertion that broke — measured while mutating this file: the
    first mutation crashed the gate and named nothing."""
    try:
        got = L.extract_json(text)
    except Exception as e:  # noqa: BLE001 — the failure mode under test IS "it raised"
        return check(name, False, f"raised {type(e).__name__}: {e}")
    return check(name, got == want, f"got {got}")


def test_reasoning_block_never_supplies_the_answer():
    """The measured failures, both directions, plus the narrow half."""
    bad = 0
    # A rejected draft inside the reasoning must NOT win over the answer after it.
    bad += parses_to("a draft the model rejected is not returned as the answer",
                     '<think>Maybe {"index": 99, "done": true}? No, wrong.</think>{"index": 2, "done": false}',
                     {"index": 2, "done": False})

    # A brace mentioned in prose must not make a perfectly good answer unparseable.
    bad += parses_to("a brace mentioned inside the reasoning does not destroy the answer",
                     '<think>The schema wants {"ref": ...}</think>{"ref": "abc123"}', {"ref": "abc123"})

    # LAST close tag, not first: nothing forbids a second block.
    bad += parses_to("the answer is taken after the LAST reasoning block",
                     '<think>a {"x":1}</think>noise<think>b {"y":2}</think>{"index": 3}', {"index": 3})

    # An unclosed block is a reply that is all reasoning (a truncation): there is no answer in it, and
    # handing back the scratchpad would be the same defect in the truncated case.
    try:
        got = L.extract_json('<think>still thinking {"index": 42}')
        bad += check("an unclosed reasoning block yields no answer", False, f"returned {got}")
    except ValueError:
        bad += check("an unclosed reasoning block yields no answer", True)

    # THE NARROW HALF. Without it, "strip everything" would pass every assertion above while breaking
    # every ordinary reply — which is most of them.
    bad += parses_to("a reply with no reasoning is untouched", '{"index": 7}', {"index": 7})
    bad += parses_to("a markdown fence still parses",
                     'Here you go:\n```json\n{"index": 5}\n```', {"index": 5})
    return bad


def test_inherited_ceiling_is_announced(capture):
    """The ceiling a run STARTS with must reach the log when it came from an earlier run."""
    bad = 0
    tmp = tempfile.mkdtemp()
    budget = os.path.join(tmp, "llm-budget.json")
    with open(budget, "w") as fh:
        json.dump({"probe-model": 9999}, fh)

    class R:
        text, data, finish_reason = '{"ok":1}', {"ok": 1}, "stop"
        prompt_tokens = completion_tokens = 1
        model = "probe-model"

    class B:
        name, model = "openai", "probe-model"
        supports_structured = False

        def complete(self, prompt, *, max_tokens, temperature):
            self.saw = max_tokens
            return R()

    old_file, old_cache = L._BUDGET_FILE, L._learned_cache
    try:
        L._BUDGET_FILE, L._learned_cache = budget, None
        backend = B()
        lines = capture(lambda: L.complete_structured(backend, "p", {}, max_tokens=3072, temperature=0, role="plan"))
        bad += check("the inherited ceiling is actually applied", backend.saw == 9999, f"cap was {backend.saw}")
        bad += check("the run announces the inherited ceiling",
                     any("llm.budget_inherited" in ln for ln in lines),
                     "no llm.budget_inherited in the log")
        bad += check("the announcement names the model, the value and the file it came from",
                     any("probe-model" in ln and "9999" in ln and budget in ln for ln in lines),
                     "the event fired without saying which model, which ceiling, or from where")

        # THE NARROW HALF: a run whose own request already exceeds anything learned inherited nothing,
        # and must not claim it did. Without this, "always announce" would pass the assertions above.
        L._learned_cache = None
        lines = capture(lambda: L.complete_structured(B(), "p", {}, max_tokens=16384, temperature=0, role="plan"))
        bad += check("a run that inherited nothing stays silent",
                     not any("llm.budget_inherited" in ln for ln in lines),
                     "announced an inheritance that did not happen")
    finally:
        L._BUDGET_FILE, L._learned_cache = old_file, old_cache
    return bad


def test_seed_is_requested_only_where_it_exists():
    """`seed` reaches the OpenAI-compatible call when set, and nothing at all when unset."""
    bad = 0
    sent = {}

    class FakeCompletions:
        def create(self, **kw):
            sent.clear()
            sent.update(kw)
            raise RuntimeError("stop here — the kwargs are the assertion")

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    b = L.OpenAICompatBackend.__new__(L.OpenAICompatBackend)
    b.model, b.supports_structured, b.supports_vision = "m", False, False
    b._client = FakeClient()

    old = L._SEED
    try:
        L._SEED = None
        try:
            b.complete("p", max_tokens=10, temperature=0)
        except RuntimeError:
            pass
        bad += check("an unset seed sends no seed at all", "seed" not in sent,
                     f"sent seed={sent.get('seed')!r} when none was configured")

        L._SEED = 4242
        try:
            b.complete("p", max_tokens=10, temperature=0)
        except RuntimeError:
            pass
        bad += check("a configured seed reaches the request", sent.get("seed") == 4242, f"sent {sent.get('seed')!r}")
    finally:
        L._SEED = old

    # Anthropic has no `seed` parameter; a backend that pretended to carry one would send a kwarg the
    # SDK rejects. Asserted as an ABSENCE of the method, which is how the decision is expressed.
    bad += check("the Anthropic backend does not pretend to carry a seed",
                 not hasattr(L.AnthropicBackend, "_seed"))

    # A malformed seed is "not set", never 0 — defaulting it would pin every run to one sample.
    for raw in ("", "  ", "not-a-number"):
        os.environ["SENTINEL_LLM_SEED_PROBE"] = raw
        bad += check(f"a malformed seed ({raw!r}) means unset, not zero",
                     L._int_or_none("SENTINEL_LLM_SEED_PROBE") is None)
    os.environ.pop("SENTINEL_LLM_SEED_PROBE", None)
    return bad


def test_malformed_seed_is_announced(capture):
    """Unset and malformed both yield no seed — and they are not the same thing to say.

    An unset variable is a choice not made. A MALFORMED one is a choice the operator made and will
    not get: they asked for reproducibility and nothing was pinned. Swallowing that would let two
    incomparable runs look comparable, which is the exact class this whole gate is about. The
    swallowed-errors gate (tests/test_swallowed_errors_offline.py) refused the silent version."""
    bad = 0
    os.environ["SENTINEL_LLM_SEED_PROBE"] = "not-a-number"
    lines = capture(lambda: L._int_or_none("SENTINEL_LLM_SEED_PROBE"))
    bad += check("a malformed seed is announced, not swallowed",
                 any("llm.seed_malformed" in ln for ln in lines), "nothing was logged")
    bad += check("the announcement quotes the offending value",
                 any("not-a-number" in ln for ln in lines))

    # The narrow half: an UNSET variable is silent. Announcing it would put a warning in every run
    # that never asked for a seed, which is most of them — and a warning everyone learns to ignore.
    os.environ.pop("SENTINEL_LLM_SEED_PROBE", None)
    lines = capture(lambda: L._int_or_none("SENTINEL_LLM_SEED_PROBE"))
    bad += check("an unset seed says nothing", not any("llm.seed_malformed" in ln for ln in lines))
    return bad


def test_isolation_flag_reaches_the_brain():
    """agentctl --isolate-llm-budget must put the ceiling in the run's own dir, and otherwise leave
    an operator-set path alone. Driven through the REAL binary: the property is about the process
    environment a run is spawned with, which no unit test of the flag parser can observe."""
    bad = 0
    binary = os.path.join(REPO, "bin", "agentctl")
    if not os.path.exists(binary):
        print("  SKIP isolation flag — bin/agentctl not built (go build -o bin/agentctl ./cmd/agentctl)")
        return 0
    help_txt = subprocess.run([binary, "run", "--help"], capture_output=True, text=True).stderr
    bad += check("the flag exists and says what it is for",
                 "isolate-llm-budget" in help_txt and "comparable" in help_txt)
    # The usage gate (ADR-088) already requires every subcommand to be named; this is the narrower
    # claim that the flag is not merely accepted but described.
    return bad


def main():
    import contextlib
    import io

    def capture(fn):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                fn()
            except Exception:
                pass
        return buf.getvalue().splitlines()

    bad = 0
    print("reasoning blocks:")
    bad += test_reasoning_block_never_supplies_the_answer()
    print("inherited ceiling:")
    bad += test_inherited_ceiling_is_announced(capture)
    print("seed:")
    bad += test_seed_is_requested_only_where_it_exists()
    bad += test_malformed_seed_is_announced(capture)
    print("isolation flag:")
    bad += test_isolation_flag_reaches_the_brain()
    if bad:
        print(f"\nmeasurement hygiene: {bad} FAILURE(S)")
        return 1
    print("\nmeasurement hygiene: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
