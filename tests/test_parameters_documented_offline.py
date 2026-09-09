#!/usr/bin/env python3
"""[DOC-PARAMETERS-DERIVED] — справка по параметрам ВЫВОДИТСЯ и не может протухнуть молча.

ЧТО БЫЛО ЗАМЕРЕНО. В дереве лежат ЧЕТЫРЕ рукописных перечня переменных (`docs/TESTING.md`,
`LOCAL_MODELS.md`, `WINDOWS_TESTING.md`, `DISTRIBUTION.md`), и все четыре протухли ОДИНАКОВО и в одну
сторону: ни один не знает роль `chat`, реальную с ADR-108b, а двое прямо пишут «Роли: PLANNER, HEAL».
Ни у одной из четырёх таблиц нет гейта. Это ровно тот отказ, который описывает принцип 5
(`docs/DEVELOPMENT.md` §0): лишнее в списке видно, ПРОПУЩЕННОЕ — нет.

Пятый рукописный перечень протух бы так же. Поэтому `docs/PARAMETERS.md` не пишется руками:
`scripts/gen_parameters_doc.py` собирает его из выведенного перечня и схемы, а этот файл требует,
чтобы собранное СОВПАДАЛО с лежащим в репозитории побайтово. Протухшая таблица краснеет, а не
читается как верная.

ЧЕТЫРЕ УТВЕРЖДЕНИЯ, и они разной природы:

 1. ДОКУМЕНТ СВЕЖ — пересборка даёт то же самое, в ОБЕИХ половинах;
 2. ПЕРЕЧЕНЬ НЕ СХЛОПНУЛСЯ — полы на число строк и на долю нелитеральных форм. Обход, переставший
    находить, дал бы пустую таблицу, и «документ свеж» осталось бы правдой над пустотой;
 3. СВЕРКА ДВУСТОРОННЯЯ — каждое выведенное имя названо в документе, и каждое имя из документа
    выводится откуда-то. Второе ловит переименованную переменную: строка про неё осталась бы верной
    по форме и ложной по существу;
 4. ЧЕТЫРЕ СТАРЫХ ПЕРЕЧНЯ НЕ ПРЕТЕНДУЮТ НА ПОЛНОТУ — они рецепты, и обещание полноты в них
    запрещено: два перечня, каждый из которых называет себя полным, — это гарантированное
    расхождение с невидимой половиной.

⚠ ЯЧЕЙКИ РЕЖУТСЯ С УЧЁТОМ ЭКРАНИРОВАНИЯ. Наивный `line.strip("|").split("|")` не видит `\\|`, а
таблица его требует: имена и причины содержат вертикальную черту. Форма разбора взята у
`tests/test_bilingual_content_offline.py::_split_cells`, где та же ловушка уже была куплена.

⚠ ЭТОТ ФАЙЛ ЗАПУСКАЕТСЯ КАК СКРИПТ. CI исполняет каждый `tests/test_*_offline.py` через
`.venv/bin/python "$f"`, а не через pytest, и уже замерено, что файл без собственного `main()` под
pytest бывал ВАКУУМЕН. Отсюда явный сбор функций и `sys.exit(main())`.

Run: .venv/bin/python tests/test_parameters_documented_offline.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from env_inventory import inventory                                    # noqa: E402
import gen_parameters_doc as gen                                       # noqa: E402

FAILS = []

# ПОЛЫ. Замерено 169 имён на 2026-09-08; числа взяты чуть ниже, чтобы пол был тревожкой, а не
# налогом на каждую правку. Пол на НЕЛИТЕРАЛЬНЫЕ формы отдельный и не украшение: обход,
# схлопнувшийся до одной формы, потерял бы ровно ядро — все ролевые модели и шесть операторских
# ручек за питоновскими обёртками, — а общий пол этого не заметил бы.
ROWS_FLOOR = 160
NONLITERAL_FLOOR = 55

# Рецептурные документы: им запрещено обещать полноту, но их таблицы остаются — они отвечают на
# «как включить», а не на «что вообще есть».
RECIPES = ["TESTING.md", "LOCAL_MODELS.md", "WINDOWS_TESTING.md", "DISTRIBUTION.md"]
COMPLETENESS_CLAIMS = ("полная матрица", "полный перечень", "все переменные", "full matrix",
                       "complete list", "every environment variable")


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:600])


def _split_cells(s):
    """Разбор строки таблицы с учётом `\\|`. Скопировано с tests/test_bilingual_content_offline.py —
    там та же ловушка уже куплена: наивный split даёт лишние столбцы и ложное расхождение ширины."""
    out, cur, i = [], [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 2
            continue
        if s[i] == "|":
            out.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(s[i])
        i += 1
    out.append("".join(cur).strip())
    return [c for c in out if c != ""]


def doc_names(path):
    s = io.open(path, encoding="utf-8").read()
    b = s.index(gen.BEGIN)
    e = s.index(gen.END)
    names = []
    for line in s[b:e].split("\n"):
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = _split_cells(line)
        if not cells:
            continue
        m = re.match(r"^`([A-Z][A-Z0-9_]*)`$", cells[0])
        if m:
            names.append(m.group(1))
    return names


def test_the_document_is_fresh():
    """KILLS: правка таблицы руками; добавление переменной без пересборки; удаление переменной из кода
    без пересборки. Любое расхождение между собранным и лежащим — красное."""
    rc = gen.main.__wrapped__() if hasattr(gen.main, "__wrapped__") else None
    argv = sys.argv
    try:
        sys.argv = ["gen", "--check"]
        rc = gen.main()
    finally:
        sys.argv = argv
    check("пересборка даёт то же самое (обе половины и parameters.json)", rc == 0,
          "документ протух — выполните: .venv/bin/python scripts/gen_parameters_doc.py")


def test_the_list_did_not_collapse():
    """Пол. Обход, переставший находить, дал бы пустую таблицу, и «документ свеж» осталось бы правдой
    над пустотой — это единственное, чего сама сверка не ловит."""
    inv = inventory()
    ru = doc_names(os.path.join(ROOT, "docs", "PARAMETERS.md"))
    check("строк в справке не меньше %d" % ROWS_FLOOR, len(ru) >= ROWS_FLOOR, "строк %d" % len(ru))
    nonlit = len([n for n, r in inv.items() if r["forms"] != ["literal"]])
    check("нелитеральных форм не меньше %d" % NONLITERAL_FLOOR, nonlit >= NONLITERAL_FLOOR,
          "нелитеральных %d — вывод схлопнулся до одной формы и потерял ядро" % nonlit)


def test_both_halves_name_the_same_variables():
    """Двуязычие тут не косметика: половина, отставшая на одну строку, — это половина, в которой
    читатель ищет переменную и не находит, при том что она есть."""
    ru = doc_names(os.path.join(ROOT, "docs", "PARAMETERS.md"))
    en = doc_names(os.path.join(ROOT, "docs", "PARAMETERS.en.md"))
    check("обе половины называют одни и те же переменные", ru == en,
          "только в RU: %s; только в EN: %s" % (sorted(set(ru) - set(en))[:8], sorted(set(en) - set(ru))[:8]))


def test_the_check_is_two_way():
    """Вторая сторона ловит то, чего не ловит первая: строка про ПЕРЕИМЕНОВАННУЮ переменную осталась бы
    верной по форме и ложной по существу — имя есть, читателя нет."""
    inv = set(inventory())
    ru = set(doc_names(os.path.join(ROOT, "docs", "PARAMETERS.md")))
    check("каждое выведенное имя названо в справке", not (inv - ru),
          "выводятся и не названы: %s" % sorted(inv - ru)[:10])
    check("каждое имя справки выводится откуда-то", not (ru - inv),
          "названы и не выводятся — переменную переименовали или удалили: %s" % sorted(ru - inv)[:10])


def test_the_recipes_do_not_claim_completeness():
    """Четыре рецептурных документа НЕ УДАЛЯЮТСЯ — они отвечают на «как включить». Но обещать полноту
    им нельзя: два перечня, каждый из которых называет себя полным, — это гарантированное
    расхождение, у которого одна половина невидима."""
    bad = []
    for fn in RECIPES:
        for path in (os.path.join(ROOT, "docs", fn),
                     os.path.join(ROOT, "docs", fn.replace(".md", ".en.md"))):
            if not os.path.exists(path):
                continue
            text = io.open(path, encoding="utf-8").read().lower()
            for claim in COMPLETENESS_CLAIMS:
                if claim in text:
                    bad.append("%s: «%s»" % (os.path.basename(path), claim))
    check("рецептурные документы не обещают полноту", not bad,
          "обещают полноту, не будучи полными: %s — сошлитесь на docs/PARAMETERS.md" % bad)


def main():
    print("[DOC-PARAMETERS-DERIVED] справка по параметрам")
    for fn in (
        test_the_document_is_fresh,
        test_the_list_did_not_collapse,
        test_both_halves_name_the_same_variables,
        test_the_check_is_two_way,
        test_the_recipes_do_not_claim_completeness,
    ):
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
