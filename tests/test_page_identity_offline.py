#!/usr/bin/env python3
"""Офлайн-гейт: идентичность страницы включает МАРШРУТ и не трогает всё остальное (ADR-132).

Run:  .venv/bin/python tests/test_page_identity_offline.py

ЗАЧЕМ ЭТА ФУНКЦИЯ ЕСТЬ. Замерено на живой цели 2026-08-23: OWASP Juice Shop (Angular), обход
heuristic с потолком 60 — **29 шагов, 2 уникальные страницы, coverage 0.0625**; с живой моделью 33
шага и **coverage 0.0**. Причина одна: `normalize_url` отбрасывает фрагмент, а маршруты SPA живут
именно в нём, поэтому восемьдесят состояний приложения схлопывались в один адрес.

ЗАЧЕМ ЭТОТ ГЕЙТ ОТДЕЛЬНО, А НЕ ВНУТРИ ГЕЙТОВ ОБХОДА. `semantic_id` строится по этому значению и
входит в `plan_hash`. Значит КАЖДЫЙ сохранённый план, каждый голден и весь кеш починки держатся на
том, что для целей БЕЗ hash-роутинга ответ не изменился ни на байт — а миграции ключей в проекте нет
вовсе (`brain/store.py` называет единственный механизм миграции, и он не про ключи). Обход этого не
проверяет: его гейты меряют поведение на SPA, где ответ ИЗМЕНИЛСЯ намеренно.

⚠ ЧТО ИМЕННО СЧИТАЕТСЯ МАРШРУТОМ, и почему не «любой фрагмент». Маршрут начинается со слэша
(`#/orders`) или с хешбэнга (`#!/orders` — форма старых роутеров). Обычный якорь (`#section`)
идентичность НЕ меняет: иначе оглавление на одной странице расплодило бы столько «страниц», сколько
в нём пунктов, и покрытие поехало бы на КАЖДОЙ документационной цели.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain.state import normalize_url, page_identity  # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


# Адреса, чьи ключи двигаться НЕ ДОЛЖНЫ. Каждый — форма, встречающаяся в дереве: цели фикстур,
# страницы документации с якорями, адреса с запросом.
UNMOVED = [
    "file:///opt/x/testdata/site/index.html",
    "file:///opt/x/testdata/site/page-a.html",
    "https://myapp.com/",
    "https://myapp.com/shop/checkout",
    "http://127.0.0.1:8181/index.html",
    "https://docs.example/guide#installation",      # обычный якорь
    "https://docs.example/guide#top",
    "https://myapp.com/search?q=hat",               # запрос отбрасывается как и прежде
    "https://myapp.com/search?q=hat#results",       # запрос + якорь
    "",
]

# Адреса, чьи ключи ОБЯЗАНЫ отличаться от прежних: это и есть SPA.
MOVED = [
    ("https://juice.example/#/search", "https://juice.example/#/search"),
    ("https://juice.example/#/basket", "https://juice.example/#/basket"),
    ("file:///opt/x/site-spa/index.html#/orders", "file:///opt/x/site-spa/index.html#/orders"),
    ("https://old.example/#!/legacy", "https://old.example/#!/legacy"),
    ("https://myapp.com/shop/#/cart", "https://myapp.com/shop/#/cart"),
    ("https://myapp.com/p?q=1#/route", "https://myapp.com/p#/route"),   # запрос всё ещё отбрасывается
]


def test_keys_that_must_not_move_did_not_move():
    """Встречное к самой правке. Если это утверждение упадёт, упадут 106 сохранённых планов — replay
    сверяет `plan_hash` и обрывается с кодом 3, не исполнив ни шага."""
    for u in UNMOVED:
        if page_identity(u) != normalize_url(u):
            fail(f"ключ сдвинулся для {u!r}: page_identity={page_identity(u)!r} против "
                 f"normalize_url={normalize_url(u)!r} — сохранённые планы для этой цели больше не "
                 f"воспроизводятся")
    print(f"  ok  {len(UNMOVED)} адрес(ов) без hash-роутинга сохранили прежний ключ")


def test_a_route_is_part_of_the_page():
    for u, want in MOVED:
        got = page_identity(u)
        if got != want:
            fail(f"page_identity({u!r}) = {got!r}, ожидалось {want!r}")
        if got == normalize_url(u):
            fail(f"маршрут {u!r} снова схлопнулся в {got!r} — SPA опять читается как одна страница")
    print(f"  ok  {len(MOVED)} маршрут(ов) участвуют в идентичности")


def test_two_routes_of_one_document_are_two_pages():
    """Само свойство, ради которого всё это делается, — и оно НЕ следует из таблиц выше: обе они
    проверяют по одному адресу за раз, а схлопывание это утверждение о ПАРЕ."""
    a, b = "https://juice.example/#/search", "https://juice.example/#/basket"
    if page_identity(a) == page_identity(b):
        fail("два разных маршрута одного документа дали одну идентичность")
    if normalize_url(a) != normalize_url(b):
        fail("фикстура утверждения неверна: адреса обязаны совпадать ПОСЛЕ normalize_url, иначе "
             "схлопывания и не было бы, и проверка меряет не то")
    # ...а два разных ЯКОРЯ одного документа — по-прежнему одна страница.
    c, d = "https://docs.example/g#one", "https://docs.example/g#two"
    if page_identity(c) != page_identity(d):
        fail("обычные якоря развели одну страницу на две — оглавление расплодит состояния")
    print("  ok  маршруты разводят страницу, якоря — нет")


def main() -> int:
    for fn in (test_keys_that_must_not_move_did_not_move,
               test_a_route_is_part_of_the_page,
               test_two_routes_of_one_document_are_two_pages):
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("page identity: OK (маршрут участвует, якорь нет, прежние ключи на месте)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
