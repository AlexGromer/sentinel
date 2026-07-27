"""Record→scenario bridge (M9.8 §3, ADR-038, GAP-M9-13) — events.ndjson -> replayable scenario.json.

The MV3 recorder (issue #44) streams one event per line to runs/record-<session>/events.ndjson via the
WS ingest (ADR-043). This module turns that stream into an M9.2b scenario, REUSING the same grounding
emitter the goal/describe heads use (brain/scenario.ground_scenario): cross-page navigates synthesized
in code, sequential step_ids, replay-schema steps. No new planner, no fabricated selectors.

The recorder's `selectorCandidates` are REAL selectors observed on the live DOM at record time, so the
binding is the recording itself — we register each event's target as a site-map element (primary
locator + ranked alternatives) and ground the ordered actions against that map. An event with no usable
selector is dropped, never fabricated.

Event schema (the contract the recorder emits; #44 / M9.8_CONTRACT §2):
    {"type": "click"|"input"|"change"|"submit",     # DOM event type
     "url": "<page url>",
     "selectorCandidates": [{"strategy": "<name>", "locator": {<pw-executor locator>}}, ...],  # ranked
     "value": "<text>"        # optional; only for NON-secret inputs (redaction at record time)
     "secretRef": "<ENV_NAME>" # optional; a redacted secret field carries the env-var name, never the value
     "verb": "<verb>"}         # optional explicit override; else the DOM type maps to a verb

`selectorCandidates` may also be a bare pw-executor locator dict ({testid}/{role,name}/{label}/{text}/
{css}/{xpath}); the strategy is inferred from its key.
"""
from __future__ import annotations

import json
import sys

from .eventlog import log
from .scenario import VALID_VERBS, ground_scenario
from .state import canonical_plan_hash, normalize_url, semantic_id
from .strategies import prior_for as _prior_for
from .strategies import STRATEGY_BY_LOCATOR_KEY as _STRATEGY_BY_KEY
from .strategies import CSS as _CSS

# DOM event type -> replay verb. `submit` is dropped: the user's click on the submit control is already
# captured as its own `click` event, so replaying submit too would double-fire. An explicit event
# `verb` (a smarter recorder can emit type/select/press) overrides this map.
_VERB_BY_TYPE = {"click": "click", "input": "fill", "change": "fill", "submit": None}

# ADR-083: the vocabulary comes from `strategies.py` instead of a local mirror. The mirror is exactly
# how this broke — it spelled the text strategy `text` while the explorer spelled it `text_role`, and
# `healing.PRIORS` knew only the latter, so a recorded plan's text alternative fell to the
# unknown-strategy default of 0.5 (below FLAG) and silently never healed. `strategies.py` is kept
# import-light precisely so this file need not trade correctness for a cheap import.




def _infer_strategy(loc: dict) -> str:
    for k in ("testid", "role", "label", "text", "css", "xpath"):
        if k in loc:
            return _STRATEGY_BY_KEY[k]
    return _CSS


def _norm_candidate(c):
    """Accept {'strategy','locator'} or a bare pw-executor locator dict -> (strategy, locator)|None."""
    if not isinstance(c, dict) or not c:
        return None
    if isinstance(c.get("locator"), dict) and c["locator"]:
        loc = c["locator"]
        return (c.get("strategy") or _infer_strategy(loc)), loc
    return _infer_strategy(c), c   # bare locator dict


def _loc_label(loc: dict) -> str:
    """A stable distinguishing label for an element with no role+name (keeps semantic_ids distinct)."""
    for k in ("testid", "label", "text", "css", "xpath"):
        if loc.get(k):
            return f"{k}={loc[k]}"
    if loc.get("role"):
        return f"{loc['role']}:{loc.get('name', '')}"
    return json.dumps(loc, sort_keys=True)


def _resolve_locator(candidates):
    """Ranked candidates -> (primary_locator, alternatives, role, name)|None. Never fabricates."""
    norm = [nc for nc in (_norm_candidate(c) for c in (candidates or [])) if nc]
    if not norm:
        return None
    _, primary = norm[0]
    alternatives = [{"strategy": s, "locator": l, "prior": _prior_for(s)} for s, l in norm[1:]]
    role, name = "", ""
    for s, l in norm:                                   # role/name from the role_name candidate, if any
        if s == "role_name":
            role, name = l.get("role", "") or "", l.get("name", "") or ""
            break
    return primary, alternatives, role, name


def _verb_for(ev: dict):
    """Explicit event `verb` wins (recorder may say select/press/type); else map the DOM type. None=skip."""
    v = (ev.get("verb") or "").strip().lower()
    if v:
        return v if v in VALID_VERBS else None
    return _VERB_BY_TYPE.get((ev.get("type") or "").strip().lower())


def _attach_value(ref: dict, verb: str, ev: dict) -> bool:
    """Route the recorded value into the field ground_scenario._verb_step reads for this verb.

    Returns False to DROP the event: a `press` with no resolvable key would bake `key=None` into the
    step — `canonical_plan_hash` would include it and replay would call `browser.press key=None`, which
    fails. Drop it the same way an empty-selector event is dropped, never emit a malformed step.
    """
    if verb == "fill":
        if ev.get("secretRef"):                         # redacted secret -> env ref, never a literal (M9.1)
            ref["secretRef"] = ev["secretRef"]
        else:
            ref["value"] = ev.get("value", "")
    elif verb == "select":
        ref["value"] = ev.get("value", "")
    elif verb == "type":
        ref["text"] = ev.get("value", "")
    elif verb == "press":
        key = ev.get("key") or ev.get("value")
        if not key:
            return False                                # no key -> drop, never a key=None step
        ref["key"] = key
    elif verb == "assert":                              # only via an explicit recorder verb=assert
        for k in ("condition", "expected", "expect_ok"):
            if ev.get(k) is not None:
                ref[k] = ev[k]
    return True


def events_to_steps(events: list, start_id: int = 1):
    """events -> (steps, unmatched), grounded via brain/scenario.ground_scenario. Pure/offline."""
    site_map: dict = {}
    refs: list = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        verb = _verb_for(ev)
        if verb not in VALID_VERBS:                     # None (e.g. submit) or out-of-spec -> drop
            continue
        resolved = _resolve_locator(ev.get("selectorCandidates"))
        if not resolved:                                # no real selector -> skip, never fabricate
            continue
        primary, alternatives, role, name = resolved
        page = normalize_url(ev.get("url") or "")
        sid = semantic_id(page, role, name or _loc_label(primary))
        ref = {"ref": sid, "verb": verb,
               "intent": f"{verb} {role} '{name}'".strip() if (role or name) else f"{verb} {_loc_label(primary)}"}
        if not _attach_value(ref, verb, ev):            # e.g. press with no key -> drop before registering
            log("record.event_no_value", verb=verb)
            continue
        bucket = site_map.setdefault(page, [])          # register the element only once the event is known-good
        if not any(e["semantic_id"] == sid for e in bucket):
            bucket.append({"semantic_id": sid, "role": role, "name": name,
                           "locator": primary, "alternatives": alternatives, "page": page})
        # collapse consecutive same-element fills: live typing emits many input events, one per keystroke,
        # but the scenario wants a single fill carrying the final value.
        if verb == "fill" and refs and refs[-1]["ref"] == sid and refs[-1].get("verb") == "fill":
            refs[-1] = ref
            continue
        refs.append(ref)
    return ground_scenario(refs, site_map, start_page="", start_id=start_id)


def build_scenario(events: list, session: str = "", target_url: str = "", start_id: int = 1):
    """events -> (scenario_dict, unmatched). scenario_dict is the M9.2b plan: ready for agentctl --replay."""
    steps, unmatched = events_to_steps(events, start_id=start_id)
    if not target_url and steps and steps[0].get("action_type") == "navigate":
        target_url = steps[0].get("target", "")        # the synthesized first-page navigate
    scenario = {"plan_id": f"record-{session}" if session else "record",
                "target_url": target_url, "plan_hash": canonical_plan_hash(steps), "steps": steps}
    return scenario, unmatched


def load_events(path: str) -> list:
    """Read events.ndjson; tolerate blank lines and a partial trailing line (best-effort ingest)."""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log("record.line_unparseable")
    return events


def write_scenario(events_path: str, out_path: str, session: str = "", target_url: str = ""):
    """events.ndjson file -> scenario.json file. Returns (scenario_dict, unmatched)."""
    scenario, unmatched = build_scenario(load_events(events_path), session=session, target_url=target_url)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scenario, f, indent=2)
    if unmatched:
        log("record.unmatched_dropped", count=len(unmatched))
    return scenario, unmatched


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python -m brain.record_bridge <events.ndjson> <out scenario.json> [session]",
              file=sys.stderr)
        raise SystemExit(2)
    _session = sys.argv[3] if len(sys.argv) > 3 else ""
    sc, un = write_scenario(sys.argv[1], sys.argv[2], session=_session)
    print(f"wrote {sys.argv[2]}: {len(sc['steps'])} steps, {len(un)} unmatched")
