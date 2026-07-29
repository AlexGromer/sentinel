"""Append-only test revisions: history, diff, and rollback (PROD-VERSIONING).

`plan_hash` answers "is this the same plan?" but not "how does it differ from last time?", and it
keeps no history — re-authoring a test erased the previous one without a trace. For a product that
promises to MAINTAIN tests, that is the load-bearing gap: it could not answer "why did this test
change?" or "put it back the way it was".

This is an authoritative, file-based, append-only revision store — authoritative because correctness
must not depend on a network service (an air-gapped install may have no git remote, and that is a
stated differentiator; git is an optional adapter ON TOP, not the store). Append-only by
construction: a revision body is named by its plan_hash, so the same plan re-saved is idempotent and
a different plan is a new file; the ordered history is a JSONL log that only ever grows. Rollback does
not delete — it RE-APPENDS a prior revision as the new head, so "put it back" is itself a recorded
event and the intermediate history survives.

Layout under <root>/<scenario_id>/:
  <plan_hash>.json   — one revision body: {"plan": {...}, "created_at": <float>}
  _history.jsonl     — append-only ordered log, one line per save/rollback:
                       {"revision": <plan_hash>, "parent": <plan_hash|null>, "created_at": <float>,
                        "op": "save"|"rollback"}
"""
import json
import os
import pathlib
import time

from .state import canonical_plan_hash

_HISTORY = "_history.jsonl"


def _scenario_dir(root, scenario_id):
    # scenario_id is a caller-supplied identifier that becomes a path segment — reduce it to a base
    # name so it cannot traverse out of root (the same discipline the import channel uses on file names).
    base = os.path.basename(str(scenario_id).strip())
    if not base or base in (".", "..") or "/" in str(scenario_id) or "\\" in str(scenario_id):
        raise ValueError("scenario_id must be a plain identifier, not a path: %r" % (scenario_id,))
    return pathlib.Path(root) / base


def _steps(plan):
    return plan.get("steps", []) if isinstance(plan, dict) else list(plan)


def save_revision(root, scenario_id, plan, now=None):
    """Append `plan` as a revision. Returns {revision, parent, created_at, new}. Idempotent: saving a
    plan identical to the current head is a no-op that returns the existing head with new=False, so a
    re-run that did not change the plan does not inflate the history."""
    now = now or time.time
    d = _scenario_dir(root, scenario_id)
    d.mkdir(parents=True, exist_ok=True)
    revision = canonical_plan_hash(_steps(plan))
    hist = list_revisions(root, scenario_id)
    parent = hist[-1]["revision"] if hist else None
    if parent == revision:
        return {"revision": revision, "parent": (hist[-2]["revision"] if len(hist) > 1 else None),
                "created_at": hist[-1]["created_at"], "new": False}
    ts = now()
    (d / (revision + ".json")).write_text(
        json.dumps({"plan": plan, "created_at": ts}, ensure_ascii=False), encoding="utf-8")
    with (d / _HISTORY).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"revision": revision, "parent": parent, "created_at": ts, "op": "save"},
                            ensure_ascii=False) + "\n")
    return {"revision": revision, "parent": parent, "created_at": ts, "new": True}


def list_revisions(root, scenario_id):
    """The ordered history (oldest first). Empty when the scenario has none."""
    path = _scenario_dir(root, scenario_id) / _HISTORY
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _valid_revision(revision):
    # a revision id is always a canonical_plan_hash: 64 lowercase hex chars. Validating it before it
    # becomes a path segment means a crafted id (e.g. "../../etc/x") can never traverse — defence in
    # depth, since today the id comes from the history log or a computed hash, but get_plan/rollback are
    # a public surface an API could feed.
    return isinstance(revision, str) and len(revision) == 64 and all(c in "0123456789abcdef" for c in revision)


def get_plan(root, scenario_id, revision):
    """The plan body of a revision, or None if that revision id was never saved (or is malformed)."""
    if not _valid_revision(revision):
        return None
    path = _scenario_dir(root, scenario_id) / (revision + ".json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["plan"]


def head(root, scenario_id):
    """The current head revision id, or None."""
    hist = list_revisions(root, scenario_id)
    return hist[-1]["revision"] if hist else None


def _step_key(step, index):
    """Identity of a step for diffing: its semantic_id when grounded (stable across reorders), else a
    positional key (an ungrounded draft step has only its position)."""
    sid = step.get("semantic_id")
    return ("sid", sid) if sid else ("pos", index)


def diff_plans(old_plan, new_plan):
    """Step-level diff of two plans. Returns {added, removed, changed, unchanged}. `changed` names the
    exact fields that differ, so 'how does it differ' is answered per step, not as a hash mismatch."""
    old_steps = _steps(old_plan) if old_plan else []
    new_steps = _steps(new_plan) if new_plan else []
    old_by = {_step_key(s, i): s for i, s in enumerate(old_steps)}
    new_by = {_step_key(s, i): s for i, s in enumerate(new_steps)}
    added, removed, changed, unchanged = [], [], [], []
    for k, s in new_by.items():
        if k not in old_by:
            added.append({"key": list(k), "step": s})
    for k, s in old_by.items():
        if k not in new_by:
            removed.append({"key": list(k), "step": s})
    for k in old_by.keys() & new_by.keys():
        o, n = old_by[k], new_by[k]
        fields = sorted(set(o) | set(n))
        diffs = [f for f in fields if o.get(f) != n.get(f)]
        if diffs:
            changed.append({"key": list(k), "fields": diffs,
                            "before": {f: o.get(f) for f in diffs},
                            "after": {f: n.get(f) for f in diffs}})
        else:
            unchanged.append({"key": list(k)})
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}


def diff_revisions(root, scenario_id, rev_a, rev_b):
    """Diff two stored revisions by id."""
    return diff_plans(get_plan(root, scenario_id, rev_a), get_plan(root, scenario_id, rev_b))


def rollback(root, scenario_id, target_revision, now=None):
    """Make a prior revision the head again, WITHOUT deleting anything: it re-appends the target's plan
    as a new history entry (op="rollback"). "Put it back the way it was" is thus a recorded event, and
    the revisions in between remain in the history. Returns the new head entry, or raises if the target
    id was never saved."""
    now = now or time.time
    plan = get_plan(root, scenario_id, target_revision)
    if plan is None:
        raise ValueError("no such revision %r for %r" % (target_revision, scenario_id))
    d = _scenario_dir(root, scenario_id)
    hist = list_revisions(root, scenario_id)
    if hist and hist[-1]["revision"] == target_revision:
        return hist[-1]  # already at that revision — nothing to record
    ts = now()
    with (d / _HISTORY).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"revision": target_revision,
                             "parent": hist[-1]["revision"] if hist else None,
                             "created_at": ts, "op": "rollback"}, ensure_ascii=False) + "\n")
    return {"revision": target_revision, "parent": hist[-1]["revision"] if hist else None,
            "created_at": ts, "op": "rollback"}
