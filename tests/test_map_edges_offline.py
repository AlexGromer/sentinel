"""Offline gate: карта несёт рёбра, и два их вида не смешаны (ADR-150, первая половина W13).

Run:  .venv/bin/python tests/test_map_edges_offline.py

Карта отвечала на «что на странице есть» и молчала на «как отсюда попасть туда»: элемент нёс
`role/name/locator/semantic_id`, и ни одного поля про то, куда ведёт контрол. Из-за этого модель в
рассуждении пишет «actions page might be page-b… but how to get there?» и уходит в логин, а прогон
при этом зелёный, потому что `unmatched` меряет существование ref, а не достижение цели.

⚠ Обе половины ребра ОБХОД УЖЕ ДОБЫВАЛ и обе выбрасывал:

  НАБЛЮДЁННОЕ (`leads_to`) — `act` на клике знает элемент, страницу откуда и адрес приземления
      (исполнитель отдаёт `navigated` и `url`). В состояние уходил только `current_url`.
  ОБЪЯВЛЕННОЕ (`href_to`) — `browser.links` отдаёт `{href, text}` каждого якоря. Шло только во
      фронтир; как ребро выбрасывалось.

Поля РАЗНЫЕ намеренно. Наблюдение — факт, разметка — обещание, и расходятся они ровно там, где
интересно: перехваченный клик, редирект, роутер SPA. Слить их значило бы выдать обещание за факт.

Замер на `testdata/site`: клик увёл ОДИН раз из 14 элементов, а якорей семь — и среди них
единственный путь к `page-b`. Поэтому нужны оба вида, а не «более честный» один.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        return 1
    print(f"  ok   {name}")
    return 0


def _page(els):
    return {"file:///p": els}


def test_observed_edge_is_applied_to_the_control_that_made_it():
    """`ground` применяет ребро, которое `act` наблюдал, — к тому элементу, а не к странице."""
    from brain.graph import _apply_observed_edge
    bad = 0
    site = _page([{"semantic_id": "aaa", "name": "Go", "role": "button"},
                  {"semantic_id": "bbb", "name": "Other", "role": "button"}])
    out = _apply_observed_edge(dict(site), {"from": "file:///p", "ref": "aaa", "to": "file:///q"})
    els = {e["semantic_id"]: e for e in out["file:///p"]}
    bad += check("ребро легло на кликнутый контрол", els["aaa"].get("leads_to") == "file:///q",
                 f"got {els['aaa'].get('leads_to')!r}")
    # ⚠ Узкая половина: соседний контрол не должен получить ничего. Реализация «проставить всем на
    # странице» прошла бы утверждение выше и была бы ложью про каждый второй элемент.
    bad += check("соседний контрол ребра НЕ получил", "leads_to" not in els["bbb"],
                 f"got {els['bbb'].get('leads_to')!r}")

    # Свежее наблюдение вернее прежнего: приложение могло сменить маршрут под тем же контролом.
    out2 = _apply_observed_edge(out, {"from": "file:///p", "ref": "aaa", "to": "file:///z"})
    # `.get`, а не `[...]`: реализация, которая ребро вовсе не пишет, должна дать НАЗВАННЫЙ провал,
    # а не KeyError — гейт, падающий трейсбеком на дефекте, который он проверяет, не называет ни
    # одной ассерции (замерено при мутировании этого же файла и в PR-3 до него).
    bad += check("повторное наблюдение перезаписывает прежнее",
                 {e["semantic_id"]: e for e in out2["file:///p"]}["aaa"].get("leads_to") == "file:///z")

    # Ребро на неизвестный ref не должно ни падать, ни выдумывать элемент.
    before = json.dumps(out2, sort_keys=True)
    out3 = _apply_observed_edge(out2, {"from": "file:///p", "ref": "ZZZ", "to": "file:///q"})
    bad += check("ребро на неизвестный ref ничего не меняет", json.dumps(out3, sort_keys=True) == before)
    return bad


def test_declared_edge_only_when_it_cannot_be_wrong():
    """`href_to` ставится сопоставлением ПО ТЕКСТУ — то есть выводом, а не наблюдением, — поэтому
    только когда ошибиться нечем. Молчание честнее догадки: пустое поле читается как «неизвестно»,
    неверное — как факт."""
    from brain.graph import _apply_declared_edges
    bad = 0
    els = [{"semantic_id": "a1", "name": "Alpha", "role": "link"},
           {"semantic_id": "b1", "name": "Beta", "role": "link"}]
    out = _apply_declared_edges(_page(list(els)), "file:///p",
                                [{"text": "Alpha", "href": "file:///a"},
                                 {"text": "Beta", "href": "file:///b"}])
    got = {e["name"]: e.get("href_to") for e in out["file:///p"]}
    bad += check("однозначный якорь даёт объявленное ребро",
                 got == {"Alpha": "file:///a", "Beta": "file:///b"}, f"got {got}")

    # ⚠ ДВА якоря с одним текстом — сопоставлять не с чем.
    out = _apply_declared_edges(_page([{"semantic_id": "d1", "name": "Home", "role": "link"}]), "file:///p",
                                [{"text": "Home", "href": "file:///x"}, {"text": "Home", "href": "file:///y"}])
    bad += check("неоднозначный ЯКОРЬ ребра не даёт",
                 "href_to" not in out["file:///p"][0], f"got {out['file:///p'][0].get('href_to')!r}")

    # ⚠ ...и симметрично: два ЭЛЕМЕНТА с одним именем.
    out = _apply_declared_edges(_page([{"semantic_id": "e1", "name": "Home", "role": "link"},
                                       {"semantic_id": "e2", "name": "Home", "role": "link"}]),
                                "file:///p", [{"text": "Home", "href": "file:///x"}])
    bad += check("неоднозначный ЭЛЕМЕНТ ребра не даёт",
                 all("href_to" not in e for e in out["file:///p"]))

    # Не-ссылка не получает объявленного ребра: у кнопки нет href, и совпадение текста тут случайно.
    out = _apply_declared_edges(_page([{"semantic_id": "f1", "name": "Alpha", "role": "button"}]),
                                "file:///p", [{"text": "Alpha", "href": "file:///a"}])
    bad += check("кнопка не получает объявленного ребра", "href_to" not in out["file:///p"][0])
    return bad


def test_the_two_kinds_are_not_merged():
    """Наблюдение и объявление живут в РАЗНЫХ полях — иначе обещание разметки читалось бы как факт."""
    from brain.graph import _apply_declared_edges, _apply_observed_edge
    bad = 0
    site = _page([{"semantic_id": "a1", "name": "Alpha", "role": "link"}])
    site = _apply_declared_edges(site, "file:///p", [{"text": "Alpha", "href": "file:///declared"}])
    site = _apply_observed_edge(site, {"from": "file:///p", "ref": "a1", "to": "file:///observed"})
    el = site["file:///p"][0]
    bad += check("наблюдённое и объявленное расходятся и ОБА видны",
                 el.get("leads_to") == "file:///observed" and el.get("href_to") == "file:///declared",
                 f"leads_to={el.get('leads_to')!r} href_to={el.get('href_to')!r}")
    return bad


def test_the_seam_holds_schema_and_prompt_unchanged():
    """⚠ САМАЯ ЦЕННАЯ ПРОВЕРКА ЭТОГО ФАЙЛА, и она про то, чего в карте НЕ произошло.

    Первая половина W13 обязана быть бесплатной для контракта, и это держится на двух фактах,
    замеренных до кода: форма карты `{адрес: [элемент, …]}` читается СЕМЬЮ местами продукта, а меню
    для модели (`build_scenario`) собирается ЯВНЫМ перечнем четырёх полей. Пока оба верны, новое
    поле у элемента не доходит ни до промпта, ни до `plan_hash`, ни до голденов.

    Обе половины утверждаются здесь, потому что сломать их можно порознь и молча: объект страницы
    вместо списка уронит семь читателей, а `{**el}` в меню утащит новые поля в промпт — и эталонные
    хеши поедут без единой строки про них в диффе."""
    import inspect
    from brain import planner, scenario
    bad = 0

    # Форма карты: значение страницы — СПИСОК. Утверждается поведенчески, через реального читателя.
    site = _page([{"semantic_id": "a1", "name": "Alpha", "role": "link", "href_to": "file:///a"}])
    flat = scenario.flatten_site_map(site)
    bad += check("карта осталась {адрес: [элемент]} — читатель работает", len(flat) == 1 and flat[0]["semantic_id"] == "a1")

    src = inspect.getsource(planner.GoalPlanner.build_scenario)
    # Срез до `for e in`, а НЕ до первой `]`: первая скобка закрывает `e["semantic_id"]`, и наивный
    # срез обрывал меню на четверти — проверка ниже проходила бы над куском, где полей ещё нет.
    _b = src.index("menu = [")
    menu = src[_b:src.index("for e in", _b)]
    for field in ("leads_to", "href_to"):
        bad += check(f"меню модели НЕ несёт {field} — промпт не тронут", field not in menu,
                     "новое поле дошло до промпта: plan_hash и голдены поедут")
    # Пол: меню обязано по-прежнему собираться перечнем, а не `{**e}`. Без этого проверка выше
    # проходит над реализацией, которая утащит в промпт всё, включая поля, добавленные завтра.
    bad += check("меню собирается ПЕРЕЧНЕМ полей, а не копией элемента",
                 "**e" not in menu and menu.count('e.get(') >= 3, f"menu={menu[:120]!r}")
    return bad


def main():
    bad = 0
    print("наблюдённое ребро:")
    bad += test_observed_edge_is_applied_to_the_control_that_made_it()
    print("объявленное ребро:")
    bad += test_declared_edge_only_when_it_cannot_be_wrong()
    print("два вида не слиты:")
    bad += test_the_two_kinds_are_not_merged()
    print("шов (схема и промпт не тронуты):")
    bad += test_the_seam_holds_schema_and_prompt_unchanged()
    if bad:
        print(f"\nmap edges: {bad} FAILURE(S)")
        return 1
    print("\nmap edges: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
