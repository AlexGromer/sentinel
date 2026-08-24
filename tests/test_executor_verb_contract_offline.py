#!/usr/bin/env python3
"""Офлайн-гейт: перечень верб исполнителя ВЫВОДИТСЯ и сходится сам с собой (ADR-135).

Run:  .venv/bin/python tests/test_executor_verb_contract_offline.py

Контракт вербы `pw-executor` держится в ТРЁХ местах одного файла, и до этого гейта их не сверял
никто:

  1. `switch (method)` в `dispatchInner` — что исполнитель УМЕЕТ. По JSON-RPC достижимо ровно это;
  2. массив `TOOL_METHODS` — что он ОБЪЯВЛЯЕТ. Уезжает в ответ `initialize` (`capabilities`) и
     задаёт цикл регистрации инструментов MCP;
  3. объект `schemas` — КАК его звать. Ключ по имени вербы.

⚠ ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ (2026-08-24, разведка W8). Три вербы разъехались молча:
`browser.videoStop` жила в диспетчере и отсутствовала в перечне — то есть brain её ЗВАЛ
(`brain/__main__.py`), а клиент MCP, читающий контракт, о ней не знал вовсе; `browser.tabs` и
`browser.switchTab` стояли в перечне без схем. И второе оказалось не косметикой: SDK MCP вызывает
колбэк с ДРУГОЙ СИГНАТУРОЙ, если схемы нет, — первым аргументом приходит `extra` вместо `args`
(`@modelcontextprotocol/sdk/…/server/mcp.js`), то есть верба не «без описания», а СЛОМАНА по этому
транспорту. Пустой объект `{}` и отсутствие ключа ведут себя по-разному, и разницу не видно глазом.

⚠ ПОЧЕМУ ГЕЙТ, А НЕ ТРИ ПРАВКИ. Правка чинит три известных случая; следующая верба разъедется так
же, потому что перечни поддерживаются руками. `docs/DEVELOPMENT.md` §0, принцип 5: перечень
ВЫВОДИТСЯ. Отказ рукописного списка односторонний — лишнее в нём видно, а пропущенное представления
не имеет.

⚠ ПОЧЕМУ ЧТЕНИЕ ИСХОДНИКА, А НЕ ИМПОРТ. `TOOL_METHODS` и `schemas` — приватные для модуля: первый
не экспортируется, второй объявлен ВНУТРИ функции `mainMcp()` и вне её не существует. Единственный
способ спросить у кода, что в нём написано, — прочитать код. Границы разбора выведены (минимальный
отступ `case` внутри тела `switch (method)`), а не записаны числом.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO / "pw-executor" / "src" / "server.ts"

# Пол на число. Обязательный спутник вывода: разбор, переставший что-либо находить, прошёл бы
# идеально над пустым множеством — единственное, чего сам вывод не ловит.
FLOOR = 20

# Ветки диспетчера, которые НЕ являются вербами инструмента: это протокол транспорта, а не
# способность браузера. Перечислены здесь, а не выведены, потому что их отличает смысл, а не форма;
# зато список короткий, закрытый и падающий при росте — см. проверку ниже.
NOT_VERBS = {"initialize", "shutdown"}

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def _src() -> "list[str]":
    return SERVER.read_text(encoding="utf-8").splitlines()


def dispatch_cases(lines: "list[str]") -> "set[str]":
    """Ветки `case` ВЕРХНЕГО уровня внутри `switch (method)`.

    Отступ не записан числом, а выведен: внутри тела берётся минимальный отступ среди `case`, и
    только он считается верхним уровнем. Вложенный `switch (condition)` в `browser.expect` живёт
    глубже и потому отсеивается сам — без списка исключений, который пришлось бы пополнять руками
    при каждом следующем вложенном switch.
    """
    start = next((i for i, l in enumerate(lines) if re.match(r"^\s*switch \(method\) \{", l)), None)
    if start is None:
        fail("в server.ts не найден `switch (method)` — диспетчер переехал, и разбор ниже "
             "мерил бы пустоту")
        return set()
    # Конец функции: первая строка, начинающаяся с `}` в нулевой колонке после начала.
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("}")), len(lines))
    body = lines[start:end]
    found = [(len(m.group(1)), m.group(2)) for m in
             (re.match(r"^( *)case '([^']+)':", l) for l in body) if m]
    if not found:
        fail("внутри `switch (method)` не найдено ни одной ветки `case` — разбор сломался")
        return set()
    top = min(ind for ind, _ in found)
    return {name for ind, name in found if ind == top}


def tool_methods(lines: "list[str]") -> "set[str]":
    start = next((i for i, l in enumerate(lines) if re.match(r"^const TOOL_METHODS = \[", l)), None)
    if start is None:
        fail("массив TOOL_METHODS не найден — контракт объявляется где-то ещё")
        return set()
    out = set()
    for l in lines[start + 1:]:
        if l.startswith("]"):
            return out
        m = re.match(r"^\s*'([^']+)',?\s*$", l)
        if m:
            out.add(m.group(1))
    fail("массив TOOL_METHODS не закрыт — разбор дошёл до конца файла")
    return out


def schema_keys(lines: "list[str]") -> "set[str]":
    start = next((i for i, l in enumerate(lines) if "const schemas:" in l), None)
    if start is None:
        fail("объект schemas не найден — регистрация инструментов MCP описана где-то ещё")
        return set()
    out = set()
    for l in lines[start + 1:]:
        if re.match(r"^  \};", l):
            return out
        m = re.match(r"^\s*'([^']+)':", l)
        if m:
            out.add(m.group(1))
    fail("объект schemas не закрыт — разбор дошёл до конца файла")
    return out


def test_every_verb_the_executor_can_do_is_declared_and_described():
    """ТРИ множества обязаны совпасть. Каждое расхождение — свой сорт отказа, и они разные.

    · верба в диспетчере, но не в перечне — способность есть и невидима: клиент, читающий контракт,
      о ней не узнает, а `initialize` объявит неполный набор;
    · верба в перечне, но не в диспетчере — контракт обещает то, чего нет: вызов упадёт «unknown
      method» у того, кто поверил объявлению;
    · верба в перечне без схемы — по MCP она ЛОМАЕТСЯ (иная сигнатура колбэка), а по JSON-RPC
      работает, и потому отказ виден только на одном из двух транспортов.
    """
    lines = _src()
    cases = dispatch_cases(lines) - NOT_VERBS
    declared = tool_methods(lines)
    described = schema_keys(lines)

    if len(cases) < FLOOR:
        fail(f"в диспетчере выведено {len(cases)} верб(ы) при поле {FLOOR} — разбор нашёл слишком "
             f"мало, и равенства ниже стали бы утверждениями о пустоте")

    only_dispatch = sorted(cases - declared)
    only_declared = sorted(declared - cases)
    no_schema = sorted(declared - described)
    orphan_schema = sorted(described - declared)

    if only_dispatch:
        fail(f"вербы есть в диспетчере и НЕТ в TOOL_METHODS: {only_dispatch}. Способность есть, и "
             f"она невидима тому, кто читает контракт")
    if only_declared:
        fail(f"вербы объявлены в TOOL_METHODS и НЕТ в диспетчере: {only_declared}. Контракт обещает "
             f"то, чего исполнитель не умеет")
    if no_schema:
        fail(f"вербы объявлены без схемы: {no_schema}. По MCP это не «без описания», а сломанная "
             f"сигнатура колбэка — SDK передаст `extra` вместо аргументов")
    if orphan_schema:
        fail(f"схемы описывают вербы вне перечня: {orphan_schema}. Регистрация идёт по TOOL_METHODS, "
             f"поэтому такая схема не применяется ни разу и лишь утверждает, что верба есть")

    print(f"  ok  контракт сошёлся: {len(cases)} верб(ы) — диспетчер == TOOL_METHODS == schemas")


def test_the_route_journal_verb_is_part_of_that_contract():
    """ADR-135 назван отдельно, потому что общее равенство выше он удовлетворил бы и не будучи там.

    Равенство трёх множеств зелено и на множестве, где новой вербы нет вовсе. Утверждение о ней
    самой — это утверждение о том, что второй источник фронтира ДОСТИЖИМ обоими транспортами; без
    него верба могла бы уехать в `main` объявленной ровно в одном месте, и мы узнали бы об этом от
    того, кто попробовал позвать её по MCP.
    """
    lines = _src()
    for name, where, got in (("диспетчер", "case", dispatch_cases(lines)),
                             ("перечень", "TOOL_METHODS", tool_methods(lines)),
                             ("схемы", "schemas", schema_keys(lines))):
        if "browser.routes" not in got:
            fail(f"вербы browser.routes нет в «{name}» ({where}) — журнал маршрутов недостижим "
                 f"по одному из транспортов")
    print("  ok  browser.routes объявлена всеми тремя способами")


def test_the_list_of_non_verbs_stays_closed():
    """Встречное к `NOT_VERBS`. Список исключений — рукописный, и потому обязан быть под замком:
    иначе достаточно вписать в него имя, чтобы верба перестала требовать объявления, и гейт станет
    механизмом обхода самого себя."""
    if NOT_VERBS != {"initialize", "shutdown"}:
        fail(f"перечень не-верб изменился: {sorted(NOT_VERBS)}. Это не запрет на правку — это "
             f"требование, чтобы правка была ЗАМЕЧЕНА: каждое новое имя здесь выводит вербу "
             f"из-под контракта")
    print("  ok  перечень не-верб закрыт: только протокол транспорта")


def _checks():
    """Перечень проверок выводится из модуля — как в `test_crawl_boundary_offline.py`."""
    found = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    if len(found) < 3:
        fail(f"найдено {len(found)} проверок вместо трёх — вывод перечня сломался")
    return [f for _, f in found]


def main() -> int:
    if not SERVER.exists():
        print(f"FAIL — {SERVER} не найден")
        return 1
    for fn in _checks():
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("executor verb contract: OK (диспетчер, перечень и схемы описывают одно множество)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
