#!/usr/bin/env python3
"""[RUNCONFIG-EXPORT-DROPS-IGNORE-ROBOTS] — экспортированный прогон перестал терять соблюдение robots.txt.

ЧТО БЫЛО ЗАМЕРЕНО. Хаб ПИШЕТ в экспортируемый `run.yaml` строку `ignore_robots: true`, а загрузчик
её выбрасывал: ключа не было ни в `_KEY_ENV`, ни в `_ALLOWED`, и во всём `brain/` строки
`ignore_robots` не встречалось вовсе — единственным настоящим читателем была переменная окружения,
которую ставит флаг `--ignore-robots`. Кнопка экспорта обещает «повторить ТОТ ЖЕ прогон в CI или из
терминала», и обещание нарушалось МОЛЧА: повтор шёл С СОБЛЮДЕНИЕМ robots.txt, ничего об этом не
сказав. Цена ошибки здесь внешняя — её платит чужой сайт, а не наш прогон.

ПОЧЕМУ ПРАВОК ЧЕТЫРЕ, А НЕ ОДНА. Каждая из трёх соседних НЕОБХОДИМА, и это измерено, а не выведено:

  · без `_AGENTCTL_DEFAULTS["IGNORE_ROBOTS"] = "0"` ключ был бы МЁРТВЫМ КОДОМ. `agentctl` выдаёт
    `IGNORE_ROBOTS=` всегда и всегда непустым ("1"/"0"), поэтому `_overridable` видит cur="0", не
    находит имя в таблице умолчаний и отвечает «нельзя» — файл не применился бы никогда, на
    единственном пути, где RunConfig вообще существует;
  · без `"ignore-robots"` в перечне явных флагов В Go запись `_EXPLICIT_FLAG` инертна: таблица
    читает `SENTINEL_EXPLICIT`, а наполняет его жёсткий список имён в `cmd/agentctl/main.go`. Тогда
    «флаг > файл» перестало бы быть правдой ровно для этой ручки;
  · без нормализации булева значение ДОЕХАЛО БЫ И НЕ СРАБОТАЛО: YAML 1.1 читает `ignore_robots: on`
    как булев True, обобщённая ветка `apply_run_config` кладёт в окружение `str(value)` — строку
    "True", — а читатель сравнивает с "1". Это хуже, чем не доехать: прогон подтверждает выбор и не
    исполняет его.

ПОЧЕМУ ЭТОТ ФАЙЛ ВЫГЛЯДИТ ТАК. Утверждение «в `_KEY_ENV` есть строка `ignore_robots`» — суррогат:
оно совпадает с комментарием, её объясняющим, и мутация проходит его насквозь. Поэтому здесь
вызывается НАСТОЯЩАЯ пара `load_run_config` / `apply_run_config` над файлом на диске, а результат
скармливается НАСТОЯЩЕМУ `brain.robots.from_env`, который и решает, соблюдать ли правила. Форма
скопирована с соседнего `test_observe_export_offline.py` — там тот же дефект и то же лечение.

Сети нет, и это проверено прогоном: при `ignore=True` резолвер отвечает, не выходя наружу, а
отрицательные случаи берут цель со схемой `file://`, у которой robots.txt не бывает по определению.
Первая редакция брала `https://example.invalid` и печатала `run.robots_unreachable ... URLError` —
офлайновый сьют делал сетевой вызов и молчал об этом.

Run: .venv/bin/python tests/test_ignore_robots_export_offline.py
"""
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain import robots                                              # noqa: E402
from brain.runconfig import load_run_config, apply_run_config         # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


def write_cfg(body):
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="runcfg-robots-", text=True)
    with io.open(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def base_env(**over):
    """Окружение, которое `agentctl` РЕАЛЬНО отдаёт брейну: run-var пишется безусловно, поэтому
    IGNORE_ROBOTS присутствует и равен "0", когда флага не было. Именно эта «непустая нулевая»
    строка и есть причина, по которой запись в `_AGENTCTL_DEFAULTS` необходима, — закрепляем её
    здесь, а не придумываем чистое окружение, которого на этом пути не бывает."""
    # ⚠ ЦЕЛЬ СО СХЕМОЙ file://, И ЭТО НЕ УДОБСТВО. С http-целью отрицательные случаи уходили бы за
    # robots.txt по настоящей сети: замерено — гейт печатал `run.robots_unreachable ... URLError`,
    # то есть сьют, объявленный офлайновым, делал сетевой вызов и молчал об этом. У схемы file://
    # robots.txt не бывает по определению, поэтому резолвер отвечает `not_applicable`, не выходя
    # наружу, а положительный случай короткозамкнут раньше — `ignore=True` возвращает ответ до
    # проверки схемы. Обе ветки меряются без сети.
    env = {"IGNORE_ROBOTS": "0", "SENTINEL_EXPLICIT": "", "TARGET_URL": "file:///x"}
    env.update(over)
    return env


def policy_for(env):
    """Настоящий читатель. `from_env` смотрит в `os.environ`, поэтому подменяем его на время вызова —
    подставлять свой словарь в обход читателя значило бы мерить копию, а не путь доставки."""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        return robots.from_env(env.get("TARGET_URL", ""))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_value_from_the_file_reaches_the_resolver():
    """KILLS: удаление ключа из `_KEY_ENV`; удаление записи из `_AGENTCTL_DEFAULTS` (тогда
    `_overridable` вернёт False и значение не применится)."""
    path = write_cfg("mode: explore\nignore_robots: true\n")
    try:
        env = base_env()
        apply_run_config(load_run_config(path), env)
        check("файл донёс выбор до окружения", env.get("IGNORE_ROBOTS") == "1", env)
        pol = policy_for(env)
        check("резолвер увидел именно его", getattr(pol, "source", None) == "ignored", getattr(pol, "source", pol))
        check("и правила действительно не соблюдаются", getattr(pol, "respected", None) is False, pol)
    finally:
        os.unlink(path)


def test_yaml_on_off_arrive_as_the_reader_expects():
    """KILLS: снятие нормализации булева. Без неё `on` доедет строкой "True", читатель сравнит её с
    "1" и промолчит — прогон подтвердит выбор и не исполнит его. Это ХУЖЕ, чем потерянный ключ:
    потерянный ключ виден по поведению, а подтверждённый-и-неисполненный неотличим от исполненного."""
    for body, want, respected in (("ignore_robots: on\n", "1", False),
                                  ("ignore_robots: off\n", "0", True),
                                  ("ignore_robots: yes\n", "1", False),
                                  ("ignore_robots: 'true'\n", "1", False)):
        path = write_cfg("mode: explore\n" + body)
        try:
            env = base_env()
            apply_run_config(load_run_config(path), env)
            check("YAML %-24s -> %s" % (body.strip(), want), env.get("IGNORE_ROBOTS") == want, env)
            pol = policy_for(env)
            check("  и читатель согласен (respected=%s)" % respected,
                  getattr(pol, "respected", None) is respected, getattr(pol, "source", pol))
        finally:
            os.unlink(path)


def test_an_explicit_flag_beats_the_file():
    """KILLS: удаление "ignore-robots" из перечня явных флагов в cmd/agentctl/main.go (через
    `SENTINEL_EXPLICIT`), либо удаление записи из `_EXPLICIT_FLAG`.

    Человек, СНЯВШИЙ соблюдение флагом, не должен получать его обратно из случайно лежащего рядом
    файла — и симметрично: человек, соблюдающий правила, не должен терять это из-за чужого
    `run.yaml`. Второй случай и проверяется: файл просит игнорировать, флаг сказал «нет»."""
    path = write_cfg("mode: explore\nignore_robots: true\n")
    try:
        env = base_env(IGNORE_ROBOTS="0", SENTINEL_EXPLICIT="ignore-robots,target")
        apply_run_config(load_run_config(path), env)
        check("явный флаг уцелел", env.get("IGNORE_ROBOTS") == "0", env)
        pol = policy_for(env)
        check("резолвер выбрал флаг, а не файл", getattr(pol, "source", None) != "ignored", getattr(pol, "source", pol))
    finally:
        os.unlink(path)


def test_a_file_without_the_key_does_not_invent_a_choice():
    """Отрицательный контроль. Без него «файл доносит выбор» удовлетворялось бы кодом, который пишет
    ключ ВСЕГДА, — и «ничего не просили» стало бы неотличимо от «просили соблюдать»."""
    path = write_cfg("mode: explore\nmax_steps: 7\n")
    try:
        env = base_env()
        apply_run_config(load_run_config(path), env)
        check("значение осталось тем, что дал agentctl", env.get("IGNORE_ROBOTS") == "0", env)
        check("и соседний ключ из файла при этом применился", env.get("MAX_STEPS") == "7", env)
        pol = policy_for(env)
        check("правила соблюдаются", getattr(pol, "respected", None) is True, getattr(pol, "source", pol))
    finally:
        os.unlink(path)


# [EXPORT-GATE-SATISFIED-BY-AN-UNRELATED-LINE] — здесь СТОЯЛА проверка
# `test_the_hub_exports_the_choice_it_offers`, и она была ВАКУУМНОЙ. Она искала две подстроки —
# "b-ignorerobots" и "ignore_robots: " — по ВСЕМУ тексту docs/index.html, а обе давала ОДНА строка
# карты `cfgFieldIds`, лежащая за девятьсот строк от экспорта. Замерено: удалить и чекбокс, и весь
# сборщик экспорта — проверка оставалась зелёной. Её докстринг обещал «пропадёт любая половина —
# экспорт снова начнёт лгать»; ни одна половина пропасть не могла.
#
# Утверждение не ослаблено, а ПЕРЕЕХАЛО туда, где его можно сделать честно:
# scripts/hub-dom-check.mjs, «каждое поле формы прогона доезжает до экспорта». Там страница
# настоящая, контрол действительно трогают и смотрят, изменился ли экспортируемый документ —
# наблюдение РАЗНОСТНОЕ и посторонней строкой неудовлетворимое. Здесь эквивалента нет и быть не
# может: без браузера доступна только форма исходника, а утверждение о форме исходника — суррогат,
# сквозь который мутации проходят насквозь. Именно так этот файл и ошибся.


def test_the_two_tables_that_must_agree_still_agree():
    """Единственное утверждение о ФОРМЕ исходника в этом файле, и оно про то, что поведением не
    ловится: перечень явных флагов живёт в Go, а таблица, которая его читает, — в Python. Ни один
    прогон не пройдёт по обеим половинам сразу, поэтому расхождение видно только отсюда."""
    from brain.runconfig import _EXPLICIT_FLAG                          # noqa: PLC0415
    go = io.open(os.path.join(ROOT, "cmd", "agentctl", "main.go"), encoding="utf-8").read()
    i = go.find('for _, n := range []string{"planner"')
    line = go[i:go.find("\n", i)] if i >= 0 else ""
    missing = [flag for flag in _EXPLICIT_FLAG.values() if '"%s"' % flag not in line]
    check("каждый флаг из _EXPLICIT_FLAG назван и в Go", not missing,
          "в перечне cmd/agentctl/main.go нет: %s — запись в brain/runconfig.py для них инертна" % missing)


def main():
    print("[RUNCONFIG-EXPORT-DROPS-IGNORE-ROBOTS] экспорт соблюдения robots.txt")
    for fn in (
        test_the_value_from_the_file_reaches_the_resolver,
        test_yaml_on_off_arrive_as_the_reader_expects,
        test_an_explicit_flag_beats_the_file,
        test_a_file_without_the_key_does_not_invent_a_choice,
        test_the_two_tables_that_must_agree_still_agree,
    ):
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
