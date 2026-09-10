#!/usr/bin/env python3
"""Обход переживает ЛЮБУЮ страницу, а не только ту, что похожа на ожидаемую.

Run:  .venv/bin/python tests/test_crawl_survives_any_page_offline.py

ЧТО СЛУЧИЛОСЬ. Обход дошёл до `the-internet/nested_frames` и умер целиком:
`browser.snapshot: locator.ariaSnapshot: Timeout 5000ms exceeded — waiting for locator('body')`,
exit 4, `plan.json` не записан вовсе, **45 шагов работы потеряны**. Страница отдаёт чистый
`<frameset>` — элемента `<body>` в ней НЕТ по стандарту.

⚠ ТОНКОСТЬ, ИЗ-ЗА КОТОРОЙ ЭТО НЕ БРОСАЛОСЬ В ГЛАЗА. По спецификации HTML `document.body` на такой
странице возвращает не null, а сам `<frameset>` — «первый ребёнок html, который либо body, либо
frameset». То есть JS, читающий `document.body`, работает; а CSS-селектор `body` не матчит, потому
что сравнивает ИМЯ ТЕГА. Код и спецификация расходились молча, и увидеть это можно только на живой
странице — отсюда фикстура, а не юнит-тест.

ЗАМЕРЕНО ПОСЛЕ ПРАВКИ (`the-internet`, потолок 90): **90 шагов, 36 страниц, coverage 1.0, exit 0**,
`/nested_frames` пройден. Было — 45 шагов и пустой каталог.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, и почему именно это:

  1. Снимок не бросает на документе без `<body>` и находит корень, который есть.
  2. Инвентарь контролов видит то, что лежит ВНУТРИ `<frame>` — иначе страница читается как тупик.
  3. `browser.links` пересекает границу фрейма. `$$eval` её не пересекает — свойство селекторного
     движка, — и до правки инструмент возвращал `{links: []}`, что неотличимо от «ссылок нет».
  4. `<frame>` адресуется СВОИМ тегом. Прежний `frameSelector` строил только `iframe[…]`: для фрейма
     с именем он возвращал `iframe[name="…"]` — адрес выдан, адресуемое по нему не резолвится, — а
     без имени индекс считался по `querySelectorAll('iframe')` и давал -1, то есть корень молча
     выпадал из обхода.
  5. Обычная страница с `<body>` не изменилась ни в чём: узлов столько же, поля-причины нет.
     ⚠ У самого `<frameset>` доступных узлов НЕ бывает — весь контент во фреймах, — поэтому пустое
     дерево там законно, и требовать непустоты значило бы утверждать неверное. Замерено: 0 против 58.
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"
FRAMESET = "file://" + str(FIXTURES / "l12-frameset.html")
PLAIN = "file://" + str(FIXTURES / "l1.html")

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _executor_gate import drive as _gate_drive, drives, missing_prerequisite  # noqa: E402  (path set above)


def _drive(calls: list) -> list:
    """The shared discriminator, with this suite's recorded 600s ceiling.

    See tests/_executor_gate.py: a skip is now an OBSERVATION of the prerequisites, never an
    inference from a missing success marker. Raises rather than returning None, so a broken
    executor can no longer read as "no browser available".
    """
    return _gate_drive(calls, timeout=600)



def test_a_document_without_a_body_is_snapshotted_rather_than_fatal():
    res = _drive([("browser.navigate", {"url": FRAMESET}), ("browser.snapshot", {})])
    snap = res[1]
    # Сам факт возврата — и есть утверждение: до правки этот вызов БРОСАЛ, и прогон кончался здесь.
    if snap.get("rootless"):
        fail(f"снимок объявил документ без корня, хотя <frameset> в нём есть: {snap['rootless']}")
    # ⚠ ПУСТОЕ ДЕРЕВО ЗДЕСЬ ЗАКОННО, и требовать непустоты было бы неверным утверждением: у самого
    # <frameset> нет доступных узлов — весь контент живёт во фреймах, и его приносят
    # `browser.interactives` и `browser.links` (проверяются ниже). Замерено: 0 узлов на frameset
    # против 58 на обычной странице. Ценность снимка здесь в том, что он ОТВЕЧАЕТ, а не в том, что
    # он что-то нашёл.
    if "ariaSnapshot" not in snap:
        fail("снимок не вернул поля ariaSnapshot вовсе")
    print(f"  ok  frameset снят без отказа: {snap.get('nodeCount')} узл(ов) "
          f"(у самого frameset их и не бывает — контент во фреймах)")


def test_controls_and_links_inside_frames_are_visible():
    res = _drive([("browser.navigate", {"url": FRAMESET}),
                  ("browser.interactives", {}), ("browser.links", {})])
    els = res[1].get("elements") or []
    names = [(e.get("name") or "").strip() for e in els]
    if not any("во фрейме" in n for n in names):
        fail(f"кнопка внутри <frame> не найдена — страница читается как тупик: {names}")

    # ⚠ Адрес фрейма обязан называть ТОТ тег, который на странице. `iframe[name=…]` для <frame> —
    # это адрес, по которому ничего не резолвится, то есть хуже, чем его отсутствие: отсутствие
    # видно, а неверный адрес выглядит рабочим.
    scopes = {e.get("frame") for e in els if e.get("frame")}
    if not scopes:
        fail("ни один элемент не несёт scope фрейма — обход фреймов не состоялся")
    for sc in scopes:
        if sc.startswith("iframe"):
            fail(f"<frame> адресован как iframe ({sc!r}) — по такому адресу ничего не найдётся")

    links = [l.get("href", "") for l in (res[2].get("links") or [])]
    # Ссылки лежат в РАЗНЫХ фреймах: одна в верхнем, другая в нижнем. Проверяются обе, потому что
    # обход только первого фрейма выглядит как успех ровно до второй страницы.
    if not any("l10-frames" in u for u in links):
        fail(f"ссылка из ВЕРХНЕГО фрейма не попала во фронтир: {links}")
    if not any(u.endswith("l1.html") for u in links):
        fail(f"ссылка из НИЖНЕГО фрейма не попала во фронтир: {links}")
    print(f"  ok  во фреймах видно {len(els)} контрол(ов) и {len(links)} ссыл(ок), scope: {sorted(scopes)}")


def test_an_ordinary_page_is_unchanged():
    """Встречное утверждение. Без него «снимок не падает» удовлетворяется снимком, который не работает
    нигде: пустая строка не бросает точно так же."""
    res = _drive([("browser.navigate", {"url": PLAIN}), ("browser.snapshot", {})])
    snap = res[1]
    if snap.get("rootless"):
        fail(f"обычная страница объявлена бескорневой: {snap['rootless']}")
    if (snap.get("nodeCount") or 0) < 5:
        fail(f"снимок обычной страницы обеднел: {snap.get('nodeCount')} узл(ов)")
    print(f"  ok  обычная страница: {snap.get('nodeCount')} узл(ов), поля-причины нет")


def main() -> int:
    # A prerequisite genuinely absent is an OBSERVATION made before anything runs; it is the only
    # thing allowed to produce a skip here. Anything else that goes wrong is a failure (see
    # tests/_executor_gate.py).
    fns = (test_a_document_without_a_body_is_snapshotted_rather_than_fatal,
           test_controls_and_links_inside_frames_are_visible,
           test_an_ordinary_page_is_unchanged)
    missing = missing_prerequisite()
    if missing:
        print(f"  SKIP-LOUD: {missing}")
        print(f"     {len(fns)} crawl-survives checks are UNCHECKED here, not 'checked and fine'")
        print("EXECUTOR-DRIVEN 0 crawl-survives")
        return 0
    driven = 0
    for fn in fns:
        before = drives()
        fn()
        if drives() > before:
            driven += 1
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("crawl survives any page: OK")
    print(f"EXECUTOR-DRIVEN {driven} crawl-survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
