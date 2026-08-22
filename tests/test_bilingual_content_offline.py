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
    # ⚠ ПОД PYTEST ЭТОТ ФАЙЛ БЫЛ ВАКУУМЕН, и это замер, а не опасение: 2026-08-22 `python3 -m
    # pytest tests/test_bilingual_content_offline.py -q` напечатал «6 passed» на английской
    # половине, обрезанной по §6 — то есть ровно на том дефекте, ради которого файл и заведён.
    # Причина: функции ничего не утверждают, они КОПЯТ сообщения, а печатает и роняет прогон
    # блок `__main__`, которого pytest не исполняет. CI гоняет офлайн-набор скриптами
    # (`.github/workflows/ci.yml`, шаг «Python offline suite»), поэтому в CI гейт настоящий —
    # но человек, проверяющий свою правку через pytest, получал зелёный на сломанном документе.
    # Копящая форма нужна и сохранена: скрипт обязан показать ВСЕ расхождения за один проход,
    # иначе правка идёт по одному дефекту за прогон. Под pytest же поднимаем сразу.
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise AssertionError(msg)


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


# --- Структура: заголовки и таблицы ---------------------------------------------------------
#
# ⚠ ЗДЕСЬ БЫЛА ПРОВЕРКА ШИРИНЫ СТОЛБЦОВ, УДАЛЁННАЯ 2026-08-21 СО СЛОВАМИ «считать столбцы по
# числу символов `|` нельзя — они законно встречаются ВНУТРИ текста ячеек». Замер, стоявший за
# удалением, верен: та версия объявила «неправильной ширины» 139 строк из 147 в русской половине.
# Неверен был ВЫВОД из него. Проверка переписана ПОД настоящее устройство GFM, а не обёрнута
# исключением, потому что причина тех 139 срабатываний оказалась НАСТОЯЩИМ дефектом разметки:
#
#   1. GFM режет строку на ячейки ДО разбора инлайнов. Поэтому `|` внутри кодовой вставки —
#      НЕ «законный контент ячейки», а живой разделитель: `` `a||b` `` даёт два лишних столбца
#      и в отрисованной таблице. Остановить его может только `\|`, и обратный слэш GFM затем
#      съедает сам. Замерено 2026-08-22: 21 такой символ в каждой половине, 9 строк.
#   2. Разбор по одному символу с учётом `\|` (`_split_cells`) отличает разделитель от
#      экранированного. Прежняя версия этого не делала — она и не могла: считала `|` подряд.
#   3. Оставшиеся после экранирования расхождения были вторым дефектом, не шумом: 14 строк
#      журнала в русской половине и 2 в английской несли ЛИТЕРАЛЬНЫЙ `\n` (два символа) после
#      закрывающей `|`, а две из них — ещё и приклеенный `---`. Это давало пятый столбец
#      в четырёхстолбцовой таблице.
#
# Итог замера: 19 строк неверной ширины в RU и 7 в EN до правки, 0 и 0 после. Детектор, который
# «срабатывает на законном контенте», в этом доме удаляют — но сперва проверяют, законен ли
# контент. Здесь он не был.

FENCE = re.compile(r"^\s*(?:```|~~~)")
DELIM = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
HEADING = re.compile(r"^(#{1,6}) +(.*)$")
SECTION_NO = re.compile(r"^(\d+)\.")

# Полы: чуть НИЖЕ замеренного 2026-08-22 — у обеих половин 21 заголовок, 7 таблиц, 316 строк
# таблиц. Пустой обход равен пустому обходу, поэтому сравнение множеств без пола проходит
# вакуумно, а именно вакуумный проход этот файл и заводился ловить.
HEADING_FLOOR = 18
TABLE_FLOOR = 6
TABLE_ROW_FLOOR = 300


def _split_cells(line: str) -> "list[str]":
    r"""Ячейки строки таблицы — деление по НЕЭКРАНИРОВАННЫМ `|`, посимвольно.

    `line.split("|")` здесь неверен ровно тем, чем был неверен удалённый предшественник: он не
    видит `\|`. Ведущая и замыкающая пустые ячейки отбрасываются — GFM их не считает.
    """
    s, parts, cur, i = line.strip(), [], [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2]); i += 2; continue
        if s[i] == "|":
            parts.append("".join(cur)); cur = []; i += 1; continue
        cur.append(s[i]); i += 1
    parts.append("".join(cur))
    if parts and not parts[0].strip():
        parts.pop(0)
    if parts and not parts[-1].strip():
        parts.pop()
    return parts


def _tables(text: str) -> "tuple[list[list[tuple[int, int]]], list[int]]":
    """(таблицы, осиротевшие строки). Таблица — список (номер строки, число ячеек).

    Таблицей считается то, что таблицей считает отрисовщик: строка с `|`, под которой стоит
    строка-разделитель. Всё остальное, начинающееся с `|`, — СИРОТА: markdown печатает её
    абзацем с голыми палками. Так и выглядели 33 строки, отрезанные пустыми строками внутри
    таблиц (17 в русской половине, 16 в английской, замер 2026-08-22).
    """
    lines = text.split("\n")
    tables: "list[list[tuple[int, int]]]" = []
    claimed: "set[int]" = set()
    fenced: "set[int]" = set()
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            fenced.add(i)
            continue
        if in_fence:
            fenced.add(i)

    i = 0
    while i < len(lines):
        if i in fenced:
            i += 1; continue
        if "|" in lines[i] and i + 1 < len(lines) and i + 1 not in fenced \
                and "|" in lines[i + 1] and DELIM.match(lines[i + 1]):
            rows = [(i + 1, len(_split_cells(lines[i])))]
            claimed.update((i, i + 1))
            j = i + 2
            while j < len(lines) and j not in fenced and lines[j].strip().startswith("|"):
                rows.append((j + 1, len(_split_cells(lines[j]))))
                claimed.add(j)
                j += 1
            tables.append(rows)
            i = j
            continue
        i += 1

    orphans = [n + 1 for n, line in enumerate(lines)
               if line.strip().startswith("|") and n not in claimed and n not in fenced]
    return tables, orphans


def _headings(text: str) -> "list[tuple[int, str]]":
    """Структурный ключ заголовка: (уровень, номер раздела) — то, что переводу НЕ подлежит.

    Текст заголовка сравнивать нельзя: половины — перевод друг друга. Уровень и номер раздела
    переживают перевод, и именно их расхождение было дефектом: английская половина обрывалась
    на §6, теряя §7, §8 и весь раздел расширений — 21 заголовок против 15 (замер 2026-08-22).
    """
    out = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING.match(line)
        if m:
            no = SECTION_NO.match(m.group(2).strip())
            out.append((len(m.group(1)), no.group(1) if no else "—"))
    return out


def test_heading_structure_matches_in_both_halves() -> None:
    """Множество И ПОРЯДОК заголовков, по уровню и номеру раздела."""
    ru, en = _headings(read(RU)), _headings(read(EN))
    if len(ru) != len(en):
        fail(f"заголовков: ARCHITECTURE.md {len(ru)}, ARCHITECTURE.en.md {len(en)} — "
             f"половины описывают документ разной формы; недостающие ключи: "
             f"{sorted(set(ru) - set(en)) or sorted(set(en) - set(ru))}")
        return
    drift = [(i + 1, a, b) for i, (a, b) in enumerate(zip(ru, en)) if a != b]
    if drift:
        sample = "; ".join(f"#{i}: RU {a} vs EN {b}" for i, a, b in drift[:8])
        fail(f"{len(drift)} заголовк(ов) стоят на разных местах или уровнях: {sample}"
             f"{' …' if len(drift) > 8 else ''} — совпадение ЧИСЛА заголовков ничего не значит, "
             f"если порядок разошёлся")


def test_table_rows_have_the_same_width_in_both_halves() -> None:
    """Таблица за таблицей, строка за строкой — одинаковое число столбцов.

    Проверяется И согласованность внутри половины (строка против своей шапки), И между
    половинами: две ОДИНАКОВО сломанные половины прошли бы одну лишь сверку пары.
    """
    ru_t, ru_orphans = _tables(read(RU))
    en_t, en_orphans = _tables(read(EN))

    for name, tables in (("ARCHITECTURE.md", ru_t), ("ARCHITECTURE.en.md", en_t)):
        for t_no, rows in enumerate(tables, 1):
            header = rows[0][1]
            bad = [(n, w) for n, w in rows if w != header]
            if bad:
                sample = ", ".join(f"строка {n}: {w} столбц(ов)" for n, w in bad[:6])
                fail(f"{name}, таблица #{t_no}: шапка объявляет {header} столбц(ов), а "
                     f"{len(bad)} строк(и) несут другое ({sample}{' …' if len(bad) > 6 else ''}) "
                     f"— неэкранированная `|` внутри ячейки рвёт таблицу на лишние столбцы")

    for name, orphans in (("ARCHITECTURE.md", ru_orphans), ("ARCHITECTURE.en.md", en_orphans)):
        if orphans:
            fail(f"{name}: {len(orphans)} строк(и) начинаются с `|`, но не принадлежат ни одной "
                 f"таблице (строки {orphans[:8]}{' …' if len(orphans) > 8 else ''}) — под ними нет "
                 f"шапки с разделителем, обычно из-за ПУСТОЙ СТРОКИ внутри таблицы; отрисовщик "
                 f"печатает их абзацем с голыми палками")

    if len(ru_t) != len(en_t):
        fail(f"таблиц: ARCHITECTURE.md {len(ru_t)}, ARCHITECTURE.en.md {len(en_t)} — "
             f"сравнивать построчно нечего")
        return
    for t_no, (ru_rows, en_rows) in enumerate(zip(ru_t, en_t), 1):
        if len(ru_rows) != len(en_rows):
            fail(f"таблица #{t_no}: {len(ru_rows)} строк в русской половине против "
                 f"{len(en_rows)} в английской")
            continue
        mism = [(r[0], r[1], e[0], e[1]) for r, e in zip(ru_rows, en_rows) if r[1] != e[1]]
        if mism:
            sample = "; ".join(f"RU:{a} {b} стлб vs EN:{c} {d} стлб" for a, b, c, d in mism[:6])
            fail(f"таблица #{t_no}: {len(mism)} строк(и) разной ширины между половинами: {sample}")


def test_the_structural_floors_hold() -> None:
    """Полы на структуру — по той же причине, что полы на записи: обход мог перестать находить."""
    for path, name in ((RU, "ARCHITECTURE.md"), (EN, "ARCHITECTURE.en.md")):
        text = read(path)
        tables, _ = _tables(text)
        n_head, n_tab = len(_headings(text)), len(tables)
        n_rows = sum(len(t) for t in tables)
        if n_head < HEADING_FLOOR:
            fail(f"{name}: {n_head} заголовк(ов) при поле {HEADING_FLOOR} — разделы исчезли, "
                 f"либо разбор перестал их видеть")
        if n_tab < TABLE_FLOOR:
            fail(f"{name}: {n_tab} таблиц(ы) при поле {TABLE_FLOOR} — то же самое")
        if n_rows < TABLE_ROW_FLOOR:
            fail(f"{name}: {n_rows} строк таблиц при поле {TABLE_ROW_FLOOR} — то же самое")


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
    ru_tables, _ = _tables(ru_text)
    # Гейт обязан ПРОИЗНОСИТЬ, что он сверил: молчаливый «OK» неотличим от проверки, которая
    # ничего не нашла и потому ни на что не пожаловалась.
    print(f"bilingual content: OK ({len(ADR_ROW.findall(ru_text))} ADR, "
          f"{len(LOG_ROW.findall(ru_text))} строк журнала — множества совпадают в обе стороны, "
          f"полы {ADR_FLOOR}/{LOG_FLOOR} держатся)")
    print(f"bilingual structure: OK ({len(_headings(ru_text))} заголовков, {len(ru_tables)} таблиц, "
          f"{sum(len(x) for x in ru_tables)} строк таблиц — уровни, номера разделов и ширина каждой "
          f"строки совпадают в обеих половинах, сирот нет, "
          f"полы {HEADING_FLOOR}/{TABLE_FLOOR}/{TABLE_ROW_FLOOR} держатся)")
