#!/usr/bin/env python3
"""Двуязычность по СОДЕРЖИМОМУ, а не по наличию файла — `[DOC-ARCH-EN-DRIFT]`.

Run:  .venv/bin/python tests/test_bilingual_content_offline.py

ЧТО ЭТО ЗАКРЫВАЕТ. `scripts/check_bilingual.py` проверяет два правила: у каждого первичного `*.md`
есть пара `*.en.md`, и наоборот. Третье правило — расхождение содержимого — сведено к сравнению
ЧИСЛА ЗАГОЛОВКОВ и объявлено **WARN-only, never fail**. Таблица, потерявшая записи, заголовков не
меняет, поэтому дыра держалась месяцами и была найдена не гейтом, а случайной попыткой дописать
строку в английскую половину: **ARCHITECTURE.md нёс 128 записей ADR и 147 строк журнала §6, а
ARCHITECTURE.en.md — 90 и 100.** Тридцать восемь решений проекта англоязычному читателю не
существовали.

ПОЧЕМУ ОБХОД ТАБЛИЦЫ, А НЕ СРАВНЕНИЕ ТЕКСТА. Половины — перевод друг друга, поэтому побайтовое
сравнение бессмысленно, а сравнение объёма шумит на каждой длинной фразе. Зато у обеих таблиц есть
КЛЮЧ, который переводу не подлежит: номер ADR и дата записи журнала. Множество ключей обязано
совпадать в обе стороны — это утверждение переживает любой перевод и ловит ровно тот дефект,
который случился.

⚠ ПОЛ НА ЧИСЛО ОБЯЗАТЕЛЕН (docs/DEVELOPMENT.md §0, принцип 5): обход, переставший что-либо находить,
пройдёт идеально над пустым множеством. Сравнение множеств этого не ловит — два пустых множества
равны, — поэтому ниже стоят полы, чуть меньше замеренного.
"""
import os
import re
import sys
from collections import Counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RU = os.path.join(REPO, "ARCHITECTURE.md")
EN = os.path.join(REPO, "ARCHITECTURE.en.md")

# Чуть НИЖЕ замеренного на 2026-08-21 (RU: 128 ADR, 147 строк журнала). Пол растёт вместе с
# таблицами и никогда не опускается: «пол, переживший то, что считал, падает на правде».
ADR_FLOOR = 120
LOG_FLOOR = 140

ADR_ROW = re.compile(r"^\| (ADR-\d+) \|", re.M)
LOG_ROW = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \|", re.M)

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_every_adr_exists_in_both_halves() -> None:
    """Ключ записи — её номер, и он переводу не подлежит."""
    ru, en = ADR_ROW.findall(read(RU)), ADR_ROW.findall(read(EN))
    only_ru = [a for a in ru if a not in set(en)]
    only_en = [a for a in en if a not in set(ru)]
    if only_ru:
        fail(f"{len(only_ru)} запис(ей) ADR есть в ARCHITECTURE.md и НЕТ в ARCHITECTURE.en.md: "
             f"{', '.join(only_ru[:12])}{' …' if len(only_ru) > 12 else ''} — англоязычный читатель "
             f"о них не знает, а гейт наличия файла этого не видит")
    if only_en:
        fail(f"{len(only_en)} запис(ей) ADR есть только в английской половине: {', '.join(only_en[:12])} "
             f"— первичная половина русская, значит либо запись потеряна в ней, либо выдумана здесь")


def test_every_changelog_row_exists_in_both_halves() -> None:
    """Мультимножество, а не множество: в один день бывает несколько записей журнала."""
    ru, en = Counter(LOG_ROW.findall(read(RU))), Counter(LOG_ROW.findall(read(EN)))
    missing = {d: ru[d] - en.get(d, 0) for d in ru if ru[d] - en.get(d, 0) > 0}
    extra = {d: en[d] - ru.get(d, 0) for d in en if en[d] - ru.get(d, 0) > 0}
    if missing:
        total = sum(missing.values())
        sample = ", ".join(f"{d}×{n}" for d, n in sorted(missing.items())[:8])
        fail(f"{total} строк(и) журнала §6 отсутствуют в английской половине: {sample}"
             f"{' …' if len(missing) > 8 else ''}")
    if extra:
        fail(f"строки журнала есть только в английской половине: "
             f"{', '.join(f'{d}×{n}' for d, n in sorted(extra.items())[:8])}")


def test_the_floors_hold() -> None:
    """Пустой обход равен пустому обходу — только счёт отличает «совпало» от «нечего сравнивать»."""
    for path, name in ((RU, "ARCHITECTURE.md"), (EN, "ARCHITECTURE.en.md")):
        text = read(path)
        n_adr, n_log = len(ADR_ROW.findall(text)), len(LOG_ROW.findall(text))
        if n_adr < ADR_FLOOR:
            fail(f"{name}: {n_adr} записей ADR при поле {ADR_FLOOR} — либо таблица усохла, либо обход "
                 f"перестал её находить, и оба случая выглядят как «расхождений нет»")
        if n_log < LOG_FLOOR:
            fail(f"{name}: {n_log} строк журнала при поле {LOG_FLOOR} — то же самое")


# ⚠ ЗДЕСЬ БЫЛА ПРОВЕРКА ШИРИНЫ СТОЛБЦОВ, И ОНА УДАЛЕНА, А НЕ ОБЁРНУТА ИСКЛЮЧЕНИЕМ.
# Замысел был честный: строка на столбец короче ломает таблицу молча. Но считать столбцы по числу
# символов `|` нельзя — они законно встречаются ВНУТРИ текста ячеек (в кодовых вставках и в прозе),
# и обе таблицы этого файла имеют разное число столбцов. Замерено при первом же запуске: 139 строк
# из 147 в русской половине объявлены «неправильной ширины» при том, что таблица целая.
# Детектор, срабатывающий на законный контент, в этом доме удаляют — иначе его научатся молча
# игнорировать, и он перестанет ловить то, ради чего заводился. Ширину столбцов ловит человек,
# открывающий отрисованный документ, и это записано как граница, а не забыто.


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    ru_text = read(RU)
    print(f"bilingual content: OK ({len(ADR_ROW.findall(ru_text))} ADR, "
          f"{len(LOG_ROW.findall(ru_text))} строк журнала — множества совпадают в обе стороны, "
          f"полы {ADR_FLOOR}/{LOG_FLOOR} держатся)")
