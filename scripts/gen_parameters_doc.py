#!/usr/bin/env python3
"""Собрать `docs/PARAMETERS.md` / `.en.md` и `docs/parameters.json` ИЗ ВЫВЕДЕННОГО перечня.

ПОЧЕМУ ГЕНЕРАТОР, А НЕ ЕЩЁ ОДНА ТАБЛИЦА. В дереве уже лежат ЧЕТЫРЕ рукописных перечня переменных
(`docs/TESTING.md`, `LOCAL_MODELS.md`, `WINDOWS_TESTING.md`, `DISTRIBUTION.md`), и все четыре
протухли ОДИНАКОВО и в одну сторону: ни один не знает роль `chat`, реальную с ADR-108b, а двое прямо
пишут «Роли: PLANNER, HEAL». Это ровно тот отказ, который описывает принцип 5 (docs/DEVELOPMENT.md
§0): лишнее в списке видно, ПРОПУЩЕННОЕ — нет. Пятый рукописный перечень протух бы так же, и его
протухание было бы так же невидимо.

Поэтому тело таблицы СОБИРАЕТСЯ из двух источников, ни один из которых не является прозой:
  · `scripts/env_inventory.py` — какие имена продукт читает (шесть форм вывода);
  · офлайновый снимок схемы в мастере — что о них объявлено (дескриптор либо записанная причина).
Снимок побайтово совпадает с живым обработчиком: это держит Go-гейт
`TestSetupWizardSchemaSnapshotMatchesHandler`, сверяющий деревья рекурсивно и в обе стороны.

⚠ ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ И СПОСОБ ЗАДАНИЯ НЕ ХРАНЯТСЯ В ДОКУМЕНТЕ ВТОРОЙ КОПИЕЙ — они берутся из
схемы при КАЖДОЙ сборке. Документ здесь — представление, а не источник; гейт
`tests/test_parameters_documented_offline.py` пересобирает его и требует побайтового совпадения,
поэтому протухший документ краснеет, а не читается как верный.

Run:  .venv/bin/python scripts/gen_parameters_doc.py          # переписать документы
      .venv/bin/python scripts/gen_parameters_doc.py --check  # только сказать, свежи ли они
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from env_inventory import inventory                                    # noqa: E402

BEGIN = "<!-- parameters:env -->"
END = "<!-- /parameters:env -->"

HEAD_RU = "| Переменная | Что делает | Умолчание | Как задаётся |"
HEAD_EN = "| Variable | What it does | Default | How it is set |"
RULE = "|---|---|---|---|"


def wizard_schema():
    html = io.open(os.path.join(ROOT, "docs", "setup", "index.html"), encoding="utf-8").read()
    b = html.index("/* SCHEMA-SNAPSHOT-BEGIN */")
    e = html.index("/* SCHEMA-SNAPSHOT-END */")
    body = html[b:e]
    return json.loads(body[body.index("{"):body.rindex("}") + 1])


def cell(text):
    """⚠ `|` внутри ячейки ЭКРАНИРУЕТСЯ. Без этого строка получает лишние столбцы и в отрисовке, и в
    двуязычном гейте, который сверяет ШИРИНУ каждой строки: замерено на записи вида `argon2id | m=…`,
    и лечение там было тем же."""
    return " ".join(str(text or "").split()).replace("|", "\\|")


def fmt_default(d):
    if d is None:
        return "—"
    if isinstance(d, bool):
        return "`true`" if d else "`false`"
    if d == "":
        return "*(пусто)*"
    return "`%s`" % d


def fmt_default_en(d):
    if d is None:
        return "—"
    if isinstance(d, bool):
        return "`true`" if d else "`false`"
    if d == "":
        return "*(empty)*"
    return "`%s`" % d


def rows():
    """Одна строка на КАЖДОЕ выведенное имя — и опубликованное, и закрытое причиной. Разделять их на
    два документа значило бы дать читателю выбор, в каком из двух искать; а искать он будет по имени."""
    inv = inventory()
    sch = wizard_schema()
    roles = sch.get("roles") or []
    out = {}

    def put(name, ru, en, default, how_ru, how_en, group):
        out[name] = {"name": name, "ru": ru, "en": en, "default": default,
                     "how_ru": how_ru, "how_en": how_en, "group": group}

    for block, how_ru, how_en in (("settings", "настройка развёртывания (интерфейс, `agentctl config set`, окружение)",
                                   "deployment setting (interface, `agentctl config set`, environment)"),
                                  ("service", "окружение процесса службы (compose/systemd)",
                                   "the service process's environment (compose/systemd)")):
        for key, d in sorted((sch.get(block) or {}).items()):
            if not isinstance(d, dict) or not d.get("env"):
                continue
            hint = d.get("hint") or {}
            put(d["env"], hint.get("ru", ""), hint.get("en", ""), d.get("default"), how_ru, how_en, d.get("group", ""))

    for key, d in sorted((sch.get("fields") or {}).items()):
        if not isinstance(d, dict):
            continue
        ways = d.get("set_by") or []
        hint = d.get("hint") or {}
        ru = hint.get("ru") or ("поле прогона `%s`" % key)
        en = hint.get("en") or ("run field `%s`" % key)
        for way in ways:
            if way.startswith("env:"):
                put(way[4:], ru, en, d.get("default"),
                    "поле прогона `%s`; " % key + ", ".join("`%s`" % w for w in ways),
                    "run field `%s`; " % key + ", ".join("`%s`" % w for w in ways), d.get("group", ""))

    # Семейство публикуется ПРАВИЛОМ: ключи блока `llm` × роли. Двадцать четыре записи руками были бы
    # двадцатью четырьмя местами, где можно забыть роль, — ровно то, как потерялась роль `chat`.
    for key, d in sorted((sch.get("llm") or {}).items()):
        if not isinstance(d, dict) or not d.get("env"):
            continue
        hint = d.get("hint") or {}
        base = d["env"]
        put(base, hint.get("ru", ""), hint.get("en", ""), d.get("default"),
            "блок `llm` конфигурации или окружение", "the `llm` config block or the environment", "llm")
        for r in roles:
            put("%s_%s" % (base, r.upper()),
                (hint.get("ru", "") + " — только для роли `%s`" % r).strip(),
                (hint.get("en", "") + " — for the `%s` role only" % r).strip(),
                None,
                "перекрывает `%s` для роли `%s`" % (base, r),
                "overrides `%s` for the `%s` role" % (base, r), "llm")

    for name, why in sorted((sch.get("not_published") or {}).items()):
        put(name, why, why, None, "**не настройка** — см. причину", "**not a setting** — see the reason", "—")

    # ⚠ Строки только для ВЫВЕДЕННЫХ имён. Схема может назвать имя, которого продукт больше не
    # читает; такая строка — протухшая запись, и её ловит гейт, а не этот генератор.
    return [out[n] for n in sorted(out) if n in inv], len(inv)


def table(lang):
    rs, total = rows()
    head = HEAD_RU if lang == "ru" else HEAD_EN
    lines = [BEGIN, head, RULE]
    for r in rs:
        if lang == "ru":
            lines.append("| `%s` | %s | %s | %s |" % (r["name"], cell(r["ru"]), fmt_default(r["default"]), cell(r["how_ru"])))
        else:
            lines.append("| `%s` | %s | %s | %s |" % (r["name"], cell(r["en"]), fmt_default_en(r["default"]), cell(r["how_en"])))
    lines.append(END)
    return "\n".join(lines), len(rs), total


def splice(path, body):
    s = io.open(path, encoding="utf-8").read()
    b = s.index(BEGIN)
    e = s.index(END) + len(END)
    return s[:b] + body + s[e:]


def main():
    check = "--check" in sys.argv
    stale = []
    for lang, path in (("ru", os.path.join(ROOT, "docs", "PARAMETERS.md")),
                       ("en", os.path.join(ROOT, "docs", "PARAMETERS.en.md"))):
        body, n, total = table(lang)
        if not os.path.exists(path):
            print("нет файла %s — сперва создайте каркас с маркерами" % path)
            return 2
        new = splice(path, body)
        if io.open(path, encoding="utf-8").read() != new:
            stale.append(path)
            if not check:
                io.open(path, "w", encoding="utf-8").write(new)
        print("%s: строк %d из %d выведенных имён" % (os.path.basename(path), n, total))
    rs, total = rows()
    jpath = os.path.join(ROOT, "docs", "parameters.json")
    payload = json.dumps({"generated_from": "scripts/env_inventory.py + офлайновый снимок схемы",
                          "count": len(rs), "derived_total": total, "parameters": rs},
                         ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not os.path.exists(jpath) or io.open(jpath, encoding="utf-8").read() != payload:
        stale.append(jpath)
        if not check:
            io.open(jpath, "w", encoding="utf-8").write(payload)
    if check and stale:
        print("ПРОТУХЛО: %s" % ", ".join(os.path.basename(p) for p in stale))
        return 1
    print("готово" if not check else "свежо")
    return 0


if __name__ == "__main__":
    sys.exit(main())
