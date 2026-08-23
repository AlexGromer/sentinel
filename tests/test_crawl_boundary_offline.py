#!/usr/bin/env python3
"""Граница обхода: адрес вне цели во фронтир не попадает.

Run:  .venv/bin/python tests/test_crawl_boundary_offline.py

ЧТО СЛУЧИЛОСЬ И ПОЧЕМУ ЭТОТ ГЕЙТ ЕСТЬ. `base_origin` вычислялся одной строкой — «всё до последнего
слэша»: ``normalize_url(target).rsplit("/", 1)[0] + "/"``. Для цели БЕЗ завершающего слэша, то есть
для самой естественной формы записи (``--target https://myapp.com``), последним слэшем оказывался
второй слэш схемы, и граница вырождалась в ``"https://"``. Проверка ``nu.startswith(origin)`` в
brain/graph.py истинна тогда для ЛЮБОГО https-адреса.

ЗАМЕРЕНО ПОВЕДЕНИЕМ (2026-08-22), а не вычитано: две локальные площадки, 8181 и 8182, со ссылкой с
первой на вторую.

    --target http://127.0.0.1:8181   → 6 шагов, ТРИ из них на ЧУЖОМ хосте: /index.html, /b2, /b3
    --target http://127.0.0.1:8181/  → 3 шага,  на чужом хосте НИ ОДНОГО
    после починки, без слэша         → 3 шага,  на чужом хосте НИ ОДНОГО

Цена дефекта не в числе шагов: инструмент UI-тестирования уходил на посторонние сайты — посылал туда
заголовки, снимал кадры и складывал чужие страницы в артефакт прогона. Разрешения на это никто не
давал, и человек, написавший цель без слэша, не мог узнать, что оно произошло.

ПОЧЕМУ ГЕЙТ ОФЛАЙНОВЫЙ, А НЕ ПРОГОН. Живой прогон с двумя площадками стоит браузера и полутора минут,
и он уже сделан — им и куплена эта запись. Здесь проверяется то, что можно проверить дёшево и на
КАЖДОМ коммите: сама граница на таблице целей, СВЯЗКА границы с фильтром фронтира (граница верна, а
сравнение применено не то — это два разных способа сломаться), и то, что вычисление границы не
разъехалось обратно на рукописную формулу.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.state import base_origin_of, normalize_url  # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


# Таблица целей. Первым идёт случай, ради которого всё написано.
CASES = [
    ("http://127.0.0.1:8181", "http://127.0.0.1:8181/", "цель без слэша — граница обязана быть ХОСТОМ, а не схемой"),
    ("https://myapp.com", "https://myapp.com/", "то же для https"),
    ("https://myapp.com/", "https://myapp.com/", "со слэшем — тот же ответ, форма записи не должна менять границу"),
    ("https://myapp.com/shop", "https://myapp.com/", "файл в корне: граница — хост"),
    ("https://myapp.com/shop/", "https://myapp.com/shop/", "раздел сужает границу: обход раздела не расползается на весь сайт"),
    ("https://myapp.com/a/b/c.html", "https://myapp.com/a/b/", "вложенный путь сужает так же"),
    ("file:///opt/x/site/index.html", "file:///opt/x/site/", "у file:// хоста нет — граница остаётся каталогом"),
    ("", "", "пустая цель не даёт границы"),
]


def test_boundary_table() -> None:
    for target, want, why in CASES:
        got = base_origin_of(target)
        if got != want:
            fail(f"base_origin_of({target!r}) = {got!r}, ожидалось {want!r} — {why}")


def test_the_boundary_actually_excludes_a_foreign_host() -> None:
    """Граница верна САМА ПО СЕБЕ и в СВЯЗКЕ с фильтром.

    Проверяется тем же сравнением, каким пользуется brain/graph.py (`nu.startswith(origin)`): граница
    может быть безупречной, а применена не так — это отдельный способ сломаться, и таблица выше его
    не видит.
    """
    origin = base_origin_of("http://127.0.0.1:8181")
    own = ["http://127.0.0.1:8181/index.html", "http://127.0.0.1:8181/page2.html"]
    foreign = [
        "http://127.0.0.1:8182/index.html",   # другой ПОРТ — тот случай, что был замерен живьём
        "http://evil.example/x",
        "https://myapp.com/",
        "http://127.0.0.1:8181.evil.example/",  # префикс хоста: строковое сравнение обязано его отвергнуть
    ]
    for u in own:
        if not normalize_url(u).startswith(origin):
            fail(f"собственный адрес {u!r} не проходит границу {origin!r} — обход не увидит свой же сайт")
    for u in foreign:
        if normalize_url(u).startswith(origin):
            fail(f"ЧУЖОЙ адрес {u!r} проходит границу {origin!r} — прогон уйдёт на сайт, разрешения "
                 f"на который никто не давал")


def test_the_boundary_is_computed_in_one_place() -> None:
    """Обе точки вычисления зовут функцию, а не повторяют формулу.

    Дефект жил в ДВУХ местах brain/__main__.py сразу — холодный старт и продолжение беседы, — и
    исправить одно значило оставить второе. Рукописная формула здесь запрещена явно: она уже один раз
    разъехалась с самой собой.
    """
    src = read(os.path.join("brain", "__main__.py"))
    calls = len(re.findall(r"base_origin\s*=\s*base_origin_of\(", src))
    if calls < 2:
        fail(f"brain/__main__.py вычисляет base_origin через base_origin_of() {calls} раз(а), а точек "
             f"вычисления две — холодный старт и продолжение беседы")
    if re.search(r'rsplit\("/",\s*1\)\[0\]\s*\+\s*"/"', src):
        fail("brain/__main__.py снова считает границу вручную через rsplit — ровно та формула, что "
             "вырождалась в схему на цели без завершающего слэша")


def test_the_frontier_still_filters_by_the_boundary() -> None:
    """И сам фильтр никуда не делся.

    Свойство поведенческое и куплено живым прогоном; здесь — только страж проводки: если сравнение
    исчезнет из узла perceive, граница станет верной и НЕ ПРИМЕНЯЕМОЙ, а таблица выше останется
    зелёной. Это единственное утверждение в файле о ФОРМЕ исходника, и оно объявлено таковым.
    """
    src = read(os.path.join("brain", "graph.py"))
    if not re.search(r"startswith\(origin\)", src):
        fail("brain/graph.py больше не сверяет адрес с origin — граница вычисляется и не применяется, "
             "то есть фронтир принимает всё")


def main() -> int:
    for fn in (test_boundary_table, test_the_boundary_actually_excludes_a_foreign_host,
               test_the_boundary_is_computed_in_one_place, test_the_frontier_still_filters_by_the_boundary):
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"crawl boundary: OK ({len(CASES)} целей, граница исключает чужой хост, другой порт и "
          f"префиксного двойника; вычисление в одном месте, фильтр на месте)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
