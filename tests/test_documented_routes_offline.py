#!/usr/bin/env python3
"""[OUTPUTS-ARTIFACT-ROUTE-NAME] — ни один документ не называет адреса control-api, которого нет.

ЧТО БЫЛО ЗАМЕРЕНО. `docs/OUTPUTS.md` и `.en.md` называли `GET /v1/runs/{id}/artifacts/{name}`
(множественное число, имя сегментом пути). Зарегистрирован `GET /v1/runs/{id}/artifact` — единственное
число, имя параметром запроса `?name=`. Предложение верно по СУТИ («логи так не скачать») и ложно по
имени: по названному адресу не скачивается ничего, включая то, что скачивается.

⚠ ДЕФЕКТ НЕ В ОПИСКЕ. Тот же файл ВЫШЕ печатает адрес ВЕРНО — то есть документ содержит собственное
опровержение, и ни одна проверка этого столкновения не замечала. Механизма сверки имён маршрутов с
таблицей `routes()` в дереве не было вовсе, хотя соседний гейт (`test_capabilities_offline.py`) делает
ровно это — но только для рефов из `docs/capabilities.json`. Обе локали несли ОДНУ ошибку, поэтому
двуязычный паритет её не видел.

НАБЛЮДЕНИЕ НЕЗАВИСИМОЕ: истина берётся из таблицы `routes()` и прямых `HandleFunc` — то есть из того,
что РЕГИСТРИРУЕТ сервер, — а не из другого документа. Ни перечня маршрутов, ни перечня файлов гейт не
держит: оба выводятся обходом.

ПРОЗАИЧЕСКИЕ СОКРАЩЕНИЯ РАСКРЫВАЮТСЯ, А НЕ ИГНОРИРУЮТСЯ. Документы законно пишут `DELETE
/v1/{scenarios|tests|chats}/{id}` и `GET /v1/live/{status,frame.jpg}` — одна фраза про семейство
адресов. Замерено: без раскрытия такие формы дают ложные попадания, а список исключений на них был бы
длиннее предмета. Раскрытие решает класс целиком.

Run: .venv/bin/python tests/test_documented_routes_offline.py
"""
import glob
import io
import itertools
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Адреса, которые документ называет законно, хотя сервер их не регистрирует. У каждой записи
# ОБЯЗАТЕЛЕН текст причины; запись, ставшая ненужной, роняет гейт — протухшее исключение есть тихо
# выключенная проверка.
ROUTES_NOT_OURS = {}

VERB = r'(?:GET|POST|PUT|DELETE|PATCH)'
CITE = re.compile(r'\b((?:' + VERB + r')(?:/(?:' + VERB + r'))*)\s+(/v1/[A-Za-z0-9_{}|,/.\-]*)')

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:600])


def norm(path):
    """`{id}` и `{name}` — одно и то же место адреса; сравнение идёт по ФОРМЕ пути, а не по имени
    параметра, иначе гейт краснел бы на законном переименовании."""
    return re.sub(r'\{[^}]*\}', '{}', path).rstrip('.,;:)`').rstrip('/') or '/'


def registered():
    src = io.open(os.path.join(ROOT, 'cmd', 'control-api', 'access.go'), encoding='utf-8').read()
    pats = set(re.findall(r'pattern:\s*"(' + VERB + r' /[^"]*)"', src))
    main = io.open(os.path.join(ROOT, 'cmd', 'control-api', 'main.go'), encoding='utf-8').read()
    pats |= set(re.findall(r'HandleFunc\("(' + VERB + r' /[^"]*)"', main))
    out = set()
    for p in pats:
        verb, path = p.split(' ', 1)
        out.add(verb + ' ' + norm(path))
    return out


def expand(verbs, path):
    """Раскрывает прозаические сокращения в конкретные адреса: `GET/DELETE` и `{a|b}` / `{a,b}`."""
    paths = [path]
    m = re.search(r'\{([A-Za-z0-9_.]+(?:[|,][A-Za-z0-9_.]+)+)\}', path)
    if m:
        paths = [path[:m.start()] + alt + path[m.end():] for alt in re.split(r'[|,]', m.group(1))]
    return [(v, p) for v, p in itertools.product(verbs.split('/'), paths)]


def test_no_document_names_a_route_the_server_does_not_register():
    have = registered()
    check("таблица маршрутов разобрана", len(have) >= 30,
          "разобрано %d маршрутов — разбор отстал от формы исходника, и сравнение ниже вакуумно" % len(have))

    files = sorted(set(glob.glob(os.path.join(ROOT, 'docs', '*.md')) + glob.glob(os.path.join(ROOT, '*.md'))))
    files = [f for f in files if os.path.basename(f) != 'BACKLOG.md']   # реестр задач цитирует и неверные адреса — он о них и заведён
    check("документы обойдены", len(files) >= 20, "файлов %d" % len(files))

    # ⚠ ТОЛЬКО ВНУТРИ КОД-СПАНА, И ЭТО КУПЛЕНО СОБСТВЕННЫМ ПАДЕНИЕМ. Первая редакция брала любое
    # упоминание в прозе — и покраснела на ADR этой же волны, который НАЗЫВАЕТ неверный адрес, чтобы
    # записать, что он неверен. Гейт был прав по букве и вреден по существу: документ обязан иметь
    # право сказать «такого адреса нет». Правило то же, что у гейта имён контрактов: код-спан обещает
    # то, что можно НАБРАТЬ, а проза о нём — нет.
    cited, bad = 0, []
    for path in files:
        for i, line in enumerate(io.open(path, encoding='utf-8'), 1):
            for span in re.findall(r'`([^`\n]+)`', line):
              for m in CITE.finditer(span):
                  for verb, p in expand(m.group(1), m.group(2)):
                      key = verb + ' ' + norm(p)
                      cited += 1
                      if key not in have and key not in ROUTES_NOT_OURS:
                            bad.append("%s:%d %s" % (os.path.relpath(path, ROOT), i, key))
    check("адреса в прозе найдены", cited >= 40,
          "найдено %d упоминаний — извлечение перестало совпадать" % cited)
    check("каждый названный адрес зарегистрирован сервером", not bad,
          "документ обещает адрес, которого нет: %s" % sorted(set(bad)))

    for r in ROUTES_NOT_OURS:
        if r in have:
            check("исключение для %s ещё нужно" % r, False, "сервер зарегистрировал адрес — уберите запись")


def main():
    print("[OUTPUTS-ARTIFACT-ROUTE-NAME] адреса в документации против таблицы маршрутов")
    test_no_document_names_a_route_the_server_does_not_register()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
