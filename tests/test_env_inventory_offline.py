#!/usr/bin/env python3
"""[CONFIG-SCHEMA-PUBLISHES-42-OF-174] — обещание «every knob the product has» стало ИЗМЕРИМЫМ.

ЧТО БЫЛО ЗАМЕРЕНО. `agentctl config schema` обещает «every knob the product has, with its env name
and default». Схема публиковала 42 дескриптора, и лишь 22 из них несли имя переменной — у всего
блока `fields` его не было вовсе. Но главным был не разрыв, а то, что РАЗРЫВ БЫЛО НЕЧЕМ ИЗМЕРИТЬ:
знаменатель называли «~174», один замер дал 117, другой 128, третий 146, и ни под одним из них не
лежало списка имён. Обещание, у которого нет перечня, не нарушается и не исполняется — оно просто
не имеет истинностного значения.

ЧТО УТВЕРЖДАЕТСЯ ЗДЕСЬ, и это ДВЕ РАЗНЫЕ ВЕЩИ:

 1. ЗНАМЕНАТЕЛЬ ЗАКРЫТ. Каждое выведенное имя — ЛИБО опубликовано дескриптором, ЛИБО названо в
    `not_published` с записанной причиной. Третьего состояния («ни там, ни там») не существует, и
    именно оно было нормой. Отсюда следует, что остатка «на следующую волну» не образуется по
    построению: новая переменная краснит гейт в день появления.

 2. ОПУБЛИКОВАННОЕ ДОЕЗЖАЕТ. Дескриптор в `settings` — это ОБЕЩАНИЕ ДОСТАВКИ, и оно бывает ложным:
    замерено, что `HEAL_LLM` стоял в схеме, интерфейс предлагал его сохранить, сохранение
    подтверждалось — и до прогона значение не доезжало ВООБЩЕ. Дважды: имени не было в аллоулисте
    окружения `agentctl`, и безусловный run-var затирал его нулём поверх унаследованного. Проверка
    ниже сверяет доставку с НЕЗАВИСИМЫМ наблюдением — разбором самого аллоулиста и перечня run-var,
    — а не повторяет формулу той функции, которая доставку и строит.

ПОЧЕМУ СХЕМА ЧИТАЕТСЯ ИЗ СНИМКА МАСТЕРА. Обработчик схемы — Go, поднимать его отсюда нельзя (сьют
офлайновый и без стендов). Мастер настройки несёт офлайновый снимок той же схемы, и его побайтовое
совпадение с живым обработчиком держит Go-гейт `TestSetupWizardSchemaSnapshotMatchesHandler`,
сверяющий деревья рекурсивно и в обе стороны. Цепочка замкнута: этот файл ↔ снимок ↔ (Go-гейт) ↔
обработчик. Читать снимок и НЕ иметь такого гейта было бы суррогатом; он есть, и это записано здесь,
чтобы следующий читатель не принял снимок за самостоятельный источник.

Run: .venv/bin/python tests/test_env_inventory_offline.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from env_inventory import inventory                                    # noqa: E402

FAILS = []

# ПОЛЫ. Обход, переставший что-либо находить, проходит идеально над пустым множеством — это
# единственное, чего сам вывод не ловит (docs/DEVELOPMENT.md §0, принцип 5). Числа взяты чуть ниже
# замеренных на 2026-09-08, чтобы пол был тревожкой, а не налогом на каждую правку.
TOTAL_FLOOR = 160          # замерено 170
LITERAL_FLOOR = 95         # замерено 105
NONLITERAL_FLOOR = 55      # замерено 65 (обёртки 30 + отображения 32 + константы 4 + семейство 24, за вычетом пересечений)
FAMILY_FLOOR = 20          # замерено 24 = 6 ключей × (1 + 3 роли)
COMPOSE_FLOOR = 15         # замерено 22
RUNVAR_FLOOR = 18          # замерено 20 в литерале `extra` (HEAL_LLM дописывается условно ВНЕ него)


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:700])


def wizard_schema():
    """Снимок схемы из мастера. См. довод в докстринге: цепочка к живому обработчику замкнута
    Go-гейтом, поэтому здесь читается снимок, а не поднимается сервер."""
    html = open(os.path.join(ROOT, "docs", "setup", "index.html"), encoding="utf-8").read()
    b = html.index("/* SCHEMA-SNAPSHOT-BEGIN */")
    e = html.index("/* SCHEMA-SNAPSHOT-END */")
    body = html[b:e]
    i = body.index("{")
    j = body.rindex("}")
    return json.loads(body[i:j + 1])


def published_names(schema):
    """Имена, которые схема НАЗЫВАЕТ: через `env` дескриптора и через `set_by` вида `env:ИМЯ`."""
    names = {}
    for block in ("settings", "service", "llm"):
        for key, d in (schema.get(block) or {}).items():
            if isinstance(d, dict) and d.get("env"):
                names[d["env"]] = "%s.%s" % (block, key)
    for key, d in (schema.get("fields") or {}).items():
        if not isinstance(d, dict):
            continue
        if d.get("env"):
            names[d["env"]] = "fields.%s" % key
        for way in d.get("set_by") or []:
            if way.startswith("env:"):
                names[way[4:]] = "fields.%s" % key
    # Семейство публикуется ПРАВИЛОМ, а не двадцатью четырьмя записями: ключи блока `llm` × роли.
    roles = schema.get("roles") or []
    for key, d in (schema.get("llm") or {}).items():
        if not isinstance(d, dict) or not d.get("env"):
            continue
        for r in roles:
            names["%s_%s" % (d["env"], r.upper())] = "llm.%s×role" % key
    return names


def agentctl_delivery():
    """НЕЗАВИСИМОЕ наблюдение доставки: разбираем сам аллоулист и перечень run-var, а не повторяем
    формулу функции, которая окружение строит."""
    src = open(os.path.join(ROOT, "cmd", "agentctl", "main.go"), encoding="utf-8").read()
    body = src[src.index("func filteredEnv()"):]
    body = body[:body.index("\nfunc ", 10)]
    exact = set(re.findall(r'"([A-Z][A-Z0-9_]*)":\s*true', body))
    prefixes = re.search(r'prefixes := \[\]string\{([^}]*)\}', body)
    prefixes = re.findall(r'"([A-Z_]+)"', prefixes.group(1)) if prefixes else []
    # Run-var, дописываемые БЕЗУСЛОВНО: они идут после унаследованного окружения, и os/exec берёт
    # последнее значение — то есть такое имя не наследуется, чем бы его ни разрешал аллоулист.
    extra = re.search(r'extra := \[\]string\{(.*?)\n\t\}', src, re.S)
    unconditional = set(re.findall(r'"([A-Z][A-Z0-9_]*)=', extra.group(1))) if extra else set()
    return exact, prefixes, unconditional


def deliverable(name, exact, prefixes, unconditional):
    if name in unconditional:
        return False, "затирается безусловным run-var — унаследованное значение до мозга не доедет"
    if name in exact:
        return True, "точное имя в аллоулисте"
    for p in prefixes:
        if name.startswith(p):
            return True, "префикс %s" % p
    return False, "нет ни точного имени, ни разрешённого префикса — filteredEnv срежет"


def test_the_walk_still_finds_things():
    """Первое утверждение — про сам обход. Регулярка, переставшая совпадать, даёт ПУСТОЙ перечень, и
    всё, что ниже, прошло бы идеально: «ноль имён без записи» — правда над пустым множеством."""
    inv = inventory()
    forms = {}
    for rec in inv.values():
        for f in rec["forms"]:
            forms[f] = forms.get(f, 0) + 1
    print("       выведено %d имён; по формам: %s" % (len(inv), forms))
    check("перечень не схлопнулся (пол %d)" % TOTAL_FLOOR, len(inv) >= TOTAL_FLOOR,
          "выведено %d" % len(inv))
    check("литеральная форма жива (пол %d)" % LITERAL_FLOOR, forms.get("literal", 0) >= LITERAL_FLOOR,
          forms)
    # ⚠ ОТДЕЛЬНЫЙ ПОЛ НА НЕЛИТЕРАЛЬНЫЕ, и он не украшение: обход, схлопнувшийся до одной формы,
    # потерял бы ровно ядро — все ролевые модели, шесть операторских ручек за питоновскими обёртками
    # и обе именованные константы, — а общий пол этого не заметил бы.
    nonlit = len([n for n, r in inv.items() if r["forms"] != ["literal"]])
    check("нелитеральные формы живы (пол %d)" % NONLITERAL_FLOOR, nonlit >= NONLITERAL_FLOOR,
          "нелитеральных %d" % nonlit)
    fam = len([n for n, r in inv.items() if "family" in r["forms"]])
    check("семейство разворачивается (пол %d)" % FAMILY_FLOOR, fam >= FAMILY_FLOOR, "семейство %d" % fam)
    comp = len([n for n, r in inv.items() if "compose" in r["forms"]])
    check("compose-имена собираются (пол %d)" % COMPOSE_FLOOR, comp >= COMPOSE_FLOOR, "compose %d" % comp)


def test_every_derived_name_is_published_or_explained():
    """ГЛАВНОЕ УТВЕРЖДЕНИЕ ВОЛНЫ: третьего состояния нет.

    KILLS: удаление дескриптора; удаление записи причины; появление новой переменной без того и
    другого — она краснит гейт в день появления, а не «в следующую волну»."""
    inv = inventory()
    schema = wizard_schema()
    published = published_names(schema)
    explained = schema.get("not_published") or {}
    orphans = sorted(n for n in inv if n not in published and n not in explained)
    check("каждое выведенное имя опубликовано или объяснено",
          not orphans,
          "без дескриптора и без причины (%d): %s" % (len(orphans), ", ".join(orphans)))
    # Обратная сторона: причина, записанная для имени, которого больше никто не читает, — это
    # протухшая запись, и она так же вредна, как отсутствующая.
    stale = sorted(n for n in explained if n not in inv)
    check("нет причин для имён, которых больше нет", not stale,
          "объяснены, но не выводятся ниоткуда (%d): %s" % (len(stale), ", ".join(stale)))
    empty = sorted(n for n, why in explained.items() if not (why or "").strip())
    check("у каждой причины есть текст", not empty, "пустые причины: %s" % ", ".join(empty))


def test_every_published_setting_actually_reaches_a_run():
    """ВТОРОЕ УТВЕРЖДЕНИЕ, и оно ловит то, чего первое не ловит: имя может быть опубликовано и не
    работать. Замерено на `HEAL_LLM` — он был в схеме, интерфейс его сохранял, подтверждал, и до
    прогона значение не доезжало ни разу.

    KILLS: перенос записи HEAL_LLM ОБРАТНО ВНУТРЬ литерала `extra`; удаление его из аллоулиста;
    добавление в `settings` любой новой ручки, до прогона не доезжающей.

    ⚠ ЧЕГО ЭТА ПРОВЕРКА НЕ ЛОВИТ, И ЭТО ЗАПИСАНО ЗДЕСЬ, А НЕ УМОЛЧАНО. Раньше строка выше обещала
    «возврат безусловного run-var для HEAL_LLM». Замерено, что ДОБРОСОВЕСТНЫЙ откат W15-правки её
    не будит: `unconditional` собирается регуляркой по ОДНОМУ литералу `extra := []string{…}`, а
    запись HEAL_LLM живёт ЗА ним (`extra = append(extra, …)` под `if setFlags[...]`). Снять `if`,
    оставив `append`, — и множество `unconditional` не меняется, `deliverable("HEAL_LLM")` отвечает
    «точное имя в аллоулисте», гейт зелёный над восстановленным дефектом. Утверждение о ФОРМЕ
    ИСХОДНИКА не может закрыть доставку; её закрывает наблюдение за окружением, которое мозг реально
    получил, — cmd/agentctl/heal_llm_delivery_test.go."""
    schema = wizard_schema()
    exact, prefixes, unconditional = agentctl_delivery()
    check("аллоулист разобран, а не пуст", len(exact) > 10 and prefixes,
          "exact=%d prefixes=%s — разбор сломался, и все утверждения ниже стали бы вакуумными"
          % (len(exact), prefixes))
    # ТРЕТИЙ разбор тоже нуждается в поле, и до W16 его не было: `unconditional` собирается одной
    # регуляркой, а при промахе тихо становится пустым множеством (`if extra else set()`). Пустое
    # множество делает `deliverable` разрешающим для ВСЕХ имён, то есть проверка ниже проходит
    # идеально ровно тогда, когда перестала что-либо измерять. Замерено: переименование `extra` в
    # `runVars` роняет 20 имён до 0 молча.
    check("перечень безусловных run-var разобран (пол %d)" % RUNVAR_FLOOR,
          len(unconditional) >= RUNVAR_FLOOR and "RUN_MODE" in unconditional,
          "unconditional=%d — разбор литерала `extra` сломался, и вся проверка стала вакуумной"
          % len(unconditional))
    bad = []
    for key, d in (schema.get("settings") or {}).items():
        env = d.get("env") if isinstance(d, dict) else None
        if not env:
            continue
        okd, why = deliverable(env, exact, prefixes, unconditional)
        if not okd:
            bad.append("settings.%s (%s): %s" % (key, env, why))
    check("каждая настройка развёртывания доезжает до прогона", not bad,
          "объявлены настройкой и не доезжают:\n        " + "\n        ".join(bad))


def test_every_runvar_agentctl_writes_is_published_or_explained():
    """ЗНАМЕНАТЕЛЬ, ВЗЯТЫЙ СНАРУЖИ ОБХОДА, — и это весь смысл проверки.

    `test_every_derived_name_is_published_or_explained` бежит квантором по `inventory()`, то есть по
    выходу того самого обхода, чью полноту оно и должно было бы удостоверять: имя, невидимое
    регуляркам, сиротой стать не может, и «третьего состояния нет» остаётся правдой ровно потому,
    что третье состояние не видно. Замерено: так пропали `CI` (короче трёх символов) и `SCENARIO` с
    `SENTINEL_EXPLICIT` (читаются как `env.get(...)`, а форма ждала однобуквенного получателя).

    Здесь множество строится разбором Go-исходника, который run-var ПИШЕТ, а не регуляркой по тем,
    кто их читает. Сузить обход и усыпить эту проверку одним движением нельзя.

    KILLS: любое новое имя в литерале `extra` без дескриптора и без записанной причины — красное в
    день появления; сужение регулярок обхода — этой проверке безразлично."""
    src = open(os.path.join(ROOT, "cmd", "agentctl", "main.go"), encoding="utf-8").read()
    extra = re.search(r'extra := \[\]string\{(.*?)\n\t\}', src, re.S)
    runvars = sorted(set(re.findall(r'"([A-Z][A-Z0-9_]*)=', extra.group(1)))) if extra else []
    # Пол ОБЯЗАТЕЛЕН и по той же причине, что у соседей: разбор — одна регулярка, привязанная к
    # форме литерала, и её обрыв даёт пустое множество, над которым «все имена объяснены» — правда.
    check("run-var агентctl разобраны (пол %d)" % RUNVAR_FLOOR, len(runvars) >= RUNVAR_FLOOR,
          "разобрано %d имён — регулярка литерала `extra` сломалась" % len(runvars))
    schema = wizard_schema()
    published = published_names(schema)
    explained = schema.get("not_published") or {}
    orphans = [n for n in runvars if n not in published and n not in explained]
    check("каждый run-var опубликован или объяснён", not orphans,
          "агентctl кладёт мозгу, а схема о них молчит (%d): %s" % (len(orphans), ", ".join(orphans)))


def test_service_block_does_not_promise_delivery():
    """Зеркальное утверждение. Блок `service` заведён ровно потому, что часть ручек читает САМ
    СЕРВИС, а не прогон; положить их в `settings` значило бы пообещать доставку, которой нет, — то
    есть завести новый `HEAL_LLM`. Здесь проверяется, что блоки не перепутаны в обратную сторону."""
    schema = wizard_schema()
    svc = schema.get("service") or {}
    if not svc:
        print("       блок service ещё не заведён — утверждение неприменимо")
        return
    both = sorted(set(svc) & set(schema.get("settings") or {}))
    check("ни один ключ не объявлен и настройкой, и службой", not both, "в обоих блоках: %s" % both)
    noenv = sorted(k for k, d in svc.items() if not (isinstance(d, dict) and d.get("env")))
    check("у каждой служебной ручки названо имя переменной", not noenv,
          "без env: %s — ручка службы задаётся ТОЛЬКО окружением, безымянная бесполезна" % noenv)


# Сколько проверок в этом файле — ВЫВОДИТСЯ, а не перечисляется. Пол на случай, если вывод
# перестанет находить: обход по пустому множеству прошёл бы идеально (принцип 5).
TESTS_FLOOR = 5            # замерено 5


def main():
    print("[CONFIG-SCHEMA-PUBLISHES-42-OF-174] знаменатель обещания «every knob»")
    # ⚠ БЫЛО РУКОПИСНЫМ КОРТЕЖЕМ, и он ровно этим и отказал: добавленная в W16 проверка
    # `test_every_runvar_agentctl_writes_is_published_or_explained` в него не попала и МОЛЧА не
    # исполнялась — тест, который не запускается, не может упасть, и от пройденного он неотличим.
    # Та же болезнь, что чинит весь этот PR, в рантайме самого гейта. Теперь перечень выводится из
    # модуля, а пол ловит вывод, переставший находить.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    if len(fns) < TESTS_FLOOR:
        print("FAIL — обнаружено %d проверок при поле %d: вывод сломался" % (len(fns), TESTS_FLOOR))
        return 1
    print("проверок обнаружено: %d" % len(fns))
    for fn in fns:
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
