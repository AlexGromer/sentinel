#!/usr/bin/env python3
"""ADR-139 — признак провала и код выхода выводятся из ОДНОГО источника.

ЗАЧЕМ ЭТОТ ФАЙЛ. «Провалился ли прогон» вычислялось ПЯТЬЮ независимыми выражениями, и охранявший
эту границу тест (`test_grounding_gate_offline.py`) не мог поймать расхождение по трём замеренным
причинам сразу: правило `not_grounded` живёт только на пути авторинга; порог пяти шагов вычислялся
в вызывающей функции, куда узел графа не заглядывает; а обе его фикстуры — авторинговые прогоны,
где порог не участвует вовсе. Итог был предсказуем: правка, нацеленная на ОДИН случай класса, была
обойдена следующим случаем того же класса.

ЧТО УТВЕРЖДАЕТСЯ ЗДЕСЬ И ПОЧЕМУ ЭТО НЕЛЬЗЯ ПРОЙТИ ЧАСТИЧНОЙ ПРАВКОЙ. Для КАЖДОЙ достижимой
комбинации входов утверждается `кадр.exit_code == фактический код процесса`. Обе величины —
НАБЛЮДАЕМЫЕ: код берётся возвратом шипнутой `_run_explore`, кадр — разбором строки `@@AGUI` из
перехваченного stdout. Файл НЕ импортирует `decide` и правила не знает, поэтому тавтологии нет:
мутация внутри `decide` эту половину не убивает (кадр всё равно выведется), её убивает вторая
половина — рукописная таблица «ячейка → ожидаемое число», которая и есть контракт.

⚠ СЕТКА НЕ ИСЧЕРПЫВАЮЩАЯ, И ЭТО СКАЗАНО ПРЯМО. Оси `stop_reason` и числа шагов в неё НЕ входят —
замерено, что ни одна ветка решения их не читает, и включение дало бы кратность, которую легко
принять за покрытие. Ось «упало посреди прогона» представлена двумя случаями (спасли / не спасли),
а `KeyboardInterrupt` не входит: он пробрасывается и кода не даёт (это утверждает
`test_artifact_retention_offline.py`).

Офлайн: без сети, без браузера, без сервера — исполнители фейковые, `_run_explore` настоящая.

Run:  PYTHONPATH="$PWD" .venv/bin/python tests/test_run_outcome_offline.py
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain.__main__ import _run_explore                                   # noqa: E402

URL = "file:///x/app.html"


# --- исполнители: каждый воспроизводит ОДНУ ось входов ------------------------------------------

class _Base:
    """Здоровая цель: одна страница, две кнопки, обход сходится."""

    url = URL
    clicks_fail = False
    saw_nothing = False

    def __init__(self):
        self.clicked = 0

    def call(self, m, **p):
        if self.saw_nothing:
            return {}
        if m == "browser.navigate":
            return {"url": self.url, "title": "t", "status": 200}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": "t"}
        if m == "browser.snapshot":
            return {"ariaSnapshot": '- button "Go"\n- button "Stop"', "nodeCount": 3}
        if m == "browser.interactives":
            return {"elements": [
                {"role": "button", "name": "Go", "locator": {"css": "#go"}, "visible": True,
                 "enabled": True, "kind": "button", "testid": None},
                {"role": "button", "name": "Stop", "locator": {"css": "#stop"}, "visible": True,
                 "enabled": True, "kind": "button", "testid": None}]}
        if m == "browser.links":
            return {"links": []}
        if m == "browser.click":
            self.clicked += 1
            if self.clicks_fail:
                raise RuntimeError("click refused by the application")
            return {"ok": True, "navigated": False}
        if m == "browser.probe":
            return {"count": 1}
        if m == "browser.screenshotHash":
            return {"hash": "h"}
        return {}

    def close(self):
        pass


class _Vacuum(_Base):
    """Сломанный исполнитель: отвечает пустотой на всё. Ни элементов, ни АДРЕСА."""

    saw_nothing = True


class _TextOnly(_Base):
    """ЗАКОННАЯ страница без интерактивных элементов: адрес есть, кликать нечего.

    ⚠ Ключевая ячейка всего файла. Замерено: от вакуума её отличает РОВНО адрес — шагов у обеих 0,
    `interactive_seen` пуст, `any(site_map.values())` ложно, `errors` пуст, `reason` одинаков. Без
    третьего подпункта предиката она получила бы `exit 4` «инструмент сломался», то есть мы обвинили
    бы себя вместо честного «смотреть нечего»."""

    def call(self, m, **p):
        if m == "browser.interactives":
            return {"elements": []}
        return super().call(m, **p)


class _HalfBroken(_Base):
    """Исполнитель, который не видит цели И роняет клики: половинчатая поломка.

    ⚠ ЗАМЕРЕНО, И ЭТО ЗАПИСЬ ОБ ЭКВИВАЛЕНТНОЙ МУТАЦИИ. Заводился ради того, чтобы сделать ПОРЯДОК
    вопросов в `decide` наблюдаемым — перестановка «вакуум» и «отказы шагов» местами гейт не убивает.
    Замер показал, почему её и НЕЛЬЗЯ убить: конъюнкция недостижима по построению. Отказы шагов
    рождаются кликами, клики — кандидатами, кандидаты — картой; у вакуума карта пуста, поэтому
    `errors` у него РОВНО ноль (замерено: `_HalfBroken` → errors=0, seen=0, mapped=0, addr=False;
    `_Failing` → errors=4, seen=2, mapped=2, addr=True). Ветки взаимоисключающи, и их порядок
    ненаблюдаем — мутация ЭКВИВАЛЕНТНА, а не пропущена.

    Ячейка при этом оставлена: она утверждает нужное само по себе — половинчатая поломка это
    `fault: tool`, а не находка о приложении.
    """

    def call(self, m, **p):
        if m == "browser.interactives":
            return {"elements": []}
        if m == "browser.currentUrl":
            return {"url": "", "title": ""}
        if m == "browser.navigate":
            return {"url": "", "title": "", "status": 200}
        if m == "browser.click":
            raise RuntimeError("click refused")
        return super().call(m, **p)


class _Failing(_Base):
    """Здоровая цель, но клики отказывают — находка ПРО ПРИЛОЖЕНИЕ."""

    clicks_fail = True


class _Crashing(_Base):
    """Исполнитель, роняющий прогон посреди обхода."""

    def call(self, m, **p):
        if m == "browser.snapshot":
            raise RuntimeError("executor died")
        return super().call(m, **p)


# --- прогон и наблюдение --------------------------------------------------------------------------

def run(ex, *, goal=None, describe=False, max_steps=6):
    """Гоняет ШИПНУТУЮ `_run_explore` и возвращает (код процесса, кадры AG-UI)."""
    out = Path(tempfile.mkdtemp())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run_explore(ex, "run", out, URL, 0.85, max_steps)
    frames = []
    for line in buf.getvalue().splitlines():
        if line.startswith("@@AGUI"):
            try:
                frames.append(json.loads(line[len("@@AGUI "):]))
            except json.JSONDecodeError:
                pass
    return rc, frames


def verdicts(frames):
    return [f for f in frames if f.get("type") == "verdict"]


# КОНТРАКТ: ячейка → ожидаемый код. Половина РУКОПИСНАЯ намеренно — это утверждение о том, каким
# исход ДОЛЖЕН быть, а не пересказ того, каким его считает код. Мутация в правиле ломает её.
CASES = [
    ("здоровый обход, всё сошлось",            _Base,     0),
    ("исполнитель отвечает пустотой",          _Vacuum,   4),
    ("законная страница без контролов",        _TextOnly, 0),
    ("клики отказывают — находка о приложении", _Failing,  1),
    ("исполнитель упал посреди обхода",        _Crashing, 5),
    ("ничего не увидел И роняет клики",        _HalfBroken, 4),
]


def test_the_frame_and_the_process_never_disagree():
    """ГЛАВНОЕ утверждение файла. Обе величины наблюдаемые, правило не импортируется — сравниваются
    два независимых наблюдения над одним прогоном."""
    bad = []
    for name, cls, _ in CASES:
        rc, frames = run(cls())
        v = verdicts(frames)
        if not v:
            bad.append(f"{name}: кадра verdict НЕТ вовсе (код процесса {rc})")
        elif v[0]["data"]["exit_code"] != rc:
            bad.append(f"{name}: кадр {v[0]['data']['exit_code']} ≠ процесс {rc}")
    assert not bad, "кадр разошёлся с кодом выхода:\n  " + "\n  ".join(bad)


def test_every_case_returns_the_code_the_contract_promises():
    """Вторая половина: рукописная таблица. Она и ловит правку правила — согласие само по себе
    выполнимо любым согласованным враньём."""
    got = []
    for name, cls, want in CASES:
        rc, _ = run(cls())
        got.append((name, rc, want))
    wrong = [(n, r, w) for n, r, w in got if r != w]
    assert not wrong, "код выхода разошёлся с контрактом:\n  " + "\n  ".join(
        f"{n}: получено {r}, обещано {w}" for n, r, w in wrong)


def test_exactly_one_verdict_frame_per_run():
    """Ноль кадров — это пути падения, где кадра не было ВООБЩЕ. Два — это второй автор."""
    for name, cls, _ in CASES:
        _, frames = run(cls())
        assert len(verdicts(frames)) == 1, f"{name}: кадров {len(verdicts(frames))}"


def test_a_fully_converged_small_site_is_not_called_a_finding():
    """Причина, по которой порог `len(steps) >= 5` убран. Замерено настоящим `_run_explore`:
    крошечный сайт, обойдённый ЦЕЛИКОМ, отдавал `exit 1` — то есть `fault: app` «тест нашёл
    проблему» — только потому, что приложение маленькое."""
    rc, frames = run(_Base())
    assert rc == 0, rc
    data = verdicts(frames)[0]["data"]
    assert data["verdict"] == "pass", data


def test_a_broken_executor_is_our_fault_and_a_bare_page_is_nobody_s():
    """Пара, ради которой предикат имеет ТРИ подпункта, а не два. Всё, кроме адреса, у них совпадает."""
    rc_vac, _ = run(_Vacuum())
    rc_txt, _ = run(_TextOnly())
    assert rc_vac == 4, rc_vac       # fault: tool — сломались МЫ
    assert rc_txt == 0, rc_txt       # fault: none — смотреть было нечего, и это не дефект


def test_a_crawl_that_hit_errors_is_a_finding_even_when_it_is_long():
    """Обратное направление расхождения, которого запись реестра не называла: `errors` не входил в
    критерий успеха ВОВСЕ, поэтому прогон с упавшими кликами выходил нулём, пока кадр говорил
    `failed`. Теперь оба говорят одно."""
    rc, frames = run(_Failing())
    assert rc == 1, rc
    assert verdicts(frames)[0]["data"]["exit_code"] == 1, frames


# --- структурные утверждения: узкие, дешёвые, с названной работой --------------------------------

def test_the_graph_node_no_longer_emits_a_verdict():
    """По AST, а не грепом: комментарии в дерево не попадают, поэтому объяснение рядом с удалённым
    кодом не может подделать эту проверку. Второго эмиттера придётся ПИСАТЬ ЗАНОВО."""
    tree = ast.parse(open(os.path.join(ROOT, "brain", "graph.py"), encoding="utf-8").read())
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and n.value == "verdict"]
    assert not hits, f"в brain/graph.py снова есть литерал \"verdict\" (строки {[n.lineno for n in hits]})"


def test_the_decision_is_a_pure_function_of_the_declared_facts():
    """Превращает «оси перечислены полностью» из обещания в проверяемое утверждение: новый повод
    покраснеть нельзя добавить, не расширив `Facts`, а расширение видно в диффе одной строкой."""
    tree = ast.parse(open(os.path.join(ROOT, "brain", "outcome.py"), encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "decide")
    forbidden = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and not (
                isinstance(node.value, ast.Name) and node.value.id == "f"):
            forbidden.append(f"{node.lineno}: обращение к {ast.unparse(node)}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id not in (
                "_outcome", "bool", "int", "len"):
            forbidden.append(f"{node.lineno}: вызов {node.func.id}")
    assert not forbidden, ("decide() перестала быть чистой функцией от Facts:\n  "
                           + "\n  ".join(forbidden))


def test_the_exit_code_is_never_a_literal_in_the_explore_tail():
    """`_run_explore` не имеет собственного выражения об исходе: каждый её возврат на путях прогона
    идёт через `announce`. Литерал в возврате — это второй автор, вернувшийся тихо."""
    src = open(os.path.join(ROOT, "brain", "__main__.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_explore")
    literals = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, int)]
    assert not literals, f"в _run_explore вернулся числовой литерал (строки {literals})"


def test_teardown_is_reachable_from_every_ordinary_path():
    """Найдено этой работой сверх задачи: путь авторинга возвращался ДО `_stop_trace`/`_stop_video`/
    `shutdown`, поэтому на `--goal`/`--describe` не звался ни один из них, а `SENTINEL_TRACE_ALWAYS`
    не делал ничего. Чинится по построению — у обычного пути один возврат."""
    src = open(os.path.join(ROOT, "brain", "__main__.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_explore")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    announced = [n for n in returns
                 if isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                 and n.value.func.id == "announce"]
    assert len(announced) == len(returns), (
        f"не каждый возврат _run_explore идёт через announce: {len(announced)} из {len(returns)}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
