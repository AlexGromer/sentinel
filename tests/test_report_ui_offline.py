#!/usr/bin/env python3
"""[PROD-REPORT-UI] — отчёт перестал быть артефактом, до которого не дойти.

ЧТО БЫЛО ЗАМЕРЕНО. Серверную половину закрыл ADR-089: control-API после `run.finished` спавнит
`agentctl report` fail-open, а `report.html` и `report.json` лежат в `artifactWhitelist` и отдаются
по `/v1/runs/{id}/artifact`. Файлы писались. До человека они не доходили, потому что панель
артефактов хаба держала СВОЙ список из четырёх имён, и `report.html` в нём не было. Кнопки просто
не существовало — а «нет кнопки» неотличимо от «нет артефакта».

Списков в одном файле было ДВА: короткий в `bShowArtifacts` и длинный `ART_NAMES`. Это и есть
механизм отказа: копия перечня отстаёт в невидимую сторону — лишнее имя даёт мёртвую кнопку и
видно сразу, пропущенное не даёт ничего и не видно никогда.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, и почему именно так. Утверждать «в файле есть строка report.html» бесполезно:
такая проверка совпадает и с комментарием про неё. Проверяется СООТНОШЕНИЕ ДВУХ РЕЕСТРОВ —
множество имён, которое предлагает страница, против множества, которое сервер соглашается отдать
(`artifactWhitelist` в `cmd/control-api/main.go`). Тот же приём, что у `api_projection_test.go`:
авторитетом объявлен код, а не копия списка.

 1. каждое имя со страницы сервер отдаёт (иначе мёртвая кнопка);
 2. `report.html` и `report.json` — на странице (иначе отчёт снова невидим);
 3. панель артефактов пользуется ОДНИМ списком (второго литерального перечня имён в ней нет);
 4. пол на размер обоих множеств — регекс, переставший что-либо находить, прошёл бы идеально.

Офлайн, stdlib.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "docs", "index.html")
CAPI = os.path.join(ROOT, "cmd", "control-api", "main.go")

MIN_SERVER_NAMES = 8   # пол: whitelist сервера заведомо больше горстки
MIN_PAGE_NAMES = 6     # пол: страница предлагает не единицы

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:500])


def read(p):
    return io.open(p, encoding="utf-8").read()


def server_whitelist(src):
    """Имена из `var artifactWhitelist = map[string]bool{ … }` — читаем объявление, а не копию."""
    m = re.search(r"var\s+artifactWhitelist\s*=\s*map\[string\]bool\{(.*?)\n\}", src, re.S)
    assert m, "объявление artifactWhitelist не найдено — гейт потерял свой авторитет"
    return set(re.findall(r'"([^"]+)"\s*:\s*true', m.group(1)))


def page_names(src):
    """Имена из `var ART_NAMES = [ … ]` — единственный перечень артефактов на странице."""
    m = re.search(r"var\s+ART_NAMES\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "ART_NAMES не найден — страница потеряла свой единственный перечень"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def main():
    print("[PROD-REPORT-UI] отчёт достижим из интерфейса")
    hub, capi = read(HUB), read(CAPI)
    server, page = server_whitelist(capi), page_names(hub)

    check("реестр сервера прочитан и непуст", len(server) >= MIN_SERVER_NAMES,
          f"{len(server)} имён, пол {MIN_SERVER_NAMES} — регекс перестал матчить?")
    check("перечень страницы прочитан и непуст", len(page) >= MIN_PAGE_NAMES,
          f"{len(page)} имён, пол {MIN_PAGE_NAMES}")

    dead = sorted(page - server)
    check("страница не предлагает того, чего сервер не отдаёт", not dead,
          f"мёртвые кнопки: {dead}")

    # ОБРАТНАЯ сторона, и она важнее: артефакт, который сервер отдаёт, а страница не называет,
    # невидим — «нет кнопки» неотличимо от «нет файла». Именно так пропал `report.html`. Исключение
    # объявляется ЗДЕСЬ, с причиной, по форме `componentsWithoutProbe`: молчание гейт не принимает.
    NOT_OFFERED = {
        "trace-removed.json": "не файл для скачивания, а маркер: страница читает его и рисует "
                              "пояснение, почему трейса больше нет (SEC-TRACE-SWEPT-SILENTLY). "
                              "Кнопка на него была бы предложением скачать одну строку служебного "
                              "состояния",
    }
    invisible = sorted(server - page - set(NOT_OFFERED))
    check("сервер не отдаёт того, о чём страница молчит", not invisible,
          f"невидимые артефакты: {invisible} — добавьте в ART_NAMES либо объявите исключение с причиной")
    stale_exc = sorted(set(NOT_OFFERED) - server)
    check("исключения не протухли", not stale_exc,
          f"исключение для имени, которого сервер больше не отдаёт: {stale_exc}")

    for must in ("report.html", "report.json"):
        check(f"{must} предлагается человеку", must in page,
              f"артефакт пишется и отдаётся, но кнопки на него нет — ровно закрываемый дефект")
        check(f"{must} отдаётся сервером", must in server,
              "кнопка была бы мёртвой")

    # Панель артефактов обязана пользоваться ЭТИМ списком, а не завести свой второй.
    m = re.search(r"async function bShowArtifacts\(.*?\n  \}", hub, re.S)
    check("тело панели артефактов найдено", bool(m))
    if m:
        body = m.group(0)
        check("панель берёт единый перечень", "varnames=ART_NAMES" in body.replace(" ", ""),
              "панель снова держит свой список — именно так report.html и оказался невидимым")
        second = re.findall(r"\[\s*'[a-z0-9\-]+\.(?:json|html|xml|zip|prom|yaml)'\s*,", body)
        check("второго литерального перечня в панели нет", not second, second)

    # Отчёт СКАЧИВАЕТСЯ: сервер отдаёт .html вложением намеренно, и страница обязана говорить это
    # словом, а не оставлять человека гадать, почему «ссылка» ничего не открыла.
    check("сервер отдаёт .html вложением", 'strings.HasSuffix(name, ".html")' in capi and
          'Content-Disposition", `attachment' in capi,
          "если это изменилось — решение про скачивание надо пересмотреть, а не тихо разойтись")

    # Отсутствие отчёта объяснено: пустая и молчащая панель — то, что запрещает смоук интерфейса.
    check("отсутствие отчёта объясняется, а не молчит",
          "heal-report.json" in hub and "sawReport" in hub,
          "explore/goal-прогон отчёта не имеет по построению; это надо СКАЗАТЬ")

    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print(f"\nALL PASS (сервер {len(server)} имён, страница {len(page)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
