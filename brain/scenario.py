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
from .state import normalize_url, page_identity, semantic_id

# Verbs an authored step may carry (brain/replay.py schema). An LLM verb outside this -> unmatched.
VALID_VERBS = {"click", "fill", "type", "select", "press", "assert"}


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


def _nav_step(page: str, step_id: int, target: str = "") -> dict:
    """`target` defaults to `page`, but a RECORDED transition supplies the address actually observed.

    The difference is the query string: `page_identity` drops it, so without this a recording of
    `?tab=orders` would replay a navigate to the bare path — a different screen than the one that was
    recorded. `semantic_id` still keys off `page`, deliberately: moving that key would move every
    saved plan_hash, and no key-migration mechanism exists.
    """
    return {"step_id": step_id, "action_type": "navigate",
            "semantic_id": semantic_id(page, "navigate", ""), "intent": f"navigate to {target or page}",
            "target": target or page, "locator": None, "alternatives": None, "is_milestone": False,
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


def _emit(bound: list, start_page: str, start_id: int, trust_observed: bool = False) -> list:
    """bound = [(element, verb, extra)]. Synthesize cross-page navigates; assign sequential step_ids.

    ⚠ A synthesized navigate is a GUESS that the page changed by itself, and it is wrong exactly when
    the previous step is what changed it. Measured cost of guessing wrong: on a path-routed SPA the
    hard `goto` replaces the document and wipes the application's in-memory state (404 as well, on a
    server with no history fallback); and on any router it silently REPAIRS a broken transition — a
    click that lands on the wrong route is corrected by the next navigate, so replay stays green and
    the routing regression is structurally invisible. `trust_observed` is honoured only for a
    recording, whose `route_arrived` came from watching the browser rather than from a model.
    """
    steps, sid, cur_page = [], start_id, page_identity(start_page or "")
    for element, verb, extra in bound:
        page = page_identity(element.get("page", ""))
        if page and page != cur_page:
            if trust_observed and extra.get("route_arrived"):
                cur_page = page                              # the transition was OBSERVED, not assumed
            else:
                steps.append(_nav_step(page, sid, extra.get("observed_url") or "")); sid += 1
                cur_page = page
        steps.append(_verb_step(element, verb, extra, sid)); sid += 1
    if steps:
        steps[0]["is_milestone"] = True
    return steps


def ground_scenario(llm_refs: list, site_map: dict, start_page: str = "", start_id: int = 1,
                    trust_observed: bool = False):
    """Goal head: bind ordered LLM `{ref, verb, value?}` to real elements. Returns (steps, unmatched).

    A `ref` (semantic_id) not in the map is dropped to `unmatched` — never fabricated (grounding).

    `trust_observed` (ADR-138) is opt-in and OFF by default on purpose: this function is the last
    validator on the LLM authoring path, and a model that returned `route_arrived: true` would
    otherwise be able to delete a navigate from its own plan. Only `record_bridge` sets it.
    """
    idx = _index_by_id(site_map)
    bound, unmatched = [], []
    for r in llm_refs or []:
        el = idx.get(r.get("ref"))
        if not el:
            unmatched.append({"ref": r.get("ref"), "reason": "ref not in site map"})
            continue
        verb = (r.get("verb") or "click").strip().lower()
        if verb not in VALID_VERBS:                        # out-of-spec verb -> not authorable
            unmatched.append({"ref": r.get("ref"), "reason": f"unsupported verb {verb!r}"})
            continue
        # secretRef is fill-only across the whole product (executor, recorder, _verb_step). A
        # secretRef on any other verb is REJECTED, not carried and then silently dropped by
        # _verb_step: a dropped secretRef reads as "the secret is protected" while the field is
        # actually left empty (SEC-SCENARIO-SECRETREF).
        if r.get("secretRef") is not None and verb != "fill":
            unmatched.append({"ref": r.get("ref"), "reason": f"secretRef is valid on fill only, not {verb!r}"})
            continue
        bound.append((el, verb, r))
    return _emit(bound, start_page, start_id, trust_observed), unmatched


def route_consistency(steps: list, site_map: dict) -> dict:
    """Сходится ли сценарий сам с собой — то есть следует ли он за собственными действиями (ADR-151).

    ⚠ ЭТО НЕ ПРОВЕРКА СВЯЗНОСТИ, и попытка сделать её первой была замером отменена: `_nav_step`
    вставляется АВТОМАТИЧЕСКИ между шагами на разных страницах, поэтому сценарий связен ПО
    ПОСТРОЕНИЮ и такая проверка не поймала бы ничего. Рёбра (ADR-150) дают другое — видно, что
    сценарий делает ПОСЛЕ клика, о котором уже известно, куда он ведёт:

      ИЗБЫТОЧНЫЙ navigate — предыдущий клик уже привёл на этот адрес, а сценарий идёт туда снова.
          Не ошибка исполнения (replay пройдёт), но лишний шаг в тесте, который человек будет читать.
      ТЕЛЕПОРТ — сценарий переходит на адрес, до которого от текущего положения ребра НЕТ. Значит
          это не маршрут пользователя, а прыжок по адресной строке: тест «работает» и при этом не
          проверяет тот путь, которым пользователь ходит.

    Замерено на живом goal-прогоне (зелёном, unmatched=0): один избыточный navigate и один телепорт.
    То есть дефект есть в сценарии, который продукт объявляет безупречным.

    ⚠ Возвращает ФАКТЫ, а не приговор. Провалить прогон за телепорт нельзя: цель — направление, а не
    спецификация (HEALTH-004), и приложение может законно не иметь ссылки туда, куда пользователь
    попадает закладкой. Судит человек, а продукт обязан сказать.

    Положение считается ТОЛЬКО по известным рёбрам. Клик, про который карта ничего не знает, делает
    положение НЕИЗВЕСТНЫМ — и тогда следующий navigate не обвиняется ни в чём: обвинение по незнанию
    хуже молчания."""
    edges = {}
    for pg, els in (site_map or {}).items():
        for el in els or []:
            to = el.get("leads_to") or el.get("href_to")
            if to:
                edges[(pg, el.get("role"), el.get("name"))] = to
    known = {t for t in edges.values()}
    cur, redundant, teleports = None, [], []
    for st in steps or []:
        if st.get("action_type") == "navigate":
            tgt = page_identity(st.get("target") or "")
            if cur is not None and tgt:
                if tgt == cur:
                    redundant.append(st.get("step_id"))
                elif cur in known or any(p == cur for p, _r, _n in edges):
                    # Обвиняем только когда про ТЕКУЩУЮ страницу вообще что-то известно: иначе
                    # «ребра нет» означает лишь, что мы её не обходили.
                    if not any(p == cur and to == tgt for (p, _r, _n), to in edges.items()):
                        teleports.append(st.get("step_id"))
            cur = tgt or cur
        else:
            loc = st.get("locator") or {}
            cur = edges.get((cur, loc.get("role"), loc.get("name")), None) if cur else None
    return {"redundant_navigations": redundant, "teleports": teleports}


# Исходы достижения цели. ТРИ, а не два, и третий — не осторожность, а урок W13: положение,
# посчитанное по неизвестному, обвиняет на пустом месте, и такую проверку научаются игнорировать.
GOAL_REACHED, GOAL_NOT_REACHED, GOAL_UNKNOWN = "reached", "not_reached", "unknown"


def pages_visited(steps: list, site_map: dict) -> list:
    """След страниц сценария — в порядке появления, без повторов. ДВА источника, и оба нужны.

    1. Цель каждого `navigate`. Эти шаги синтезирует `_emit` при КАЖДОЙ смене страницы, а адрес
       берёт из поля `page` РЕАЛЬНОГО элемента карты — то есть строит его наш код, а не текст
       модели. `start_page` в продукте нигде не задаётся непустым (все три места вызова
       `ground_scenario`), поэтому сценарий всегда начинается с `navigate`, и дыр в начале нет.
    2. РЕБРО элемента, по которому шаг действует (ADR-150), найденное по `semantic_id` шага.

    ⚠ ВТОРОЙ ИСТОЧНИК ЗАВЕДЁН НЕ ДЛЯ ПОЛНОТЫ, А ПО ЗАМЕРУ — первая редакция без него ОШИБАЛАСЬ,
    и ошибалась в самую опасную сторону. Живой прогон с целью «Open the page C» (`qwen3:8b`,
    2026-09-04) дал сценарий `navigate index · click 'Alpha' · navigate page-a · click 'To C'` —
    цель ДОСТИГНУТА, «To C» ведёт на `page-c`. Но `_emit` вставляет `navigate` только когда СЛЕДУЮЩИЙ
    шаг лежит на другой странице, а следующего шага здесь нет: сценарий заканчивается кликом.
    Страница, до которой сценарий дошёл ПОСЛЕДНИМ действием, в след по одним `navigate` не попадала
    НИКОГДА, и проверка объявляла `not_reached` над прогоном, который цели достиг. Ложное обвинение
    ровно того класса, ради предотвращения которого заводился третий исход.

    ⚠ ПОЧЕМУ НЕ ОБХОД `route_consistency`, хотя он уже ведёт `cur`. Замерено на настоящем
    логин-сценарии: `cur` обнуляется на ПЕРВОМ же не-навигирующем шаге (`cur = edges.get(...) if cur
    else None`), а это шаг 4 — `fill` поля «User». Проверка на нём отвечала бы «неизвестно» почти
    всегда, то есть была бы вакуумной. Здесь ребро читается ПО `semantic_id` шага и положение
    вообще не ведётся — терять нечего.

    ⚠ ЕДИНСТВЕННАЯ ДЫРА, И ОНА ВНЕ ЭТОГО ПУТИ. При `trust_observed and extra["route_arrived"]`
    (`_emit`) переход НЕ синтезируется — страница меняется молча. Этот режим включает ровно один
    вызывающий, `record_bridge`, и он ЗАПИСЫВАЕТ действия человека, а не авторит по цели; на
    goal-пути `trust_observed` оставлен False НАМЕРЕННО (докстринг `ground_scenario`: модель,
    вернувшая `route_arrived`, иначе стирала бы переход из собственного плана)."""
    idx = _index_by_id(site_map)
    seen, out = set(), []

    def _add(u: str) -> None:
        pg = page_identity(u or "")
        if pg and pg not in seen:
            seen.add(pg)
            out.append(pg)

    for st in steps or []:
        if st.get("action_type") == "navigate":
            _add(st.get("target") or "")
            continue
        el = idx.get(st.get("semantic_id")) or {}
        # Наблюдённое ребро перекрывает объявленное — тот же порядок, что в `route_consistency`.
        _add(el.get("leads_to") or el.get("href_to") or "")
    return out


def goal_reached(steps: list, goal_page: str, site_map: dict) -> dict:
    """Дошёл ли сценарий до страницы, которую модель назвала целевой (ADR-152).

    ДВЕ НЕЗАВИСИМЫЕ ВЕЛИЧИНЫ, и в этом весь смысл проверки. `goal_page` — НАМЕРЕНИЕ: модель
    спрашивают о нём ОТДЕЛЬНЫМ вызовом, где она видит цель и страницы, но НЕ ВИДИТ собственных
    шагов. `pages_visited` — НАБЛЮДЕНИЕ: след, выведенный из шагов нашим кодом. Спросить и то и
    другое одним ответом было бы «копией, соглашающейся сама с собой»: модель отчиталась бы о конце
    своего же списка, и проверка соглашалась бы с ней при любой ошибке.

    ⚠ ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ (qwen3:8b, `testdata/site`, 2026-09-04). Раздельная форма отвечает
    верно 9 из 9: «View the actions page» → `page-b` ×3, «Open the page C» → `page-c` ×3, «Log in» →
    `page-a` ×3. При этом ШАГИ по той же цели «View the actions page» дважды из двух уходили в логин
    на `page-a`. То есть намерение модель формулирует правильно, а исполняет неправильно, и разрыв
    между двумя величинами — ровно тот дефект, который продукт до сих пор не произносил: три живых
    прогона, из них два не дошли до цели, и у всех трёх вердикт был побайтово одинаков.

    ВАЖЕН РЕЗУЛЬТАТ, А НЕ МАРШРУТ (решение Alex): цель, достижимая несколькими путями, засчитывается
    при любом — поэтому спрашивается ПРИНАДЛЕЖНОСТЬ следу, а не совпадение с его концом. Сценарий,
    побывавший на целевой странице и ушедший дальше, цели достиг.

    ⚠ ЭТО ОБЪЯВЛЕНИЕ, А НЕ ПРИГОВОР. Код выхода не трогается: цель — направление, а не спецификация
    (HEALTH-004), и `Facts`/`decide` про достижение цели ничего не знают. Тот же выбор, что у
    ADR-151: судит человек, продукт обязан сказать.

    `unknown` (а не `not_reached`) во всех трёх случаях, когда сказать НЕЧЕГО: модель не назвала
    страницу · назвала не из карты (заземление намерения — та же дисциплина, что у `ref`, ADR-022) ·
    следа нет вовсе. Обвинение по незнанию хуже молчания."""
    trail = pages_visited(steps, site_map)
    known = {page_identity(p) for p in (site_map or {}) if p}
    for pg, els in (site_map or {}).items():
        for el in els or []:
            if el.get("page"):
                known.add(page_identity(el["page"]))
    tgt = page_identity(goal_page or "")
    if not tgt:
        return {"verdict": GOAL_UNKNOWN, "reason": "goal_page_not_named",
                "goal_page": "", "pages_visited": trail}
    if tgt not in known:
        # Модель назвала страницу, которой в карте нет. Это НЕ «не дошёл» — это «мы не знаем, куда
        # она нас послала», и молча приравнять одно к другому значило бы обвинить сценарий за
        # промах авторинга намерения.
        return {"verdict": GOAL_UNKNOWN, "reason": "goal_page_not_in_map",
                "goal_page": tgt, "pages_visited": trail}
    if not trail:
        return {"verdict": GOAL_UNKNOWN, "reason": "no_page_trail",
                "goal_page": tgt, "pages_visited": trail}
    return {"verdict": (GOAL_REACHED if tgt in trail else GOAL_NOT_REACHED), "reason": "",
            "goal_page": tgt, "pages_visited": trail}


def _match(draft_target: dict, flat_map: list):
    """Deterministic, CONSERVATIVE match of a draft target to ONE real element (else None)."""
    role = (draft_target.get("role") or "").strip().lower()
    name = (draft_target.get("name") or "").strip().lower()
    text = (draft_target.get("text") or "").strip().lower()
    page = page_identity(draft_target.get("page") or "")
    pool = [e for e in flat_map if (not page or page_identity(e.get("page", "")) == page)]

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
        if verb not in VALID_VERBS:                        # out-of-spec verb -> not authorable
            unmatched.append({"intent": d.get("intent"), "reason": f"unsupported verb {verb!r}"})
            continue
        # secretRef is fill-only (see ground_scenario): reject it on any other verb rather than let
        # _verb_step drop it silently and leave the secret field empty (SEC-SCENARIO-SECRETREF).
        if d.get("secretRef") is not None and verb != "fill":
            unmatched.append({"intent": d.get("intent"),
                              "reason": f"secretRef is valid on fill only, not {verb!r}"})
            continue
        extra = {k: d.get(k) for k in ("value", "text", "key", "clear", "condition", "expected",
                                       "expect_ok", "secretRef", "intent") if d.get(k) is not None}
        bound.append((el, verb, extra))
    return _emit(bound, start_page, start_id), unmatched
