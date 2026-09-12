#!/usr/bin/env python3
"""[OUTPUTS-APP-FAULTS-STALE-NOT-SUPPORTED] — закрытый пробел не может цитироваться как живое ограничение.

ЧТО БЫЛО ЗАМЕРЕНО. `GAP-PROD-001` закрыт 2026-07-26 (ADR-072) и записан закрытым в `GAPS.md`, а
четыре места в документах продолжали объявлять его ЖИВЫМ ограничением и отсылать «за разбором» в
`REGRESSION_MAP.md` §6 — документ, который на тот же вопрос отвечает противоположно. Цена: читатель,
разбирающийся, почему сборка зелёная при сыплющем исключениями приложении, делает вывод «этого
продукт не умеет» и не ищет ручку, которая есть.

⚠ ЗАПИСЬ РЕЕСТРА ЗАНИЗИЛА ЧИСЛО МЕСТ ВДВОЕ: она называет `docs/OUTPUTS.md` и `.en.md`, а та же ложь
живёт ещё в `docs/OBSERVABILITY.md` и `.en.md` — то есть ровно в том документе, куда приходит человек,
разбирающийся с логами. Правка «по записи буквально» оставила бы половину лжи стоять.

⚠ И ВТОРАЯ ПОЛОВИНА ФРАЗЫ БЫЛА ИСТИННОЙ: «прогон отдаёт `exit 0`» — правда на умолчаниях, потому что
`SENTINEL_FAIL_ON_APP_ERRORS` по умолчанию 0 = «докладывать, не гейтить» (brain/replay.py). Переписать
бульку в «доходят и краснят сборку» значило бы поставить НОВУЮ ложь на место старой.

ПОЧЕМУ ПРОВЕРКА ИМЕННО ТАКОЙ ФОРМЫ. Сначала была написана грубая — «закрытый пробел рядом со словом
отрицания» — и она дала 35 подозрений, почти все ложные: строки журнала ADR упоминают пробел, который
ЗАКРЫЛИ, и отрицание в них относится к другому. Проверка сузилась до точного признака: раздел,
который САМ ЗАЯВЛЯЕТ ОТСУТСТВИЕ («важные ограничения», «Чего здесь нет», «important limits», «What is
missing»). Цитата закрытого пробела там — противоречие ПО ПОСТРОЕНИЮ, а не по интонации.

Исключение ровно одно и оно не список файлов, а ПРИЗНАК: если рядом с пробелом стоит слово о его
закрытии (`закрыт`, `closed`, `RESOLVED`, `ADR-NNN`), то раздел не объявляет его живым, а наоборот —
сообщает, что ограничения больше нет. Такая булька в `OUTPUTS` уже есть, и она правдива.

Run: .venv/bin/python tests/test_resolved_gaps_not_claimed_open_offline.py
"""
import glob
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Признак раздела, ЗАЯВЛЯЮЩЕГО отсутствие. Не список файлов: файлы выводятся глобом, а разделы —
# этим признаком, поэтому новый документ попадает под проверку по построению.
SECTION = re.compile(r'(важные ограничени|Чего здесь нет|Чего нет|important limits|What is missing)', re.I)
CLOSED_NEARBY = re.compile(r'(закрыт|закрыта|закрыто|closed|RESOLVED|ADR-\d{3})', re.I)
GAP = re.compile(r'GAP-[A-Z0-9-]+')

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:600])


def resolved_gaps():
    """Карта пробел → закрыт ли, разобранная из таблицы GAPS.md. Независимое наблюдение: реестр
    пробелов ведётся отдельно от прозы, которая на него ссылается."""
    out = {}
    for name in ("GAPS.md", "GAPS.en.md"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        for line in io.open(path, encoding="utf-8"):
            m = re.match(r"\|\s*(GAP-[A-Z0-9-]+)\s*\|", line)
            if m:
                out[m.group(1)] = ("RESOLVED" in line) or ("✅" in line)
    return out


def limitation_lines():
    """(файл, номер, строка) для каждой строки внутри раздела, заявляющего отсутствие. Раздел
    кончается на следующем заголовке или на следующем выделенном блоке — так он и читается глазами."""
    out = []
    files = sorted(set(glob.glob(os.path.join(ROOT, "docs", "*.md")) + glob.glob(os.path.join(ROOT, "*.md"))))
    for path in files:
        # GAPS.* — САМ реестр: там закрытые пробелы и обязаны стоять, с отметкой о закрытии.
        # BACKLOG.md — реестр ЗАДАЧ, а не документация продукта: его записи ЦИТИРУЮТ дефекты, в том
        # числе давно закрытые, потому что описывают, что было не так до починки. То же исключение и
        # по той же причине уже сделано в scripts/check_bilingual.py («tooling-managed, not end-user
        # docs»). ⚠ Замерено: без этого исключения обход давал два ложных попадания — строки задач не
        # начинаются ни с `#`, ни с `**`, поэтому раздел в BACKLOG не кончался никогда и поглощал файл
        # целиком. Числа и координаты самого реестра проверяет отдельный гейт этой же волны.
        if os.path.basename(path).startswith("GAPS") or os.path.basename(path) == "BACKLOG.md":
            continue
        inside = False
        for i, line in enumerate(io.open(path, encoding="utf-8"), 1):
            if SECTION.search(line):
                inside = True
                out.append((path, i, line))
                continue
            if inside:
                if line.startswith("#") or (line.startswith("**") and not line.strip().startswith("- ")):
                    inside = False
                    continue
                out.append((path, i, line))
    return out, files


def test_no_resolved_gap_is_cited_as_a_live_limitation():
    status = resolved_gaps()
    lines, files = limitation_lines()
    # Полы: обход по пустому множеству «подтверждает» что угодно.
    check("реестр пробелов разобран", len(status) >= 40,
          "разобрано %d пробелов — разбор отстал от формы таблицы" % len(status))
    check("среди них есть закрытые", sum(1 for v in status.values() if v) >= 5,
          "закрытых %d — сравнивать не с чем" % sum(1 for v in status.values() if v))
    check("документы обойдены", len(files) >= 20, "файлов %d" % len(files))
    check("разделы об отсутствии найдены", len(lines) >= 5,
          "строк внутри таких разделов %d — признак перестал совпадать, и проверка ниже вакуумна" % len(lines))

    bad = []
    for path, i, line in lines:
        for g in set(GAP.findall(line)):
            if status.get(g) and not CLOSED_NEARBY.search(line):
                bad.append("%s:%d %s" % (os.path.relpath(path, ROOT), i, g))
    check("закрытый пробел не объявлен живым ограничением", not bad,
          "раздел заявляет отсутствие и ссылается на ЗАКРЫТЫЙ пробел: %s — читатель делает вывод "
          "«этого продукт не умеет» и не ищет ручку, которая есть" % bad)


def main():
    print("[OUTPUTS-APP-FAULTS-STALE-NOT-SUPPORTED] закрытые пробелы против прозы об ограничениях")
    test_no_resolved_gap_is_cited_as_a_live_limitation()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
