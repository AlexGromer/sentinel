"""Offline gate for MODEL-002's measurement harness (scripts/model_convergence.py) and the two
brain/llm.py probes it depends on. No network, no browser, no live model — every LLM/HTTP call is
injected (FakeBackend / a fake `fetch`), matching the repo's offline-test convention.

Covers:
  - trap 1 (budget-file leak): build_run_env gives every (model, run) call a DISTINCT
    SENTINEL_LLM_BUDGET_FILE/SENTINEL_LLM_ATTEMPT_LOG, and plan_matrix never reuses a path across two
    different (model, run_index) entries.
  - trap 2 (truncation erased by a successful retry): brain.llm._record_attempt/_ATTEMPT_LOG records
    the FIRST (truncated) attempt's finish_reason even though complete_structured's returned LLMResult
    only carries the LAST attempt's — and is a true no-op (no file, no cost) when the env var is unset.
  - trap 3: LLM_MAX_TOKENS_PICK is set to the CLI's --pick-tokens value, not left implicit.
  - trap 4 (plan_hash conflates order with content): grounded_step_set is order-insensitive and
    excludes step 1 (the deterministic pre-planner navigate); classify() tells "same elements,
    different order" apart from "different elements".
  - discovery floors: an empty fixture/model list is a hard error, never a vacuous pass; a requested
    model/fixture absent from the discovered set is rejected rather than silently substituted.
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain.llm as L  # noqa: E402

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "model_convergence", _REPO_ROOT / "scripts" / "model_convergence.py")
MC = importlib.util.module_from_spec(_SPEC)
# Registered BEFORE exec: the module uses `from __future__ import annotations` + @dataclass, and
# dataclass's deferred-annotation resolution looks the module up by name in sys.modules — without this
# it resolves to None and crashes on import (a harness quirk, not a bug in the script itself).
sys.modules["model_convergence"] = MC
_SPEC.loader.exec_module(MC)


# --------------------------------------------------------------------------------------------------
# discovery + floors
# --------------------------------------------------------------------------------------------------

def test_discover_fixtures_lists_the_directory_sorted():
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / "b.html").write_text("<html></html>")
        (d / "a.html").write_text("<html></html>")
        (d / "not-html.txt").write_text("x")
        assert MC.discover_fixtures(d) == ["a.html", "b.html"]


def test_discover_fixtures_missing_dir_is_empty_not_an_error():
    assert MC.discover_fixtures(pathlib.Path("/no/such/dir/at/all")) == []


def test_require_nonempty_raises_on_empty_set():
    try:
        MC.require_nonempty([], "widgets")
        assert False, "must raise on an empty set"
    except MC.ConvergenceError as e:
        assert "widgets" in str(e)


def test_require_nonempty_passes_through_nonempty():
    assert MC.require_nonempty(["x"], "widgets") == ["x"]


def test_resolve_fixture_prefers_default_when_present():
    assert MC.resolve_fixture(None, ["a.html", MC.DEFAULT_FIXTURE, "z.html"]) == MC.DEFAULT_FIXTURE


def test_resolve_fixture_falls_back_to_first_sorted_when_default_absent():
    assert MC.resolve_fixture(None, ["z.html", "a.html"]) == "a.html"


def test_resolve_fixture_rejects_a_request_not_in_the_discovered_set():
    try:
        MC.resolve_fixture("ghost.html", ["a.html"])
        assert False, "must reject a fixture that was not discovered"
    except MC.ConvergenceError:
        pass


def test_resolve_fixture_empty_discovery_is_a_hard_error():
    try:
        MC.resolve_fixture("a.html", [])
        assert False, "an empty discovered set must refuse, not silently accept the request"
    except MC.ConvergenceError:
        pass


def test_resolve_models_defaults_to_everything_discovered():
    assert MC.resolve_models(None, ["m1", "m2"]) == ["m1", "m2"]


def test_resolve_models_rejects_a_name_the_endpoint_does_not_have():
    try:
        MC.resolve_models(["m1", "ghost"], ["m1", "m2"])
        assert False, "a requested model absent from discovery must be rejected"
    except MC.ConvergenceError as e:
        assert "ghost" in str(e)


def test_resolve_models_empty_discovery_is_a_hard_error_even_with_no_request():
    try:
        MC.resolve_models(None, [])
        assert False, "an empty model list from the endpoint must refuse, never pass vacuously"
    except MC.ConvergenceError:
        pass


def test_usable_model_names_sorted_and_skips_unnamed_entries():
    models = [{"name": "b"}, {"name": "a"}, {"no": "name"}, "not-a-dict"]
    assert MC.usable_model_names(models) == ["a", "b"]


def test_ollama_root_strips_exactly_one_trailing_v1():
    assert MC.ollama_root_from_base_url("http://h:11434/v1") == "http://h:11434"
    assert MC.ollama_root_from_base_url("http://h:11434/v1/") == "http://h:11434"
    assert MC.ollama_root_from_base_url("http://h:11434") == "http://h:11434"  # no /v1 -> unchanged


def test_probe_ollama_reachable_uses_the_injected_fetch_no_real_socket():
    def fake_fetch(url, timeout):
        assert url == "http://h:11434/api/tags"
        return 200, {"models": [{"name": "qwen3:8b"}]}
    r = MC.probe_ollama("http://h:11434/v1", fetch=fake_fetch)
    assert r["reachable"] is True and r["http_code"] == 200
    assert MC.usable_model_names(r["models"]) == ["qwen3:8b"]


def test_probe_ollama_unreachable_is_a_reported_fact_not_a_raise():
    def fake_fetch(url, timeout):
        raise TimeoutError("connection timed out")
    r = MC.probe_ollama("http://h:11434/v1", fetch=fake_fetch)
    assert r["reachable"] is False
    assert r["models"] == []
    assert "TimeoutError" in r["error"]


# --------------------------------------------------------------------------------------------------
# trap 4 — order-insensitive step-set vs. the order-sensitive plan_hash
# --------------------------------------------------------------------------------------------------

def _steps(picks):
    """[(action_type, semantic_id, target), ...] -> a plan.json-shaped steps list, step_id from 1."""
    out = [{"step_id": 1, "action_type": "navigate", "semantic_id": "nav-init", "target": "start"}]
    for i, (at, sid, tgt) in enumerate(picks, start=2):
        out.append({"step_id": i, "action_type": at, "semantic_id": sid, "target": tgt,
                    "intent": f"do {sid}", "locator": {"role": "button", "name": sid}})
    return out


def test_grounded_step_set_excludes_the_deterministic_init_step():
    steps = _steps([])   # only the init navigate
    assert MC.grounded_step_set(steps) == frozenset()


def test_grounded_step_set_ignores_order():
    a = _steps([("click", "s1", None), ("click", "s2", None)])
    b = _steps([("click", "s2", None), ("click", "s1", None)])
    assert MC.grounded_step_set(a) == MC.grounded_step_set(b)


def test_grounded_step_set_differs_on_different_elements():
    a = _steps([("click", "s1", None)])
    b = _steps([("click", "s3", None)])
    assert MC.grounded_step_set(a) != MC.grounded_step_set(b)


def test_reordering_the_same_picks_changes_plan_hash_but_not_the_step_set():
    """The exact conflation trap 4 names: canonical_plan_hash (brain/state.py) is order-sensitive
    because step_id is embedded in every entry; grounded_step_set must not be."""
    from brain.state import canonical_plan_hash
    a = _steps([("click", "s1", None), ("click", "s2", None)])
    b = _steps([("click", "s2", None), ("click", "s1", None)])
    # re-number step_id in traversal order, like the real graph would for a genuinely different walk
    for i, s in enumerate(b, start=1):
        s["step_id"] = i
    assert canonical_plan_hash(a) != canonical_plan_hash(b), "fixture is wrong: hashes should differ"
    assert MC.grounded_step_set(a) == MC.grounded_step_set(b)


def test_classify_stable_when_every_hash_matches():
    h = ["deadbeef", "deadbeef", "deadbeef"]
    s = [frozenset({("click", "s1", None)})] * 3
    assert MC.classify(h, s) == "stable"


def test_classify_order_instability_same_set_different_hash():
    h = ["hash-a", "hash-b"]
    same_set = frozenset({("click", "s1", None), ("click", "s2", None)})
    assert MC.classify(h, [same_set, same_set]) == "order_instability"


def test_classify_different_interpretation_when_sets_differ():
    h = ["hash-a", "hash-b"]
    s = [frozenset({("click", "s1", None)}), frozenset({("click", "s2", None)})]
    assert MC.classify(h, s) == "different_interpretation"


def test_classify_refuses_an_empty_group():
    try:
        MC.classify([], [])
        assert False, "an empty group must not silently classify as 'stable'"
    except MC.ConvergenceError:
        pass


# --------------------------------------------------------------------------------------------------
# trap 1 — per-(model, run) isolation of the budget file and attempt log
# --------------------------------------------------------------------------------------------------

def test_plan_matrix_gives_every_combination_a_distinct_budget_and_attempt_path():
    with tempfile.TemporaryDirectory() as tmp:
        matrix = MC.plan_matrix(["m1", "m2"], 2, pathlib.Path(tmp))
        assert len(matrix) == 4
        budget_paths = [str(rp.budget_file) for rp in matrix]
        attempt_paths = [str(rp.attempt_log) for rp in matrix]
        assert len(set(budget_paths)) == 4, f"budget files collided: {budget_paths}"
        assert len(set(attempt_paths)) == 4, f"attempt logs collided: {attempt_paths}"
        # cross-check: no budget path is shared with an attempt-log path either
        assert not (set(budget_paths) & set(attempt_paths))


def test_build_run_env_wires_the_isolated_paths_and_explicit_pick_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        rp = MC.RunPaths(model="qwen3:8b", run_index=1, run_id="qwen3_8b-r1",
                         artifact_dir=pathlib.Path(tmp) / "art",
                         budget_file=pathlib.Path(tmp) / "budget.json",
                         attempt_log=pathlib.Path(tmp) / "attempts.jsonl")
        env = MC.build_run_env({"PATH": "/bin"}, model="qwen3:8b", target_url="file:///x.html",
                               paths=rp, base_url="http://h:11434/v1", pick_tokens=777,
                               max_steps=6, coverage_target=0.85, pw_executor_cmd="node x.js")
        assert env["LLM_MODEL_PLANNER"] == "qwen3:8b"    # trap-4 hint: model_planner, not "model"
        assert env["LLM_MAX_TOKENS_PICK"] == "777"        # trap 3: explicit, from the CLI arg
        assert env["SENTINEL_LLM_BUDGET_FILE"] == str(rp.budget_file)
        assert env["SENTINEL_LLM_ATTEMPT_LOG"] == str(rp.attempt_log)
        assert env["PLANNER"] == "llm"
        assert env["PATH"] == "/bin"                      # base env preserved, not replaced


# --------------------------------------------------------------------------------------------------
# trap 2 — brain.llm's per-attempt log: the FIRST truncated attempt survives a later successful retry
# --------------------------------------------------------------------------------------------------

class _FakeBackend:
    """Same shape as tests/test_adaptive_budget_offline.py's FakeBackend: truncated below `floor`,
    a real reply at/above it."""
    name = "openai"
    supports_vision = False
    supports_structured = False

    def __init__(self, model="fake-model", floor=1500):
        self.model = model
        self.floor = floor

    def complete(self, prompt, *, max_tokens, temperature):
        if max_tokens < self.floor:
            return L.LLMResult("", 10, max_tokens, model=self.model, finish_reason="length")
        return L.LLMResult('{"index": 0}', 10, 40, model=self.model, finish_reason="stop")


def _isolate_llm_module(tmp):
    L._BUDGET_FILE = os.path.join(tmp, "llm-budget.json")
    L._learned_cache = None
    L._TOKEN_HARD_MAX = 4000
    L._ADAPTIVE = True


def test_record_attempt_is_a_true_noop_when_the_env_var_is_unset():
    """"No-op" means no ATTEMPT is made, not merely "no file survives" — a guard that were removed
    would still leave zero files behind (open("") raises inside _record_attempt's own try/except and
    is swallowed there), so a file-count assertion alone would pass right through that mutation, and
    even a raising spy would be swallowed by the SAME try/except it is trying to catch a bypass of.
    A counting spy, checked AFTER complete_structured returns, sees past both."""
    import builtins
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_llm_module(tmp)
        L._ATTEMPT_LOG = ""   # explicitly unset, like every existing caller/test before MODEL-002
        b = _FakeBackend(floor=0)
        real_open = builtins.open
        attempted_paths = []

        def _spy_open(path, *a, **kw):
            if isinstance(path, str) and ("attempt" in path.lower() or path == ""):
                attempted_paths.append(path)
            return real_open(path, *a, **kw)

        builtins.open = _spy_open
        try:
            L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
        finally:
            builtins.open = real_open
        assert attempted_paths == [], f"_record_attempt tried to open a path while unset: {attempted_paths}"
        # nothing must exist anywhere under tmp except (possibly) the budget file
        leftovers = [p for p in os.listdir(tmp) if p != "llm-budget.json"]
        assert leftovers == [], f"attempt logging must be a no-op when unset: {leftovers}"


def test_attempt_log_preserves_the_truncated_first_attempt_the_summary_erases():
    """The exact defect trap 2 names: complete_structured's RETURNED result carries finish_reason=
    "stop" (the successful retry) — the attempt log is the only place "length" on attempt 0 survives."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_llm_module(tmp)
        log_path = os.path.join(tmp, "attempts.jsonl")
        L._ATTEMPT_LOG = log_path
        try:
            b = _FakeBackend(floor=1500)   # attempt 0 (cap=800) truncates; attempt 1 (cap=1600) succeeds
            r = L.complete_structured(b, "p", {"type": "object"}, max_tokens=800, temperature=0)
            assert r.finish_reason == "stop", "sanity: the AGGREGATE result must look clean"
            records = MC.parse_attempt_log(pathlib.Path(log_path))
            assert len(records) == 2, f"expected one record per attempt: {records}"
            assert records[0]["finish_reason"] == "length" and records[0]["cap"] == 800
            assert records[1]["finish_reason"] == "stop" and records[1]["cap"] == 1600
            truncated, total = MC.truncation_rate(records)
            assert (truncated, total) == (1, 2)
        finally:
            L._ATTEMPT_LOG = ""


def test_parse_attempt_log_skips_a_malformed_line_instead_of_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "attempts.jsonl"
        p.write_text('{"finish_reason": "length"}\nnot json at all\n{"finish_reason": "stop"}\n')
        records = MC.parse_attempt_log(p)
        assert len(records) == 2
        assert MC.truncation_rate(records) == (1, 2)


def test_parse_attempt_log_missing_file_is_empty_not_an_error():
    assert MC.parse_attempt_log(pathlib.Path("/no/such/attempts.jsonl")) == []


# --------------------------------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------------------------------

def test_collect_run_result_reads_plan_hash_and_step_set_from_a_real_plan_json():
    with tempfile.TemporaryDirectory() as tmp:
        art = pathlib.Path(tmp)
        steps = _steps([("click", "s1", None)])
        from brain.state import canonical_plan_hash
        (art / "plan.json").write_text(json.dumps({"plan_hash": canonical_plan_hash(steps), "steps": steps}))
        rp = MC.RunPaths(model="m1", run_index=1, run_id="m1-r1", artifact_dir=art,
                         budget_file=art / "b.json", attempt_log=art / "a.jsonl")
        result = MC.collect_run_result("m1", 1, rp, {"exit_code": 0, "duration_s": 1.0, "timed_out": False})
        assert result.ok is True
        assert result.plan_hash == canonical_plan_hash(steps)
        assert result.step_set == frozenset({("click", "s1", None)})


def test_collect_run_result_a_crashed_run_with_no_plan_json_is_reported_not_ok():
    with tempfile.TemporaryDirectory() as tmp:
        art = pathlib.Path(tmp)
        rp = MC.RunPaths(model="m1", run_index=1, run_id="m1-r1", artifact_dir=art,
                         budget_file=art / "b.json", attempt_log=art / "a.jsonl")
        result = MC.collect_run_result("m1", 1, rp, {"exit_code": 4, "duration_s": 0.5, "timed_out": False,
                                                      "stderr_tail": "boom"})
        assert result.ok is False
        assert result.error and "exit 4" in result.error


def _run_result(model, i, plan_hash, step_set, truncated=0, total=0):
    return MC.RunResult(model=model, run_index=i, run_id=f"{model}-r{i}", ok=True, exit_code=0,
                        duration_s=1.0, plan_hash=plan_hash, step_count=len(step_set), step_set=step_set,
                        attempts_total=total, attempts_truncated=truncated)


def test_aggregate_classifies_each_model_and_the_cross_model_group():
    same_set = frozenset({("click", "s1", None)})
    other_set = frozenset({("click", "s2", None)})
    results = [
        _run_result("m1", 1, "h1", same_set, truncated=1, total=4),
        _run_result("m1", 2, "h1", same_set, truncated=0, total=3),   # m1: byte-identical every run
        _run_result("m2", 1, "h2", other_set, truncated=2, total=2),  # m2: 100% truncated
        _run_result("m2", 2, "h3", other_set, truncated=0, total=2),
    ]
    agg = MC.aggregate(results)
    assert agg["per_model"]["m1"]["classification"] == "stable"
    assert agg["per_model"]["m1"]["truncation_rate"] == 1 / 7
    assert agg["per_model"]["m2"]["classification"] == "order_instability"  # same set, hash h2 != h3
    assert agg["per_model"]["m2"]["truncation_rate"] == 2 / 4
    # m1 picked `same_set`, m2 picked `other_set` -> the two models disagree with each other
    assert agg["cross_model"]["classification"] == "different_interpretation"


def test_aggregate_reports_no_successful_runs_rather_than_a_false_stable():
    results = [MC.RunResult(model="m1", run_index=1, run_id="m1-r1", ok=False, exit_code=4,
                            duration_s=1.0, error="crashed")]
    agg = MC.aggregate(results)
    assert agg["per_model"]["m1"]["classification"] == "no_successful_runs"
    assert agg["per_model"]["m1"]["distinct_plan_hashes"] == []


def test_aggregate_cross_model_notes_when_fewer_than_two_models_succeeded():
    results = [_run_result("m1", 1, "h1", frozenset({("click", "s1", None)}))]
    agg = MC.aggregate(results)
    assert agg["cross_model"]["classification"] is None
    assert "fewer than 2" in agg["cross_model"]["note"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok   {fn.__name__}")
    print(f"\n{len(tests)}/{len(tests)} checks passed")
