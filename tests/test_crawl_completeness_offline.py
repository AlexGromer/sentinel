#!/usr/bin/env python3
"""Обход говорит, ПРОШЁЛ ли он весь сайт, и не теряет найденное, когда падает.

Run:  .venv/bin/python tests/test_crawl_completeness_offline.py

ДВА ДЕФЕКТА, КОТОРЫЕ ЭТО ЗАКРЫВАЕТ, и оба замерены на живых целях 2026-08-23.

**Первый — обход врал о полноте.** На `the-internet` он упёрся в потолок шагов и записал
`coverage_achieved: 1.0`. Обе цифры верны по отдельности и вместе врут: покрытие считается долей от
`interactive_seen`, а `seen` — это только то, что успели УВИДЕТЬ, поэтому оборванный обход легко даёт
единицу, когда за краем осталось тридцать страниц фронтира. Поля «обход неполон» не существовало
нигде: `exploration_complete` — булев защёлк, ставящийся ОДИНАКОВО при сходимости, потолке, пустых
кандидатах и остановке оркестратором, а `reason`, который причину знал, уезжал только в
`llm-transcript.jsonl` — файл, не входящий в перечень отдаваемых артефактов.

**Второй — падение отбрасывало всю работу.** Исключение на 46-м шаге теряло 45 предыдущих: `plan.json`
пишет узел `report` в конце графа, до которого не доходили, а копию состояния тут же удаляли вместе с
чекпоинтом. Человек получал exit 4 и пустой каталог.

⚠ ПОЧЕМУ ГЕЙТ БЕЗ БРАУЗЕРА. Проверяются два свойства ГРАФА — что он объявляет о своей полноте и что
спасение собирает план из состояния, — а не то, как выглядит страница. Исполнитель здесь фейковый и
управляемый: только так можно потребовать падения на ЗАДАННОМ шаге, а живой браузер такого не обещает.
Устойчивость к настоящей странице проверяет tests/test_crawl_survives_any_page_offline.py на фикстуре.
"""
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
from brain.graph import build_graph                           # noqa: E402
from brain.planner import HeuristicPlanner                    # noqa: E402
from brain.state import base_origin_of, normalize_url, semantic_id  # noqa: E402
from brain import budget                                      # noqa: E402
from brain import eventlog                                    # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


PAGES = {
    "http://t/": [("b1", "Кнопка 1"), ("b2", "Кнопка 2")],
    "http://t/a": [("b3", "Кнопка 3")],
    "http://t/b": [("b4", "Кнопка 4")],
    "http://t/c": [],
}
LINKS = {
    "http://t/": ["http://t/a", "http://t/b", "http://t/c"],
    "http://t/a": ["http://t/"],
    "http://t/b": ["http://t/"],
    "http://t/c": ["http://t/"],
}


class FakeEx:
    """Поверхность исполнителя, которой пользуется граф обхода. `break_at` заставляет снимок бросить
    на заданном вызове — так воспроизводится падение посреди обхода, не трогая настоящий браузер."""

    def __init__(self, break_at: "int | None" = None):
        self.url = "http://t/"
        self.snaps = 0
        self.break_at = break_at

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url, "status": 200, "timing": None}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            self.snaps += 1
            if self.break_at is not None and self.snaps >= self.break_at:
                raise RuntimeError("browser.snapshot: FAKE — broken on purpose at call %d" % self.snaps)
            return {"ariaSnapshot": "- document", "nodeCount": 1}
        if m == "browser.interactives":
            return {"elements": [
                {"role": "button", "name": n, "testid": None, "text": n, "id": i,
                 "locator": {"testid": i}, "alternatives": [], "disabled": False, "visible": True}
                for i, n in PAGES.get(self.url, [])]}
        if m == "browser.links":
            return {"links": [{"href": h, "text": h} for h in LINKS.get(self.url, [])]}
        if m == "browser.click":
            return {"clicked": True, "url": self.url}
        if m == "browser.perceptionAudit":
            return {"ratio": 1.0, "total": 1, "addressable": 1}
        return {}


def _run(max_steps: int, break_at: "int | None" = None) -> "tuple[dict, str]":
    """Прогнать НАСТОЯЩИЙ граф обхода и вернуть (что записано в plan.json, каталог артефакта)."""
    art = tempfile.mkdtemp(prefix="crawl-complete-")
    target = "http://t/"
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    # ⚠ Счётчик деградаций живёт НА МОДУЛЕ («один процесс brain — один прогон»), а здесь в одном
    # процессе прогоняется несколько. Без сброса второй прогон унаследовал бы коды первого, и
    # встречное утверждение «чистый обход не объявляет деградаций» проходило бы или падало по
    # порядку тестов, а не по поведению.
    eventlog.reset_degradations()
    ex = FakeEx(break_at=break_at)
    init = {"step_id": 1, "intent": f"navigate to target {target}",
            "semantic_id": semantic_id(normalize_url(target), "navigate", ""),
            "action_type": "navigate", "target": normalize_url(target),
            "locator": None, "alternatives": None, "is_milestone": True}
    st = {"run_id": "cc", "run_mode": "explore", "target_url": target,
          "base_origin": base_origin_of(target), "coverage_target": 0.85, "artifact_dir": art,
          "goal": "", "describe": "", "site_map": {}, "phase": "explore",
          "scenario_steps": [], "scenario_unmatched": [], "current_url": target, "page_model": {},
          "exploration_plan": [init], "plan_hash": "", "current_step": 1,
          "interactive_seen": [], "interactive_exercised": [], "visited_paths": [],
          "nav_frontier": [], "coverage_achieved": 0.0, "exploration_complete": False,
          "max_steps": max_steps,
          "executed_actions": [{"step_id": 1, "type": "navigate", "ok": True}], "errors": []}
    app = build_graph(ex, HeuristicPlanner(), lambda r: None).compile(checkpointer=MemorySaver())
    cfg = {"recursion_limit": max(60, max_steps * 8), "configurable": {"thread_id": "cc"}}
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            app.invoke(st, config=cfg)
        except Exception as crash:
            from brain.__main__ import _salvage_explore
            _salvage_explore(app, cfg, pathlib.Path(art), "cc", target, crash)
    p = os.path.join(art, "plan.json")
    return (json.load(open(p)) if os.path.exists(p) else {}), art


def test_a_crawl_that_hit_the_ceiling_says_so():
    plan, _ = _run(max_steps=3)
    c = plan.get("completeness")
    if not c:
        fail("в plan.json нет блока completeness — обход снова молчит о своей полноте")
        return
    if c.get("complete") is not False:
        fail(f"обход, упёршийся в потолок, объявил себя полным: {c}")
    if c.get("reason") != "max_steps":
        fail(f"причина остановки не названа потолком: {c.get('reason')!r}")
    # ⚠ Остаток фронтира — это и есть «сколько осталось непройденного». Без него `complete: false`
    # говорит, что чего-то не хватает, и не говорит, сколько именно.
    if not c.get("frontier_left"):
        fail(f"фронтир объявлен пустым, хотя обход оборван на потолке: {c}")
    # Встречное: покрытие при этом может быть каким угодно, и это ровно та цифра, которая врала.
    print(f"  ok  потолок: complete={c['complete']} reason={c['reason']} "
          f"frontier_left={c['frontier_left']} coverage={plan.get('coverage_achieved')}")


def test_a_crawl_that_finished_says_that_too():
    """Встречное утверждение. Без него «complete всегда false» удовлетворяет тест выше идеально."""
    plan, _ = _run(max_steps=60)
    c = plan.get("completeness") or {}
    if c.get("complete") is not True:
        fail(f"обход, прошедший сайт до конца, не объявил себя полным: {c}")
    if c.get("reason") != "converged":
        fail(f"законченный обход назвал причиной {c.get('reason')!r}, а не сходимость")
    if c.get("frontier_left"):
        fail(f"сошедшийся обход оставил непустой фронтир: {c}")
    print(f"  ok  сходимость: complete={c.get('complete')} reason={c.get('reason')}")


def test_a_crawl_that_crashed_keeps_what_it_found():
    plan, art = _run(max_steps=60, break_at=3)
    if not plan:
        fail("обход упал и не оставил plan.json — вся работа снова потеряна")
        return
    steps = plan.get("steps") or []
    if len(steps) < 2:
        fail(f"спасённый план содержит {len(steps)} шаг(ов) — спасать было что, но не спасли")
    c = plan.get("completeness") or {}
    if c.get("complete") is not False or c.get("reason") != "aborted":
        fail(f"спасённый план не объявил себя оборванным: {c}")
    # ⚠ САМА ОШИБКА обязана лежать рядом. Без неё «aborted» неотличимо от «дошли до потолка», и
    # читатель не узнает, обо что именно споткнулся инструмент.
    if "broken on purpose" not in (c.get("error") or ""):
        fail(f"причина обрыва не записана в артефакт: {c.get('error')!r}")
    if not os.path.exists(os.path.join(art, "site-map.json")):
        fail("карта сайта не спасена — авторить тест будет не по чему")
    # ⚠ Ключ `degradations` здесь ПУСТ, и это правильно — утверждение о его содержимом живёт в
    # tests/test_artifact_retention_offline.py, где прогон идёт через настоящий `_run_explore`.
    # Причина не косметическая: `explore.crashed` произносит ВЫЗЫВАЮЩИЙ перед спасением, а этот
    # гейт зовёт `_salvage_explore` руками и потому его не производит. А `explore.salvaged` в файл
    # о себе попасть не может по построению: он сообщает ИСХОД записи и потому произносится после
    # неё — событие об операции, сказанное до операции, уже стоило нам строки «45 шагов сохранены»
    # над пустым каталогом.
    if plan.get("degradations") is None:
        fail("в спасённом plan.json нет ключа degradations вовсе")
    print(f"  ok  спасение: шагов={len(steps)} reason={c.get('reason')} "
          f"error={(c.get('error') or '')[:48]}…")


def test_a_degraded_crawl_says_so_in_the_artefact():
    """Потерянное качество обхода доезжает до `plan.json`, а не только до журнала.

    ⚠ ЧТО БЫЛО. `eventlog.degradations()` читался ровно в ОДНОМ месте — `replay.py:604`, — поэтому
    деградации попадали в `report.json` повторного прогона и не попадали в артефакт обхода ни разу.
    Каталог всегда знал, какие коды означают потерю качества, и всегда нёс фразу для вердикта;
    читателя с той стороны не было. Обход без ключа к модели, на исчерпанном бюджете или не прошедший
    сайт оставлял файл, который читается как чистый.

    Утверждение ПАРНОЕ, и пара здесь не украшение: «список всегда пуст» удовлетворяет половину про
    чистый обход идеально, а «список всегда полон» — половину про оборванный. Только вместе они
    говорят, что ключ считается, а не проставляется.

    ⚠ И перечень НЕПОЛОН ПО ПОСТРОЕНИЮ: `plan.json` замораживается в узле `report`, а разборка прогона
    идёт после графа, поэтому `system.trace_missing` и события видео в него не попадут никогда. Это
    записано и здесь, и над самой строкой в `graph.py` — иначе следующий читатель примет отсутствие
    за дефект и «починит» его, перенеся запись файла туда, где план уже не заморожен."""
    plan, _ = _run(max_steps=3)
    degr = plan.get("degradations")
    if degr is None:
        fail("в plan.json нет ключа degradations — потерянное качество обхода снова невидимо")
        return
    if "explore.incomplete" not in degr:
        fail(f"обход, оборванный на потолке, не объявил деградацию: {degr}")

    clean, _ = _run(max_steps=60)
    if clean.get("degradations") != []:
        fail(f"чистый обход объявил деградации, которых не было: {clean.get('degradations')!r} — "
             "список, который никогда не пуст, ничего не сообщает")
    print(f"  ok  деградации: оборванный={degr} · чистый={clean.get('degradations')}")


def _run_real(goal: str, max_steps: int):
    """Прогнать НАСТОЯЩИЙ `_run_explore` (не только граф) и вернуть каталог артефакта.

    Отличие от `_run` выше существенно: проводка между узлом `report` и записью сценария живёт
    именно в `_run_explore`, и гейт, зовущий части руками, её не видит."""
    import contextlib
    import importlib
    art = tempfile.mkdtemp(prefix="crawl-goal-")
    os.environ["SENTINEL_LIVE_FRAMES"] = "0"
    os.environ.pop("ORCH_ADDR", None)
    os.environ.pop("DESCRIBE", None)
    os.environ["GOAL"] = goal
    try:
        budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
        eventlog.reset_degradations()
        run_explore = importlib.import_module("brain.__main__")._run_explore
        with contextlib.redirect_stdout(io.StringIO()):
            run_explore(FakeEx(), "goal", pathlib.Path(art), "http://t/", 0.85, max_steps)
    finally:
        os.environ.pop("GOAL", None)
    return pathlib.Path(art)


def test_the_scenario_does_not_claim_a_complete_crawl_when_the_crawl_was_cut_short():
    """Два артефакта одного прогона обязаны говорить одно.

    ⚠ ЧТО БЫЛО. `crawl_complete` заведено ровно затем, чтобы `unmatched` не читался как фантазия
    модели, когда причина в оборванном обходе. Но параметр имеет умолчание `True`, и оба ОБЫЧНЫХ
    пути (`_run_explore` и чат) звали `_write_scenario` без него — то есть поле было истинным именно
    там, где обход и упирается в потолок. В одном каталоге лежали `plan.json` с
    `completeness: {complete: false, reason: "max_steps"}` и `scenario.json` с
    `crawl_complete: true`, и читатель не мог знать, какому верить.

    Утверждение ПАРНОЕ: полнота проверяется и на оборванном прогоне, и на прошедшем до конца.
    Половина про `false` удовлетворяется константой `False`, половина про `true` — константой
    `True`; вместе они требуют, чтобы значение ВЫВОДИЛОСЬ."""
    for label, max_steps, want in (("оборванный потолком", 3, False), ("прошедший до конца", 60, True)):
        art = _run_real("log in and finish", max_steps)
        plan = json.load(open(art / "plan.json"))
        got = (plan.get("completeness") or {}).get("complete")
        if got is not want:
            fail(f"{label}: сам план объявил complete={got!r}, ожидалось {want!r} — фикстура не "
                 "воспроизводит случай, и утверждение ниже проверяло бы не то")
            continue
        for name in ("scenario.json", "reconcile-report.json"):
            q = art / name
            if not q.exists():
                fail(f"{label}: {name} не записан — сценарий goal-прогона обязан существовать")
                continue
            said = json.load(open(q)).get("crawl_complete")
            if said is not want:
                fail(f"{label}: {name} говорит crawl_complete={said!r}, а plan.json — "
                     f"completeness.complete={want!r}. Два артефакта одного прогона противоречат друг другу")
        print(f"  ok  {label}: plan={want} · scenario={json.load(open(art / 'scenario.json')).get('crawl_complete')}")


def main() -> int:
    for fn in (test_a_crawl_that_hit_the_ceiling_says_so,
               test_a_crawl_that_finished_says_that_too,
               test_a_crawl_that_crashed_keeps_what_it_found,
               test_a_degraded_crawl_says_so_in_the_artefact,
               test_the_scenario_does_not_claim_a_complete_crawl_when_the_crawl_was_cut_short):
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("crawl completeness: OK (потолок, сходимость и спасение — каждое объявляет себя своим словом)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
