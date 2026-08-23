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
    print(f"  ok  спасение: шагов={len(steps)} reason={c.get('reason')} "
          f"error={(c.get('error') or '')[:48]}…")


def main() -> int:
    for fn in (test_a_crawl_that_hit_the_ceiling_says_so,
               test_a_crawl_that_finished_says_that_too,
               test_a_crawl_that_crashed_keeps_what_it_found):
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
