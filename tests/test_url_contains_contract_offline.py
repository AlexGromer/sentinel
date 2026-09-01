#!/usr/bin/env python3
"""ADR-142 — у `url_contains` ОДНО значение `expected` на весь продукт: ЛИТЕРАЛЬНАЯ ПОДСТРОКА.

ЗАЧЕМ ЭТОТ ФАЙЛ. `expected` трогают ПЯТЬ мест, и расходилось ровно ОДНО. Исполнитель
(`pw-executor/src/server.ts`) сравнивает подстрокой: `u.href.includes(want)`. Экспортёр
(`brain/exporter.py`) экранирует значение НАМЕРЕННО, чтобы согласоваться с исполнителем, и написал
об этом в своём докстринге. Путь Cypress в импортёре кладёт литерал. Продюсер
(`brain/record_bridge.py`) кладёт наблюдённый маршрут. А путь Playwright клал СЫРУЮ РЕГУЛЯРКУ.

ЗАМЕР, КОТОРЫЙ КУПИЛ ЭТОТ ГЕЙТ (2026-09-01, сквозной): `toHaveURL(/dash.*board/)` сохранялся как
`dash.*board`, и правило исполнителя давало False на `/dashboard` и на `/dash-XYZ-board` — то есть
на ВСЕХ адресах, ради которых регулярка писалась. True получалось только на буквальном
`/dash.*board`, которого не бывает. Обратный экспорт выдавал `/dash\\.\\*board/` — ДРУГУЮ регулярку,
так что круг был не только нерабочим, но и менял смысл. Ни один тест этого не ловил: у импортёра
были свои утверждения, у экспортёра свои, и НИ ОДНО не сводило их вместе.

ЧТО УТВЕРЖДАЕТСЯ ЗДЕСЬ И ПОЧЕМУ ЭТО НЕЛЬЗЯ ПРОЙТИ ЧАСТИЧНОЙ ПРАВКОЙ. Утверждение ПАРНОЕ и идёт
через настоящее правило исполнителя, а не через его пересказ: правило ВЫВОДИТСЯ из `server.ts`
(и гейт краснеет, если исполнитель начнёт считать регулярку), а импортированное значение
проверяется на адресах, ради которых исходное утверждение писалось. Починить одну половину, не
трогая другую, нельзя: обе читают один и тот же источник.

⚠ ЧЕГО ЭТОТ ФАЙЛ ДОКАЗАТЬ НЕ МОЖЕТ, сказано прямо. Он не запускает браузер, поэтому не проверяет,
что `waitForURL` действительно ждёт. Он проверяет СОГЛАСИЕ СЕМАНТИК: что значение, положенное
импортёром, принимается правилом, которое исполнитель объявляет в своём исходнике. Живое ожидание
покрыто `pw-executor/src/routes.test.ts` на настоящем Chromium.

Офлайн: без сети, без браузера, без бинарей.
Запуск: .venv/bin/python tests/test_url_contains_contract_offline.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# Пол на число мест. Обязательный спутник вывода — docs/DEVELOPMENT.md §0, принцип 5.
SITE_FLOOR = 5   # замерено 2026-09-01: executor, exporter, importer×2 (playwright+cypress), producer


def spec(arg: str) -> str:
    return ("import { test, expect } from '@playwright/test';\n"
            "test('t', async ({ page }) => {\n"
            "  await page.goto('https://app.test/');\n"
            f"  await expect(page).toHaveURL({arg});\n"
            "});\n")


def imported(arg: str):
    """(expected, виды заметок) для одного `toHaveURL(...)`."""
    from brain.importer import parse_playwright_spec
    t = parse_playwright_spec(spec(arg))["tests"][0]
    urls = [s for s in t["steps"] if s.get("condition") == "url_contains"]
    return (urls[0].get("expected") if urls else None), [n["kind"] for n in t["notes"]]


def executor_rule():
    """Правило сравнения, ВЫВЕДЕННОЕ из исходника исполнителя, а не переписанное сюда.

    Возвращает функцию (expected, href) -> bool. Если исполнитель перестанет сравнивать подстрокой,
    вывод не найдёт своей формы и гейт покраснеет — вместо того чтобы молча проверять вчерашнее
    правило.
    """
    src = read(os.path.join("pw-executor", "src", "server.ts"))
    m = re.search(r"case 'url_contains': \{(.*?)\n          \}", src, re.S)
    if not m:
        fail("pw-executor/src/server.ts: ветка `url_contains` не найдена — гейт читал бы пустоту")
        return None
    body = m.group(1)
    if "href.includes(want)" not in body:
        fail("исполнитель больше НЕ сравнивает подстрокой (`href.includes(want)` пропал) — "
             "значение `expected` во всём продукте перестало быть литералом, и импортёр/экспортёр "
             "надо приводить в соответствие ОСОЗНАННО, а не обнаруживать расхождение прогоном")
        return None
    if re.search(r"\bnew RegExp\b|\bRegExp\(", body):
        fail("исполнитель начал строить RegExp из `expected` — это ТРЕТЬЯ семантика, ровно то "
             "расхождение, которое ADR-142 убрал")
    return lambda expected, href: expected in href


def test_the_executor_rule_is_derived_and_still_a_substring() -> None:
    """Предпосылка всего файла: правило исполнителя читается из его исходника."""
    rule = executor_rule()
    if rule is None:
        return
    if not rule("board", "https://app.test/dashboard"):
        fail("выведенное правило исполнителя не принимает очевидную подстроку — вывод сломан")


def test_an_imported_regex_assertion_holds_on_the_addresses_it_was_written_for() -> None:
    """ГЛАВНОЕ УТВЕРЖДЕНИЕ. До ADR-142 оно не выполнялось НИ НА ОДНОМ адресе.

    KILLS: сохранение сырого образца (`step["expected"] = rx.group(1)`), как было до ADR-142.
    """
    rule = executor_rule()
    if rule is None:
        return
    exp, kinds = imported("/dash.*board/")
    if exp is None:
        fail("toHaveURL(/dash.*board/) не дал шага вовсе — у образца есть литеральные куски")
        return
    for href in ("https://app.test/dashboard", "https://app.test/dash-XYZ-board"):
        if not rule(exp, href):
            fail(f"импортированное {exp!r} НЕ срабатывает на {href!r} — а исходная регулярка "
                 f"писалась ровно ради него; это утверждение, зелёное по построению")
    # И не приняло то, к чему исходная регулярка отношения не имеет.
    if rule(exp, "https://app.test/plain"):
        fail(f"импортированное {exp!r} принимает посторонний адрес — ослабление зашло слишком "
             f"далеко и утверждение перестало что-либо утверждать")
    if "narrowed" not in kinds:
        fail("ослабление регулярки до литерального фрагмента НЕ объявлено заметкой `narrowed` — "
             "«стало слабее» и «стало неправдой» читатель обязан различать, и молчание тут "
             "неотличимо от точного перевода")


def test_a_pattern_with_no_literal_at_all_is_dropped_with_a_reason() -> None:
    """Утверждение, которое истинно всегда, хуже отсутствующего — но выбросить молча нельзя.

    KILLS: подстановка пустой строки (её `includes` принимает ЛЮБОЙ адрес).
    """
    exp, kinds = imported("/.*/")
    if exp == "":
        fail("toHaveURL(/.*/) сохранён как пустая строка — `href.includes('')` истинно всегда, "
             "то есть шаг зелёный на любом адресе и проверяет ровно ничего")
    if exp is not None:
        fail(f"toHaveURL(/.*/) дал expected={exp!r} — у образца нет ни одного литерального куска")
    if "unmatched" not in kinds:
        fail("шаг отброшен МОЛЧА — пропуск обязан объявляться причиной (docs/DEVELOPMENT.md §0)")


def test_the_string_form_declares_that_equality_became_a_substring() -> None:
    """Четвёртая ось, которую реестр не называл.

    `toHaveURL('...')` — матчер РАВЕНСТВА; `url_contains` сравнивает подстроку. Значение верное,
    но утверждение слабее исходного, и это обязано быть сказано.
    KILLS: молчаливый импорт строковой формы (как было до ADR-142).
    """
    rule = executor_rule()
    if rule is None:
        return
    exp, kinds = imported("'https://app.test/plain'")
    if exp != "https://app.test/plain":
        fail(f"строковая форма исказилась при импорте: {exp!r}")
    if not rule(exp, "https://app.test/plain?x=1"):
        fail("ослабление до подстроки не наблюдается — предпосылка заметки исчезла, и заметку надо "
             "убирать, а не оставлять описывать несуществующее")
    if "narrowed" not in kinds:
        fail("сужение равенства до подстроки НЕ объявлено: импорт молча принимает адреса, которых "
             "исходный тест не принимал")


def test_every_other_site_stores_a_literal() -> None:
    """Остальные четыре места кладут литерал — и это проверяется, а не предполагается.

    KILLS: возврат регулярки на пути Cypress или в продюсере.
    """
    sites = 0

    # Cypress: `cy.url().should('include', '/x')` — литерал по построению.
    from brain.importer import parse_cypress_spec
    cy = ("describe('d', () => {\n"
          "  it('t', () => {\n"
          "    cy.visit('https://app.test/');\n"
          "    cy.url().should('include', '/dash.*board');\n"
          "  });\n"
          "});\n")
    try:
        t = parse_cypress_spec(cy)["tests"][0]
        u = [s for s in t["steps"] if s.get("condition") == "url_contains"]
        if u:
            sites += 1
            if u[0].get("expected") != "/dash.*board":
                fail(f"путь Cypress исказил литерал: {u[0].get('expected')!r} — он обязан "
                     f"сохраняться посимвольно, метасимволы там значения не имеют")
    except Exception as exc:  # noqa: BLE001 — форма API важнее, чем её отсутствие
        fail(f"путь Cypress не разобрался: {exc}")

    # Producer: record_bridge кладёт НАБЛЮДЁННЫЙ маршрут, а не образец.
    bridge = read(os.path.join("brain", "record_bridge.py"))
    if '"condition": "url_contains"' in bridge:
        sites += 1
        if re.search(r'"condition": "url_contains", "expected": re\.', bridge):
            fail("brain/record_bridge.py кладёт в `expected` регулярку — продюсер обязан класть "
                 "наблюдённый адрес")

    # Exporter: экранирует значение, то есть считает его литералом.
    exporter = read(os.path.join("brain", "exporter.py"))
    if "url_contains" in exporter:
        sites += 1
        if "_esc_re" not in exporter:
            fail("brain/exporter.py перестал экранировать значение — экспортированный тест "
                 "разойдётся с исполнителем на первом же метасимволе")

    # Importer, путь Playwright — проверен утверждениями выше.
    sites += 1
    # Executor — правило выведено выше.
    sites += 1

    if sites < SITE_FLOOR:
        fail(f"обход нашёл {sites} мест, пол {SITE_FLOOR} — место перестало опознаваться, и гейт "
             f"проверяет меньше, чем думает")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if failures:
        print(f"FAIL — {len(failures)} problem(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    print(f"url_contains contract OK: the executor's substring rule derived from server.ts; "
          f"an imported /dash.*board/ now holds on /dashboard and /dash-XYZ-board (it held on "
          f"NEITHER before ADR-142); a pattern with no literal is dropped with a reason; the string "
          f"form declares its narrowing; {SITE_FLOOR} sites agree; {len(fns)} checks")
