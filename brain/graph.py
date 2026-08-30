"""Sentinel brain — the LangGraph StateGraph (explore loop).

Nodes (9): perceive, ground, plan, act, verify, heal (STUB), checkpoint, report (+ START/END).
The graph autonomously explores a site, converges on a measurable coverage target (ADR-010),
and freezes plan.json / plan_hash. See ../docs/M1_CONTRACT.md and ../docs/STATE_MACHINE.md.

M2 change: each interactive element captures an ordered L1–L6 `alternatives` list (testid /
role+name / text), and the frozen click step records `locator` (primary) + `alternatives` so the
replay path (brain/replay.py) can self-heal a broken locator. The explore graph's `heal` node
stays a stub; real healing happens in replay (HealingEngine).

Coverage model: the "clickable" set is buttons; links drive navigation via the frontier.
Coverage = exercised buttons / seen buttons. Nodes are closures over the injected `ex`
(pw-executor client), `planner`, and `tx_write` (transcript sink).
"""
import json
import os

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from . import agui, runcontrol
from .otel import span
from .eventlog import log
from .frames import capture_frame   # ADR-108d; shared with replay since PROD-FAIL-MEDIA part A
from .state import (RunState, normalize_url, page_identity, semantic_id,
                    control_id, canonical_plan_hash)
from . import strategies as S     # ADR-083: one vocabulary, shared with the recorder








def summarise_site_map(site_map: dict) -> dict:
    """ADR-108c: what the tool FOUND, in the terms a person decides on.

    A count of elements is not a report — "412 interactives" tells nobody whether authoring a test over
    this map is a good idea. What a person weighs is: how many pages were reached, what KINDS of control
    are there, and — the part that decides it — whether anything looks destructive or looks like a login,
    because those are the two ways an autonomous run does damage or silently achieves nothing.
    """
    pages = sorted(site_map or {})
    kinds, forms, risky, auth = {}, 0, [], []
    total = 0
    for page in pages:
        for el in site_map.get(page) or []:
            total += 1
            role = (el.get("role") or el.get("tag") or "element").lower()
            kinds[role] = kinds.get(role, 0) + 1
            name = (el.get("name") or el.get("text") or "").strip()
            low = name.lower()
            if role in ("textbox", "input", "combobox", "checkbox", "radio"):
                forms += 1
            # Word lists, deliberately: this is a REPORT, not a safety mechanism. It exists so a person
            # sees what to look at, and it must never be mistaken for a guarantee that nothing else is
            # destructive — which is exactly why the decision stays with the person.
            if any(w in low for w in ("delete", "remove", "удалить", "удали", "drop", "wipe",
                                      "deactivate", "cancel subscription", "pay", "оплат", "buy", "купить")):
                risky.append({"page": page, "name": name, "role": role})
            if any(w in low for w in ("login", "sign in", "войти", "password", "пароль", "log in")):
                auth.append({"page": page, "name": name, "role": role})
    # ⚠ ЧЕТЫРЕ СРЕЗА НИЖЕ БЫЛИ МОЛЧАЛИВЫМИ, И ЭТО ТА ЖЕ БОЛЕЗНЬ, ЧТО В ПРОМПТАХ (ADR-136). Сводка —
    # то, по чему человек даёт разрешение на авторинг (ADR-108c): он читает «страницы: …» и решает.
    # На двадцать первой странице перечень молча терял хвост, и решение принималось по неполной
    # картине, выглядевшей полной. Число рядом с каждым срезом стоит НЕ вместо перечня, а рядом с
    # ним: `pages` уже говорит, сколько их всего, поэтому достаточно назвать, сколько СКРЫТО.
    kinds_sorted = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "pages": len(pages), "page_list": pages[:20], "interactives": total,
        "kinds": dict(kinds_sorted[:8]),
        "form_fields": forms,
        "looks_destructive": risky[:10], "looks_like_auth": auth[:10],
        # Скрытое ОБЪЯВЛЕНО. Ноль здесь — законный и частый ответ, и он тоже информация: он говорит
        # читателю, что перечень выше ПОЛОН, а не что его никто не считал.
        "omitted": {"pages": max(0, len(pages) - 20), "kinds": max(0, len(kinds_sorted) - 8),
                    "looks_destructive": max(0, len(risky) - 10),
                    "looks_like_auth": max(0, len(auth) - 10)},
    }


# --- ADR-108c: the map gate -----------------------------------------------------------------------
# Alex's directive: after exploring, the tool ANALYSES the map itself, reports what it found, and asks
# permission before authoring a test over it. The interception sits at the top of the `scenario` node
# because that node serves BOTH paths — the one-shot goal/describe run and the warm chat resume — so a
# gate placed anywhere else would guard one of them and quietly miss the other.

MAP_GATE_POLL_SECONDS = 1.0


def _map_gate_timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("SENTINEL_MAP_GATE_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def await_map_decision(rc, run_id: str, summary: dict) -> str:
    """Return "approve" | "reject" | "skipped".

    "skipped" is the honest answer in two situations, and neither is a failure:

      - no orchestrator is wired, so there is NOBODY who can answer. A headless run — CI, cron, the
        air-gapped bundle — has no operator, and a gate that waited for one there would not be a
        safeguard but a hang. The report is still emitted; only the waiting is skipped.
      - SENTINEL_MAP_GATE=0, an explicit opt-out for automation that has already decided.

    A TIMEOUT is a rejection, not an approval. The whole point of asking is that authoring over an
    unreviewed map is the thing worth preventing; treating silence as consent would give the gate the
    opposite meaning at exactly the moment it matters.
    """
    import time

    if os.environ.get("SENTINEL_MAP_GATE") == "0":
        log("map.gate_disabled")
        return "skipped"
    if not getattr(rc, "wired", False):
        log("map.gate_unattended", pages=summary.get("pages", 0))
        return "skipped"

    deadline = time.monotonic() + _map_gate_timeout()
    log("map.gate_waiting", pages=summary.get("pages", 0), interactives=summary.get("interactives", 0))
    errors_before = getattr(rc, "transport_errors", 0)
    while True:
        decision = rc.map_decision(run_id)
        if decision in ("approve", "reject"):
            return decision
        if time.monotonic() >= deadline:
            # "Nobody answered" and "nobody COULD answer" look identical from here and are not the same
            # problem: one waits on a person, the other on a broken channel, and only the second is
            # something the operator can fix. Both refuse — silence is not consent either way — but the
            # run must SAY which happened, and both are declared degradations so the verdict carries it.
            failed = getattr(rc, "transport_errors", 0) - errors_before
            if failed > 0:
                log("map.gate_unreachable", errors=failed)
            else:
                log("map.gate_timeout", seconds=int(_map_gate_timeout()))
            return "reject"
        time.sleep(MAP_GATE_POLL_SECONDS)


def _agui(event_type: str, run_id: str, **data) -> None:
    """Best-effort AG-UI emission (M14, ADR-055; docs/M14_CONTRACT.md §2/§4): additive stdout only,
    UNCONDITIONAL — never gates on run outcome and never touches plan_hash/exit codes/artifacts. A
    failure to emit (e.g. a non-JSON-serializable data value) is swallowed so it can never break the
    run; callers below are plain node code, not wrapped in their own try/except."""
    try:
        agui.emit(event_type, run_id, **data)
    except Exception as e:
        log("system.agui_emit_failed", error=e)


def _tool_args_summary(p: dict) -> str:
    """A short, non-secret one-line description of a pending action for `tool.call.args_summary`
    (§2). Reuses the already-unsanitized `intent`/`target` fields the codebase already logs/persists
    elsewhere (tx_write, log) — never the raw locator dict or a fill/type value (M9.1 secrets stay as
    `secretRef`, resolved only inside pw-executor; see replay.py `_act`)."""
    at = p.get("action_type", "")
    if at == "navigate":
        return f"navigate -> {p.get('target', '')}"
    return p.get("intent") or f"{at} {p.get('name') or p.get('semantic_id', '')}"


def _perception_audit(ex, path: str, elements: "list | None" = None) -> dict:
    """Ask the executor what it can and cannot see on the current page (ADR-092).

    Fail-open on an older executor: a run must not break because a MEASUREMENT is unavailable. But the
    absence is recorded rather than defaulted to a flattering number — `ratio: None` reads as "not
    measured", while a missing key would silently become "fine" in every consumer.

    ADR-097: `elements` (the page model) splits what we SEE into what we can ACT on and what we
    cannot. The executor cannot do that split — it reports per-control facts (`visible`, `disabled`),
    and deciding what they mean for planning is the brain's job. Seeing and being able to act are
    different capabilities, and the interface has to say which one a number is about: a page where
    every control is behind a closed panel is fully perceived and entirely unusable.
    """
    try:
        a = ex.call("browser.perceptionAudit") or {}
    except Exception as e:
        log("perception.audit_unavailable", error=e)
        return {"ratio": None, "reason": "executor does not support browser.perceptionAudit"}
    if elements is not None:
        usable = sum(1 for e in elements
                     if e.get("visible") is not False and not e.get("disabled"))
        # `no_role` is the gap between what the executor perceived and what reached the page model.
        # It is reported rather than folded into either side: an element with no ARIA role is not a
        # blind spot (we saw it) and not a usable control (nothing can address it), and quietly
        # adding it to either would make the breakdown stop summing to the whole.
        a["usable"] = usable
        a["blocked"] = len(elements) - usable
        a["no_role"] = max(0, (a.get("seen") or 0) - len(elements))
    if a.get("total") and a.get("ratio") is not None and a["ratio"] < 1.0:
        # Said out loud, once per page: the blind spot is a property of the page under test, and a
        # person planning around a coverage number deserves to know the denominator was incomplete.
        log("perception.partial", page=path, seen=a.get("seen"), total=a.get("total"),
            ratio=a.get("ratio"), outside=(a.get("unseen") or {}).get("outside_selector"),
            iframe=(a.get("unseen") or {}).get("iframe"))
    return a


def _elements_from_interactives(elements: list, path: str) -> list:
    """Build element descriptors (semantic_id + primary locator + L1–L6 alternatives + role + page) from
    pw-executor `browser.interactives`.

    M9.2b (ADR-028): generalized beyond buttons to **input/select/link** so a login/form/billing scenario
    can ground.

    semantic_id anchors on testid (stable across DOM drift) when present, else the accessible name. The
    primary locator is role+name (human-natural, drift-fragile); stabler testid/label/text are healing
    alternatives ordered by strategy prior (testid 0.95 > role_name 0.90 > label 0.88 > text 0.80).

    ADR-094: there is NO tag->role ladder here any more. The executor now reports the true ARIA role,
    so this function consumes it instead of re-deriving it. The ladder it replaces asked about the TAG
    first (`if tag == "button" or erole == "button"`), which inverted the ARIA rule that an explicit
    `role` attribute overrides the tag's implicit role — so every `<button role="tab">` was frozen as
    a button, and `getByRole('button', ...)` can never match a control the accessibility tree calls a
    tab. It also flattened every `<input>` to `textbox` regardless of `type`, which is wrong for
    checkbox, radio, submit and search. Deriving a role in a second place was the defect; the fix is
    to have only one place, not a better ladder.
    """
    out = []
    for e in elements:
        role = (e.get("role") or "").lower()
        if not role:
            # No ARIA role at all — a hidden input, an anchor without href. Not a control; putting it
            # in the page model would mean planning steps against something nothing can address.
            continue
        name = (e.get("name") or "").strip()
        testid = e.get("testid")
        text = (e.get("text") or "").strip()
        anchor = testid or name or text
        if not anchor:
            continue
        # ADR-095: WHERE the control lives, scoping every locator built below. Merged as a dict so a
        # control in the top frame produces a locator with NO `frame` key at all — not `None` — which
        # is what keeps existing plans byte-identical: `canonical_plan_hash` hashes every field of
        # every step, so an extra key present-but-null would move all 106 stored hashes.
        #
        # A scope, not a strategy: the alternative below is still `role_name`, still scored by the
        # same prior. Making it a seventh strategy would have introduced a prior nobody measured,
        # next to six already admitted to be unmeasured (GAP-RISK-002).
        fr = {"frame": e["frame"]} if e.get("frame") else {}
        alts = []
        if testid:
            alts.append({"strategy": S.TESTID, "locator": {"testid": testid, **fr},
                         "prior": S.PRIORS[S.TESTID]})
        if name:
            alts.append({"strategy": S.ROLE_NAME, "locator": {"role": role, "name": name, **fr},
                         "prior": S.PRIORS[S.ROLE_NAME]})
        if role != "button" and e.get("label"):    # buttons stay byte-identical to the old cataloguer (plan_hash)
            alts.append({"strategy": S.LABEL, "locator": {"label": e["label"], **fr},
                         "prior": S.PRIORS[S.LABEL]})
        if text and text != name:
            alts.append({"strategy": S.TEXT_ROLE, "locator": {"text": text, **fr},
                         "prior": S.PRIORS[S.TEXT_ROLE]})
        primary = {"role": role, "name": name, **fr} if name else (alts[0]["locator"] if alts else None)
        # M9-LIVE: `disabled` rides along so plan() can skip a control that cannot be actuated right
        # now. It is NOT used to drop the element from perception: the same button is usually enabled
        # later in a filled form, and a page model that forgets it would report coverage over a
        # smaller page than the one under test. plan_hash is unaffected — it hashes the STEPS, and no
        # step carries this field (state.py canonical_plan_hash).
        # ADR-093: `visible` is carried as THREE states, not coerced to a bool like `disabled` above.
        # `None` means an older executor never said — and it must not read as "invisible", or every
        # element from that executor would be dropped from the candidate set and explore would find
        # nothing at all. Absent evidence is not evidence, so the reader tests `is False`.
        # ADR-095: the frame is part of the control's IDENTITY, not just of its address. Two payment
        # frames each holding a "Pay" button are two controls, and without this they would collide on
        # one semantic_id — coverage would count one where there are two, and the second would look
        # already-exercised. Appended to the anchor rather than added as a fourth component so a
        # top-frame control hashes to exactly what it always did (`fr` is empty there).
        sid_anchor = f"{e['frame']}|{anchor}" if e.get("frame") else anchor
        out.append({"semantic_id": semantic_id(path, role, sid_anchor),
                    # ADR-137: вторая ось — «этот контрол», без маршрута. Считается от того же
                    # якоря, что и `semantic_id` (testid, если он есть, иначе доступное имя), и с
                    # тем же префиксом фрейма: контрол во фрейме и одноимённый в верхнем документе —
                    # РАЗНЫЕ контролы, и склеивать их значило бы объявить проработанным тот, до
                    # которого не дотрагивались.
                    "control_id": control_id(role, sid_anchor), "role": role, "name": name,
                    "testid": testid, "locator": primary, "alternatives": alts, "page": path,
                    "disabled": bool(e.get("disabled")), "visible": e.get("visible"), **fr})
    return out


def _user_turns(messages: list) -> list:
    """M9.10 (ADR-048): pull user-turn text out of the messages channel for refine context. Entries are
    BaseMessage objects (after add_messages coercion) or plain dicts — duck-typed on `.type`/`.content`
    so the brain needs no langchain_core import. BaseMessage `.type` is 'human'; a dict uses 'user'."""
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            role, content = m.get("role"), m.get("content")
        else:
            role, content = getattr(m, "type", None), getattr(m, "content", None)
        if role in ("human", "user") and content:
            out.append(content)
    return out


_REFINE_HISTORY_KEEP = int(os.environ.get("SENTINEL_REFINE_HISTORY_KEEP", "6"))

# How many times explore may fail on the SAME element before dropping it from the candidate set.
# Two, not one: the first failure is worth a retry (an overlay mid-animation, a control that enables a
# beat later), the second says the page is not going to change its mind. Nothing recovers in between —
# the explore graph's heal node is a stub by design (healing belongs to replay) — so a third attempt
# can only produce the same exception. Env-tunable because a slow real app may legitimately want 3.
_EXPLORE_FAIL_LIMIT = max(1, int(os.environ.get("SENTINEL_EXPLORE_FAIL_LIMIT", "2")))

# Roles explore proposes clicking, and therefore the roles coverage is measured over (ADR-094).
#
# It is `{button, tab}` rather than `{button}` to keep the SET OF ELEMENTS exactly what it was, now
# that they carry correct labels. Before this ADR a `<button role="tab">` was mis-typed as a button
# and so it was already a candidate — an unreachable one, since the locator named a role the page
# does not have. Dropping to `{button}` alone would have quietly REMOVED four controls per tabbed
# page from both the candidate set and the coverage denominator, and a coverage number that improves
# because the page got smaller is the flattering-number failure this codebase keeps finding.
#
# Deliberately NOT widened further. `checkbox`, `radio` and `textbox` are clickable too, but explore
# has never proposed them and adding them here would be a behaviour change wearing a bug fix's
# clothes: coverage denominators would grow on every form page for reasons unrelated to roles.
_CLICK_ROLES = ("button", "tab")


def _rolling_summary(user_turns: list) -> str:
    """M9.10/GAP-M9-20: a bounded one-line summary of a conversation (turn count + the opening request).
    Feeds the chats projection (brain/store.py) and the 'earlier context' prefix when older turns are
    capped out of the refine prompt — so neither the prompt nor the stored summary grows with length."""
    turns = user_turns or []
    if not turns:
        return ""
    return f"{len(turns)} turn(s); started: {(turns[0] or '')[:80]!r}"


def _capped_history(user_turns: list, keep: int = _REFINE_HISTORY_KEEP) -> list:
    """GAP-M9-20: cap refine history to the last `keep` user-turns; older turns collapse into a single
    summary line so the refine prompt (and its token cost) stays bounded as a conversation grows. A
    short conversation (<= keep turns) is returned unchanged — byte-identical to the pre-cap behavior."""
    turns = list(user_turns or [])
    if len(turns) <= keep:
        return turns
    older, recent = turns[:-keep], turns[-keep:]
    return [f"[earlier: {_rolling_summary(older)}]"] + recent


# ⚠ ОДНИ ВОРОТА ДЛЯ ВСЕХ ИСТОЧНИКОВ ФРОНТИРА, И ЭТО НЕ НАВЕДЕНИЕ ПОРЯДКА.
#
# До ADR-134 обе проверки — граница обхода (ADR-130) и воля владельца сайта (ADR-133) — стояли
# ФИЗИЧЕСКИ ВНУТРИ цикла `for l in links` узла `ground`. Пока источник фронтира был один (`a[href]`),
# разницы не было. Как только источников становится два, второй обходит обе проверки целиком — и
# `tests/test_robots_offline.py` этого НЕ ЗАМЕТИТ: он кормит граф исключительно якорями, поэтому
# останется зелёным над самой дырой. Класс тот же, что дефект границы, замеренный 2026-08-22:
# инструмент уйдёт туда, куда владелец сайта запретил, а блок `robots.excluded` в `plan.json` будет
# утверждать, что запрет соблюдён.
#
# Поэтому решение принимается ЗДЕСЬ и только здесь, а вызывающий обязан произнести его вслух: ответ
# — не булев, а НАЗВАННЫЙ, потому что «не пустили» бывает трёх разных сортов, и два из них человек
# обязан увидеть в артефакте по-разному.
ADMIT = "admit"            # адрес наш, не посещён, не запрещён — во фронтир
ADMIT_OUTSIDE = "outside"  # за границей обхода: чужой хост или чужой раздел
ADMIT_KNOWN = "known"      # уже посещён, уже во фронтире или это текущая страница
ADMIT_ROBOTS = "robots"    # владелец сайта попросил сюда не ходить


def admit_to_frontier(nu, *, origin, robots, visited, frontier, current) -> str:
    """Пускать ли адрес во фронтир. ЕДИНСТВЕННОЕ место, где это решается.

    ⚠ ПОРЯДОК ПРОВЕРОК ЗНАЧИМ. Граница идёт ПЕРВОЙ: чужой адрес не должен попасть в перечень
    исключённых по `robots`, иначе перечень начнёт утверждать, что владелец СОСЕДНЕГО сайта нам
    что-то запретил — а он о нас ничего не говорил.

    ⚠ `robots` СУДИТ ПО ПУТИ ДОКУМЕНТА, а не по маршруту, и теперь это сказано, а не выходит
    случайно. `robots.txt` говорит о ресурсах, которые отдаёт СЕРВЕР; hash-маршрут (`#/orders`) на
    сервер не уходит вовсе, и правила о нём не бывает. `RobotsPolicy.allows` берёт `urlsplit(...).path`
    — то есть путь документа, — и для двух маршрутов одной страницы ответ по построению один.
    """
    if not nu:
        return ADMIT_KNOWN
    if not nu.startswith(origin or ""):
        return ADMIT_OUTSIDE
    if nu == current or nu in visited or nu in frontier:
        return ADMIT_KNOWN
    if robots is not None and not robots.allows(nu):
        return ADMIT_ROBOTS
    return ADMIT


def build_graph(ex, planner, tx_write, scenario_head=None, rc=None, robots=None):
    """Build and return an uncompiled StateGraph. Caller compiles it with a checkpointer.

    M9.2b (ADR-028): when `scenario_head` (a GoalPlanner or DescribePlanner) is wired, a `scenario` node
    runs once after the explore converges — it authors a grounded scenario over the site map (as much
    of it as the prompt budget carries; ADR-136 spreads that budget across pages and names the rest).
    Pure explore (scenario_head=None) routes straight through scenario as a no-op to report.

    M9.8 F4 (ADR-054): `rc` is the RunControl client (orchestrator link). Defaults to make_client()
    (no-op unless ORCH_ADDR is set) — injectable so the offline takeover test drives interrupt/resume
    with a fake orchestrator. Production behaviour is unchanged when rc is left None."""
    rc = rc if rc is not None else runcontrol.make_client()  # M8: token deltas to the Go orchestrator (no-op if ORCH_ADDR unset)

    def perceive(state: RunState) -> dict:
        """Snapshot the current page (URL + accessibility tree). No LLM."""
        rid = state.get("run_id", "")
        if not state.get("page_model"):
            # M14 (ADR-055): the explore loop re-enters perceive once per step (via checkpoint), but
            # page_model is only ever empty on the very first pass (both real __main__.py init states
            # and the offline harnesses seed current_step=1 for a synthetic pre-recorded nav step, so
            # current_step can't be used as the cold-start signal here — page_model can).
            _agui("run.started", rid, mode=state.get("run_mode", ""), target=state.get("target_url", ""),
                  planner=planner.name)
        _agui("state.transition", rid, to="perceive")
        cur = ex.call("browser.currentUrl")
        snap = ex.call("browser.snapshot")
        # ⚠ НАЗВАННАЯ ПРИЧИНА ДОЛЖНА БЫТЬ ПРОИЗНЕСЕНА, А НЕ ПРОСТО ВОЗВРАЩЕНА. Инструмент теперь
        # отличает «у документа нет корня» от «корень есть, а снимок не удался» и присылает текст
        # причины — но до сих пор его читали только тесты: отсюда наружу уходили ровно
        # `ariaSnapshot` и `nodeCount`. Человек видел `nodeCount: 0` и не имел ни одного способа
        # узнать, пустая это страница или непрочитанная. Код несёт `degrades: true`, поэтому причина
        # доезжает и до вердикта, и до блока `degradations` в `plan.json`.
        reason = snap.get("rootless") or snap.get("snapshotError")
        if reason:
            log("browser.snapshot_degraded", reason=reason)
        return {"current_url": cur.get("url", ""),
                "page_model": {"url": cur.get("url", ""), "title": cur.get("title", ""),
                               "aria": snap.get("ariaSnapshot", ""),
                               "nodeCount": snap.get("nodeCount", 0),
                               "degraded": reason}}

    def ground(state: RunState) -> dict:
        """Catalogue buttons (with healing alternatives), grow the frontier, recompute coverage."""
        pm = dict(state.get("page_model") or {})
        # ADR-132: идентичность страницы, а не адреса — маршрут SPA живёт во фрагменте.
        path = page_identity(pm.get("url", ""))
        elements = _elements_from_interactives(ex.call("browser.interactives").get("elements", []), path)
        buttons = [e for e in elements if e["role"] in _CLICK_ROLES]  # coverage/candidates
        # ADR-092: measure how much of THIS page perception can even see, per page, before deciding
        # anything about coverage. Coverage answers "how much of what we saw did we exercise"; this
        # answers "how much was there to see" — and a coverage of 1.00 over a page we half-perceive is
        # exactly the reassuring number the register promised to guard against and never did.
        perception = dict(state.get("perception") or {})
        # Once per PAGE, not once per step: `ground` runs on every step of the walk, and a live run
        # against l5.html printed the same "partly visible" line five times. A finding repeated until
        # it becomes wallpaper is a finding nobody reads — the repeat-collapsing the log view does for
        # the application's own output (ADR-070) exists for exactly this reason, and our own
        # diagnostics should not need it.
        if path not in perception:
            # ADR-097: the page model goes in so the measurement can separate what we SEE from what
            # we can ACT on. Both come from the same moment on the same page — computing them apart
            # is how the audit and perception came to describe different pages in the first place.
            perception[path] = _perception_audit(ex, path, elements)
        # ADR-137: знаменатель покрытия — ось КОНТРОЛА, а не вхождения. До этой правки один и тот
        # же контрол рельса считался столько раз, сколько маршрутов его показывали (замерено: 137
        # вместо 49 на `site-spa`), и точно так же раздувался числитель — поэтому само ЧИСЛО
        # покрытия почти не менялось (0.2701 против 0.2653), а вот перечень непроработанных
        # раздувался втрое и уводил туда бюджет.
        seen = list(dict.fromkeys(list(state.get("interactive_seen", []))
                                  + [b["control_id"] for b in buttons]))
        # M9.2b (ADR-028): accumulate the site-wide element map (superset of buttons) for the scenario head.
        site_map = dict(state.get("site_map") or {})
        have = {el["semantic_id"] for el in site_map.get(path, [])}
        site_map[path] = list(site_map.get(path, [])) + [el for el in elements if el["semantic_id"] not in have]
        links = ex.call("browser.links").get("links", [])
        origin = state.get("base_origin", "")
        visited = set(state.get("visited_paths", []))
        frontier = list(state.get("nav_frontier", []))
        # Якоря — ПЕРВЫЙ источник фронтира, и он ходит через те же ворота, что и всякий следующий.
        excluded = list(state.get("robots_excluded", []) or [])

        def _admit_all(candidates, source):
            """Пропустить перечень адресов через ворота. ЕДИНСТВЕННЫЙ путь во фронтир — оба источника.

            Вынесено в функцию не ради краткости: пока проводка была написана в теле цикла по
            якорям, второй источник физически НЕ МОГ пройти теми же воротами — его пришлось бы
            написать заново, и написанное заново разошлось бы (ADR-134 предсказал это дословно).
            Теперь добавить источник — значит вызвать это, и другого способа положить адрес во
            фронтир в узле нет."""
            admitted = 0
            for nu in candidates:
                verdict = admit_to_frontier(nu, origin=origin, robots=robots, visited=visited,
                                            frontier=frontier, current=path)
                if verdict == ADMIT:
                    frontier.append(nu)
                    admitted += 1
                elif verdict == ADMIT_ROBOTS and nu not in excluded:
                    excluded.append(nu)
                    log("plan.route_refused_by_robots", url=nu, source=source)
            return admitted

        # Якоря — ПЕРВЫЙ источник фронтира, и он ходит через те же ворота, что и всякий следующий.
        _admit_all((page_identity(l.get("href", "")) for l in links), "links")

        # ADR-135: ВТОРОЙ источник — журнал смен маршрута, который вела сама страница. Отвечает на
        # вопрос, которого якоря не слышат: какие адреса приложение у себя уже открывало. Маршрут,
        # открытый `pushState` и покинутый раньше, чем адрес прочитали снаружи (редирект роутера,
        # `replaceState`), не виден НИ снимку `browser.currentUrl`, НИ якорям — он виден только тут.
        #
        # ⚠ ВЫЗОВ ЗАЩИЩЁН, В ОТЛИЧИЕ ОТ СОСЕДНЕГО. `browser.links` зовётся голым, и это безопасно
        # ровно потому, что верб старый. Незнакомый метод исполнитель ОТКЛОНЯЕТ броском, а этот узел
        # ловли не имеет — прогон против исполнителя прежней сборки падал бы целиком, вместо того
        # чтобы просто не увидеть маршрутов. Та же забота, что ADR-134 проявил к отсутствующему полю
        # `navigated`: новый контракт не имеет права ломать старую пару.
        try:
            _taken = ex.call("browser.routes") or {}
        except Exception as e:
            _taken = {}
            log("browser.route_journal_unavailable", error=str(e)[:200])
        _records = _taken.get("routes") or []
        if _records or _taken.get("dropped"):
            _admitted = _admit_all((page_identity(r.get("url", "")) for r in _records), "routes")
            # Событие произносится ПОСЛЕ работы ворот и несёт ОБА числа. Одно «журнал отдал N»
            # читалось бы как находка, хотя N записей об уже посещённых адресах — это ноль находок.
            log("browser.routes_observed", seen=len(_records), admitted=_admitted,
                dropped=int(_taken.get("dropped") or 0))
        visited_paths = list(dict.fromkeys(list(state.get("visited_paths", [])) + [path]))
        frontier = [f for f in frontier if f != path]
        exercised = set(state.get("interactive_exercised", []))
        total = len(seen)
        done_n = len([s for s in seen if s in exercised])
        coverage = (done_n / total) if total else 0.0
        pm["buttons"] = buttons
        return {"interactive_seen": seen, "nav_frontier": frontier, "visited_paths": visited_paths,
                "coverage_achieved": coverage, "page_model": pm, "site_map": site_map,
                "perception": perception, "robots_excluded": excluded}

    def plan(state: RunState) -> dict:
        """Assemble candidates, enforce convergence, ask the planner for the next action."""
        pm = state.get("page_model") or {}
        exercised = set(state.get("interactive_exercised", []))
        # An element that has raised `_EXPLORE_FAIL_LIMIT` times is out of the candidate set. Retrying
        # is right the first time — a transient overlay, an animation still settling — and pointless
        # after that: the explore graph's heal node is a stub, so nothing between attempts has changed.
        failed = state.get("interactive_failed", {}) or {}
        spent = {sid for sid, n in failed.items() if n >= _EXPLORE_FAIL_LIMIT}
        candidates = []
        blocked = 0
        for b in pm.get("buttons", []):
            # ADR-137: проработанность спрашивается у оси КОНТРОЛА. Прежняя проверка по
            # `semantic_id` означала «этот контрол на ЭТОМ экране», поэтому переход на новый маршрут
            # воскрешал весь рельс целиком, и `clicks[0]` уходил в него снова. Замерено: со ВТОРОГО
            # шага прогона и до конца — 66 % бюджета.
            if b["control_id"] in exercised:
                continue
            # Three distinct reasons not to propose it, counted together because the tester's question
            # is the same either way: "why does coverage say 60% when I can see ten buttons?"
            #
            # ADR-093 adds `visible`. A control in a `display:none` panel cannot be actuated now, and
            # proposing it spends the ADR-070 attempt budget on something that was never going to
            # work — twice, then blacklists it, and the blacklist is permanent for the run even
            # though the panel may open two steps later. Measured on `l5.html`: 7 of the 23 perceived
            # controls sit in closed tab panels. Like `disabled`, it does NOT remove the element from
            # `interactive_seen`: the page has that control, and a denominator that forgets it would
            # report coverage over a smaller page than the one under test.
            if b["control_id"] in spent or b.get("disabled") or b.get("visible") is False:
                blocked += 1
                continue
            # ADR-094: the element's OWN role, not the literal "button". This was a third place where
            # a role was decided rather than read — and it wrote the wrong one into the human-facing
            # intent too, so a live run printed "click button 'Overview'" for a control the page calls
            # a tab. The locator was already right; the label beside it disagreed, which is exactly
            # how a wrong role stays invisible to a reader.
            candidates.append({"kind": "click", "semantic_id": b["semantic_id"],
                               "control_id": b["control_id"],
                               "role": b["role"], "name": b["name"], "target": None,
                               "intent": f"click {b['role']} '{b['name']}'",
                               "locator": b["locator"], "alternatives": b["alternatives"]})
        for nu in state.get("nav_frontier", []):
            nav_sid = semantic_id(nu, "navigate", "")
            if nav_sid in spent:      # a link that cannot be followed loops exactly the same way
                continue
            candidates.append({"kind": "navigate", "semantic_id": nav_sid,
                               "role": None, "name": None, "target": nu, "alternatives": None,
                               "locator": None, "intent": f"navigate to {nu}"})
        step = state.get("current_step", 0)
        frontier_empty = len(state.get("nav_frontier", [])) == 0
        cov_ok = state.get("coverage_achieved", 0.0) >= state.get("coverage_target", 0.85)
        if step >= state.get("max_steps", 40) or not candidates or (cov_ok and frontier_empty):
            reason = ("max_steps" if step >= state.get("max_steps", 40)
                      else "converged" if (cov_ok and frontier_empty) else "no_candidates")
            # Said once, at the only moment the question arises: the run is stopping short of its
            # coverage target and the reader needs to know it was the page, not the planner giving up.
            # Ending on `no_candidates` with nothing blocked is a different situation (everything was
            # exercised) and stays quiet.
            if reason == "no_candidates" and blocked:
                log("plan.unactionable_elements", blocked=blocked,
                    coverage=round(state.get("coverage_achieved", 0.0) * 100))
            tx_write({"step": step, "planner": planner.name, "model": planner.model,
                      "decision": "done", "reason": reason,
                      "prompt_tokens": None, "completion_tokens": None})
            # Причина уезжает В СОСТОЯНИЕ, а не только в транскрипт. Транскрипт не входит в перечень
            # отдаваемых артефактов, поэтому до сих пор единственный ответ на вопрос «почему обход
            # кончился» не доезжал до человека ни по какому каналу.
            return {"exploration_complete": True, "stop_reason": reason}
        decision = planner.propose(dict(state), candidates)
        if decision.get("done") or not decision.get("action"):
            tx_write({"step": step, "planner": planner.name, "model": planner.model,
                      "decision": "done", "reason": decision.get("reason", ""),
                      "prompt_tokens": None, "completion_tokens": None})
            # Планировщик сказал «хватит» — это ТРЕТЬЯ причина, и она не совпадает ни с покрытием, ни
            # с потолком: модель могла решить так на втором шаге. Своё имя, чтобы читатель не принял
            # её за сходимость.
            return {"exploration_complete": True, "stop_reason": "planner_done"}
        a = decision["action"]
        sid = step + 1
        planned = {"step_id": sid, "intent": a["intent"], "semantic_id": a["semantic_id"],
                   "action_type": a["kind"], "target": a.get("target"),
                   "locator": (a.get("locator") if a["kind"] == "click" else None),
                   "alternatives": (a.get("alternatives") if a["kind"] == "click" else None),
                   "is_milestone": False}
        tok = decision.get("tokens") or {}
        tx_write({"step": sid, "planner": planner.name, "model": planner.model,
                  "decision": a["intent"], "reason": decision.get("reason", ""),
                  "prompt_tokens": tok.get("prompt"), "completion_tokens": tok.get("completion")})
        if rc.report(state.get("run_id", ""), "plan", tok.get("prompt"),
                     tok.get("completion")) == runcontrol.ABORT:
            log("plan.orchestrator_abort")
            # ⚠ ПРИЧИНА ЕСТЬ, И ОНА ОБЯЗАНА БЫТЬ НАЗВАНА. Эта ветка возвращала только защёлку, а
            # `report` не имеет способа отличить её ни от чего другого и выводил `unknown` — то есть
            # прогон, оборванный ЧУЖИМ РЕШЕНИЕМ по бюджету, объявлялся неполным по неизвестной
            # причине. Человек, у которого сработал потолок расходов, читал «непонятно почему».
            return {"exploration_complete": True, "stop_reason": "orchestrator_abort"}
        return {"exploration_plan": list(state.get("exploration_plan", [])) + [planned],
                "_pending": planned}

    def act(state: RunState) -> dict:
        """Execute the pending action via pw-executor; mark the element exercised."""
        p = state.get("_pending")
        if not p:
            return {"_last_ok": False}
        rid = state.get("run_id", "")
        _agui("tool.call", rid, name=p.get("action_type", ""), args_summary=_tool_args_summary(p))
        _agui("step.progress", rid, n=p.get("step_id", 0), total=state.get("max_steps", 40),
              desc=p.get("intent", ""))
        moved = ""      # адрес после действия; заполняется только кликом, см. ниже
        try:
            at = p["action_type"]
            if at == "navigate":
                from .replay import note_load_speed
                note_load_speed(ex.call("browser.navigate", url=p["target"]), p["target"])
            elif at == "click":
                # ⚠ ОТВЕТ КЛИКА НЕСЁТ АДРЕС, И ОН ВЫБРАСЫВАЛСЯ. `browser.click` возвращает `url`
                # (pw-executor), а здесь результат не читался вовсе. На SPA это и есть навигация:
                # приложение меняет маршрут, ничего не перезагружая, — и до следующего `perceive`
                # состояние утверждало, что обход стоит там же, где стоял. Цена видна на последнем
                # шаге: прогон, упёршийся в потолок на клике, терял найденный им маршрут целиком,
                # потому что `ground` до него уже не доходил.
                _clicked = ex.call("browser.click", locator=p["locator"]) or {}
                # ⚠ ФАКТ БЕРЁТСЯ У ИСПОЛНИТЕЛЯ, А НЕ ВЫВОДИТСЯ ЗДЕСЬ (ADR-134). Сравнивая адреса
                # сам, вызывающий не отличал «не двигались» от «не успели увидеть»: обе ситуации
                # выглядели как равные строки, и отложенный переход роутера терялся молча. Теперь
                # `navigated` произносит тот, кто единственный может его знать, — тот, кто ждал.
                # `.get(..., None)` и запасной путь: исполнитель прежней версии поля не пришлёт, и
                # прогон против него обязан остаться рабочим, а не тихо считать всё навигацией.
                _nav = _clicked.get("navigated")
                moved = page_identity(_clicked.get("url") or "")
                if _nav is False:
                    moved = ""
            elif at in ("fill", "type", "select"):
                # M9.1 forward-compat: the explorer emits only click/navigate today; frozen/authored
                # plans run through act reuse replay's verb dispatch (single source of truth).
                from .replay import _act
                _act(ex, at, p.get("locator") or {}, p)
            elif at == "press":
                if p.get("locator"):
                    ex.call("browser.press", locator=p["locator"], key=p.get("key"))
                else:
                    ex.call("browser.press", key=p.get("key"))
            elif at == "assert":
                from .replay import _expect_params
                ex.call("browser.expect", **_expect_params(p))
            else:
                ex.call("browser.click", locator=p["locator"])
        except Exception as e:
            # M9-LIVE: record the failure AGAINST THE ELEMENT, not just the step. Success marks an
            # element exercised and so removes it from the candidate set; failure used to mark
            # nothing, which meant a control that can never be actuated (a disabled button) stayed a
            # candidate and was proposed again on the very next step. Live logs showed the same click
            # 34 times, ~5s apart, until max_steps — the run burned its whole budget on one element
            # and reported it as exploration.
            # ADR-137: ОТКАЗ СЧИТАЕТСЯ ПО ОСИ КОНТРОЛА — по той же, что и проработанность.
            #
            # ⚠ ЭТО ВТОРАЯ ПОЛОВИНА ТОЙ ЖЕ БОЛЕЗНИ, и найдена она тестом, а не задумана. Пока отказ
            # ключевался вхождением, чёрный список действовал ТОЛЬКО на том маршруте, где контрол
            # сломался: на следующем это уже другой `semantic_id`, снова кандидат, снова
            # `_EXPLORE_FAIL_LIMIT` попыток. Для одного глобального контрола на двенадцати маршрутах
            # это 24 шага впустую — ровно та осцилляция, ради устранения которой заведена ось.
            # Два вопроса — «проработан ли» и «стоит ли предлагать снова» — обязаны спрашиваться у
            # одной оси, иначе они расходятся.
            #
            # ⚠ ЧЕМ ЗА ЭТО ПЛАТИМ, НАЗВАНО: контрол, закрытый оверлеем на ОДНОМ экране и рабочий на
            # другом, попадёт в чёрный список глобально. Цена ограничена тем, что до списка надо
            # набрать ВЕСЬ лимит отказов: контрол, сработавший хоть где-то, к этому моменту уже
            # проработан и из кандидатов ушёл. Потеря — один контрол; выигрыш — лимит × число
            # маршрутов шагов.
            _btns_f = (state.get("page_model") or {}).get("buttons") or []
            sid_el = next((b.get("control_id") for b in _btns_f
                           if b.get("semantic_id") == p.get("semantic_id")), None) \
                     or p.get("semantic_id") or ""
            failed = dict(state.get("interactive_failed", {}))
            if sid_el:
                failed[sid_el] = failed.get(sid_el, 0) + 1
                if failed[sid_el] == _EXPLORE_FAIL_LIMIT:
                    # Logged exactly on the crossing, so the line means "this is where we gave up on
                    # it" rather than repeating once per attempt — which would recreate the noise the
                    # blacklist exists to remove.
                    log("plan.element_blacklisted", element=sid_el,
                        intent=p.get("intent", ""), attempts=failed[sid_el])
            frame = capture_frame(ex, state.get("artifact_dir"), p.get("step_id"))
            if frame:
                # A frame of the FAILURE is the one most worth having: it shows the page as it was when
                # the step could not be performed.
                _agui("step.frame", rid, n=p.get("step_id", 0), frame=frame, ok=False)
            return {"errors": list(state.get("errors", [])) + [f"act#{p['step_id']}: {e}"],
                    "interactive_failed": failed,
                    "_last_ok": False, "current_step": p["step_id"]}
        exercised = list(state.get("interactive_exercised", []))
        moved_to = {}
        if p["action_type"] == "click":
            # ADR-137: в проработанные ложится ось КОНТРОЛА, и берётся она из МОДЕЛИ СТРАНИЦЫ,
            # а не из шага.
            #
            # ⚠ ПОЛОЖИТЬ `control_id` В САМ ШАГ БЫЛО НЕЛЬЗЯ, и это не стилистика:
            # `canonical_plan_hash` хеширует ВСЕ поля всех шагов (`brain/state.py`), поэтому лишнее
            # поле сдвинуло бы хеш КАЖДОГО замороженного плана — включая голдены `testdata/site` и
            # `site-v2`, у которых эта правка не меняет ни одного шага. Шаг остаётся байт-в-байт
            # прежним; вторая ось живёт рядом, в модели страницы, где её и вычислили.
            _btns = (state.get("page_model") or {}).get("buttons") or []
            _cid = next((b.get("control_id") for b in _btns
                         if b.get("semantic_id") == p["semantic_id"]), None)
            exercised = list(dict.fromkeys(exercised + [_cid or p["semantic_id"]]))
            # Клик, сменивший адрес, ПРИЗНАЁТСЯ навигацией: это дешевле и честнее, чем угадывать
            # маршруты из разметки. В состояние идёт только то, что действительно известно, —
            # текущий адрес; `visited_paths` и фронтир по-прежнему ведёт `ground`, у которого есть
            # снимок страницы, а не один URL.
            if moved and moved != page_identity(state.get("current_url", "")):
                moved_to = {"current_url": moved}
        execs = list(state.get("executed_actions", [])) + [
            {"step_id": p["step_id"], "type": p["action_type"], "ok": True}]
        frame = capture_frame(ex, state.get("artifact_dir"), p.get("step_id"))
        if frame:
            # The frame belongs to the step it shows, so it rides the step's own event rather than a
            # separate stream the UI would have to correlate by timestamp.
            _agui("step.frame", rid, n=p.get("step_id", 0), frame=frame, ok=True)
        return {"interactive_exercised": exercised, "executed_actions": execs,
                "current_step": p["step_id"], "_last_ok": True, **moved_to}

    def verify(state: RunState) -> dict:
        """Explore-mode verify: trust act's result. Replay-mode healing lives in brain/replay.py.

        M14 (ADR-055): a successful verify is the auto-HITL failure-streak's ONLY reset point (the
        heal node below is a stub that can never itself succeed in explore mode)."""
        ok = bool(state.get("_last_ok", True))
        _agui("state.transition", state.get("run_id", ""), to=("checkpoint" if ok else "heal"))
        out = {"_verify_ok": ok}
        if ok:
            out["consecutive_heal_failures"] = 0
        else:
            out["failed_steps"] = state.get("failed_steps", 0) + 1
        return out

    def heal(state: RunState) -> dict:
        """STUB in the explore graph (explore discovers, it does not heal). See brain/replay.py.

        M14 (ADR-055): still the auto-HITL failure signal — every entry means the prior act+verify
        failed and this stub cannot recover it, so it always counts as a miss. The reset lives in
        verify() above, on the next successful action."""
        log("heal.explore_stub")
        rid = state.get("run_id", "")
        n = state.get("consecutive_heal_failures", 0) + 1
        _agui("heal", rid, step=state.get("current_step", 0), strategy="stub", ok=False)
        return {"consecutive_heal_failures": n}

    def checkpoint(state: RunState) -> dict:
        """LangGraph persists at each superstep boundary.

        M9.8 F4 (ADR-054): operator-takeover gate. A 0-token poll to the orchestrator; if a takeover is
        pending, ARM it (a state latch) — the actual pause runs in the dedicated `takeover` node next
        superstep. The decision is latched into STATE (not re-derived from the volatile poll) so the
        interrupting node's interrupt() is reached identically on the resume re-run.

        abort > takeover: if the orchestrator ABORTS (budget breach / external Abort) while or after a
        takeover, converge immediately instead of resuming the walk. This node is re-entered on resume
        (bypassing plan()'s own abort check), so it must honour abort here too. No-op / no arm when no
        orchestrator is wired (poll() -> "continue"), so the standalone/offline path is byte-identical.

        M14 (ADR-055): full auto-escalate-to-HITL. Past SENTINEL_AUTO_HITL_THRESHOLD consecutive heal
        failures, arm the SAME `_takeover_armed` latch an operator takeover would — route_checkpoint
        already routes an armed latch to the `takeover` node, so no new pause machine is needed.
        Default threshold is 0 (env unset/0 = OFF): the check below never fires, so
        `_takeover_armed`/plan_hash/exit-code behavior is byte-identical to pre-M14."""
        rid = state.get("run_id", "")
        verb = rc.poll(rid, "checkpoint")
        if verb == runcontrol.ABORT:
            log("hitl.abort_over_takeover")
            # То же и здесь, и по той же причине: остановил ЧЕЛОВЕК, и это самая знаемая из всех
            # причин остановки — молчать о ней в отчёте нечем.
            return {"exploration_complete": True, "_takeover_armed": False,
                    "stop_reason": "operator_abort"}
        if verb == runcontrol.TAKEOVER:
            log("hitl.takeover_arming")
            return {"_takeover_armed": True}
        threshold = int(os.environ.get("SENTINEL_AUTO_HITL_THRESHOLD", "0"))
        n = state.get("consecutive_heal_failures", 0)
        if threshold > 0 and n >= threshold:
            log("hitl.auto_threshold_reached", n=n, threshold=threshold)
            _agui("hitl_needed", rid, reason="consecutive_heal_failures", count=n)
            return {"_takeover_armed": True}
        return {}

    def takeover(state: RunState) -> dict:
        """M9.8 F4 (ADR-054): paused for an operator takeover. interrupt() yields the live browser to the
        human (CDP, M9-LIVE) and persists the partial run; app.invoke() returns with `__interrupt__`. On
        the orchestrator's Return the brain resumes this thread (Command(resume=...)), re-enters here where
        interrupt() now RETURNS the resume payload, clears the arm, and records the return. The interrupt()
        is UNCONDITIONAL — the decision was latched by checkpoint, so this node re-runs cleanly on resume.
        Edge back to checkpoint re-polls (handles a not-yet-propagated Return) before the run continues."""
        payload = interrupt({"reason": "operator_takeover", "run_id": state.get("run_id", "")})
        log("hitl.takeover_resumed", payload=repr(payload))
        return {"_takeover_armed": False,
                "takeover_returns": list(state.get("takeover_returns", [])) + [payload]}

    def scenario(state: RunState) -> dict:
        """M9.2b (ADR-028): phase-2 head — author a grounded scenario over the site map.

        ⚠ НЕ «COMPLETE», И ЭТО ПОПРАВКА ПО ЗАМЕРУ (ADR-136). Слово стояло тут и в двух соседних
        докстрингах с появления среза `[:8000]`, то есть было неверно всё время его существования:
        на `testdata/site-spa` до модели доезжали 55 элементов из 184, а восемь страниц из
        двенадцати не были представлены ни одним. Теперь бюджет раскладывается по всем страницам, а
        остаток произносится в самом промпте и в журнале — но «весь» он от этого не стал.
        No-op unless `scenario_head` is wired (goal/describe mode). Appends grounded steps to the plan;
        records `scenario_unmatched` (refs/draft steps that couldn't bind to a real element).

        M9.10 (ADR-048): also the RESUME entrypoint for multi-turn chat (conditional edge from START on a
        warm thread). It re-authors over the PERSISTED site_map using the prior conversation turns as
        refine context, then records an assistant summary so the next turn inherits the thread. `prior`
        is empty for one-shot goal/describe (no messages) ⇒ that path stays byte-identical."""
        if scenario_head is None:
            return {}
        from .scenario import flatten_site_map, ground_scenario, reconcile
        site_map = state.get("site_map") or {}
        # ADR-108c: report what was found, then ask — before a single step is authored over it.
        rid = state.get("run_id", "")
        summary = summarise_site_map(site_map)
        _agui("map.ready", rid, **summary)
        log("map.ready", pages=summary["pages"], interactives=summary["interactives"],
            destructive=len(summary["looks_destructive"]), auth=len(summary["looks_like_auth"]))
        decision = await_map_decision(rc, rid, summary)
        if decision == "reject":
            # A refusal ends the run the ORDINARY way: the explore plan is still frozen and its
            # artefacts still written, because the map is what the person was looking at and throwing it
            # away would make "no" cost them the exploration too.
            _agui("map.rejected", rid, pages=summary["pages"])
            log("map.rejected", pages=summary["pages"])
            return {"scenario_steps": [], "scenario_unmatched": [], "phase": "map_rejected",
                    "messages": [{"role": "assistant",
                                  "content": "the map was not approved, so no test was authored"}]}
        if decision == "approve":
            _agui("map.approved", rid, pages=summary["pages"])
            log("map.approved", pages=summary["pages"])
        base_id = len(state.get("exploration_plan", []))
        # M9.10: prior user turns (all but the current — which IS this turn's goal/describe) = refine context.
        # GAP-M9-20: cap to the last N turns + a rolling-summary prefix so the prompt stays bounded.
        prior = _capped_history(_user_turns(state.get("messages"))[:-1])
        if scenario_head.name == "goal":
            out = scenario_head.build_scenario(flatten_site_map(site_map), state.get("goal"), history=prior)
            steps, unmatched = ground_scenario(out.get("refs", []), site_map, start_id=base_id + 1)
        else:  # describe: LLM draft -> deterministic reconcile against the real map
            out = scenario_head.draft(history=prior)
            steps, unmatched = reconcile(out.get("draft", []), site_map, start_id=base_id + 1)
        tok = out.get("tokens") or {}
        tx_write({"step": "scenario", "planner": scenario_head.name, "model": scenario_head.model,
                  "decision": "scenario", "reason": f"{len(steps)} grounded, {len(unmatched)} unmatched",
                  "prompt_tokens": tok.get("prompt"), "completion_tokens": tok.get("completion")})
        rc.report(state.get("run_id", ""), "plan", tok.get("prompt"), tok.get("completion"))
        # M9.10: record an assistant summary into the conversation thread for the next turn's context.
        summary = {"role": "assistant",
                   "content": f"authored {len(steps)} grounded step(s), {len(unmatched)} unmatched"}
        return {"exploration_plan": list(state.get("exploration_plan", [])) + steps,
                "scenario_steps": steps, "scenario_unmatched": unmatched, "phase": "scenario",
                "messages": [summary]}

    def report(state: RunState) -> dict:
        """Freeze plan.json with a deterministic plan_hash over the ordered steps."""
        steps = list(state.get("exploration_plan", []))
        ph = canonical_plan_hash(steps)
        plan_obj = {"plan_id": state.get("run_id"), "plan_hash": ph,
                    "target_url": state.get("target_url"), "run_mode": state.get("run_mode"),
                    "coverage_target": state.get("coverage_target"),
                    "coverage_achieved": round(state.get("coverage_achieved", 0.0), 4),
                    "interactive_seen": len(state.get("interactive_seen", [])),
                    "interactive_exercised": len(state.get("interactive_exercised", [])),
                    "steps": steps}
        # ⚠ ПОЛНОТА ОБХОДА — ОТДЕЛЬНОЕ УТВЕРЖДЕНИЕ, И ДО СИХ ПОР ЕГО НЕ БЫЛО НИГДЕ.
        #
        # Замерено 2026-08-23 на `the-internet`: обход упёрся в потолок шагов и записал
        # `coverage_achieved: 1.0`. Обе цифры верны по отдельности и вместе врут: покрытие считается
        # долей от `interactive_seen`, а `seen` — это только то, что успели УВИДЕТЬ, поэтому оборванный
        # обход легко даёт единицу. Читателю показывали «покрыто всё», когда за краем осталось
        # тридцать страниц фронтира.
        #
        # `exploration_complete` для этого не годится: этот булев защёлк ставится ОДИНАКОВО и при
        # сходимости, и при потолке, и при пустых кандидатах, и при остановке оркестратором. А
        # `reason`, который причину знал, уезжал только в `llm-transcript.jsonl` — файл, не входящий
        # в перечень отдаваемых артефактов.
        #
        # ⚠ ПРИЧИНА ВЫВОДИТСЯ, ЕСЛИ ЕЁ НЕ СООБЩИЛИ. Есть третий путь выхода, минующий plan() вовсе:
        # маршрутизатор `route_checkpoint` уводит в `scenario` по `current_step >= max_steps`, и тогда
        # `stop_reason` пуст. Молчаливое «неизвестно» здесь было бы худшим из ответов, поэтому потолок
        # распознаётся по тем же числам, по которым его распознал бы plan().
        step_now = state.get("current_step", 0)
        max_steps = state.get("max_steps", 40)
        frontier_left = len(state.get("nav_frontier", []) or [])
        # ⚠ ВЕТКА `unknown` СЕЙЧАС НЕДОСТИЖИМА, И ЭТО ЗАМЕРЕНО, А НЕ ПРЕДПОЛОЖЕНО. Все четыре места,
        # ставящие `exploration_complete: True`, называют свою причину (сходимость/потолок/пустые
        # кандидаты · решение планировщика · останов оркестратором · останов человеком), а
        # единственный оставшийся путь в `scenario` мимо `plan()` — `route_checkpoint` по
        # `current_step >= max_steps` — попадает во вторую ветку и даёт то же «max_steps».
        # Следствие честно: мутация, выбрасывающая `unknown`, ЭКВИВАЛЕНТНА — гейт её не убивает и
        # убить не может. Ветка оставлена полом на будущее: она стоит ровно столько, сколько стоит
        # следующая ветка останова, которая забудет назвать себя, и тогда читатель получит слово
        # «unknown» вместо молчаливого «max_steps» на третьем шаге из девяноста.
        reason = state.get("stop_reason") or ("max_steps" if step_now >= max_steps else "unknown")
        plan_obj["completeness"] = {
            # Полным обход считается ровно в одном случае: он сам решил, что больше некуда идти.
            # Потолок, пустые кандидаты и решение планировщика — все три означают, что за краем
            # осталось неизвестное количество непройденного.
            "complete": reason == "converged",
            "reason": reason,
            "stopped_at_step": step_now,
            "max_steps": max_steps,
            "frontier_left": frontier_left,
        }
        if reason != "converged":
            # Событие с `degrades: true`: обход, прошедший не весь сайт, — это потерянное качество
            # прогона, а не деталь его устройства, и хаб читает деградации именно так.
            log("explore.incomplete", reason=reason, step=step_now,
                frontier=frontier_left, coverage=round(state.get("coverage_achieved", 0.0) * 100))
        # ADR-092: perception coverage rides ALONGSIDE the steps, not inside them — a step's identity
        # must not change because a page turned out to have a shadow root, or every existing plan_hash
        # would break for a measurement that describes the page rather than the test.
        perception = state.get("perception") or {}
        if perception:
            worst = min((v.get("ratio") for v in perception.values() if v.get("ratio") is not None),
                        default=None)
            plan_obj["perception"] = {
                "pages": perception,
                # The WORST page, not the average: an average hides the one screen we half-see behind
                # nine we see fully, and it is the half-seen screen that makes a plan incomplete.
                "worst_ratio": worst,
            }
        from . import budget  # M15.1: per-run token totals -> persistResult ingests tokens_* + cost_usd
        plan_obj["tokens"] = budget.tracker().summary()
        # The model that SPENT the planner tokens above — which is not always the explore planner.
        # `planner` walks the site and is heuristic by default (no `.model` at all), while the scenario
        # head is what actually calls the LLM in goal/describe mode. Reading only `planner` left
        # `models.plan` null on every goal run: cost came out 0 and, more importantly, the `model` label
        # on every metric point came out empty — and that label is the seam a cross-project rollup groups
        # on (ADR-056). Both are recorded; `plan` keeps naming the single spender for pricing, because
        # both heads resolve through the same make_backend("planner") and therefore the same model.
        _explore_model = getattr(planner, "model", None)
        _author_model = getattr(scenario_head, "model", None)
        plan_obj["models"] = {"plan": _explore_model or _author_model,
                              "explore": _explore_model, "author": _author_model}
        # ⚠ ДЕГРАДАЦИИ ОБХОДА ДО СИХ ПОР НЕ ДОЕЗЖАЛИ ДО АРТЕФАКТА ВООБЩЕ.
        #
        # `eventlog.degradations()` читался ровно в одном месте — `replay.py:604`, — поэтому потерянное
        # качество попадало в `report.json` повторного прогона и НЕ попадало в `plan.json` обхода ни
        # разу. Обход без ключа к модели, обход на исчерпанном бюджете и обход, не прошедший сайт,
        # оставляли артефакт, который читается как чистый: каталог всегда знал, какие коды означают
        # деградацию, и всегда нёс фразу для вердикта — читать это со стороны обхода было некому.
        #
        # ⚠ ПЕРЕЧЕНЬ НЕПОЛОН ПО ПОСТРОЕНИЮ, И ЭТО НАДО ЗНАТЬ. Файл пишется ЗДЕСЬ, а разбор прогона
        # продолжается после графа: `system.trace_missing` и события видео произносятся в teardown
        # (`_stop_trace`/`_stop_video` в `__main__.py`), то есть уже после этой строки. Они доезжают до
        # человека журналом и вердиктом, но в `plan.json` их не будет. Врать об этом нечем: ключ
        # называет то, что было известно НА МОМЕНТ ЗАМОРОЗКИ ПЛАНА, и заморозка стоит раньше разборки.
        #
        # `plan_hash` не двигается: он считается `canonical_plan_hash(steps)` — только по шагам, — и
        # ни один ключ уровня плана в него не входит. Проверено тем же гейтом, что стережёт 106
        # сохранённых планов.
        # ⚠ ИСКЛЮЧЁННОЕ ПИШЕТСЯ ВСЕГДА, а не только когда оно непусто. Пустой перечень при
        # `respected: true` говорит читателю «правила прочитаны, и под них ничего не попало» —
        # это ДРУГОЕ утверждение, чем отсутствие ключа, из которого читатель не узнает ничего.
        _rex = list(state.get("robots_excluded", []) or [])
        if robots is not None:
            plan_obj["robots"] = robots.as_artifact(_rex)
            if _rex:
                log("plan.robots_excluded", count=len(_rex))
        from . import eventlog
        plan_obj["degradations"] = eventlog.degradations()
        with open(os.path.join(state.get("artifact_dir", "."), "plan.json"), "w") as f:
            json.dump(plan_obj, f, indent=2)
        # PROD-IMPORT: the explore map, written as an artifact. `ground_imported` has always been able
        # to answer "does this imported step still bind to an element the app HAS?" — but nothing ever
        # produced the map it needs. It lived only in graph state and in the per-run checkpoint.db,
        # which ADR-099 deletes in `finally`, so the capability was real in code and unreachable in
        # practice: the only file of this shape in the repository was a synthetic test fixture.
        #
        # It carries the application's accessible names, so it is foreign text — but the SAME foreign
        # text plan.json already carries in its role+name locators (ADR-100's distinction: inherent to
        # the function, not incidental). More of it, not a new class of it, and it lands in the same
        # artifact directory the retention and redaction machinery already governs.
        site_map = state.get("site_map") or {}
        # `any(values)`, not `if site_map`: perceive records a key for every path it visited, so a page
        # with nothing interactive yields {path: []} — a NON-EMPTY dict describing NO elements. Writing
        # that produces a map against which every imported step grounds as "gone": a confident wrong
        # diagnosis, which is worse than no diagnosis. Caught by running the graph, not by reading it.
        if any(site_map.values()):
            with open(os.path.join(state.get("artifact_dir", "."), "site-map.json"), "w") as f:
                json.dump(site_map, f, ensure_ascii=False, indent=2)
        # M14 (ADR-055): a best-effort AG-UI verdict from this node's own view of the run (errors seen
        # during explore) — NOT the true process exit code, which __main__.py computes after
        # app.invoke() returns (outside this graph); that final code is out of scope here.
        # PLAN-NOT-GROUNDED-SILENT. This frame used to be computed from `errors` ALONE — that is,
        # from what the EXPLORE phase saw. The scenario node writes no errors, so an authoring run
        # that grounded nothing emitted `verdict: ok, exit_code: 0` while the process exited 1.
        # Measured twice on a live model, and the two lines sat in the same log file.
        #
        # The frame still is not the process exit code (that is computed in __main__ after the graph
        # returns) and does not pretend to be. What it must not do is contradict it: an authoring run
        # whose only deliverable is a scenario, that produced no grounded step, has failed, and the
        # UI's headline event is the last place that should be the one to disagree.
        not_grounded = bool(state.get("scenario_unmatched")) and not state.get("scenario_steps")
        failed_run = bool(state.get("errors")) or not_grounded
        _agui("verdict", state.get("run_id", ""), verdict=("failed" if failed_run else "ok"),
              exit_code=(1 if failed_run else 0), healed=0,
              failed=state.get("failed_steps", 0))
        return {"plan_hash": ph, "completeness": plan_obj["completeness"]}

    def route_plan(state: RunState) -> str:
        return "scenario" if state.get("exploration_complete") else "act"

    def route_verify(state: RunState) -> str:
        return "checkpoint" if state.get("_verify_ok", True) else "heal"

    def route_checkpoint(state: RunState) -> str:
        # M9.8 F4 (ADR-054): abort during a takeover converges (abort > takeover); an armed takeover
        # diverts to the pause node before the run continues.
        if state.get("exploration_complete"):
            return "scenario"
        if state.get("_takeover_armed"):
            return "takeover"
        return "scenario" if state.get("current_step", 0) >= state.get("max_steps", 40) else "perceive"

    def route_entry(state: RunState) -> str:
        """M9.10 (ADR-048): conditional entry. A RESUMED multi-turn thread carries a persisted `site_map`
        AND prior `messages` → skip the browser explore, go straight to re-author (`scenario`). A cold
        turn-1 / one-shot run has an empty site_map → the full `perceive`-walk. Pure explore is unchanged
        (site_map starts {} ⇒ always `perceive`)."""
        return "scenario" if (state.get("site_map") and state.get("messages")) else "perceive"

    def _traced(node_name, fn):
        """Wrap a node in a per-node OTel span (M8, ADR-021); no-op when tracing isn't configured."""
        def wrapped(state):
            with span(f"node.{node_name}"):
                return fn(state)
        return wrapped

    b = StateGraph(RunState)
    for name, fn in [("perceive", perceive), ("ground", ground), ("plan", plan),
                     ("act", act), ("verify", verify), ("heal", heal),
                     ("checkpoint", checkpoint), ("takeover", takeover),
                     ("scenario", scenario), ("report", report)]:
        b.add_node(name, _traced(name, fn))
    # M9.10 (ADR-048): conditional entry — resume a warm multi-turn thread straight into `scenario`,
    # else the normal cold/one-shot `perceive` walk. (Was an unconditional START->perceive edge.)
    b.add_conditional_edges(START, route_entry, {"perceive": "perceive", "scenario": "scenario"})
    b.add_edge("perceive", "ground")
    b.add_edge("ground", "plan")
    b.add_conditional_edges("plan", route_plan, {"act": "act", "scenario": "scenario"})
    b.add_edge("act", "verify")
    b.add_conditional_edges("verify", route_verify, {"checkpoint": "checkpoint", "heal": "heal"})
    b.add_edge("heal", "checkpoint")
    # M9.8 F4 (ADR-054): an armed takeover routes to the pause node, which loops back to checkpoint after
    # the operator returns (re-poll handles a not-yet-propagated Return) before the run continues.
    b.add_conditional_edges("checkpoint", route_checkpoint,
                            {"perceive": "perceive", "scenario": "scenario", "takeover": "takeover"})
    b.add_edge("takeover", "checkpoint")
    b.add_edge("scenario", "report")  # M9.2b: scenario node (no-op in pure explore) -> report
    b.add_edge("report", END)
    return b
