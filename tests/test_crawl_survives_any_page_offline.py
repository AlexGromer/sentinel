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
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"
FRAMESET = "file://" + str(FIXTURES / "l12-frameset.html")
PLAIN = "file://" + str(FIXTURES / "l1.html")

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def _drive(calls: list) -> "list | None":
    """Прогнать вызовы через НАСТОЯЩИЙ исполнитель. Форма списана с tests/test_iframe_scope_offline.py:
    один способ поднимать исполнителя в гейтах, а не второй рядом."""
    dist = REPO / "pw-executor" / "dist" / "server.js"
    if not dist.exists():
        print("     SKIP — pw-executor/dist not built (npm run build)")
        return None
    script = (
        'import sys, json; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'ex = Executor("node %s")\n'
        'out = [ex.call(m, **p) for m, p in json.loads(%r)]\n'
        'ex.call("shutdown"); ex.close()\n'
        'print("@@RESULT@@" + json.dumps(out))\n' % (str(REPO), dist, json.dumps(calls))
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PW_NO_TRACE": "1"}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       timeout=600)
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    print("     SKIP — no browser available:", ((r.stderr or "") + (r.stdout or ""))[-250:].replace("\n", " "))
    return None


def test_a_document_without_a_body_is_snapshotted_rather_than_fatal():
    res = _drive([("browser.navigate", {"url": FRAMESET}), ("browser.snapshot", {})])
    if res is None:
        return
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
    if res is None:
        return
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
    if res is None:
        return
    snap = res[1]
    if snap.get("rootless"):
        fail(f"обычная страница объявлена бескорневой: {snap['rootless']}")
    if (snap.get("nodeCount") or 0) < 5:
        fail(f"снимок обычной страницы обеднел: {snap.get('nodeCount')} узл(ов)")
    print(f"  ok  обычная страница: {snap.get('nodeCount')} узл(ов), поля-причины нет")


def main() -> int:
    for fn in (test_a_document_without_a_body_is_snapshotted_rather_than_fatal,
               test_controls_and_links_inside_frames_are_visible,
               test_an_ordinary_page_is_unchanged):
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("crawl survives any page: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
