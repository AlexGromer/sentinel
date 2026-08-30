#!/usr/bin/env python3
"""Офлайн-гейт: идентичность КОНТРОЛА отделена от идентичности вхождения (ADR-137).

Run:  .venv/bin/python tests/test_control_identity_offline.py

ЧТО ЭТО ЗАКРЫВАЕТ. Одна величина отвечала на два разных вопроса:

  «К какому вхождению привязать шаг?»  — маршрут НУЖЕН. Шаг воспроизведения это «нажать Dashboard
    НА ЭКРАНЕ ЗАКАЗОВ»; `semantic_id = sha1(path|role|name)` используется так в 83 местах 13
    модулей — replay, голдены, починка, импортёр, ревизии, стор, junit.

  «Этот контрол уже проработан?»       — маршрут ЛИШНИЙ. Кнопка рельса одна; нажав её однажды,
    обход узнал о ней всё.

ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ (2026-08-30, `testdata/site-spa`, heuristic, бюджет 40). После ADR-132 путь
страницы включает маршрут SPA, поэтому ОДИН контрол рельса получал двенадцать разных `semantic_id`.
Эвристика — буквально `clicks[0]`, «первый непроработанный в порядке DOM» (`brain/planner.py`), — а
рельс в разметке стоит ПЕРЕД содержимым экрана. Итог: **25 кликов из 38 приходились на рельс,
который уже нажимали** (66 % бюджета; при потолке 200 — 76 из 106, то есть 72 %), вырождение
начиналось со ВТОРОГО шага, на содержимое экранов оставалось пять шагов из тридцати восьми.
После разделения осей: повторного рельса НОЛЬ, карта 12 → 19 страниц при том же бюджете.

⚠ ЧЕГО РАЗДЕЛЕНИЕ НЕ ДЕЛАЕТ, И ЭТО ТОЖЕ ЗАМЕР. Статический пересчёт того же прогона в новой единице
давал 13/49 = 0.2653 против 38/137 = 0.2701 — то есть покрытие «не должно было измениться»:
числитель раздут ровно во столько же раз, что и знаменатель. Прогноз оказался НЕВЕРЕН, потому что
смена оси меняет САМ ПРОГОН: освободившийся бюджет уходит в содержимое, обход трогает 39 контролов
вместо 13, и покрытие выходит 0.5067. Правильный ответ дал только A/B, не арифметика.

⚠ ЦЕНА НАЗВАНА. Якорь идентичности — `testid`, когда он есть, и только иначе доступное имя. Значит в
приложении БЕЗ разметки два разных «Сохранить» на разных экранах сольются в один контрол, и один
будет объявлен проработанным, пока второго не касались. Замерено по корпусу: 17 имён делятся между
страницами, `testid` нет НИ У ОДНОГО — то есть фикстуры мерят худший случай; при этом распределение
бинарное (67 имён на одной странице, 11 на всех девятнадцати), подозрительной середины ноль.
"""
import hashlib
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain.graph import _elements_from_interactives          # noqa: E402
from brain.state import control_id, semantic_id              # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def _el(name, role="button", testid=None, frame=None):
    e = {"tag": role, "role": role, "name": name, "testid": testid, "text": name, "visible": True}
    if frame:
        e["frame"] = frame
    return e


# --- 1. Две оси отвечают на два вопроса --------------------------------------------------------

def test_the_same_control_on_two_routes_is_one_control_and_two_occurrences():
    """ГЛАВНОЕ утверждение, и оно ПАРНОЕ.

    Одна половина («контрол один») удовлетворяется кодом, выбросившим маршрут отовсюду, — и тогда
    ломается воспроизведение. Другая («вхождения разные») удовлетворяется тем, что было до правки.
    Вместе они требуют, чтобы обе оси существовали ОДНОВРЕМЕННО и отвечали каждая на своё.
    """
    a = _elements_from_interactives([_el("Dashboard")], "https://app/#/orders")[0]
    b = _elements_from_interactives([_el("Dashboard")], "https://app/#/billing")[0]
    if a["control_id"] != b["control_id"]:
        fail("один и тот же контрол рельса на двух маршрутах получил РАЗНЫЕ control_id — "
             "перечень непроработанных снова раздувается маршрутами, и бюджет уходит в рельс")
    if a["semantic_id"] == b["semantic_id"]:
        fail("вхождения на разных маршрутах получили ОДИН semantic_id — шаг перестал знать, на "
             "каком экране он был записан, и воспроизведение свяжется не с тем элементом")
    print("  ok  контрол один, вхождения разные — обе оси на месте")


def test_different_controls_stay_different_on_the_same_page():
    """Встречное. Без него первое утверждение удовлетворяется кодом, объявившим все контролы одним."""
    els = _elements_from_interactives([_el("Save"), _el("Delete")], "https://app/#/x")
    if els[0]["control_id"] == els[1]["control_id"]:
        fail("две разные кнопки на одной странице слились в один контрол — покрытие объявит "
             "проработанной ту, до которой не дотрагивались")
    print("  ok  разные контролы одной страницы не слились")


def test_the_testid_wins_over_the_name_so_a_marked_up_app_never_collides():
    """⚠ ЭТО И ЕСТЬ ГРАНИЦА ЦЕНЫ, которую платит ось контрола.

    Схлопывание опирается на имя лишь тогда, когда `testid` отсутствует. В приложении, размечающем
    контролы, два разных «Сохранить» имеют разные `testid` и не сольются — то есть цена платится
    ровно теми приложениями, которые не дали нам ничего стабильнее имени.
    """
    same_name = [_el("Save", testid="save-billing"), _el("Save", testid="save-profile")]
    els = _elements_from_interactives(same_name, "https://app/#/x")
    if els[0]["control_id"] == els[1]["control_id"]:
        fail("две кнопки с одним ИМЕНЕМ, но разными testid слились — якорь идентичности перестал "
             "предпочитать testid, и разметка приложения больше не спасает от схлопывания")
    # И обратная половина: без testid они действительно сливаются, и это ЗАЯВЛЕННОЕ поведение.
    no_tid = _elements_from_interactives([_el("Save"), _el("Save")], "https://app/#/x")
    if no_tid[0]["control_id"] != no_tid[1]["control_id"]:
        fail("две одноимённые кнопки БЕЗ testid не слились — тогда заявленная цена оси не платится, "
             "и описание в ADR-137 расходится с поведением")
    print("  ok  testid побеждает имя; без testid схлопывание происходит и объявлено")


def test_a_control_inside_a_frame_is_not_the_same_control_as_its_namesake_outside():
    """⚠ ФРЕЙМ ВХОДИТ В ЯКОРЬ, и это не мелочь. `browser.interactives` обходит фреймы глубины 1
    (ADR-095); одноимённая кнопка внутри фрейма — ДРУГОЙ контрол, и склеить их значило бы объявить
    проработанным тот, до которого не дотрагивались."""
    top = _elements_from_interactives([_el("Submit")], "https://app/#/x")[0]
    inner = _elements_from_interactives([_el("Submit", frame="iframe[name='inner']")],
                                        "https://app/#/x")[0]
    if top["control_id"] == inner["control_id"]:
        fail("кнопка во фрейме и одноимённая в верхнем документе слились в один контрол")
    print("  ok  контрол во фрейме отличается от одноимённого снаружи")


# --- 2. Ось не протекла в артефакт --------------------------------------------------------------

def test_the_control_axis_never_reaches_the_frozen_step():
    """⚠ САМОЕ ДОРОГОЕ СВОЙСТВО ЭТОЙ ПРАВКИ, и оно молчаливое.

    `canonical_plan_hash` хеширует ВСЕ поля всех шагов. Лишнее поле в шаге сдвинуло бы хеш КАЖДОГО
    замороженного плана — включая голдены `testdata/site` и `site-v2`, на которых эта правка не
    меняет ни одного шага. Поэтому вторая ось живёт в модели страницы, а в шаг не попадает.

    KILLS: добавление `control_id` в `planned` (`brain/graph.py`, узел `plan`).
    """
    src = (REPO / "brain" / "graph.py").read_text()
    i = src.index("planned = {")
    j = src.index("}", src.index("_agui", i)) if "_agui" in src[i:i + 2000] else i + 700
    block = src[i:i + 700]
    if "control_id" in block:
        fail("`control_id` попал в замороженный шаг — plan_hash каждого сохранённого плана сдвинется, "
             "и голдены перестанут воспроизводиться")
    print("  ok  ось контрола не протекла в шаг плана")


def test_the_two_axes_are_computed_from_the_same_anchor():
    """Обе оси обязаны считаться от ОДНОГО якоря, иначе они разойдутся на первом же элементе с
    testid: `semantic_id` брал бы testid, а `control_id` — имя, и «этот контрол» перестал бы
    соответствовать «этому вхождению»."""
    e = _elements_from_interactives([_el("Save", testid="save-1")], "https://app/#/x")[0]
    expect_ctrl = control_id("button", "save-1")
    expect_sid = semantic_id("https://app/#/x", "button", "save-1")
    if e["control_id"] != expect_ctrl:
        fail(f"control_id считается не от того якоря: {e['control_id']} против {expect_ctrl}")
    if e["semantic_id"] != expect_sid:
        fail(f"semantic_id считается не от того якоря: {e['semantic_id']} против {expect_sid}")
    print("  ok  обе оси считаются от одного якоря (testid, иначе имя)")


def _checks():
    found = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    if len(found) < 6:
        fail(f"найдено {len(found)} проверок вместо шести — вывод перечня сломался")
    return [f for _, f in found]


def main() -> int:
    for fn in _checks():
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("control identity: OK (две оси, один якорь, шаг не тронут)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
