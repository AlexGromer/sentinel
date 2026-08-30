"""Sentinel brain — shared RunState and pure helpers (M1)."""
import hashlib
import json
from typing import Annotated, TypedDict
from urllib.parse import urlsplit, urlunsplit

from langgraph.graph.message import add_messages


class RunState(TypedDict, total=False):
    # identity / config
    run_id: str
    run_mode: str
    target_url: str
    base_origin: str
    coverage_target: float
    max_steps: int
    artifact_dir: str
    goal: str                     # M9.2a: NL goal text for goal-mode (GoalPlanner); "" in explore-mode
    describe: str                 # M9.2b: NL flow description for describe-mode (DescribePlanner); "" otherwise
    # M9.10 (ADR-048): multi-turn conversation accumulator (chat mode). The brain feeds plain dicts
    # {role, content}; LangGraph's add_messages reducer coerces them to BaseMessage and APPENDS across
    # turns, persisted by the shared checkpointer (thread_id=conversation_id). Empty/absent for one-shot
    # explore/goal/describe runs — so their behavior (and plan_hash) is unchanged.
    messages: Annotated[list, add_messages]
    # ADR-108a: the conversation's OBJECTIVE, pinned on the first turn and never rewritten —
    # {"kind": "goal"|"describe", "text": str}.
    #
    # It exists because `goal` was doing two jobs at once. control-api sent each turn's text AS the
    # goal, so "what this conversation is for" and "what the person just typed" were the same field,
    # and nothing could tell a refinement from a new objective. That made the rule "a conversation has
    # one goal; for a new goal start a new chat" unstateable, let alone enforceable.
    #
    # Lives in the checkpointer (thread_id=conversation_id), which is the conversation's real state —
    # NOT in the `chats` SQL row, which ADR-050 defines as a browsable projection and whose `last_goal`
    # column keeps meaning exactly what its name says: the most recent turn.
    chat_intent: dict
    # M9.2b two-phase authoring (ADR-028): a site-wide element map built during the explore walk, then
    # a one-shot scenario head grounds the goal/describe into replayable steps.
    site_map: dict                # page_path -> [element {semantic_id,role,name,testid,locator,alternatives,page}]
    perception: dict              # ADR-092: page_path -> {seen,total,ratio,unseen{...},opaque{...}} — how much of the page perception can SEE, as opposed to how much of what it saw was exercised (coverage)
    phase: str                    # "explore" | "scenario"
    scenario_steps: list          # the grounded authored steps (appended to exploration_plan)
    scenario_unmatched: list      # refs/draft-steps that could not be grounded to a real element
    # perception
    current_url: str
    page_model: dict
    # exploration accounting
    exploration_plan: list
    plan_hash: str
    current_step: int
    interactive_seen: list        # semantic_ids (dedup'd, JSON-safe)
    interactive_exercised: list
    # M9-LIVE: semantic_id -> how many times acting on it RAISED. `act` marks an element exercised
    # only on success, so before this existed a permanently unactionable control (a disabled button)
    # stayed a candidate forever and the planner proposed it every step until max_steps — the ×34
    # repeat live logs made visible. A dict, not a list, because the retry budget is per element.
    interactive_failed: dict
    visited_paths: list
    nav_frontier: list
    coverage_achieved: float
    exploration_complete: bool
    # ПОЧЕМУ обход кончился. `exploration_complete` — булев маршрутный защёлк: он ставится ОДИНАКОВО
    # и когда покрытие достигнуто, и когда упёрлись в потолок шагов, и когда кандидаты кончились, и
    # когда оркестратор прервал прогон. То есть по нему нельзя отличить «обошли всё» от «нам не дали
    # доходить». Замерено: `reason` вычислялся в plan() и уезжал ТОЛЬКО в llm-transcript.jsonl, а тот
    # не входит в перечень отдаваемых артефактов — до человека причина не доезжала ни по какому
    # каналу, и `coverage_achieved: 1.0` на оборванном обходе читалось как «покрыто всё».
    stop_reason: str
    # Полнота обхода, вычисленная узлом `report` (ADR-131). Живёт в состоянии, а не только в
    # файле: вызывающему она нужна для пометки на сценарии, а второй расчёт по тем же полям
    # был бы вторым автором одного факта — они расходятся первыми.
    completeness: dict
    # Адреса, не попавшие во фронтир по воле владельца сайта (ADR-133). Живут в состоянии,
    # потому что писать их обязан ТОТ ЖЕ узел, что пишет план, — и на спасённом плане тоже.
    robots_excluded: list
    executed_actions: list
    errors: list
    # M9.8 F4 (ADR-054): operator-takeover resume payloads, appended each time the checkpoint node
    # resumes from an interrupt() (one entry per takeover→return cycle). Observability only — not in
    # plan.json/scenario.json, so plan_hash is unaffected.
    takeover_returns: list
    # M14 (ADR-055): auto-HITL counters (docs/M14_CONTRACT.md §4). consecutive_heal_failures counts
    # heal-node misses in a row (the explore graph's heal node is a stub — see graph.py — so every
    # entry is a miss); reset to 0 on any successful verify. Drives the checkpoint node's auto-arm of
    # _takeover_armed past SENTINEL_AUTO_HITL_THRESHOLD. failed_steps is a running total of verify
    # failures (observability + M15-metrics substrate). Both default absent -> 0 (state.get(..., 0)),
    # so existing runs/tests with no M14 wiring are unaffected.
    consecutive_heal_failures: int
    failed_steps: int
    # transient channels (must be declared so LangGraph keeps them across nodes)
    _pending: dict
    _last_ok: bool
    _verify_ok: bool
    _takeover_armed: bool         # M9.8 F4 (ADR-054): latched by checkpoint when a takeover is pending; drives the pause node


def normalize_url(u: str) -> str:
    """Drop query + fragment; keep scheme/host/path. Stable page identity."""
    if not u:
        return ""
    s = urlsplit(u)
    return urlunsplit((s.scheme, s.netloc, s.path, "", ""))


def page_identity(u: str) -> str:
    """Идентичность СТРАНИЦЫ: `normalize_url` плюс фрагмент, если он маршрутоподобен.

    ⚠ ЗАЧЕМ ЭТО НУЖНО, ЗАМЕРЕНО НА ЖИВОЙ ЦЕЛИ. OWASP Juice Shop (Angular), обход heuristic с потолком
    60: **29 шагов, 2 уникальные страницы, coverage 0.0625**; с живой моделью — 33 шага и
    **coverage 0.0**. Причина ровно одна: `normalize_url` отбрасывает фрагмент, а маршруты SPA живут
    именно в нём, поэтому все восемьдесят состояний приложения схлопывались в один адрес. Обход
    считал, что стоит на месте, и «сходился», не увидев сайта.

    ⚠ ПОЧЕМУ НЕ ВСЯКИЙ ФРАГМЕНТ, а только маршрутоподобный. `semantic_id` строится ПО ЭТОМУ ЗНАЧЕНИЮ
    и входит в `plan_hash`. Безусловное включение фрагмента сменило бы КАЖДЫЙ ключ и обрушило все
    сохранённые планы (`replay` сверяет хеш и жёстко обрывается с кодом 3), голдены и кеш починки — а
    механизма миграции ключей в проекте нет вовсе. Маршрут начинается со слэша (`#/orders`) или с
    хешбэнга (`#!/orders`, форма старых роутеров), и ни та, ни другая на целях без hash-роутинга не
    встречается — поэтому их ключи не двигаются. Обычный якорь (`#section`, `#top`) идентичность НЕ
    меняет: иначе оглавление на одной странице расплодило бы столько «страниц», сколько в нём пунктов.

    ⚠ ГРАНИЦА ОБХОДА СЧИТАЕТСЯ НЕ ОТСЮДА. `base_origin_of` работает на адресе БЕЗ фрагмента — правило
    «путь сужает границу» про путь, а не про маршрут, и применять его к `#/orders` значило бы молча
    завести семантику, которой ADR-130 не проектировал.
    """
    if not u:
        return ""
    s = urlsplit(u)
    frag = s.fragment
    if frag.startswith("/") or frag.startswith("!/"):
        return urlunsplit((s.scheme, s.netloc, s.path, "", frag))
    return urlunsplit((s.scheme, s.netloc, s.path, "", ""))


def base_origin_of(target: str) -> str:
    """Граница обхода: адреса вне неё во фронтир не попадают (brain/graph.py, узел perceive).

    ⚠ ЭТО ГРАНИЦА БЕЗОПАСНОСТИ, А НЕ УДОБСТВО, и прежняя однострочная форма её снимала.
    Было: ``normalize_url(target).rsplit("/", 1)[0] + "/"`` — «всё до последнего слэша». Для цели
    БЕЗ завершающего слэша — то есть для самой естественной формы, ``--target https://myapp.com``, —
    последний слэш это второй слэш схемы, и граница получалась равной ``"https://"``. Проверка
    ``nu.startswith(origin)`` в graph.py истинна тогда для ЛЮБОГО https-адреса в интернете.

    ЗАМЕРЕНО поведением, а не чтением (2026-08-22, две локальные площадки на 8181 и 8182, ссылка с
    первой на вторую):

        --target http://127.0.0.1:8181   → 6 шагов, ТРИ из них на чужом хосте (/index.html, /b2, /b3)
        --target http://127.0.0.1:8181/  → 3 шага,  на чужом хосте НИ ОДНОГО

    Один символ решал, останется ли инструмент на своей цели или уйдёт обходить посторонний сайт —
    посылая туда заголовки, снимая кадры и записывая чужие страницы в артефакт прогона.

    Правило: для http(s) граница НИКОГДА не шире собственного хоста цели. Путь сужает её и дальше
    (``https://app/shop/x`` → ``https://app/shop/``), потому что обход, начатый в разделе, не должен
    расползаться на весь сайт. Для ``file://`` граница остаётся каталогом цели: у схемы нет хоста,
    и каталог — единственное осмысленное «здесь».
    """
    u = normalize_url(target)
    if not u:
        return ""
    sp = urlsplit(u)
    head = u.rsplit("/", 1)[0] + "/"
    if sp.scheme in ("http", "https"):
        root = f"{sp.scheme}://{sp.netloc}/"
        # `head` уже, чем корень хоста, — берём его; иначе он выродился в схему, и это тот дефект.
        return head if head.startswith(root) else root
    return head


def semantic_id(path: str, role: str, name: str) -> str:
    return hashlib.sha1(f"{path}|{role}|{name}".encode()).hexdigest()[:12]


def control_id(role: str, name: str) -> str:
    """Идентичность КОНТРОЛА — «эта кнопка», без привязки к экрану, на котором она сейчас видна.

    ⚠ ЭТО ВТОРАЯ ОСЬ, А НЕ ЗАМЕНА `semantic_id`, и разделение сделано потому, что одна величина
    отвечала на ДВА разных вопроса.

      «К какому вхождению привязать шаг?»  — маршрут НУЖЕН. Шаг воспроизведения это «нажать
        Dashboard НА ЭКРАНЕ ЗАКАЗОВ»; без пути он перестанет связываться, а голдены, починка,
        импортёр и ревизии ключуются именно так. Отвечает `semantic_id`, и он не меняется.

      «Этот контрол уже проработан?»       — маршрут ЛИШНИЙ. Кнопка «Dashboard» в рельсе одна;
        нажав её однажды, обход узнал о ней всё. Отвечает эта функция.

    ⚠ ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ (2026-08-30, `testdata/site-spa`, heuristic, бюджет 40). После
    ADR-132 путь страницы включает маршрут SPA, поэтому один и тот же контрол рельса получал
    ДВЕНАДЦАТЬ разных `semantic_id` — по одному на маршрут. Эвристика (`planner.py`: `clicks[0]`,
    «первый непроработанный в порядке DOM») честно перебирала их все, а рельс в разметке стоит
    ПЕРЕД содержимым экрана. Итог прогона: **25 кликов из 38 — рельс, который уже нажимали**
    (66 % бюджета; при потолке 200 — 76 из 106, то есть 72 %), и вырождение начиналось со ВТОРОГО
    шага. На содержимое экранов оставалось пять шагов.
    Распределение дублей оказалось бинарным: 40 контролов встречались на ОДНОМ маршруте, 9 — на
    всех двенадцати. Эти девять раздували перечень кликабельных с 49 до 137.

    ⚠ И ЧЕГО ЭТО НЕ ДЕЛАЕТ: покрытие не растёт. Замерено — 38/137 = 0.2701 против 13/49 = 0.2653:
    числитель раздут ровно во столько же раз, что и знаменатель, потому что «проработанными»
    считались те же тринадцать контролов, посчитанные по разу на маршрут. Покрытие было честным;
    менялся впустую только БЮДЖЕТ.
    """
    return hashlib.sha1(f"{role}|{name}".encode()).hexdigest()[:12]


def canonical_plan_hash(steps: list) -> str:
    """Deterministic SHA-256 over the ENTIRE ordered step dicts — every field is included (`sort_keys`
    only makes key order irrelevant; nothing is excluded). So any field change, including the M9.1 step
    fields (secretRef/value/text/clear/condition/expected/expect_ok/key), is tamper-detectable
    (a plan_hash mismatch hard-aborts replay with exit 3)."""
    payload = json.dumps(steps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
