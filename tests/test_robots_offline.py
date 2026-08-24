#!/usr/bin/env python3
"""Офлайн-гейт: воля владельца сайта читается, соблюдается и ЗАПИСЫВАЕТСЯ (ADR-133).

Run:  .venv/bin/python tests/test_robots_offline.py

ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ. `practice.expandtesting.com`, чьё разрешение подтверждено дословно: его
`robots.txt` говорит `Allow: /` и следом СЕМЬ `Disallow`. С главной ведут 96 внутренних ссылок, ТРИ
из них — в запрещённые пути. Обход про `robots.txt` не знал вовсе: фронтир фильтровался только
границей `base_origin`.

⚠ ГЛАВНОЕ УТВЕРЖДЕНИЕ ЗДЕСЬ — НЕ «НЕ ПОШЛИ», А «СКАЗАЛИ». Обход, молча выбросивший три адреса,
неотличим от обхода, который их не нашёл: человек откроет карту сайта, не увидит раздела и решит, что
инструмент до него не добрался. Поэтому перечень исключённого утверждается В АРТЕФАКТЕ, и утверждается
он поведенчески — прогоном настоящего графа, а не чтением исходника.

⚠ СЕТИ ЗДЕСЬ НЕТ ПО УСТРОЙСТВУ. `load()` принимает `fetch`, и гейт подставляет свой — иначе проверка
зависела бы от чужого сайта, то есть краснела бы по причине вне репозитория.
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("SENTINEL_LIVE_FRAMES", "0")

from langgraph.checkpoint.memory import MemorySaver          # noqa: E402

from brain import budget, eventlog                            # noqa: E402
from brain.graph import build_graph                           # noqa: E402
from brain.planner import HeuristicPlanner                    # noqa: E402
from brain.robots import load                                 # noqa: E402
from brain.state import base_origin_of, page_identity, semantic_id  # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


BODY = "\n".join([
    "User-agent: *",
    "Allow: /",
    "Disallow: /download-secure",
    "Disallow: /notes/api/",
    "Disallow: /infinite-scroll/",
])


def _fetch_ok(url):
    return BODY, ""


def _fetch_404(url):
    return None, "absent"


def _fetch_500(url):
    return None, "unreachable:HTTP 503"


def test_the_rules_are_read_and_obeyed():
    p = load("https://t.example/", fetch=_fetch_ok)
    if p.source != "fetched" or not p.respected:
        fail(f"правила прочитаны, а политика говорит {p.source!r}/{p.respected!r}")
    if p.rules != 3:
        fail(f"насчитано {p.rules} запрет(ов) вместо трёх — счёт для артефакта неверен")
    for u in ("https://t.example/download-secure", "https://t.example/notes/api/x",
              "https://t.example/infinite-scroll/2"):
        if p.allows(u):
            fail(f"запрещённый владельцем адрес {u} признан разрешённым")
    for u in ("https://t.example/", "https://t.example/contact", "https://t.example/download"):
        if not p.allows(u):
            fail(f"разрешённый адрес {u} признан запрещённым — обход ослеп на собственной цели")
    print(f"  ok  правила прочитаны: {p.rules} запрета, три пути закрыты, три открыты")


def test_the_shape_that_defeats_the_standard_library():
    """Форма, из-за которой матчер написан свой, а не взят из `urllib`.

    `Allow: /` СТОИТ ВЫШЕ запретов — и это не выдумка, а ровно тот файл, который вскрыл нужду в этой
    работе (`practice.expandtesting.com`: `Allow: /` и следом семь `Disallow`). CPython реализует
    ПЕРВОЕ совпадение по порядку строк, поэтому `Allow: /` матчит всё и выигрывает; RFC 9309 требует
    САМОЕ ДЛИННОЕ совпадение, при котором `/download-secure` длиннее `/` и запрещает.

    ⚠ ЗАМЕР ЛОВУШКИ ЗДЕСЬ ЖЕ, а не в комментарии: стандартная библиотека спрашивается напрямую и
    обязана ответить «разрешено». Если однажды CPython это починит, утверждение покраснеет — и
    правильной реакцией будет ПЕРЕЗАМЕРИТЬ и решить, нужен ли ещё свой матчер, а не удалить строку."""
    from urllib.robotparser import RobotFileParser
    rp = RobotFileParser()
    rp.parse(BODY.splitlines())
    if rp.can_fetch("Sentinel", "https://t.example/download-secure") is not True:
        fail("urllib.robotparser больше не разрешает запрещённый путь на этой форме — ловушка "
             "исчезла, и надо ПЕРЕЗАМЕРИТЬ, нужен ли собственный матчер")
    ours = load("https://t.example/", fetch=_fetch_ok)
    if ours.allows("https://t.example/download-secure"):
        fail("наш матчер повторил ошибку стандартной библиотеки на той самой форме, ради которой писался")
    # И встречное: самое длинное совпадение работает в обе стороны — более длинный Allow побеждает
    # более короткий Disallow.
    body = "User-agent: *\nDisallow: /a/\nAllow: /a/public/\n"
    p2 = load("https://t.example/", fetch=lambda u: (body, ""))
    if p2.allows("https://t.example/a/secret"):
        fail("короткий Disallow перестал работать")
    if not p2.allows("https://t.example/a/public/x"):
        fail("более длинный Allow не победил более короткий Disallow — правило «самое длинное "
             "совпадение» работает только в одну сторону, а это не правило")
    print("  ok  форма «Allow: / выше запретов»: urllib разрешает, мы запрещаем; длинный Allow побеждает")


def test_absence_allows_and_unreachability_says_so():
    """Две РАЗНЫЕ новости, и их нельзя путать: «файла нет» — законный ответ половины сайтов, а
    «прочитать не удалось» означает, что волю владельца мы не видели. Обе разрешают обход (см. верх
    brain/robots.py), но вторая обязана быть ГРОМКОЙ."""
    eventlog.reset_degradations()
    a = load("https://t.example/", fetch=_fetch_404)
    if a.source != "absent" or not a.allows("https://t.example/anything"):
        fail(f"отсутствие robots.txt дало {a.source!r} и запрет — это не то, что означает 404")

    u = load("https://t.example/", fetch=_fetch_500)
    if u.source != "unreachable":
        fail(f"недостижимый robots.txt дал источник {u.source!r}")
    if not u.allows("https://t.example/anything"):
        fail("недостижимый robots.txt запретил обход — стенд, чей файл отдал 503, получил бы отказ "
             "обходить собственный сайт")
    if "не удалось" not in u.detail_ru or "could not be read" not in u.detail:
        fail(f"недостижимость не названа в обеих половинах: {u.detail!r} / {u.detail_ru!r}")
    # ГРОМКОСТЬ — не украшение: код несёт `degrades`, поэтому уезжает в вердикт.
    cat = json.loads((REPO / "brain" / "events.json").read_text(encoding="utf-8"))["events"]
    if cat["run.robots_unreachable"].get("degrades") is not True:
        fail("run.robots_unreachable не деградирует вердикт — «мы не видели правил» стало бы тихим")
    if cat["run.robots_absent"].get("degrades") is True:
        fail("run.robots_absent деградирует вердикт — тогда половина сайтов даёт тревогу на ровном месте")
    print("  ok  отсутствие и недостижимость — разные новости, вторая громкая")


def test_ignoring_is_a_persons_choice_and_it_is_recorded():
    seen = []
    import brain.robots as R
    real, R.log = R.log, lambda code, **kw: seen.append(code)
    try:
        p = load("https://t.example/", ignore=True, fetch=_fetch_ok)
    finally:
        R.log = real
    if p.respected or p.source != "ignored":
        fail(f"--ignore-robots дал {p.source!r}/respected={p.respected!r}")
    if not p.allows("https://t.example/download-secure"):
        fail("флаг попросили — а запрет всё равно применён")
    if "run.robots_ignored" not in seen:
        fail(f"выбор человека не записан в журнал: {seen}")
    print("  ok  отказ от правил — выбор человека, и он записан")


def test_a_file_target_has_nothing_to_respect():
    p = load("file:///opt/x/site/index.html", fetch=_fetch_ok)
    if p.source != "not_applicable" or not p.allows("file:///opt/x/site/a.html"):
        fail(f"у file:// политика вышла {p.source!r} — сети там нет и запретов быть не может")
    print("  ok  у file:// соблюдать нечего")


# --- поведенчески: исключённое доезжает до артефакта ----------------------------------------------
PAGES = {"https://t.example/": [("b1", "Кнопка")]}
LINKS = {"https://t.example/": ["https://t.example/contact",
                                "https://t.example/download-secure",
                                "https://t.example/notes/api/list"]}

# ADR-135: то, что страница открыла у себя САМА — второй источник фронтира. Ни один из этих адресов
# НЕ встречается в LINKS, и в этом весь смысл: до PR-2 запрет владельца применялся физически внутри
# цикла по якорям, поэтому проверить его на другом источнике было нечем. Один адрес разрешён, один
# запрещён — пара, без которой утверждение удовлетворяется каналом, не пропускающим НИЧЕГО.
#
# ⚠ ЗАПРЕТ ЗДЕСЬ — ПО ПУТИ, А НЕ ПО ФРАГМЕНТУ, и это не выбор оформления. `robots.txt` говорит о
# ресурсах, которые отдаёт сервер; hash-маршрут на сервер не уходит, и `RobotsPolicy.allows` судит по
# `urlsplit(...).path`. Гейт, написанный на `#/…`-маршрутах, не смог бы получить вердикт `robots`
# НИКОГДА и был бы зелен по построению — ровно тот вакуум, ради которого этот файл существует.
ROUTES = {"https://t.example/": [
    {"url": "https://t.example/panel/reports", "ts": 1, "how": "push"},
    {"url": "https://t.example/notes/api/list/2", "ts": 2, "how": "replace"},
]}


# Вербы, которые эта реплика умеет. Перечень существует ровно затем, чтобы отсутствие в нём было
# ОТКАЗОМ: см. комментарий в `call`.
_KNOWN_VERBS = {"browser.navigate", "browser.currentUrl", "browser.snapshot", "browser.interactives",
                "browser.links", "browser.click", "browser.perceptionAudit", "browser.routes"}


class FakeEx:
    def __init__(self):
        self.url = "https://t.example/"
        self.route_calls = 0
        # Копия на прогон: журнал СЪЕДАЕТСЯ чтением, и общий на два прогона он сделал бы второй
        # прогон зависящим от первого — а второй тут и есть встречный случай (`--ignore-robots`).
        self.routes = {k: list(v) for k, v in ROUTES.items()}

    def call(self, m, **p):
        # ⚠ НЕИЗВЕСТНЫЙ ВЕРБ — ОТКАЗ, А НЕ ПУСТОЙ СЛОВАРЬ. Прежний `return {}` в конце делал этот
        # фейк согласным на что угодно: узел графа, начавший звать верб, которого фейк не знает,
        # получал пустоту, `.get(...)` давал пустой список, и гейт оставался ЗЕЛЁНЫМ над механизмом,
        # которого в замере нет вовсе. Замерено на этом самом файле: проводка второго источника
        # фронтира была внесена целиком, и все четыре гейта обхода прошли, не заметив.
        if m.startswith("browser.") and m not in _KNOWN_VERBS:
            raise AssertionError(
                f"граф зовёт {m}, а фейк исполнителя такого верба не знает. Это не поломка теста: "
                f"это единственное место, где видно, что замер перестал мерить то, что произошло")
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url, "status": 200, "timing": None}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- document", "nodeCount": 1}
        if m == "browser.interactives":
            return {"elements": [{"role": "button", "name": n, "testid": None, "text": n, "id": i,
                                  "locator": {"testid": i}, "alternatives": [], "disabled": False,
                                  "visible": True} for i, n in PAGES.get(self.url, [])]}
        if m == "browser.links":
            return {"links": [{"href": h, "text": h} for h in LINKS.get(self.url, [])]}
        if m == "browser.click":
            return {"clicked": True, "url": self.url}
        if m == "browser.perceptionAudit":
            return {"ratio": 1.0, "total": 1, "addressable": 1}
        if m == "browser.routes":
            # Отдаёт И ОЧИЩАЕТ — как настоящий верб. Если бы реплика отдавала журнал на каждом шаге,
            # утверждение «адрес пришёл ИМЕННО из журнала» удовлетворялось бы и кодом, читающим его
            # один раз, и кодом, читающим бесконечно, — то есть не различало бы их.
            out = self.routes.pop(self.url, [])
            self.route_calls += 1
            return {"routes": out, "dropped": 0, "journal": True}
        raise AssertionError(f"фейк не знает верба {m} — см. _KNOWN_VERBS")


def _walk(policy):
    art = tempfile.mkdtemp(prefix="robots-")
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    eventlog.reset_degradations()
    target = "https://t.example/"
    init = {"step_id": 1, "intent": "navigate", "semantic_id": semantic_id(target, "navigate", ""),
            "action_type": "navigate", "target": target, "locator": None, "alternatives": None,
            "is_milestone": True}
    st = {"run_id": "rb", "run_mode": "explore", "target_url": target,
          "base_origin": base_origin_of(target), "coverage_target": 0.85, "artifact_dir": art,
          "goal": "", "describe": "", "site_map": {}, "phase": "explore", "scenario_steps": [],
          "scenario_unmatched": [], "current_url": target, "page_model": {},
          "exploration_plan": [init], "plan_hash": "", "current_step": 1, "interactive_seen": [],
          "interactive_exercised": [], "visited_paths": [], "nav_frontier": [],
          "robots_excluded": [], "coverage_achieved": 0.0, "exploration_complete": False,
          "max_steps": 6, "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}],
          "errors": []}
    app = build_graph(FakeEx(), HeuristicPlanner(), lambda r: None,
                      robots=policy).compile(checkpointer=MemorySaver())
    with contextlib.redirect_stdout(io.StringIO()):
        final = app.invoke(st, config={"recursion_limit": 60, "configurable": {"thread_id": "rb"}})
    # ⚠ ФИНАЛЬНОЕ СОСТОЯНИЕ ВОЗВРАЩАЕТСЯ ВМЕСТЕ С АРТЕФАКТОМ. В `plan.json` фронтир не пишется —
    # только его ДЛИНА, — поэтому утверждение «этот адрес во фронтир не попал» из артефакта
    # недоказуемо: длина совпадёт у любого набора той же мощности.
    return json.load(open(os.path.join(art, "plan.json"))), final


def test_what_was_excluded_is_written_down():
    """ГЛАВНОЕ утверждение файла, и оно ПАРНОЕ.

    Половина про «не пошли» удовлетворяется политикой, запрещающей всё. Половина про «сказали»
    удовлетворяется перечнем, в который пишут что попало. Вместе они требуют, чтобы в артефакте
    оказались ИМЕННО запрещённые адреса — и чтобы разрешённый остался во фронтире."""
    plan, _final = _walk(load("https://t.example/", fetch=_fetch_ok))
    r = plan.get("robots")
    if not r:
        fail("в plan.json нет блока robots — исключённое снова неотличимо от ненайденного")
        return
    ex = set(r.get("excluded") or [])
    want = {"https://t.example/download-secure", "https://t.example/notes/api/list",
            "https://t.example/notes/api/list/2"}
    if ex != want:
        fail(f"в артефакте исключено {sorted(ex)}, ожидалось {sorted(want)}")
    if r.get("respected") is not True or r.get("source") != "fetched":
        fail(f"блок не описывает, откуда правила: {r}")
    # ⚠ ВСТРЕЧНОЕ УТВЕРЖДЕНИЕ, КОТОРОЕ ЗДЕСЬ ОБЕЩАЛОСЬ КОММЕНТАРИЕМ И НЕ ДЕЛАЛОСЬ. Множество
    # `seen_paths` вычислялось и не использовалось ни разу — то есть «не пошли» держалось на одном
    # только перечне исключённого, а перечень удовлетворяется кодом, который пишет в него что попало
    # и ходит куда хочет. Теперь запрещённый адрес обязан ОТСУТСТВОВАТЬ среди целей навигации.
    seen_paths = {s.get("target") for s in (plan.get("steps") or [])}
    went_where_forbidden = seen_paths & ex
    if went_where_forbidden:
        fail(f"обход СХОДИЛ по запрещённым адресам {sorted(went_where_forbidden)}, "
             f"хотя перечень исключённого утверждает обратное")
    if "https://t.example/contact" in ex:
        fail("разрешённый адрес попал в исключённые")

    # И вторая половина пары: без политики блок молчит, а фронтир полон.
    plan2, _f2 = _walk(load("https://t.example/", ignore=True, fetch=_fetch_ok))
    r2 = plan2.get("robots") or {}
    if r2.get("respected") is not False or r2.get("excluded"):
        fail(f"с --ignore-robots блок обязан говорить respected=false и НИЧЕГО не исключать: {r2}")
    print(f"  ok  исключённое в артефакте: {sorted(ex)} · с флагом — пусто")


def test_the_second_source_of_the_frontier_goes_through_the_same_gate():
    """ADR-135. Запрещённый адрес приходит НЕ ИЗ ЯКОРЕЙ — и всё равно не берётся.

    ⚠ ДО ЭТОГО ГЕЙТА ТАКОГО СЛУЧАЯ НЕ БЫЛО НИ ОДНОГО во всём репозитории, и это не пробел
    покрытия, а прямое следствие устройства: до ADR-134 запрет владельца и граница обхода
    применялись физически ВНУТРИ цикла по `browser.links`, поэтому проверить их на другом источнике
    было технически нечем — другого источника не существовало. ADR-134 вынес решение в
    `admit_to_frontier` и предсказал дословно: «любой новый канал фронтира обойдёт оба фильтра, а
    `test_robots_offline.py` останется зелёным — он кормит граф исключительно якорями».

    Утверждение ПАРНОЕ, потому что непарное удовлетворяется каналом, который не пропускает ничего:
      · разрешённый маршрут из журнала обязан ОКАЗАТЬСЯ во фронтире — иначе второго источника нет;
      · запрещённый обязан оказаться в перечне исключённого и НЕ оказаться во фронтире.
    """
    plan, final = _walk(load("https://t.example/", fetch=_fetch_ok))
    frontier = set(final.get("nav_frontier") or [])
    visited = set(final.get("visited_paths") or [])
    allowed = page_identity("https://t.example/panel/reports")
    denied = page_identity("https://t.example/notes/api/list/2")

    if allowed not in frontier | visited:
        fail(f"разрешённый маршрут из журнала не дошёл ни до фронтира, ни до посещённых "
             f"({sorted(frontier)}) — второго источника фронтира нет, и следующее утверждение "
             f"было бы зелёным просто потому, что канал пуст")
    if denied in frontier | visited:
        fail(f"ЗАПРЕЩЁННЫЙ маршрут {denied} прошёл во фронтир мимо ворот — воля владельца сайта "
             f"применяется не ко всем источникам")
    if denied not in set((plan.get("robots") or {}).get("excluded") or []):
        fail(f"запрещённый маршрут из журнала не назван в перечне исключённого: "
             f"{(plan.get('robots') or {}).get('excluded')}")
    print("  ok  журнал маршрутов ходит через те же ворота: разрешённый взят, запрещённый назван")


def _checks():
    """Перечень проверок ВЫВОДИТСЯ из модуля, а не переписывается руками.

    ⚠ Рукописный кортеж, стоявший здесь, — это список того, что надо покрыть, внутри самой проверки:
    ровно то, что запрещает `docs/DEVELOPMENT.md` §0, принцип 5. Отказывает он молча в одну сторону:
    лишнее имя роняет прогон и потому заметно, а ЗАБЫТОЕ представления не имеет — функция просто не
    исполняется, и файл рапортует успех. Пол на число — обязательный спутник вывода: обход,
    переставший что-либо находить, прошёл бы идеально над пустым множеством.
    """
    found = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    if len(found) < 7:
        fail(f"в модуле найдено {len(found)} проверок — было семь; либо их стало меньше, "
             f"либо вывод перечня сломался и дальше он молча пройдёт над пустым множеством")
    return [f for _, f in found]


def main() -> int:
    for fn in _checks():
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("robots: OK (правила читаются, соблюдаются, исключённое записано, отказ — выбор человека)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
