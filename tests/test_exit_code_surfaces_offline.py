#!/usr/bin/env python3
"""ADR-141 — код выхода доходит до человека ОДИНАКОВО ВЕЗДЕ.

ЗАЧЕМ ЭТОТ ФАЙЛ. Каталог `brain/events.json` → `exit_codes` объявлен источником правды с ADR-087, и
ADR-113 записал, что три таблицы сведены в одну. Замер 2026-08-31 показал, что не сведены: код
выхода становился видимым человеку по ТРИНАДЦАТИ адресам, и каталог читал ровно ОДИН из них.
Остальные держали собственные таблицы — восемь штук, — и они не совпадали:

  · `docs/index.html` `B_SEVCOL`      — шесть родов против семи в каталоге, седьмой уходил в
                                        МОЛЧАЛИВЫЙ откат `|| 'var(--mut)'`, и exit 5 красился СЕРЫМ;
  · `docs/index.html` `chFinalize`    — цепочка на четыре кода, остальное голой строкой БЕЗ ЦВЕТА;
  · `docs/index.html` `tRenderRuns`   — три состояния, коды 1..5 неразличимы на глаз;
  · `docs/index.html` `verdictColor`/`verdictIcon` — по СЛОВУ, три слова, остальное жёлтым «⚠»;
  · `cmd/control-api` `verdictEnum`   — `default: return "problem"` съедал 4, 5 и -1;
  · `brain/report.py` `_EXIT_COLOR`   — 0..3, а серый был ЗНАЧЕНИЕМ ПО УМОЛЧАНИЮ у `.get`;
  · `brain/junit.py`                  — отличимую форму получал только код 3;
  · `brain/outcome.py` `VERDICT_WORD` — восьмая копия, чей комментарий называл каталог источником
                                        правды, каталог при этом не читая.

Замеренное следствие: ОДИН прогон с кодом 5 рисовался тремя противоречащими способами, а панель
результатов говорила «problem» жёлтым — то есть обвиняла приложение пользователя в том, что
сломались мы.

ЧТО УТВЕРЖДАЕТСЯ ЗДЕСЬ И ПОЧЕМУ ЭТОГО НЕЛЬЗЯ ПРОЙТИ ЧАСТИЧНОЙ ПРАВКОЙ. Ни одно множество здесь не
записано руками: роды берутся из каталога, документы — обходом дерева, коды — из каталога. Поэтому
запись, добавленная в каталог завтра, попадает под все проверки ПО ПОСТРОЕНИЮ, а не потому, что
кто-то вспомнил дописать её сюда. У каждого вывода есть ПОЛ на число: обход, переставший что-либо
находить, прошёл бы идеально над пустым множеством, и это единственное, чего сам вывод не ловит.

⚠ ЧЕГО ЭТОТ ФАЙЛ ДОКАЗАТЬ НЕ МОЖЕТ, сказано прямо. Он читает ФОРМУ исходников — таблицы цветов в
`docs/index.html` и `brain/report.py`, — а форма согласна и с кодом, который никогда не исполняется.
Поведение этих поверхностей проверяет `scripts/hub-dom-check.mjs` на живом headless Chromium, и
именно там добавлены коды 4, 5 и -1. Разделение намеренное: здесь — «второй таблицы не появилось»,
там — «нарисовано то, что надо». Ни одна из двух проверок не заменяет другую, и это ЗАМЕРЕНО:
DOM-гейт был зелёным всё время, пока exit 5 красился серым, потому что он не знал про exit 5.

Офлайн: без сети, без браузера, без бинарей.
Запуск: .venv/bin/python tests/test_exit_code_surfaces_offline.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def catalog() -> dict:
    return json.loads(read(os.path.join("brain", "events.json")))


# Пол на число. Обязательный спутник вывода — см. docs/DEVELOPMENT.md §0, принцип 5.
CODE_FLOOR = 7        # замерено 2026-08-31: 0, 1, 2, 3, 4, 5, -1
SEVERITY_FLOOR = 7    # столько же родов, по одному на код
# Чуть НИЖЕ замеренного на 2026-08-31 (12 документов = шесть пар RU/EN). Пол может только расти.
# ⚠ Замер стоит того, чтобы его назвать: реестр говорил про ТРИ пары, разведка нашла ЧЕТЫРЕ, а обход
# дерева — ШЕСТЬ. Ровно поэтому состав выводится, а не перечисляется: три пропущенные пары
# (DETERMINISM, QUICKSTART, WINDOWS_TESTING) не были видны никому, включая тех, кто искал.
DOC_TABLE_FLOOR = 10


def declared() -> "tuple[set[str], set[str], set[str]]":
    """(коды, роды, слова вердикта) — всё из каталога, ничего отсюда."""
    exits = catalog()["exit_codes"]
    return (set(exits), {e["severity"] for e in exits.values()},
            {e["verdict"] for e in exits.values()})


def js_object_keys(src: str, name: str) -> "set[str]":
    """Ключи литерала `var NAME={a:..., b:...}` из JS. Пусто — если литерала нет."""
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        return set()
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))


def py_dict_keys(src: str, name: str) -> "set[str]":
    """Ключи литерала `NAME = {"a": ..., "b": ...}` из Python."""
    m = re.search(re.escape(name) + r"\s*=\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))


def test_the_derivation_still_finds_things() -> None:
    """Пустой обход покрывает пустое множество идеально — ловит это только счёт."""
    codes, sevs, words = declared()
    if len(codes) < CODE_FLOOR:
        fail(f"каталог объявляет {len(codes)} кодов, пол {CODE_FLOOR} — вывод обмелел")
    if len(sevs) < SEVERITY_FLOOR:
        fail(f"каталог объявляет {len(sevs)} родов, пол {SEVERITY_FLOOR}")
    if not words:
        fail("ни одна запись `exit_codes` не объявляет `verdict` — слову вердикта неоткуда взяться")


def test_every_colour_table_covers_exactly_the_catalogue_severities() -> None:
    """Цвет — свойство страницы, а РОДЫ — свойство продукта. Сверка идёт В ОБЕ СТОРОНЫ.

    Непокрытый род — это молчаливый серый на живом прогоне (так exit 5 и был потерян).
    Лишний ключ — это род, которого в каталоге больше нет: он не вредит, но он же и признак того,
    что таблицу правили руками, а не по каталогу.
    """
    _, sevs, _ = declared()
    tables = {
        "docs/index.html::B_SEVCOL": js_object_keys(read("docs/index.html"), "B_SEVCOL"),
        "brain/report.py::_SEV_COLOR": py_dict_keys(read(os.path.join("brain", "report.py")),
                                                    "_SEV_COLOR"),
    }
    for where, keys in tables.items():
        if not keys:
            fail(f"{where}: таблица цветов не найдена — гейт читал бы пустоту и был бы зелёным")
            continue
        missing = sevs - keys
        extra = keys - sevs
        if missing:
            fail(f"{where}: род(ы) {sorted(missing)} не покрыты — на живом прогоне это молчаливый "
                 f"серый, ровно как exit 5 до ADR-141")
        if extra:
            fail(f"{where}: ключ(и) {sorted(extra)} каталогу неизвестны — таблицу правили руками")


def test_no_surface_keeps_its_own_exit_code_chain() -> None:
    """В хабе не должно остаться второй таблицы «код → представление».

    Проверяется форма, и это осознанно (см. ⚠ в докстринге): цепочка `code===N` в функции, которая
    рисует вердикт, — ровно тот отпечаток, который оставили `chFinalize` и `tRenderRuns`.
    """
    hub = read("docs/index.html")
    # Литеральные сравнения кода выхода с числом — вне единственного разрешённого читателя.
    chains = re.findall(r"code\s*===\s*-?\d+", hub)
    if chains:
        fail(f"docs/index.html: {len(chains)} литеральных сравнений кода выхода "
             f"({sorted(set(chains))}) — это вторая таблица; всё представление идёт через "
             f"bExitPresent()/bExitBadge()")
    for fn in ("bExitPresent", "bExitBadge"):
        if f"function {fn}(" not in hub:
            fail(f"docs/index.html: нет функции {fn}() — единственного читателя каталога не стало")
    # Единственный читатель `exit_codes` на странице — bExitInfo; verdictColor/verdictIcon читают
    # ту же таблицу по слову. Больше никто не имеет права её открывать.
    readers = set(re.findall(r"function\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*\{[^}]*lgCatalog\.exit_codes",
                             hub))
    allowed = {"bExitInfo", "verdictSeverity", "verdictIcon"}
    if readers - allowed:
        fail(f"docs/index.html: каталог `exit_codes` открывают {sorted(readers - allowed)} — "
             f"читатель должен быть один (bExitInfo), остальные спрашивают его")


def test_no_surface_spells_out_a_list_of_verdict_words() -> None:
    """Легенда, перечисляющая вердикты руками, — это девятая копия таблицы.

    ⚠ ЭТУ НАШЁЛ СКРИНШОТ, А НЕ ГЕЙТ, и это стоит записать: под списком результатов, который к тому
    моменту уже красился ИЗ КАТАЛОГА, стояла подпись «цвет = verdict: ✓ pass · ⚠ problem ·
    ▲ regression · ✖ integrity» — четыре слова с иконами и цветами, вписанными в строку. Она
    протухла в день выпуска кода 4 и утверждала читателю, что вердиктов четыре, ровно над строкой,
    показывающей пятый. Признак такой копии — ДВА И БОЛЕЕ слова вердикта в одной строке исходника:
    одно слово это сравнение (`v==='pass'`), а два подряд — перечень.
    """
    _, _, words = declared()
    hub = read("docs/index.html").splitlines()
    for n, line in enumerate(hub, 1):
        # Слово как ЛИТЕРАЛ разметки (`>pass<`), а не как строка сравнения (`v==='pass'`).
        literal = {w for w in words if re.search(r"[>\s·]" + re.escape(w) + r"\s*<", line)}
        if len(literal) >= 2:
            fail(f"docs/index.html:{n}: перечислены вердикты {sorted(literal)} — это рукописная "
                 f"легенда, она протухнет на следующем коде; строить из каталога")


def test_the_go_and_python_verdict_words_come_from_the_catalogue() -> None:
    """Ни `verdictEnum`, ни `VERDICT_WORD` не имеют права держать свои слова."""
    go = read(os.path.join("cmd", "control-api", "main.go"))
    m = re.search(r"func verdictEnum\(exit int\) string \{(.*?)\n\}", go, re.S)
    if not m:
        fail("cmd/control-api/main.go: verdictEnum не найдена")
    else:
        body = m.group(1)
        if "eventcatalog.ExitInfoOf" not in body:
            fail("verdictEnum не читает каталог — вернулась собственная таблица слов")
        if re.search(r'case\s+-?\d+\s*:', body):
            fail("verdictEnum снова разбирает коды `case`-ами — это и была съедавшая 4/5/-1 копия")
    py = read(os.path.join("brain", "outcome.py"))
    m2 = re.search(r"^VERDICT_WORD\s*=\s*(.+)$", py, re.M)
    if not m2:
        fail("brain/outcome.py: VERDICT_WORD не найдена")
    elif "exit_codes()" not in m2.group(1):
        fail("brain/outcome.py: VERDICT_WORD снова литерал, а не вывод из каталога")


def doc_tables() -> "dict[str, set[str]]":
    """Документы с таблицей кодов выхода → множество кодов, которые они называют.

    Состав документов ВЫВОДИТСЯ обходом дерева, а не перечисляется: пропущенный документ иначе не
    имел бы представления, на которое можно посмотреть. Признак таблицы — строка markdown, чья
    ПЕРВАЯ ячейка целиком является кодом выхода (с любым обрамлением `**`/`` ` ``).
    """
    row = re.compile(r"^\|\s*[`*]{0,2}(-?\d+)[`*]{0,2}\s*\|")
    found: "dict[str, set[str]]" = {}
    skip = {".git", "node_modules", ".venv", "runs", "scratch", "dist", "worktrees"}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fn in files:
            # `*.local.md` — личные, незакоммиченные заметки мейнтейнера (.gitignore:1197). Требовать
            # от них полноты нельзя: их нет ни у кого другого, и гейт краснел бы на чужой машине по
            # причине, которой в репозитории не существует.
            if not fn.endswith(".md") or fn.endswith(".local.md"):
                continue
            rel = os.path.relpath(os.path.join(base, fn), ROOT)
            codes = {m.group(1) for m in (row.match(ln) for ln in read(rel).splitlines()) if m}
            # Таблица кодов выхода, а не любая таблица с числом в первой ячейке: требуем, чтобы в ней
            # были и 0, и 1 — пара, которой нет ни у одной другой нумерованной таблицы в дереве.
            if {"0", "1"} <= codes:
                found[rel] = codes
    return found


def test_every_document_that_tabulates_exit_codes_names_all_of_them() -> None:
    """Таблица, знающая 0..3, — это обещание, что других кодов нет.

    ⚠ ИСКЛЮЧЕНИЕ, ОБЪЯВЛЕННОЕ ЯВНО И С ПРИЧИНОЙ: `-1` не обязателен в шаблонах CI
    (`docs/ci-templates/README*.md`). Он СИНТЕТИЧЕСКИЙ — его ставит control-api, соединяя `state` с
    отсутствующим кодом, — и настоящий процесс им не завершается, поэтому в пайплайне его ловить
    нечем. Список исключений короткий, закрытый и падающий при росте.
    """
    codes, _, _ = declared()
    tables = doc_tables()
    if len(tables) < DOC_TABLE_FLOOR:
        fail(f"обход нашёл {len(tables)} документов с таблицей кодов, пол {DOC_TABLE_FLOOR} — "
             f"либо таблицу удалили, либо признак перестал её узнавать")
    exempt_synthetic = {"docs/ci-templates/README.md", "docs/ci-templates/README.en.md"}
    for rel, named in sorted(tables.items()):
        want = codes - ({"-1"} if rel in exempt_synthetic else set())
        missing = want - named
        unknown = named - codes
        if missing:
            fail(f"{rel}: таблица не называет код(ы) {sorted(missing)} — читатель прочтёт её как "
                 f"«других кодов не бывает»")
        if unknown:
            fail(f"{rel}: таблица называет код(ы) {sorted(unknown)}, которых каталог не объявляет")


def test_the_ci_templates_handle_every_code_a_pipeline_can_see() -> None:
    """Шаблоны копируются пользователю, поэтому их список рукописен ПО НЕОБХОДИМОСТИ — и сверяется.

    Замер, купивший эту проверку: `Jenkinsfile` заканчивался `error "unexpected exit ${code}"` и знал
    0..3, поэтому на легальных 4 и 5 ЧУЖОЙ пайплайн падал без объяснения. `-1` исключён по той же
    причине, что и в таблицах, — процесс им не завершается.
    """
    codes, _, _ = declared()
    want = sorted(codes - {"-1"}, key=int)
    jenkins = read(os.path.join("docs", "ci-templates", "Jenkinsfile"))
    handled = set(re.findall(r"code\s*==\s*(-?\d+)", jenkins))
    for c in want:
        if c not in handled:
            fail(f"docs/ci-templates/Jenkinsfile: код {c} не разобран — он попадёт в `else`, "
                 f"а там `error`, то есть чужая сборка падает на легальном исходе")
    gitlab = read(os.path.join("docs", "ci-templates", ".gitlab-ci.yml"))
    m = re.search(r"allow_failure:\s*\n\s*exit_codes:\s*\n((?:\s*-\s*\d+\s*\n)+)", gitlab)
    allowed = set(re.findall(r"-\s*(\d+)", m.group(1))) if m else set()
    # «Наша поломка» не имеет права ронять чужой пайплайн: решение Alex 2026-08-31.
    ours = {c for c, e in catalog()["exit_codes"].items() if e.get("fault") == "tool" and c != "-1"}
    for c in sorted(ours | {"3"}, key=int):
        if c not in allowed:
            fail(f"docs/ci-templates/.gitlab-ci.yml: код {c} не в `allow_failure.exit_codes` — "
                 f"job краснеет из-за поломки, в которой приложение пользователя не участвует")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if failures:
        print(f"FAIL — {len(failures)} problem(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    _codes, _sevs, _words = declared()
    _tables = doc_tables()
    print(f"exit-code surfaces OK: {len(_codes)} codes / {len(_sevs)} severities / "
          f"{len(_words)} verdict words derived from the catalogue; "
          f"2 colour tables cover them exactly; {len(_tables)} documents tabulate them "
          f"(floors {CODE_FLOOR}/{SEVERITY_FLOOR}/{DOC_TABLE_FLOOR}); "
          f"{len(fns)} checks")
