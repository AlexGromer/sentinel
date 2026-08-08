#!/usr/bin/env python3
"""
model_convergence.py — MODEL-002: measure how much N local models agree with themselves and with
each other on ONE explore, over M repeated runs each.

This is a MEASUREMENT tool, not a product change. It answers three questions with real numbers,
never with an estimate:

  1. Self-consistency: run the SAME model M times against the SAME fixture — does it pick the same
     walk every time (`plan_hash` stable), the same ELEMENTS in a different order (hash differs, the
     grounded step SET does not — order instability), or genuinely different elements each run?
  2. Cross-model agreement: do different models converge on the same walk, or diverge?
  3. Truncation cost: what share of LLM ATTEMPTS (not final decisions — see the module note on
     `_truncation_rate`) hit `finish_reason="length"` per model, i.e. who is starved by the token
     ceiling actually configured for this run.

What "which fields MUST agree between models" means is a SEPARATE, un-started task (MODEL-001) — this
tool only produces the numbers that decision would be based on.

Four measured traps this script exists to route around (see brain/llm.py, brain/planner.py,
brain/graph.py for the code each refers to):

  1. The learned token-ceiling file (`brain/llm.py::_BUDGET_FILE`) is read ONCE per process and
     persists on disk across separate invocations. Every (model, run) here gets its OWN
     SENTINEL_LLM_BUDGET_FILE (see `build_run_env`) so a ceiling learned by one attempt can never
     leak into another model's first attempt — or even into this SAME model's next repeated run,
     which would silently suppress the very finish_reason="length" signal this script counts.
  2. `complete_structured` sums tokens across retry attempts and keeps only the LAST attempt's
     `finish_reason` — a truncation followed by a successful retry is invisible in every existing
     artifact (llm-transcript.jsonl records one row per PLAN STEP, after retries resolve). This
     script reads a NEW opt-in per-ATTEMPT log (`SENTINEL_LLM_ATTEMPT_LOG`, brain/llm.py
     `_record_attempt`) instead.
  3. Token ceilings are passed EXPLICITLY (`--pick-tokens` -> `LLM_MAX_TOKENS_PICK`) rather than
     assumed to match brain/planner.py's current default, so a report says what ceiling was actually
     in force rather than inheriting a stale number from an old observation.
  4. `canonical_plan_hash` (brain/state.py) hashes the WHOLE ordered `exploration_plan` — a step_id
     lives in every entry, so re-ordering the SAME picks changes the hash exactly like picking
     DIFFERENT elements would. `grounded_step_set` below is the order-INSENSITIVE half of the
     comparison that tells the two apart (see its docstring for exactly where the deterministic part
     of a step ends and the planner's own choice begins).

Usage:
    scripts/model_convergence.py [--base-url URL] [--models m1,m2,...] [--runs M]
        [--fixture NAME] [--max-steps N] [--coverage-target F] [--pick-tokens N]
        [--out-dir DIR] [--keep-raw] [--dry-run]

Fixtures and models are DERIVED, never hand-typed into this file: fixtures from
testdata/fixtures/*.html, models from GET {ollama root}/api/tags (the endpoint's OWN inventory, not
a name this script assumes exists). Both are floored at >=1 discovered item — an empty set is a hard
error, not a vacuous pass (see `require_nonempty`).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from brain.state import canonical_plan_hash  # noqa: E402 — reuse the product's OWN hash fn, never reimplement

DEFAULT_FIXTURE = "l1.html"          # preferred if present; NOT trusted blindly — see resolve_fixture
DEFAULT_BASE_URL = "http://192.168.88.170:11434/v1"
MIN_DISCOVERED = 1                   # floor: an empty fixture/model set must refuse, never pass vacuously


class ConvergenceError(RuntimeError):
    """A precondition this harness requires before spending any live LLM call was not met."""


# --------------------------------------------------------------------------------------------------
# Discovery — "the list is derived, not maintained" (repo convention). Fixtures come from the
# filesystem, models from the endpoint's own /api/tags. Neither is a literal list in this file.
# --------------------------------------------------------------------------------------------------

def discover_fixtures(fixtures_dir: pathlib.Path) -> list[str]:
    """Fixture file names under `fixtures_dir` (testdata/fixtures/*.html), sorted."""
    if not fixtures_dir.is_dir():
        return []
    return sorted(p.name for p in fixtures_dir.glob("*.html"))


def ollama_root_from_base_url(base_url: str) -> str:
    """LLM_BASE_URL is the OpenAI-compat surface (".../v1"); Ollama's own /api/tags lives at the root,
    one path segment up. Strips exactly one trailing '/v1' — anything else is left untouched so a
    non-Ollama OpenAI-compat endpoint (which has no /api/tags) fails loudly at the HTTP layer rather
    than guessing at a made-up path."""
    b = base_url.rstrip("/")
    if b.endswith("/v1"):
        return b[: -len("/v1")]
    return b


def _http_get_json(url: str, timeout: float):
    """The one real-network call in this module, isolated so tests can substitute a fake `fetch`
    (dependency injection — no live socket needed to test `probe_ollama`'s branches)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — local/operator-supplied URL
        return resp.getcode(), json.loads(resp.read().decode("utf-8"))


def probe_ollama(base_url: str, timeout: float = 10.0, *, fetch=_http_get_json) -> dict:
    """GET {ollama root}/api/tags. NEVER raises (fail-open, per repo convention) — a silent endpoint
    (this one has gone HTTP 000 before, per session memory) is a FACT for the report, not a reason to
    skip the step. Returns {reachable, http_code, models, error, url}."""
    root = ollama_root_from_base_url(base_url)
    url = root + "/api/tags"
    try:
        code, doc = fetch(url, timeout)
    except Exception as e:  # noqa: BLE001 — network/parse failure is a reportable fact, not a crash
        return {"reachable": False, "http_code": None, "models": [], "error": f"{type(e).__name__}: {e}",
                "url": url}
    models = doc.get("models", []) if isinstance(doc, dict) else []
    return {"reachable": True, "http_code": code, "models": models, "error": None, "url": url}


def usable_model_names(models: list[dict]) -> list[str]:
    """Model names from a /api/tags response, sorted. No filtering by capability here — availability
    is the source of truth (repo rule: "перечень выводится, не пишется"); an operator narrows the set
    for a specific run via --models, documented at the call site, not baked into this function."""
    return sorted(m["name"] for m in models if isinstance(m, dict) and m.get("name"))


def require_nonempty(items: list, what: str) -> list:
    """MODEL-002: a floor on the count. Without this an empty discovery silently produces a report
    over zero combinations that LOOKS like a clean run. Raises `ConvergenceError`, never returns an
    empty list."""
    if not items:
        raise ConvergenceError(f"no {what} discovered — refusing to run over an empty set")
    return items


def resolve_fixture(requested: str | None, discovered: list[str]) -> str:
    """One fixture, from the DISCOVERED set (never a bare string nobody checked against disk).
    `DEFAULT_FIXTURE` is a preference, not an assumption: if it is absent from what is actually on
    disk this falls back to the first (sorted) discovered fixture instead of pointing at a file that
    may no longer exist."""
    require_nonempty(discovered, "fixtures under testdata/fixtures/*.html")
    if requested:
        if requested not in discovered:
            raise ConvergenceError(f"fixture {requested!r} not among discovered fixtures: {discovered}")
        return requested
    # Sorted here rather than trusted from the caller: this function's contract ("first discovered") is
    # about the STABLE, alphabetically-first fixture, not whatever order the caller happened to build.
    return DEFAULT_FIXTURE if DEFAULT_FIXTURE in discovered else min(discovered)


def resolve_models(requested: list[str] | None, discovered: list[str]) -> list[str]:
    """The N models for this run. `discovered` (from the live endpoint) is floored first regardless of
    whether `requested` narrows it — an operator's explicit list is a SCOPE on real availability, never
    a substitute for it (repo rule: take the list from what exists, not from what a task said should
    exist)."""
    require_nonempty(discovered, "models from the ollama endpoint (/api/tags)")
    if not requested:
        return discovered
    missing = [m for m in requested if m not in discovered]
    if missing:
        raise ConvergenceError(f"requested model(s) not present on the endpoint: {missing} "
                               f"(available: {discovered})")
    return requested


# --------------------------------------------------------------------------------------------------
# Trap 4 — the order-insensitive half of the comparison.
# --------------------------------------------------------------------------------------------------

def step_identity(step: dict) -> tuple:
    """The part of a plan.json step that identifies WHAT the planner picked, independent of where in
    the ordered list it landed. `action_type`+`semantic_id` is the planner's actual choice
    (brain/planner.py propose() returns `candidates[idx]` verbatim — see LLMPlanner.propose /
    GoalPlanner.propose); `target` is redundant for a click (folded into semantic_id already) but
    disambiguates two navigate steps by destination even if a future semantic_id change ever stopped
    folding the path in."""
    return (step.get("action_type"), step.get("semantic_id"), step.get("target"))


def grounded_step_set(steps: list) -> frozenset:
    """The SET of what the planner picked, order and step_id discarded. Excludes step 1: `_run_explore`
    (brain/__main__.py) writes the initial navigate to the target BEFORE the graph — and before any
    `planner.propose()` call — ever runs, so it is byte-identical across every model/run on the same
    fixture by construction and would only pad every set with a constant member that proves nothing.

    Locator/alternatives/intent are deliberately NOT part of the identity: given the same semantic_id
    and the same prior action sequence (which the fixture's static DOM makes deterministic), those
    fields follow from perception, not from the planner's choice — including them would make two runs
    that picked the SAME elements compare as different for a reason that has nothing to do with the
    model."""
    return frozenset(step_identity(s) for s in steps if s.get("step_id", 0) != 1)


def classify(hashes: list[str], step_sets: list[frozenset]) -> str:
    """The three-way read the task asks for, from two OBSERVED quantities over a group of runs
    (either one model's own M runs, or one representative walk per model across N models):

      - "stable_in_this_invocation" — every plan_hash in THIS group is identical.

    ⚠ THE WORD MATTERS AND IT IS NOT "stable". Measured 2026-08-08 against the live ollama, same
    fixture, same flags, same model (qwen3:8b): two runs inside ONE invocation agreed
    (df271e5a3bee twice), and three separate invocations produced THREE different hashes
    (29abe04911b5 · df271e5a3bee · 906c6715…). Nothing pins the model's sampling — neither this
    harness nor brain/llm.py passes a `seed`, and canonical_plan_hash (brain/state.py:96) hashes the
    ENTIRE ordered step list, so any variation in what the model chose changes it.

    So the quantity this classifier measures is agreement WITHIN one invocation, and calling that
    "stable" would promise reproducibility that was measured NOT to hold. The distinction is the
    whole point of the exercise: MODEL-001 has to decide what must match between models, and it
    cannot inherit a label that overstates what the numbers support.

      - "order_instability"      — hashes differ, but every step SET is the same: the same elements
                                    were picked, in a different order each time.
      - "different_interpretation" — the step SETS themselves differ: distinct runs picked distinct
                                    elements. (Explore mode has no goal to interpret — "interpretation"
                                    here is the task's own term for "the model's choices differ", and
                                    applies the same way whether the group is one model's repeats or
                                    several models' single walks.)

    Requires at least one hash/step_set pair; the caller is responsible for not calling this on an
    empty group (an empty group is not "stable", it is "no data" — a caller conflating the two would
    silently report convergence over nothing)."""
    if not hashes or not step_sets:
        raise ConvergenceError("classify() called on an empty group — no data to classify")
    if len(set(hashes)) <= 1:
        return "stable_in_this_invocation"
    if len(set(step_sets)) <= 1:
        return "order_instability"
    return "different_interpretation"


# --------------------------------------------------------------------------------------------------
# Trap 2 — per-ATTEMPT truncation, read from the opt-in log brain/llm.py now writes.
# --------------------------------------------------------------------------------------------------

def parse_attempt_log(path: pathlib.Path) -> list[dict]:
    """Every line `brain.llm._record_attempt` wrote for one run. A malformed line (a crashed write,
    truncated by a killed process) is skipped rather than aborting the whole parse — partial evidence
    from a crashed run is still evidence."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def truncation_rate(records: list[dict]) -> tuple[int, int]:
    """(# attempts with finish_reason=="length", # attempts total) — computed on the PER-ATTEMPT log,
    never on a run's final decision (see the module docstring, trap 2): `complete_structured` sums
    tokens across attempts and keeps only the LAST attempt's finish_reason, so a truncation immediately
    followed by a successful retry is invisible everywhere except this log."""
    total = len(records)
    truncated = sum(1 for r in records if r.get("finish_reason") == "length")
    return truncated, total


# --------------------------------------------------------------------------------------------------
# Run orchestration — spawns `python -m brain` once per (model, run index). Live: browser + network.
# --------------------------------------------------------------------------------------------------

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def default_python_bin() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def default_pw_executor_cmd() -> str:
    return f"node {REPO_ROOT / 'pw-executor' / 'dist' / 'server.js'}"


@dataclass
class RunPaths:
    model: str
    run_index: int
    run_id: str
    artifact_dir: pathlib.Path
    budget_file: pathlib.Path
    attempt_log: pathlib.Path


def build_run_env(base_env: dict, *, model: str, target_url: str, paths: RunPaths, base_url: str,
                  pick_tokens: int, max_steps: int, coverage_target: float, pw_executor_cmd: str) -> dict:
    """The subprocess env for one (model, run) explore. Trap 1: `SENTINEL_LLM_BUDGET_FILE` and
    trap-2's `SENTINEL_LLM_ATTEMPT_LOG` are UNIQUE per call (see `RunPaths` construction in
    `plan_matrix`) — two calls with different (model, run_index) must never resolve to the same path,
    or the isolation this whole harness depends on silently stops holding."""
    env = dict(base_env)
    env.update({
        "RUN_ID": paths.run_id,
        "ARTIFACT_DIR": str(paths.artifact_dir),
        "RUN_MODE": "explore",
        "TARGET_URL": target_url,
        "PW_EXECUTOR_CMD": pw_executor_cmd,
        "COVERAGE_TARGET": str(coverage_target),
        "MAX_STEPS": str(max_steps),
        "PLANNER": "llm",              # pure explore, LLMPlanner (brain/planner.py make_planner) — NOT GoalPlanner
        "LLM_BACKEND": "openai",
        "LLM_MODEL_PLANNER": model,    # role-specific override (brain/llm.py _env) — matches control-api's model_planner
        "LLM_BASE_URL": base_url,
        "LLM_STRUCTURED": "1",
        "LLM_MAX_TOKENS_PICK": str(pick_tokens),   # trap 3: explicit, never assumed
        "SENTINEL_LLM_BUDGET_FILE": str(paths.budget_file),
        "SENTINEL_LLM_ATTEMPT_LOG": str(paths.attempt_log),
        "PYTHONPATH": str(REPO_ROOT),
    })
    return env


def run_one(python_bin: str, env: dict, timeout: float) -> dict:
    """Spawn `python -m brain` for one explore run. Never raises on a BRAIN-side failure (non-zero
    exit, timeout, crash) — those are FACTS about the model/run, and must reach the report rather than
    aborting the whole matrix (fail-open, per repo convention)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run([python_bin, "-m", "brain"], cwd=str(REPO_ROOT), env=env,
                              capture_output=True, text=True, timeout=timeout, check=False)
        return {"exit_code": proc.returncode, "duration_s": round(time.monotonic() - t0, 1),
                "timed_out": False, "stderr_tail": proc.stderr[-4000:]}
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        return {"exit_code": None, "duration_s": round(time.monotonic() - t0, 1), "timed_out": True,
                "stderr_tail": stderr[-4000:]}


def plan_matrix(models: list[str], runs: int, out_root: pathlib.Path) -> list[RunPaths]:
    """One RunPaths per (model, run index) — this is where trap-1 isolation is actually constructed:
    every entry gets its own artifact dir, budget file and attempt log, so nothing here can leak into
    a sibling entry regardless of what brain/llm.py does internally."""
    matrix = []
    for model in models:
        for i in range(1, runs + 1):
            run_id = f"{_slug(model)}-r{i}"
            base = out_root / "raw" / _slug(model) / f"run{i}"
            matrix.append(RunPaths(model=model, run_index=i, run_id=run_id, artifact_dir=base,
                                   budget_file=base / "llm-budget.json",
                                   attempt_log=base / "llm-attempts.jsonl"))
    return matrix


# --------------------------------------------------------------------------------------------------
# Aggregation + report
# --------------------------------------------------------------------------------------------------

@dataclass
class RunResult:
    model: str
    run_index: int
    run_id: str
    ok: bool
    exit_code: int | None
    duration_s: float
    plan_hash: str | None = None
    step_count: int | None = None
    step_set: frozenset | None = None
    attempts_total: int = 0
    attempts_truncated: int = 0
    error: str | None = None


def collect_run_result(model: str, run_index: int, paths: RunPaths, proc_result: dict) -> RunResult:
    plan_file = paths.artifact_dir / "plan.json"
    plan_hash = step_count = step_set = None
    error = None
    if proc_result.get("timed_out"):
        error = "timed out"
    elif proc_result.get("exit_code") not in (0, None) and not plan_file.exists():
        error = f"exit {proc_result.get('exit_code')}, no plan.json ({proc_result.get('stderr_tail', '')[-300:]})"
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            steps = plan.get("steps", [])
            plan_hash = plan.get("plan_hash") or canonical_plan_hash(steps)
            step_count = len(steps)
            step_set = grounded_step_set(steps)
        except Exception as e:  # noqa: BLE001 — a bad plan.json is a fact for the report, not a crash
            error = error or f"plan.json unreadable: {e}"
    attempts = parse_attempt_log(paths.attempt_log)
    truncated, total = truncation_rate(attempts)
    return RunResult(model=model, run_index=run_index, run_id=paths.run_id,
                     ok=(plan_hash is not None), exit_code=proc_result.get("exit_code"),
                     duration_s=proc_result.get("duration_s", 0.0), plan_hash=plan_hash,
                     step_count=step_count, step_set=step_set, attempts_total=total,
                     attempts_truncated=truncated, error=error)


def aggregate(results: list[RunResult]) -> dict:
    by_model: dict[str, list[RunResult]] = {}
    for r in results:
        by_model.setdefault(r.model, []).append(r)

    per_model = {}
    for model, rs in by_model.items():
        ok_rs = [r for r in rs if r.ok]
        attempts_total = sum(r.attempts_total for r in rs)
        attempts_truncated = sum(r.attempts_truncated for r in rs)
        entry = {
            "runs": len(rs), "runs_ok": len(ok_rs), "runs_failed": len(rs) - len(ok_rs),
            "distinct_plan_hashes": sorted({r.plan_hash for r in ok_rs}),
            "distinct_step_set_count": len({r.step_set for r in ok_rs}),
            "attempts_total": attempts_total, "attempts_truncated": attempts_truncated,
            "truncation_rate": (attempts_truncated / attempts_total) if attempts_total else None,
            "avg_duration_s": (sum(r.duration_s for r in rs) / len(rs)) if rs else None,
        }
        if ok_rs:
            entry["classification"] = classify([r.plan_hash for r in ok_rs], [r.step_set for r in ok_rs])
        else:
            entry["classification"] = "no_successful_runs"
        per_model[model] = entry

    # cross-model: one representative (the first successful run) per model, compared to each other.
    reps: dict[str, RunResult] = {}
    for model, rs in by_model.items():
        ok_rs = [r for r in rs if r.ok]
        if ok_rs:
            reps[model] = ok_rs[0]
    cross_model = {"models_compared": sorted(reps), "note": None}
    if len(reps) >= 2:
        hashes = [r.plan_hash for r in reps.values()]
        sets = [r.step_set for r in reps.values()]
        cross_model["classification"] = classify(hashes, sets)
        cross_model["distinct_step_set_count"] = len(set(sets))
    else:
        cross_model["classification"] = None
        cross_model["note"] = "fewer than 2 models produced a successful run — no cross-model comparison"
    return {"per_model": per_model, "cross_model": cross_model}


def print_summary(report: dict) -> None:
    meta = report["meta"]
    print("=" * 72)
    print(f"MODEL-002 convergence — fixture={meta['fixture']} runs/model={meta['runs_per_model']} "
         f"max_steps={meta['max_steps']} pick_tokens={meta['pick_tokens']}")
    print(f"ollama: reachable={meta['ollama_reachable']} http_code={meta['ollama_http_code']} "
         f"url={meta['ollama_url']}")
    print("=" * 72)
    for model, e in report["aggregate"]["per_model"].items():
        tr = f"{e['truncation_rate']:.0%}" if e["truncation_rate"] is not None else "n/a"
        print(f"{model:<20} runs_ok={e['runs_ok']}/{e['runs']}  hashes={len(e['distinct_plan_hashes'])} "
             f"step_sets={e['distinct_step_set_count']}  class={e['classification']:<22} "
             f"truncated={e['attempts_truncated']}/{e['attempts_total']} ({tr})  "
             f"avg_dur={e['avg_duration_s'] and round(e['avg_duration_s'], 1)}s")
    cm = report["aggregate"]["cross_model"]
    print("-" * 72)
    print(f"cross-model ({', '.join(cm['models_compared'])}): "
         f"{cm['classification'] or cm['note']}")
    print("=" * 72)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
                  help="OpenAI-compat base URL of the ollama endpoint (default: %(default)s)")
    p.add_argument("--models", default=None,
                  help="comma-separated subset of models to run (default: every model /api/tags reports)")
    p.add_argument("--runs", type=int, default=3, help="M — repeated runs per model (default: 3)")
    p.add_argument("--fixture", default=None,
                  help=f"testdata/fixtures/*.html file name (default: {DEFAULT_FIXTURE} if present, "
                       "else the first discovered)")
    p.add_argument("--max-steps", type=int, default=6, help="explore MAX_STEPS (default: 6)")
    p.add_argument("--coverage-target", type=float, default=0.85, help="explore COVERAGE_TARGET (default: 0.85)")
    p.add_argument("--pick-tokens", type=int, default=1024,
                  help="LLM_MAX_TOKENS_PICK — explicit, see trap 3 (default: 1024)")
    p.add_argument("--timeout", type=float, default=900.0, help="per-run subprocess timeout, seconds")
    p.add_argument("--out-dir", default=None,
                  help="report + copied artifacts directory (default: runs/model-convergence/<ts>)")
    p.add_argument("--python", default=None, help="python interpreter for `-m brain` (default: .venv or current)")
    p.add_argument("--pw-executor-cmd", default=None, help="override PW_EXECUTOR_CMD")
    p.add_argument("--keep-raw", action="store_true",
                  help="keep the raw per-run artifact dirs (frames etc.) instead of cleaning runs/ after copying out plan.json/attempt-log")
    p.add_argument("--dry-run", action="store_true",
                  help="resolve fixtures/models and print the planned matrix; run nothing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    fixtures = discover_fixtures(REPO_ROOT / "testdata" / "fixtures")
    fixture = resolve_fixture(args.fixture, fixtures)

    probe = probe_ollama(args.base_url)
    if not probe["reachable"]:
        print(f"FATAL: ollama endpoint did not answer at {probe['url']}: {probe['error']}", file=sys.stderr)
        return 1
    discovered_models = usable_model_names(probe["models"])
    requested = [m.strip() for m in args.models.split(",")] if args.models else None
    try:
        models = resolve_models(requested, discovered_models)
    except ConvergenceError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else REPO_ROOT / "runs" / "model-convergence" / ts

    matrix = plan_matrix(models, args.runs, out_dir)
    target_url = f"file://{(REPO_ROOT / 'testdata' / 'fixtures' / fixture).resolve()}"

    print(f"fixture={fixture}  models={models}  runs_per_model={args.runs}  "
         f"combinations={len(matrix)}  target={target_url}")
    if args.dry_run:
        for rp in matrix:
            print(f"  DRY-RUN would run: {rp.run_id} -> {rp.artifact_dir}")
        return 0

    # Only created once real work is about to happen — a dry-run must leave no trace under runs/.
    out_dir.mkdir(parents=True, exist_ok=True)

    python_bin = args.python or default_python_bin()
    pw_cmd = args.pw_executor_cmd or default_pw_executor_cmd()
    results: list[RunResult] = []
    for rp in matrix:
        rp.artifact_dir.mkdir(parents=True, exist_ok=True)
        env = build_run_env(dict(os.environ), model=rp.model, target_url=target_url, paths=rp,
                            base_url=args.base_url, pick_tokens=args.pick_tokens,
                            max_steps=args.max_steps, coverage_target=args.coverage_target,
                            pw_executor_cmd=pw_cmd)
        print(f"-- running {rp.run_id} ...", flush=True)
        proc_result = run_one(python_bin, env, args.timeout)
        result = collect_run_result(rp.model, rp.run_index, rp, proc_result)
        results.append(result)
        print(f"   exit={result.exit_code} dur={result.duration_s}s ok={result.ok} "
             f"plan_hash={(result.plan_hash or '')[:12]} truncated={result.attempts_truncated}/{result.attempts_total}")

        # keep a small, permanent audit trail before any cleanup of the (large) raw artifact dir
        audit_dir = out_dir / "artifacts" / _slug(rp.model) / f"run{rp.run_index}"
        audit_dir.mkdir(parents=True, exist_ok=True)
        for name in ("plan.json", "llm-transcript.jsonl"):
            src = rp.artifact_dir / name
            if src.exists():
                shutil.copy2(src, audit_dir / name)
        if rp.attempt_log.exists():
            shutil.copy2(rp.attempt_log, audit_dir / "llm-attempts.jsonl")

    if not args.keep_raw:
        raw_dir = out_dir / "raw"
        try:
            shutil.rmtree(raw_dir)
        except OSError as e:
            print(f"WARN: could not clean up {raw_dir}: {e}", file=sys.stderr)

    agg = aggregate(results)
    report = {
        "meta": {
            "fixture": fixture, "target_url": target_url, "runs_per_model": args.runs,
            "max_steps": args.max_steps, "coverage_target": args.coverage_target,
            "pick_tokens": args.pick_tokens, "base_url": args.base_url,
            "ollama_reachable": probe["reachable"], "ollama_http_code": probe["http_code"],
            "ollama_url": probe["url"], "models_discovered": discovered_models, "models_run": models,
            "timestamp": ts,
        },
        "runs": [r.__dict__ | {"step_set": sorted(map(str, r.step_set)) if r.step_set else None}
                for r in results],
        "aggregate": agg,
    }
    report_file = out_dir / "report.json"
    report_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print_summary(report)
    print(f"report -> {report_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
