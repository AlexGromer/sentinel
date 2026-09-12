#!/usr/bin/env python3
"""[LOGIN-PLAN-HAS-NO-CONSUMER] — план входа исполняется ПЕРЕД прогоном и отдаёт ему сессию.

ЧТО БЫЛО ЗАМЕРЕНО. `auth.login_plan` принимался схемой, показывался формой прогона и записывался
control-api в `run.yaml` — и НЕ ЧИТАЛСЯ НИКЕМ: во всём дереве ключ встречался только в таблице
соответствий. Человек заполнял поле, получал подтверждение и обычный прогон БЕЗ сессии. До ADR-172
было хуже: ключ отображался в `PLAN_FILE` — имя ПЛАНА ПРОГОНА, — и примени он его, план входа
заменил бы собой план прогона.

ЧТО НАБЛЮДАЕТСЯ ЗДЕСЬ, И ПОЧЕМУ ИМЕННО ЭТО. Утверждение «вход исполнился раньше» нельзя проверить
по формуле реализации — она согласится с собой при любой ошибке. Поэтому наблюдение идёт через
ГРАНИЦУ С ВНЕШНИМ МИРОМ: подменяется только `make_executor` (единственное место, где мозг открывает
браузер), и записывается, СКОЛЬКО сессий он открыл и что видел в окружении КАЖДАЯ. Тогда:

  · два создания вместо одного — вход прошёл отдельной сессией, а не внутри основной;
  · у ПЕРВОЙ сессии `STORAGE_STATE` пуст, у ВТОРОЙ равен файлу состояния — значит порядок именно
    такой, и основной прогон получил ровно то, что произвёл вход.

Ни одно из двух утверждений не повторяет код `_run_login_plan`: гейт не знает, как тот устроен, он
знает только, что видел браузер.

Run: .venv/bin/python tests/test_login_plan_offline.py
"""
import io
import json
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain import __main__ as bmain                                   # noqa: E402
from brain.state import canonical_plan_hash                            # noqa: E402

PAGE = "file:///dev/null"
GOOD = {"strategy": "css", "value": "#ok"}

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:500])


class Ex:
    """Исполнитель-заглушка ФОРМЫ настоящего клиента: заглушка другой формы расходится с реальностью
    ровно там, где никто не проверяет. `saveStorageState` действительно создаёт файл — иначе «сессия
    получена» осталось бы утверждением о вызове, а не о результате."""

    def __init__(self, ok=True):
        self.ok, self.url, self.saved = ok, PAGE, None

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p.get("url", self.url); return {"url": self.url}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            return {"ariaSnapshot": "- page app", "nodeCount": 2}
        if m in ("browser.interactives", "browser.links"):
            return {"elements": [], "links": []}
        if m == "browser.probe":
            return {"count": 1 if self.ok else 0}
        if m == "browser.click":
            if self.ok:
                return {"ok": True}
            raise RuntimeError("not found")
        if m == "browser.saveStorageState":
            self.saved = p.get("path")
            pathlib.Path(self.saved).parent.mkdir(parents=True, exist_ok=True)
            io.open(self.saved, "w", encoding="utf-8").write('{"cookies":[]}')
            return {"ok": True}
        return {}

    def close(self):
        pass


def write_plan(path, ok=True):
    steps = [{"step_id": 1, "action_type": "click", "intent": "click button 'Sign in'",
              "semantic_id": "sid-login", "is_milestone": False,
              "locator": GOOD, "alternatives": []}]
    io.open(path, "w", encoding="utf-8").write(json.dumps(
        {"plan_id": "p", "steps": steps, "plan_hash": canonical_plan_hash(steps), "target_url": PAGE}))
    return path


def drive(login_ok=True, env=None):
    """Гонит НАСТОЯЩИЙ `_run_login_plan` с подменённой границей во внешний мир и возвращает журнал
    открытых сессий: по одной записи на каждое создание исполнителя, с окружением, которое оно видело."""
    out = pathlib.Path(tempfile.mkdtemp(prefix="loginplan-"))
    plan = write_plan(str(out / "login.json"))
    opened = []

    def fake_make_executor(cmd):
        opened.append({"storage_state": os.environ.get("STORAGE_STATE", "")})
        return Ex(ok=login_ok)

    real = bmain.make_executor
    bmain.make_executor = fake_make_executor
    saved_env = {k: os.environ.get(k) for k in ("LOGIN_PLAN", "STORAGE_STATE", "STORAGE_STATE_SAVE")}
    try:
        os.environ["LOGIN_PLAN"] = plan
        for k, v in (env or {}).items():
            os.environ[k] = v
        rc = bmain._run_login_plan("pw", "run-t", out)
        # Вторая сессия — та, которую открыл бы основной прогон. Открываем её тем же способом, каким
        # это делает `main()`, чтобы записать, ЧТО она видит.
        if rc == 0:
            fake_make_executor("pw")
        return rc, opened, out
    finally:
        bmain.make_executor = real
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_login_plan_runs_first_and_hands_the_session_to_the_run():
    rc, opened, out = drive(login_ok=True)
    check("проход входа завершился успехом", rc == 0, "код %r" % rc)
    check("открыто ДВЕ сессии, а не одна", len(opened) == 2,
          "сессий %d — вход прошёл бы внутри основной, и его страница осталась бы у прогона" % len(opened))
    if len(opened) == 2:
        check("ПЕРВАЯ сессия открыта без состояния", opened[0]["storage_state"] == "",
              "вход увидел %r — значит он исполнялся не первым" % opened[0]["storage_state"])
        check("ВТОРАЯ сессия получила состояние, произведённое входом",
              opened[1]["storage_state"] and pathlib.Path(opened[1]["storage_state"]).exists(),
              "основной прогон увидел %r" % opened[1]["storage_state"])
    check("файл сессии лежит рядом с артефактами прогона",
          (out / "login-state.json").exists(),
          "не создан %s — «сессия получена» осталось бы утверждением о вызове" % (out / "login-state.json"))


def test_a_failed_login_stops_the_run_with_its_own_code():
    rc, opened, _ = drive(login_ok=False)
    check("отказ входа не молчит и не ноль", rc not in (0, None), "код %r" % rc)
    check("основной прогон НЕ открывался", len(opened) == 1,
          "сессий %d — прогон пошёл дальше без сессии и выдал бы неаутентифицированный вердикт "
          "за ответ на заданный вопрос" % len(opened))
    check("состояние основному прогону не подставлено", not os.environ.get("STORAGE_STATE"),
          "STORAGE_STATE=%r после неудачного входа" % os.environ.get("STORAGE_STATE"))


def test_an_explicit_session_outranks_the_login_plan():
    rc, opened, _ = drive(login_ok=True, env={"STORAGE_STATE": "/tmp/mine.json"})
    check("проход пропущен", rc is None, "код %r — «уже есть сессия» и «сделай мне её» разные акты" % rc)
    check("браузер не открывался вовсе", not opened, "сессий %d" % len(opened))


def test_the_saved_session_goes_where_the_person_said():
    """`storage_state_save` — уже существующее имя адреса сессии. Второго для этого не заводится:
    два имени одного предмета — это два места, где они разойдутся."""
    out = pathlib.Path(tempfile.mkdtemp(prefix="loginplan-dest-"))
    dest = str(out / "asked" / "state.json")
    rc, opened, _ = drive(login_ok=True, env={"STORAGE_STATE_SAVE": dest})
    check("сессия легла туда, куда просили", rc == 0 and pathlib.Path(dest).exists(),
          "код %r, файл %s" % (rc, dest))
    if len(opened) == 2:
        check("и основной прогон получил ИМЕННО этот адрес", opened[1]["storage_state"] == dest,
              "прогон увидел %r" % opened[1]["storage_state"])


def test_the_run_itself_goes_through_the_login_pass():
    """⚠ ЧЕТЫРЕ ПРОВЕРКИ ВЫШЕ ЗОВУТ `_run_login_plan` НАПРЯМУЮ — и этого НЕ ДОСТАТОЧНО: мутация
    «убрать вызов из `main()`» прошла бы сквозь них зелёной, а ключ снова остался бы без потребителя,
    то есть ровно с тем дефектом, ради которого всё это написано. Здесь гоняется НАСТОЯЩИЙ `main()`,
    и наблюдение то же самое — журнал открытых сессий на границе с внешним миром.
    """
    out = pathlib.Path(tempfile.mkdtemp(prefix="loginplan-main-"))
    login = write_plan(str(out / "login.json"))
    run = write_plan(str(out / "run.json"))
    opened = []

    def fake_make_executor(cmd):
        opened.append({"storage_state": os.environ.get("STORAGE_STATE", "")})
        return Ex(ok=True)

    real = bmain.make_executor
    bmain.make_executor = fake_make_executor
    keys = ("RUN_MODE", "RUN_ID", "ARTIFACT_DIR", "PLAN_FILE", "TARGET_URL",
            "LOGIN_PLAN", "STORAGE_STATE", "STORAGE_STATE_SAVE", "PW_EXECUTOR_CMD")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.pop("STORAGE_STATE", None)
        os.environ.pop("STORAGE_STATE_SAVE", None)
        os.environ.update({"RUN_MODE": "replay", "RUN_ID": "main-t",
                           "ARTIFACT_DIR": str(out / "art"), "PLAN_FILE": run,
                           # Команда запуска браузера обязана быть НЕПУСТОЙ: без неё `main()` честно
                           # отказывается (HEALTH-001) ещё до диспетчеризации, и наблюдать было бы
                           # нечего. Значение не используется — `make_executor` подменён.
                           "PW_EXECUTOR_CMD": "node /nonexistent/stub.js",
                           "LOGIN_PLAN": login})
        rc = bmain.main()
    finally:
        bmain.make_executor = real
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    check("прогон целиком завершился успехом", rc == 0, "код %r" % rc)
    check("main() открыл ДВЕ сессии — вход и прогон", len(opened) == 2,
          "сессий %d — если одна, `main()` мимо прохода входа, и ключ снова без потребителя" % len(opened))
    if len(opened) == 2:
        check("вход был ПЕРВЫМ и без состояния", opened[0]["storage_state"] == "",
              "первая сессия видела %r" % opened[0]["storage_state"])
        check("прогон получил состояние, произведённое входом",
              opened[1]["storage_state"] and pathlib.Path(opened[1]["storage_state"]).exists(),
              "прогон видел %r" % opened[1]["storage_state"])


def test_the_key_reaches_the_brain_from_the_run_config():
    """⚠ ВСЁ ВЫШЕ ЗАДАЁТ `LOGIN_PLAN` НАПРЯМУЮ — то есть проверяет ПОТРЕБИТЕЛЯ и обходит ДОСТАВКУ.
    Убери отображение `login_plan -> LOGIN_PLAN` из адаптера, и ключ снова станет тем, чем был:
    принимается схемой, пишется в `run.yaml`, до мозга не доезжает. Ровно тот дефект, ради которого
    всё это написано, и ни одна проверка выше его не увидит.

    Здесь зовётся НАСТОЯЩАЯ пара `load_run_config` / `apply_run_config` над файлом на диске.
    """
    from brain.runconfig import load_run_config, apply_run_config      # noqa: PLC0415
    path = os.path.join(tempfile.mkdtemp(prefix="loginplan-cfg-"), "run.yaml")
    io.open(path, "w", encoding="utf-8").write(
        "auth:\n  storage_state_save: s.json\n  pw_no_trace: true\n  login_plan: runs/login/plan.json\n")
    env = {}
    apply_run_config(load_run_config(path), env)
    check("`auth.login_plan` из RunConfig доезжает до мозга", env.get("LOGIN_PLAN") == "runs/login/plan.json",
          "LOGIN_PLAN=%r — ключ принимается и не доставляется, как до W17" % env.get("LOGIN_PLAN"))
    # И НЕ ДЕЛИТ ИМЯ С ПЛАНОМ ПРОГОНА (ADR-172): столкновение имён закрыто и обратно не заводится.
    check("и не занимает имя ПЛАНА ПРОГОНА", not env.get("PLAN_FILE"),
          "PLAN_FILE=%r — план входа заменил бы собой план прогона" % env.get("PLAN_FILE"))


def drive_main(login_ok=True):
    """Гонит НАСТОЯЩИЙ `main()` и возвращает (код, журнал открытых сессий)."""
    out = pathlib.Path(tempfile.mkdtemp(prefix="loginplan-main-"))
    login = write_plan(str(out / "login.json"))
    run = write_plan(str(out / "run.json"))
    opened = []
    # Заглушка отказывает ТОЛЬКО на входе: основной план обязан быть исполним, иначе «прогон не
    # начался» доказывалось бы его собственной непроходимостью, а не остановкой после входа.
    state = {"n": 0}

    def fake_make_executor(cmd):
        opened.append({"storage_state": os.environ.get("STORAGE_STATE", "")})
        state["n"] += 1
        return Ex(ok=(login_ok or state["n"] > 1))

    real = bmain.make_executor
    bmain.make_executor = fake_make_executor
    keys = ("RUN_MODE", "RUN_ID", "ARTIFACT_DIR", "PLAN_FILE", "TARGET_URL",
            "LOGIN_PLAN", "STORAGE_STATE", "STORAGE_STATE_SAVE", "PW_EXECUTOR_CMD")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.pop("STORAGE_STATE", None)
        os.environ.pop("STORAGE_STATE_SAVE", None)
        os.environ.update({"RUN_MODE": "replay", "RUN_ID": "main-t",
                           "ARTIFACT_DIR": str(out / "art"), "PLAN_FILE": run,
                           "PW_EXECUTOR_CMD": "node /nonexistent/stub.js",
                           "LOGIN_PLAN": login})
        return bmain.main(), opened
    finally:
        bmain.make_executor = real
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_failed_login_stops_the_whole_run_not_just_the_pass():
    """⚠ ЭТА ПРОВЕРКА КУПЛЕНА ВЫЖИВШЕЙ МУТАЦИЕЙ. `test_a_failed_login_stops_the_run_with_its_own_code`
    выше зовёт `_run_login_plan` напрямую и смотрит на ВОЗВРАЩЁННЫЙ код — а решение «остановиться»
    принимает `main()`. Мутация «продолжать несмотря на отказ входа» прошла её ЗЕЛЁНОЙ: код
    по-прежнему возвращался ненулевым, только никто его больше не слушал. Наблюдение снова на
    границе: если прогон продолжился, браузер откроется ВТОРОЙ раз.
    """
    rc, opened = drive_main(login_ok=False)
    check("прогон остановлен ненулевым кодом", rc not in (0, None), "код %r" % rc)
    check("основной прогон НЕ начинался", len(opened) == 1,
          "сессий %d — прогон пошёл дальше без сессии, и его вердикт читался бы как ответ на "
          "заданный вопрос" % len(opened))


def main():
    print("[LOGIN-PLAN-HAS-NO-CONSUMER] план входа исполняется перед прогоном")
    for fn in (test_the_login_plan_runs_first_and_hands_the_session_to_the_run,
               test_a_failed_login_stops_the_run_with_its_own_code,
               test_an_explicit_session_outranks_the_login_plan,
               test_the_saved_session_goes_where_the_person_said,
               test_the_run_itself_goes_through_the_login_pass,
               test_the_key_reaches_the_brain_from_the_run_config,
               test_a_failed_login_stops_the_whole_run_not_just_the_pass):
        fn()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
