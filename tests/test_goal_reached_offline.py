"""Offline gate: прогон говорит, ДОСТИГ ЛИ он цели (ADR-152, волна W14).

Run:  .venv/bin/python tests/test_goal_reached_offline.py

ЗАЧЕМ. Заземление отвечает «нашлись ли элементы», согласованность (ADR-151) — «следует ли сценарий
за собственными действиями». Ни то, ни другое не отвечает на вопрос, ради которого прогон запускали:
ПРИШЛИ ЛИ МЫ ТУДА, КУДА ШЛИ. Замерено живой моделью (`qwen3:8b`, `testdata/site`, 2026-09-04), три
прогона: «Open the page C» дошёл до `page-c`; «View the actions page» дважды из двух ушёл в логин на
`page-a`, ни разу не добравшись до страницы действий. **У всех трёх вердикт побайтово одинаков** —
`pass`, exit 0, `unmatched=0`, — и различить их по выводу продукта было нечем.

КОНСТРУКЦИЯ, И ГЕЙТ УТВЕРЖДАЕТ ИМЕННО ЕЁ: сверяются ДВЕ НЕЗАВИСИМЫЕ величины. `goal_page` —
НАМЕРЕНИЕ, спрошенное у модели ОТДЕЛЬНЫМ вызовом, где она не видит собственных шагов; след страниц —
НАБЛЮДЕНИЕ, выведенное нашим кодом из шагов. Спросить и то и другое одним ответом было бы копией,
соглашающейся сама с собой, и проверка подтверждалась бы при любой ошибке. Поэтому здесь есть
утверждение о том, что схема шагов НЕ несёт целевой страницы: это единственное место, где такой
возврат заметен, а стоит он ровно всей ценности проверки.

⚠ ПЕРВАЯ РЕДАКЦИЯ ОШИБАЛАСЬ, И ГЕЙТ ДЕРЖИТ ИМЕННО ЭТОТ СЛУЧАЙ. След страниц строился по одним
`navigate`, а `_emit` вставляет их только когда СЛЕДУЮЩИЙ шаг лежит на другой странице. У сценария,
заканчивающегося кликом, следующего шага нет — и страница, до которой он дошёл ПОСЛЕДНИМ действием,
в след не попадала никогда. На живом прогоне «Open the page C» это давало `not_reached` над
сценарием, который цели ДОСТИГ: ложное обвинение ровно того класса, ради которого заводился третий
исход. Лечится вторым источником следа — ребром элемента (ADR-150).

ЭТО ОБЪЯВЛЕНИЕ, А НЕ ПРИГОВОР, и гейт утверждает в том числе это: код выхода не зависит от
достижения цели, потому что цель — направление, а не спецификация (HEALTH-004).
"""
import json
import os
import pathlib
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.scenario import goal_reached, pages_visited  # noqa: E402
from brain.state import canonical_plan_hash  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        return 1
    print(f"  ok   {name}")
    return 0


IDX = "file:///s/index.html"
PA = "file:///s/page-a.html"
PB = "file:///s/page-b.html"
PC = "file:///s/page-c.html"

# Карта по образцу `testdata/site`, снятая с настоящего прогона: `page-c` достижима ДВУМЯ путями
# (через `page-a` и через `page-b`), а кнопка «Sign in» несёт НАБЛЮДЁННОЕ ребро — она уводит,
# хотя якоря у неё нет. И то и другое — свойства живой фикстуры, а не удобные выдумки.
SITE = {
    IDX: [{"semantic_id": "alpha", "role": "link", "name": "Alpha", "page": IDX, "href_to": PA},
          {"semantic_id": "beta", "role": "link", "name": "Beta", "page": IDX, "href_to": PB},
          {"semantic_id": "start", "role": "button", "name": "Get started", "page": IDX}],
    PA: [{"semantic_id": "user", "role": "textbox", "name": "User", "page": PA},
         {"semantic_id": "pass", "role": "textbox", "name": "Password", "page": PA},
         {"semantic_id": "signin", "role": "button", "name": "Sign in", "page": PA, "leads_to": PC},
         {"semantic_id": "a2c", "role": "link", "name": "To C", "page": PA, "href_to": PC}],
    PB: [{"semantic_id": "act1", "role": "button", "name": "Action One", "page": PB},
         {"semantic_id": "b2c", "role": "link", "name": "To C", "page": PB, "href_to": PC}],
    PC: [{"semantic_id": "fin", "role": "button", "name": "Finish", "page": PC}],
}


def nav(i, to):
    return {"step_id": i, "action_type": "navigate", "target": to, "semantic_id": f"nav{i}"}


def act(i, sid, verb="click"):
    return {"step_id": i, "action_type": verb, "semantic_id": sid, "locator": {}}


def test_last_click_counts_as_arrival():
    """Сценарий, ЗАКАНЧИВАЮЩИЙСЯ кликом, доходит до страницы, куда этот клик ведёт.

    Точная форма живого прогона «Open the page C» (2026-09-04). KILLS: возврат следа к одним только
    целям `navigate` — то есть ровно ту первую редакцию, которая объявляла этот прогон недошедшим.
    """
    steps = [nav(1, IDX), act(2, "alpha"), nav(3, PA), act(4, "a2c")]
    r = goal_reached(steps, PC, SITE)
    bad = check("последний клик засчитан приходом", r["verdict"] == "reached", str(r))
    bad += check("страница из ребра попала в след", PC in r["pages_visited"], str(r["pages_visited"]))
    return bad


def test_the_measured_live_defect_is_named():
    """Живой дефект: цель «страница действий», а сценарий уходит в логин. Должно быть `not_reached`.

    Форма снята с прогона `scratch/live-act1`: клик «Get started», переход на `page-a`, два `fill`,
    клик «Sign in» (наблюдённое ребро на `page-c`). До `page-b` сценарий не добирается НИКОГДА, при
    этом прогон зелёный и `unmatched=0`. KILLS: любую редакцию, засчитывающую цель по факту того,
    что сценарий вообще куда-то сходил.
    """
    steps = [nav(1, IDX), act(2, "start"), nav(3, PA),
             act(4, "user", "fill"), act(5, "pass", "fill"), act(6, "signin")]
    r = goal_reached(steps, PB, SITE)
    bad = check("логин вместо страницы действий назван", r["verdict"] == "not_reached", str(r))
    bad += check("целевой страницы в следе нет", PB not in r["pages_visited"], str(r["pages_visited"]))
    return bad


def test_result_matters_not_route():
    """Цель, достижимая НЕСКОЛЬКИМИ путями, засчитывается при ЛЮБОМ (решение Alex).

    И засчитывается, даже если сценарий ПРОШЁЛ через цель и ушёл дальше: важен результат, а не
    маршрут. KILLS: сравнение целевой страницы с КОНЦОМ следа вместо принадлежности следу.
    """
    via_a = [nav(1, IDX), act(2, "alpha"), nav(3, PA), act(4, "a2c")]
    via_b = [nav(1, IDX), act(2, "beta"), nav(3, PB), act(4, "b2c")]
    bad = check("маршрут через page-a засчитан", goal_reached(via_a, PC, SITE)["verdict"] == "reached")
    bad += check("маршрут через page-b засчитан", goal_reached(via_b, PC, SITE)["verdict"] == "reached")
    # побывал на PB и ушёл на PC — цель PB достигнута
    left = [nav(1, IDX), act(2, "beta"), nav(3, PB), act(4, "b2c")]
    bad += check("побывал и ушёл — всё равно достигнута",
                 goal_reached(left, PB, SITE)["verdict"] == "reached", str(goal_reached(left, PB, SITE)))
    return bad


def test_three_outcomes_are_three_not_two():
    """`unknown` НЕ схлопнут в `not_reached`, и три его причины различимы.

    Обвинение по незнанию хуже молчания (урок W13): «модель не назвала страницу» и «сценарий туда не
    дошёл» — разные новости, и читатель, получивший одну вместо другой, сделает разный вывод.
    KILLS: возврат `not_reached` вместо `unknown` в любом из трёх случаев, и слияние причин в одну.
    """
    steps = [nav(1, IDX), act(2, "alpha"), nav(3, PA), act(4, "a2c")]
    not_named = goal_reached(steps, "", SITE)
    not_in_map = goal_reached(steps, "file:///s/nowhere.html", SITE)
    no_trail = goal_reached([act(1, "start")], PC, SITE)
    bad = check("не названа -> unknown", not_named["verdict"] == "unknown", str(not_named))
    bad += check("не из карты -> unknown", not_in_map["verdict"] == "unknown", str(not_in_map))
    bad += check("следа нет -> unknown", no_trail["verdict"] == "unknown", str(no_trail))
    reasons = {not_named["reason"], not_in_map["reason"], no_trail["reason"]}
    bad += check("три причины РАЗЛИЧНЫ", len(reasons) == 3, str(reasons))
    bad += check("ни одна не выдана за `not_reached`",
                 all(r["verdict"] != "not_reached" for r in (not_named, not_in_map, no_trail)))
    return bad


def test_the_intent_is_grounded_like_a_ref():
    """Страница, названная моделью, ЗАЗЕМЛЯЕТСЯ против карты — та же дисциплина, что у `ref`.

    Модель, назвавшая несуществующую страницу, не должна получать ни «дошёл», ни «не дошёл»: мы не
    знаем, куда она нас послала. Замерено в этом дереве на настоящем отказе: модель вернула
    `testdata/site/page-b.html` — строку, которой нет среди `semantic_id` (разбор ADR-148).
    KILLS: снятие проверки принадлежности карте.
    """
    steps = [nav(1, IDX), act(2, "beta"), nav(3, PB)]
    r = goal_reached(steps, "file:///s/invented.html", SITE)
    bad = check("выдуманная страница -> unknown", r["verdict"] == "unknown", str(r))
    bad += check("и причина названа именно эта", r["reason"] == "goal_page_not_in_map", str(r))
    return bad


def test_the_intent_does_not_come_from_the_steps_schema():
    """Целевой страницы НЕТ в схеме ответа, которым авторятся шаги, — и это не косметика.

    Поле `goal_page` рядом со `steps` в одном ответе сделало бы проверку копией, соглашающейся сама
    с собой: модель назвала бы конец собственного списка, и «дошёл ли сценарий до цели» стало бы
    тавтологией. Утверждается ЗНАЧЕНИЕ схемы, а не текст исходника.
    KILLS: перенос `goal_page` в `_SCHEMA_STEPS` (то есть отказ от независимости наблюдения).
    """
    from brain.planner import _SCHEMA_STEPS, _SCHEMA_GOAL_PAGE
    step_props = set(((_SCHEMA_STEPS.get("properties", {}).get("steps") or {})
                      .get("items", {}).get("properties") or {}))
    top_props = set(_SCHEMA_STEPS.get("properties", {}))
    bad = check("в полях шага целевой страницы нет", not (step_props & {"goal_page", "target_page"}),
                str(step_props))
    bad += check("и на верхнем уровне ответа шагов её тоже нет",
                 not (top_props & {"goal_page", "target_page"}), str(top_props))
    bad += check("отдельная схема существует и требует goal_page",
                 "goal_page" in (_SCHEMA_GOAL_PAGE.get("properties") or {})
                 and "goal_page" in (_SCHEMA_GOAL_PAGE.get("required") or []))
    return bad


def test_the_hash_does_not_move():
    """Признак цели лежит на ВЕРХНЕМ уровне сценария, поэтому `plan_hash` не двигается.

    `canonical_plan_hash` считает SHA-256 по ЦЕЛЫМ словарям шагов («every field is included; nothing
    is excluded»), поэтому поле, попавшее в ШАГ, сменило бы идентичность каждого сохранённого плана.
    Гейт сверяет хеш, записанный в `scenario.json`, с хешем, посчитанным по ШАГАМ БЕЗ единого поля
    цели — то есть с тем, что дала бы сборка до ADR-152.
    KILLS: перенос `goal_page`/`goal_reached` внутрь шага.
    """
    from brain.__main__ import _write_scenario
    steps = [nav(1, IDX), act(2, "alpha"), nav(3, PA), act(4, "a2c")]
    want = canonical_plan_hash([{**s, "step_id": i + 1} for i, s in enumerate(steps)])
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td)
        _write_scenario(out, "run1", IDX, steps, [], False, author_model="m",
                        crawl_complete=True, site_map=SITE, goal="open C", goal_page=PC)
        doc = json.loads((out / "scenario.json").read_text())
        rep = json.loads((out / "reconcile-report.json").read_text())
    bad = check("plan_hash тот же, что без полей цели", doc["plan_hash"] == want,
                f"{doc['plan_hash']} != {want}")
    bad += check("scenario.json несёт исход", doc.get("goal_reached") == "reached", str(doc.get("goal_reached")))
    bad += check("scenario.json несёт сам текст цели", doc.get("goal") == "open C")
    bad += check("ни один шаг не оброс полем цели",
                 all(not ({"goal_page", "goal_reached", "pages_visited"} & set(s)) for s in doc["steps"]))
    bad += check("reconcile-report объясняет ПОЧЕМУ", "goal_reached_reason" in rep, str(sorted(rep)))
    return bad


def test_describe_mode_says_nothing_about_a_goal():
    """У `describe` цели нет вовсе, поэтому полей цели там быть не должно.

    `goal_reached: "unknown"` в этом режиме читалось бы как «мы не смогли измерить» вместо «вопрос не
    задавали», а `mode` лежит в том же объекте и снимает двусмысленность отсутствия.
    KILLS: безусловную запись полей цели.
    """
    from brain.__main__ import _write_scenario
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td)
        _write_scenario(out, "run2", IDX, [nav(1, IDX)], [], True, author_model="m",
                        crawl_complete=True, site_map=SITE)
        doc = json.loads((out / "scenario.json").read_text())
    return check("describe не говорит о цели",
                 not ({"goal", "goal_page", "goal_reached", "pages_visited"} & set(doc)),
                 str(sorted(doc)))


def test_it_is_an_announcement_not_a_verdict():
    """Достижение цели НЕ влияет на код выхода: цель — направление, а не спецификация (HEALTH-004).

    Тот же выбор, что у ADR-151. KILLS: добавление достижения цели в `Facts`/`decide`.
    """
    from brain.outcome import Facts, decide
    import inspect
    src = inspect.getsource(decide)
    bad = check("`decide` не знает о достижении цели",
                not any(w in src for w in ("goal_reached", "goal_page", "pages_visited")))
    bad += check("`Facts` не обзавёлся полем цели",
                 not ({"goal_reached", "goal_page"} & set(Facts.__dataclass_fields__)))
    o = decide(Facts(mode="goal", grounded=4, unmatched=0))
    bad += check("заземлённый goal-прогон по-прежнему exit 0", o.exit_code == 0, str(o))
    return bad


def test_the_catalogue_speaks_all_four_codes():
    """Все четыре кода объявлены двуязычно, и `degrades` стоит ровно там, где качество ПОТЕРЯНО.

    `degrades: true` красит вердикт «прошло с потерей качества». Недостигнутая цель — ровно этот
    случай: тест зелёный и проверяет не то, о чём просили. А «сказать нечего» (`unknown`) и
    достигнутая цель ничего не теряют, и пометить их значило бы повторить ошибку ADR-113.
    KILLS: `degrades` на `goal_reached`/`goal_unknown`; снятие его с `goal_not_reached`.
    """
    cat = json.loads((pathlib.Path(REPO) / "brain" / "events.json").read_text())["events"]
    bad = 0
    for code in ("plan.goal_reached", "plan.goal_not_reached", "plan.goal_unknown",
                 "plan.goal_page_empty"):
        e = cat.get(code) or {}
        bad += check(f"{code}: объявлен и двуязычен", bool(e.get("ru") and e.get("en")), str(e))
    bad += check("недостигнутая цель ДЕГРАДИРУЕТ прогон",
                 cat["plan.goal_not_reached"].get("degrades") is True)
    bad += check("и несёт вердикт на обоих языках",
                 bool(cat["plan.goal_not_reached"].get("ru_verdict")
                      and cat["plan.goal_not_reached"].get("en_verdict")))
    for code in ("plan.goal_reached", "plan.goal_unknown"):
        bad += check(f"{code}: НЕ помечен degrades (ничего не потеряно)",
                     not cat[code].get("degrades"), str(cat[code]))
    return bad


def test_trail_has_no_duplicates_and_keeps_order():
    """След — порядок появления без повторов: он читается человеком, а не пересчитывается машиной."""
    steps = [nav(1, IDX), act(2, "alpha"), nav(3, PA), act(4, "a2c"), nav(5, IDX)]
    trail = pages_visited(steps, SITE)
    bad = check("без повторов", len(trail) == len(set(trail)), str(trail))
    bad += check("порядок сохранён", trail[0] == IDX and trail.index(PA) < trail.index(PC), str(trail))
    return bad


def main():
    bad = 0
    print("приход последним действием:")
    bad += test_last_click_counts_as_arrival()
    print("замеренный живой дефект:")
    bad += test_the_measured_live_defect_is_named()
    print("важен результат, а не маршрут:")
    bad += test_result_matters_not_route()
    print("три исхода, а не два:")
    bad += test_three_outcomes_are_three_not_two()
    bad += test_the_intent_is_grounded_like_a_ref()
    print("независимость наблюдения:")
    bad += test_the_intent_does_not_come_from_the_steps_schema()
    print("контракт не двигается:")
    bad += test_the_hash_does_not_move()
    bad += test_describe_mode_says_nothing_about_a_goal()
    print("объявление, а не приговор:")
    bad += test_it_is_an_announcement_not_a_verdict()
    print("каталог событий:")
    bad += test_the_catalogue_speaks_all_four_codes()
    print("след:")
    bad += test_trail_has_no_duplicates_and_keeps_order()
    if bad:
        print(f"\ngoal reached: {bad} FAILURE(S)")
        return 1
    print("\ngoal reached: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
