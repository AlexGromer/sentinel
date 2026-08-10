#!/usr/bin/env python3
"""[OBSERVE-NOT-IN-RUNCONFIG] — экспорт прогона перестал терять выбранное наблюдение.

ЧТО БЫЛО ЗАМЕРЕНО, и замерено ГЛАЗАМИ на кадре `ui-smoke`, а не гейтом. Форма прогона предлагает
«Наблюдение» и печатает рядом цену каждого режима, но раздел «3) Команда» отдавал
`agentctl run --target … --run-config run.yaml`, а сам `run.yaml` ключа `observe` не нёс. При этом
`auth.pw_no_trace: true` в тот же файл попадал — то есть экспорт был ИЗБИРАТЕЛЕН, и правило
избирательности нигде не записано. Человек экспортировал прогон «чтобы повторить в CI или из
терминала» и повторял его С ДРУГИМ наблюдением, молча.

Одной правки страницы было мало: `brain/runconfig.py::_KEY_ENV` ключа `observe` не знал, поэтому
`observe:`, вписанный в файл руками, брейн просто игнорировал.

ПОЧЕМУ ЭТОТ ФАЙЛ ВЫГЛЯДИТ ТАК. Утверждение «в `_KEY_ENV` есть строка `observe`» — суррогат: оно
совпадает и с комментарием, её объясняющим, и мутации проходят его насквозь. Поэтому здесь
вызывается НАСТОЯЩАЯ пара `load_run_config` / `apply_run_config` над файлом на диске, а результат
скармливается НАСТОЯЩЕМУ резолверу `brain.observe`, который и решает, что прогон снимет.

Три утверждения, и третье не менее важно первых двух:
 1. значение из файла ДОХОДИТ до резолвера;
 2. явный флаг `--observe` файл ПОБЕЖДАЕТ — иначе `run.yaml`, случайно оставшийся рядом, отменял бы
    выбор, только что сделанный человеком в хабе (control-api передаёт режим именно argv);
 3. отсутствие ключа НЕ превращается в пустое значение — «я не выбирал» и «я выбрал» обязаны
    остаться разными фактами, иначе лог перестанет отличать дефолт от просьбы.

Офлайн, stdlib + pyyaml (как сам загрузчик).
"""
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain import observe                                              # noqa: E402
from brain.runconfig import load_run_config, apply_run_config          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:400])


def write_cfg(body):
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="runcfg-", text=True)
    with io.open(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def base_env(**over):
    """The environment agentctl actually hands the brain: run-vars are written unconditionally, so
    SENTINEL_OBSERVE is present-and-empty when no flag was given. That emptiness is the whole reason
    `_overridable` lets the file speak — pin it here rather than inventing a cleaner env."""
    env = {"SENTINEL_OBSERVE": "", "SENTINEL_EXPLICIT": "", "TARGET_URL": "file:///x"}
    env.update(over)
    return env


def test_a_mode_written_in_the_file_reaches_the_resolver():
    path = write_cfg("mode: explore\nobserve: off\n")
    try:
        env = base_env()
        apply_run_config(load_run_config(path), env)
        check("файл донёс режим до окружения", env.get("SENTINEL_OBSERVE") == "off", env)
        plan = observe.from_env(env)
        check("резолвер увидел именно его",
              getattr(plan, "mode", None) == "off", plan)
        check("и кадры действительно выключены",
              getattr(plan, "frames", None) is False, plan)
    finally:
        os.unlink(path)


def test_an_explicit_flag_beats_the_file():
    """control-api передаёт выбор человека как argv (`appendRunFlags`). Если файл побеждал бы флаг,
    оставшийся рядом run.yaml отменял бы выбор, только что сделанный в хабе, — ровно та молчаливая
    подмена, ради устранения которой режим и переехал в argv."""
    path = write_cfg("mode: explore\nobserve: off\n")
    try:
        env = base_env(SENTINEL_OBSERVE="stream", SENTINEL_EXPLICIT="observe,target")
        apply_run_config(load_run_config(path), env)
        check("явный флаг уцелел", env.get("SENTINEL_OBSERVE") == "stream", env)
        check("резолвер выбрал его, а не файл",
              observe.from_env(env).mode == "stream", env)
    finally:
        os.unlink(path)


def test_a_file_without_the_key_does_not_invent_an_empty_choice():
    """Отрицательный контроль. Без него «файл доносит режим» удовлетворялось бы кодом, который
    пишет ключ ВСЕГДА, — и тогда «ничего не просили» стало бы неотличимо от «просили дефолт»."""
    path = write_cfg("mode: explore\nmax_steps: 7\n")
    try:
        env = base_env()
        apply_run_config(load_run_config(path), env)
        check("значение осталось пустым", env.get("SENTINEL_OBSERVE") == "", env)
        plan = observe.from_env(env)
        check("а причина названа дефолтом, не просьбой",
              "default" in (getattr(plan, "why", "") or "").lower(), getattr(plan, "why", None))
    finally:
        os.unlink(path)


def test_an_unknown_mode_from_a_file_is_refused_like_any_other():
    """Файл — не привилегированный канал: опечатка в нём обязана отказывать так же, как опечатка во
    флаге, иначе появится путь, по которому неизвестный режим тихо станет дефолтом."""
    path = write_cfg("mode: explore\nobserve: cinema\n")
    try:
        env = base_env()
        apply_run_config(load_run_config(path), env)
        try:
            res = observe.from_env(env)
        except observe.Refusal as exc:
            res = exc
        check("неизвестный режим из файла отклонён",
              type(res).__name__ == "Refusal" or getattr(res, "refused", False), res)
    finally:
        os.unlink(path)


def test_the_hub_exports_the_choice_it_offers():
    """Половина дефекта жила на странице: форма предлагала выбор и не клала его в файл, который сама
    же предлагала унести. Утверждение здесь — про ПАРУ (контрол существует И его значение попадает в
    сборку YAML), потому что каждая половина по отдельности была верна и раньше."""
    hub = io.open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
    check("контрол наблюдения на форме есть", 'id="b-observe"' in hub)
    check("и его значение уходит в экспортируемый run.yaml",
          "lines.push('observe: '+bq(obs))" in hub.replace('"', "'"),
          "в сборке YAML нет строки observe — экспорт снова теряет выбор")
    check("пустое значение в файл не пишется",
          "if (obs) lines.push('observe: '+bq(obs))" in hub.replace('"', "'"),
          "ключ с пустым значением сделал бы «не выбирал» и «выбрал» одним фактом")


def main():
    print("[OBSERVE-NOT-IN-RUNCONFIG] экспорт наблюдения")
    for fn in (
        test_a_mode_written_in_the_file_reaches_the_resolver,
        test_an_explicit_flag_beats_the_file,
        test_a_file_without_the_key_does_not_invent_an_empty_choice,
        test_an_unknown_mode_from_a_file_is_refused_like_any_other,
        test_the_hub_exports_the_choice_it_offers,
    ):
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
