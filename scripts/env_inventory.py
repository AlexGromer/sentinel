#!/usr/bin/env python3
"""Перечень переменных окружения продукта — ВЫВОДИТСЯ обходом читателей, а не поддерживается списком.

ЗАЧЕМ ЭТОТ МОДУЛЬ. `agentctl config schema` обещает «every knob the product has, with its env name
and default» (cmd/agentctl/api.go). Обещание проверяемо только против ПЕРЕЧНЯ, а перечня не было:
любое число, которым его пробовали измерить, оказывалось невоспроизводимым — реестр называл ~174,
один замер дал 117, другой 128, третий 146, и ни под одним из них не лежало списка имён.

Причина в том, КАК отказывает рукописный список (docs/DEVELOPMENT.md §0, принцип 5): лишнее в нём
видно — запись про удалённую переменную роняет прогон, — а ПРОПУЩЕННОЕ не видно, потому что у
отсутствия нет представления, на которое можно посмотреть. Поэтому здесь перечень выводится, а
модуль общий: его читают и гейт схемы, и гейт документации, чтобы «сколько у нас ручек» не стало
двумя разными числами в двух файлах.

ШЕСТЬ ФОРМ ЧТЕНИЯ, И КАЖДАЯ КУПЛЕНА ПРОМАХОМ. Обход по одной форме был бы зелёным над ядром:

 1. ЛИТЕРАЛЬНАЯ — `os.Getenv/LookupEnv`, `os.environ.get/getenv/[…]`, `process.env.X`. Даёт
    большинство имён и не даёт ни одного из перечисленных ниже.
 2. ОБЁРТКА С ЛИТЕРАЛЬНЫМ ПЕРВЫМ АРГУМЕНТОМ. Go: `envStr|envMB|envInt|envOr|envEnabled|envDisabled|
    logEnvMB`. Python: `_int_env`, и — ЭТО ГЛАВНЫЙ ПРОМАХ ПЕРВОЙ РЕДАКЦИИ — ещё `_env_conf`,
    `_env_flag`, `_env_int`. За тремя пропущенными прячутся ШЕСТЬ операторских ручек
    (SENTINEL_HEAL_AUTO, HEAL_FLAG, SLOW_LOAD_MS, VISUAL_AUTHORITATIVE, FAIL_ON_HEAL,
    FAIL_ON_APP_ERRORS) — то есть политика падения прогона и поведение самолечения. Гейт, написанный
    по неполной регулярке, был бы зелёным над отсутствием ровно этих шести.
 3. ЧТЕНИЕ ПО ПЕРЕДАННОМУ ОТОБРАЖЕНИЮ — `env.X` в TypeScript и `<получатель>.get("ИМЯ")` в Python,
    где получатель бывает однобуквенным. Резолвер наблюдения читает окружение именно так.
 4. ИМЕНОВАННАЯ КОНСТАНТА — `export const *_ENV = 'ИМЯ'`.
 5. СЕМЕЙСТВО ИЗ f-СТРОКИ — `LLM_{KEY}[_{ROLE}]` (brain/llm.py). Литерально не находится НИ ОДНО из
    ролевых имён, а среди них весь операторский набор моделей, включая роль `chat`. Ключи и роли
    берутся из КОДА, а не из докстринга: докстринг brain/llm.py уже был опровергнут собственным
    кодом (обещал две роли и пять ключей при трёх и шести).
 6. ИНТЕРПОЛЯЦИИ РАЗВЁРТЫВАНИЯ — `${VAR}` в compose-файлах. Их читает не наш код, а докер, поэтому
    обход по исходникам их не видит по построению; собираются отдельно и помечаются `compose`.

ОТСЕЧЕНИЕ КОММЕНТАРИЕВ ОБЯЗАТЕЛЬНО. Без него в перечень попадает `NAME` из фразы
`# a secret referenced by env — process.env.NAME! — stays a ref` (brain/importer.py) — имя, которое
никто не читает. Ложное срабатывание в выводимом перечне хуже, чем в рукописном: рукописный правит
человек, а выводимый начинает требовать записи о несуществующей ручке.

Run: .venv/bin/python scripts/env_inventory.py    # печатает перечень и числа
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ⚠ ИСКЛЮЧЕНИЯ ОБЪЯВЛЯЮТСЯ ЯВНО (docs/DEVELOPMENT.md §0, принцип 5). Замер, купивший это правило:
# один `uv sync` не из того каталога превратил обход 37 файлов в обход 4733 и заставил проверку
# предъявлять требования чужим пакетам. `.claude/worktrees` даёт полные копии репозитория, поэтому
# каждое имя нашлось бы трижды и «покрытие» считалось бы по чужим файлам.
SKIP_DIRS = {
    ".git", ".claude", "node_modules", "testdata", "runs", "scratch", "state", "bin", "dist",
    ".venv", "venv", "__pycache__", "ui-smoke", "ui-press", "multiuser-e2e", "site-packages",
}
# Корпус ПРОДУКТА. Гейты, стенды и тесты сюда не входят: переменная, которую читает только проверка,
# — это её приспособление, а не ручка продукта, и требовать для неё дескриптора значило бы
# публиковать внутренний инструментарий.
CORPUS = [("cmd", (".go",)), ("internal", (".go",)), ("brain", (".py",)), ("pw-executor/src", (".ts",))]

# ⚠ МИНИМУМ ДВА СИМВОЛА, а не три. При `{2,}` имя `CI` было невидимо ОБЕИМИ поддерживаемыми
# формами разом (brain/__main__.py `os.environ.get("CI", …)` и brain/observe.py `e.get("CI", …)`),
# и знаменатель, объявленный ЗАКРЫТЫМ, молча не содержал переменную, которая решает
# `fatal.force_replay_in_ci`. Замерено: во всём корпусе продукта двухсимвольное чтение ровно одно,
# так что расширение не даёт ложных срабатываний — оно возвращает единственное пропущенное.
NAME = r"([A-Z][A-Z0-9_]+)"
PATTERNS = [
    ("literal", re.compile(r'os\.(?:Getenv|LookupEnv)\(\s*"' + NAME + '"')),
    ("literal", re.compile(r'os\.environ(?:\.get|\.setdefault|\.pop)?\(\s*["\']' + NAME + '["\']')),
    ("literal", re.compile(r'os\.getenv\(\s*["\']' + NAME + '["\']')),
    ("literal", re.compile(r'os\.environ\[\s*["\']' + NAME + '["\']')),
    ("literal", re.compile(r"process\.env\." + NAME + r"\b")),
    ("literal", re.compile(r'process\.env\[\s*["\']' + NAME + '["\']')),
    ("mapping", re.compile(r"\benv\." + NAME + r"\b")),
    # ⚠ Получатель — ЛЮБОЕ имя, оканчивающееся на `env`, а не только однобуквенное. При `[a-z]`
    # форма ловила `e.get("X")` и не ловила `env.get("X")`, поэтому `SENTINEL_EXPLICIT` и `SCENARIO`
    # (brain/runconfig.py) проваливались между двумя поддерживаемыми формами: для атрибутной они не
    # атрибуты, для отображения — получатель длиннее буквы. Дверь произвольным словарям это не
    # открывает: замерено, что в brain/*.py `.get("ЗАГЛАВНОЕ")` встречается только у `os.environ`
    # и у `env`.
    ("mapping", re.compile(r'\b(?:[a-z]|[a-z_]*env)\.get\(\s*["\']' + NAME + '["\']')),
    ("wrapper", re.compile(r'\b(?:envStr|envMB|envInt|envOr|envEnabled|envDisabled|logEnvMB)\(\s*"' + NAME + '"')),
    ("wrapper", re.compile(r'\b(?:_env_conf|_tok_budget|_limit|_env_flag|_env_int|_int_env|_int_or_none)'
                           r'\(\s*["\']' + NAME + '["\']')),
    ("const", re.compile(r'_ENV[A-Za-z_]*\s*[:=]\s*["\']' + NAME + '["\']')),
]

# Форма 5. Ключи и роли ВЫВОДЯТСЯ из кода: `_env(role, "KEY")` и `_DEFAULT_MODEL`.
FAMILY_SOURCE = os.path.join("brain", "llm.py")
FAMILY_PREFIX = "LLM_"


def _files():
    for sub, exts in CORPUS:
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in sorted(filenames):
                if not fn.endswith(exts):
                    continue
                if fn.endswith("_test.go") or ".test." in fn:
                    continue
                yield os.path.join(dirpath, fn)


def _strip_comment(line, path):
    """Отсечь комментарий. Грубо и намеренно: строковый литерал с `//` внутри потеряет хвост, но имя
    переменной в такой строке — это уже не чтение окружения. Обратная ошибка дороже: без отсечения
    имя из ПРОЗЫ становится ручкой, которую гейт потребует задокументировать."""
    if path.endswith((".go", ".ts")):
        i = line.find("//")
    else:
        i = line.find("#")
    return line if i < 0 else line[:i]


def family_names():
    """Ролевые имена семейства — по коду, не по докстрингу (докстринг уже был опровергнут кодом)."""
    src = open(os.path.join(ROOT, FAMILY_SOURCE), encoding="utf-8").read()
    keys = sorted(set(re.findall(r'_env\(\s*role\s*,\s*"([A-Z_]+)"\s*\)', src)))
    block = re.search(r"_DEFAULT_MODEL\s*=\s*\{(.*?)\n\}", src, re.S)
    roles = sorted(set(re.findall(r'"([a-z_]+)"\s*:', block.group(1)))) if block else []
    out = {}
    if not keys or not roles:
        return out, keys, roles
    for k in keys:
        out[FAMILY_PREFIX + k] = "family"
        for r in roles:
            out["%s%s_%s" % (FAMILY_PREFIX, k, r.upper())] = "family"
    return out, keys, roles


def compose_names():
    """Форма 6: `${VAR}` в compose-файлах. Их читает докер, а не наш код, поэтому обходом исходников
    они не находятся ПО ПОСТРОЕНИЮ — а оператор видит именно их."""
    out = {}
    for fn in sorted(os.listdir(ROOT)):
        if not (fn.startswith("docker-compose") and fn.endswith((".yml", ".yaml"))):
            continue
        # ⚠ КОММЕНТАРИИ ОТСЕКАЮТСЯ И ЗДЕСЬ. Та же болезнь, что в исходниках, и она сработала: из
        # фразы «Every default below uses `${VAR-fallback}` (a single dash…)» в перечень попадало имя
        # `VAR`, которого не существует. Ложное срабатывание в ВЫВОДИМОМ перечне хуже, чем в
        # рукописном: гейт начинает требовать записи о несуществующей ручке, и её приходится писать.
        for line in open(os.path.join(ROOT, fn), encoding="utf-8"):
            i = line.find("#")
            if i >= 0:
                line = line[:i]
            for m in re.finditer(r"\$\{([A-Z][A-Z0-9_]{2,})", line):
                out.setdefault(m.group(1), []).append(fn)
    return out


def inventory():
    """{имя: {'forms': {...}, 'sites': [путь:строка, …]}} по всему продуктовому корпусу."""
    found = {}
    for path in _files():
        rel = os.path.relpath(path, ROOT)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for lineno, raw in enumerate(text.split("\n"), 1):
            line = _strip_comment(raw, path)
            for form, rx in PATTERNS:
                for m in rx.finditer(line):
                    for g in m.groups():
                        if not g:
                            continue
                        rec = found.setdefault(g, {"forms": set(), "sites": []})
                        rec["forms"].add(form)
                        if len(rec["sites"]) < 4:
                            rec["sites"].append("%s:%d" % (rel, lineno))
    fam, keys, roles = family_names()
    for name in fam:
        rec = found.setdefault(name, {"forms": set(), "sites": []})
        rec["forms"].add("family")
        if not rec["sites"]:
            rec["sites"].append("%s (семейство: %d ключей × %d ролей)" % (FAMILY_SOURCE, len(keys), len(roles)))
    for name, files in compose_names().items():
        rec = found.setdefault(name, {"forms": set(), "sites": []})
        rec["forms"].add("compose")
        if not rec["sites"]:
            rec["sites"].append(files[0])
    for rec in found.values():
        rec["forms"] = sorted(rec["forms"])
    return found


def main():
    inv = inventory()
    by_form = {}
    for name, rec in inv.items():
        for f in rec["forms"]:
            by_form.setdefault(f, []).append(name)
    if "--json" in sys.argv:
        print(json.dumps({k: v for k, v in sorted(inv.items())}, ensure_ascii=False, indent=1))
        return 0
    print("выведено имён: %d" % len(inv))
    for f in sorted(by_form):
        print("  %-8s %3d" % (f, len(by_form[f])))
    _, keys, roles = family_names()
    print("семейство: %d ключей × (1 + %d ролей)" % (len(keys), len(roles)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
