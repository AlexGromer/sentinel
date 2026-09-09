#!/usr/bin/env python3
"""[UI-EMBED-AND-ALLOWLIST-NOT-CHECKED] — встроенное в бинарь и разрешённое к отдаче наконец сверяются.

ЧТО БЫЛО ЗАМЕРЕНО. Файл попадает в развёртывание двумя независимыми списками: строкой `//go:embed` в
`docs/embed.go` (что вшито в бинарь) и `uiPathAllowed` в `cmd/control-api/ui.go` (что разрешено
отдавать). Равенство этих двух списков не сверяло НИЧТО — единственной гарантией был комментарий
«mirrors» рядом с одним из них.

Цена расхождения асимметрична и обе половины тихие:
  · встроено, но не разрешено — страница молча отдаёт **404 в развёртывании при зелёном CI**;
  · разрешено, но не встроено — тот же 404, но диагноз уводит в сторону раздачи, а не в сборку.
Ни один существующий гейт этого не ловит: DOM-гейты ходят по страницам, которые УЖЕ отдаются, а
`//go:embed` — директива компилятора, о которой тесты ничего не спрашивают.

ПОЧЕМУ ОБА МНОЖЕСТВА ВЫВОДЯТСЯ ИЗ ИСХОДНИКОВ. Написать здесь ожидаемый список значило бы завести
ТРЕТИЙ список, который тоже надо держать в согласии с двумя первыми, — то есть увеличить число мест,
где можно разойтись, вместо того чтобы уменьшить. Поэтому оба разбираются: один из директив
`//go:embed`, другой из тела `uiPathAllowed`.

Плюс пол: разбор, переставший что-либо находить, дал бы два ПУСТЫХ множества, которые прекрасно
равны друг другу.

Run: .venv/bin/python tests/test_ui_embed_allowlist_offline.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []

# Полы. Сегодня по семь имён в каждом множестве (четыре файла + два новых + каталоги); числа взяты
# чуть ниже, чтобы пол был тревожкой, а не налогом.
MIN_EMBEDDED = 6
MIN_ALLOWED = 6


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:500])


def embedded_names():
    """Имена из всех директив `//go:embed` файла docs/embed.go."""
    src = io.open(os.path.join(ROOT, "docs", "embed.go"), encoding="utf-8").read()
    out = set()
    for line in src.split("\n"):
        line = line.strip()
        if not line.startswith("//go:embed"):
            continue
        for tok in line[len("//go:embed"):].split():
            out.add(tok)
    return out


def allowed_names():
    """Имена из `uiPathAllowed`: и каталоги из цикла, и файлы из `switch`."""
    src = io.open(os.path.join(ROOT, "cmd", "control-api", "ui.go"), encoding="utf-8").read()
    i = src.index("func uiPathAllowed(")
    body = src[i:src.index("\nfunc ", i + 10)]
    dirs = re.search(r'range \[\]string\{([^}]*)\}', body)
    out = set(re.findall(r'"([^"]+)"', dirs.group(1))) if dirs else set()
    sw = re.search(r"switch name \{(.*?)\n\t\}", body, re.S)
    if sw:
        for case in re.findall(r"case ([^:]+):", sw.group(1)):
            # ⚠ Комментарии внутри case отсекаются: без этого в множество попадают слова из прозы,
            # и гейт начинает требовать несуществующих файлов. Та же болезнь, что в выводе перечня
            # переменных, где из комментария приходили `NAME` и `VAR`.
            case = re.sub(r"//[^\n]*", "", case)
            out |= set(re.findall(r'"([^"]+)"', case))
    return out


def test_the_parse_found_things():
    e, a = embedded_names(), allowed_names()
    print("       встроено %d, разрешено %d" % (len(e), len(a)))
    check("разбор `//go:embed` не пуст (пол %d)" % MIN_EMBEDDED, len(e) >= MIN_EMBEDDED, sorted(e))
    check("разбор `uiPathAllowed` не пуст (пол %d)" % MIN_ALLOWED, len(a) >= MIN_ALLOWED, sorted(a))


def test_the_two_lists_are_equal():
    """KILLS: добавление файла в `//go:embed` без правки `uiPathAllowed` и наоборот. Обе половины
    дают молчаливый 404 в развёртывании при зелёном CI."""
    e, a = embedded_names(), allowed_names()
    check("встроенное разрешено к отдаче", not (e - a),
          "встроено и НЕ разрешено — файл в бинаре и отдаёт 404: %s" % sorted(e - a))
    check("разрешённое встроено", not (a - e),
          "разрешено и НЕ встроено — раздача обещает файл, которого в бинаре нет: %s" % sorted(a - e))


def test_every_named_path_exists():
    """Третье утверждение, и его не покрывают первые два: два списка могут прекрасно совпадать и
    называть файл, которого в дереве нет. `//go:embed` на несуществующий путь ломает сборку, а
    `uiPathAllowed` — нет, поэтому проверяется он в первую очередь."""
    missing = sorted(n for n in embedded_names() | allowed_names()
                     if not os.path.exists(os.path.join(ROOT, "docs", n)))
    check("каждое названное имя существует в docs/", not missing, "нет в дереве: %s" % missing)


def main():
    print("[UI-EMBED-AND-ALLOWLIST-NOT-CHECKED] встроенное против разрешённого")
    for fn in (test_the_parse_found_things, test_the_two_lists_are_equal, test_every_named_path_exists):
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
