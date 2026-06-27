"""Sentinel brain — authoring substrate for two-phase goal (§L) + describe-first (§B) (M9.2b, ADR-028).

Shared, pure, offline grounding: turn a one-shot LLM output (a goal SCENARIO of refs, OR a describe
DRAFT) into REPLAYABLE plan steps bound to REAL elements from the explore site map. Both heads converge
here.

`site_map` shape: `{page_path: [element]}`, element = `{semantic_id, role, name, testid, locator,
alternatives, page}`. A step is shaped to the brain/replay.py schema so the frozen scenario replays
LLM-free and deterministically. **Grounding (ADR-022/028):** a ref / draft step that doesn't bind to a
real map element is dropped to `unmatched` — never fabricated. **Cross-page `navigate` steps are
synthesized in CODE** from the element's `page` key (a real URL), never from LLM free-text. Only specific
verb fields (value/text/key/condition/…) cross into a step — LLM `reason`/score never enter a step dict
(so `canonical_plan_hash` stays meaningful and the frozen plan replays deterministically).
"""
from .state import normalize_url, semantic_id

# Verbs an authored step may carry (brain/replay.py schema). An LLM verb outside this -> unmatched.
_VALID_VERBS = {"click", "fill", "type", "select", "press", "assert"}


def flatten_site_map(site_map: dict) -> list:
    """All elements across pages as one ordered list (each carries `page`). Deterministic order."""
    out = []
    for page in sorted(site_map or {}):
        for el in site_map[page]:
            out.append({**el, "page": el.get("page", page)})
    return out


def _index_by_id(site_map: dict) -> dict:
    return {el["semantic_id"]: {**el, "page": el.get("page", page)}
            for page in (site_map or {}) for el in site_map[page]}


def _nav_step(page: str, step_id: int) -> dict:
    return {"step_id": step_id, "action_type": "navigate",
            "semantic_id": semantic_id(page, "navigate", ""), "intent": f"navigate to {page}",
            "target": page, "locator": None, "alternatives": None, "is_milestone": False,
            "phase": "scenario"}


def _verb_step(element: dict, verb: str, extra: dict, step_id: int) -> dict:
    """A replay-schema step from a grounded element + verb (+ value/text/key/… from the LLM)."""
    step = {"step_id": step_id, "semantic_id": element["semantic_id"],
            "intent": extra.get("intent") or f"{verb} {element.get('role')} '{element.get('name')}'",
            "is_milestone": False, "phase": "scenario"}
    if verb == "assert":
        step.update({"action_type": "assert", "locator": element.get("locator"),
                     "condition": extra.get("condition", "visible"),
                     "expected": extra.get("expected"), "expect_ok": extra.get("expect_ok", True)})
        return step
    # locator verbs (click/fill/type/select/press): carry the grounded locator + alternatives copied
    # from the map element so replay can probe/heal — the determinism invariant.
    step.update({"action_type": verb, "locator": element.get("locator"),
                 "alternatives": element.get("alternatives") or []})
    if verb == "fill":
        if extra.get("secretRef") is not None:
            step["secretRef"] = extra["secretRef"]          # secret stays a ref (M9.1) — never a literal
        else:
            step["value"] = extra.get("value", "")
    elif verb == "type":
        step["text"] = extra.get("text", "")
        if extra.get("clear"):
            step["clear"] = True
    elif verb == "select":
        step["value"] = extra.get("value")
    elif verb == "press":
        step["key"] = extra.get("key")
    return step


def _emit(bound: list, start_page: str, start_id: int) -> list:
    """bound = [(element, verb, extra)]. Synthesize cross-page navigates; assign sequential step_ids."""
    steps, sid, cur_page = [], start_id, normalize_url(start_page or "")
    for element, verb, extra in bound:
        page = normalize_url(element.get("page", ""))
        if page and page != cur_page:
            steps.append(_nav_step(page, sid)); sid += 1     # cross-page navigate (real URL from the map)
            cur_page = page
        steps.append(_verb_step(element, verb, extra, sid)); sid += 1
    if steps:
        steps[0]["is_milestone"] = True
    return steps


def ground_scenario(llm_refs: list, site_map: dict, start_page: str = "", start_id: int = 1):
    """Goal head: bind ordered LLM `{ref, verb, value?}` to real elements. Returns (steps, unmatched).

    A `ref` (semantic_id) not in the map is dropped to `unmatched` — never fabricated (grounding).
    """
    idx = _index_by_id(site_map)
    bound, unmatched = [], []
    for r in llm_refs or []:
        el = idx.get(r.get("ref"))
        if not el:
            unmatched.append({"ref": r.get("ref"), "reason": "ref not in site map"})
            continue
        verb = (r.get("verb") or "click").strip().lower()
        if verb not in _VALID_VERBS:                        # out-of-spec verb -> not authorable
            unmatched.append({"ref": r.get("ref"), "reason": f"unsupported verb {verb!r}"})
            continue
        bound.append((el, verb, r))
    return _emit(bound, start_page, start_id), unmatched


def _match(draft_target: dict, flat_map: list):
    """Deterministic, CONSERVATIVE match of a draft target to ONE real element (else None)."""
    role = (draft_target.get("role") or "").strip().lower()
    name = (draft_target.get("name") or "").strip().lower()
    text = (draft_target.get("text") or "").strip().lower()
    page = normalize_url(draft_target.get("page") or "")
    pool = [e for e in flat_map if (not page or normalize_url(e.get("page", "")) == page)]

    def _unique(hits):
        return hits[0] if len(hits) == 1 else None          # >1 -> ambiguous -> unmatched (never guess)

    if role and name:
        m = _unique([e for e in pool if (e.get("role") or "").lower() == role
                     and (e.get("name") or "").strip().lower() == name])
        if m or any((e.get("role") or "").lower() == role
                    and (e.get("name") or "").strip().lower() == name for e in pool):
            return m                                         # a role+name candidate existed -> trust that tier
    if name and not role:                                    # name-only ONLY when the draft gave no role
        m = _unique([e for e in pool if (e.get("name") or "").strip().lower() == name])
        if m:
            return m
    if text:
        return _unique([e for e in pool if text in (e.get("name") or "").strip().lower()])
    return None


def reconcile(draft_steps: list, site_map: dict, start_page: str = "", start_id: int = 1):
    """Describe head: deterministically bind each draft step to a real element. Returns (steps, unmatched)."""
    flat = flatten_site_map(site_map)
    bound, unmatched = [], []
    for d in draft_steps or []:
        el = _match(d.get("hypothesized_target") or {}, flat)
        if not el:
            unmatched.append({"intent": d.get("intent"), "target": d.get("hypothesized_target"),
                              "reason": "no unique real element matched"})
            continue
        verb = (d.get("verb") or "click").strip().lower()
        if verb not in _VALID_VERBS:                        # out-of-spec verb -> not authorable
            unmatched.append({"intent": d.get("intent"), "reason": f"unsupported verb {verb!r}"})
            continue
        extra = {k: d.get(k) for k in ("value", "text", "key", "clear", "condition", "expected",
                                       "expect_ok", "secretRef", "intent") if d.get(k) is not None}
        bound.append((el, verb, extra))
    return _emit(bound, start_page, start_id), unmatched
