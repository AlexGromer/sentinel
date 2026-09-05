"""Offline gate: пошаговый goal-планировщик — РЕЗЕРВ, а не долг, и продукт об этом говорит (W14 PR-4).

Run:  .venv/bin/python tests/test_goal_walker_reserved_offline.py

ЧТО ЗДЕСЬ ЗАЩИЩАЕТСЯ. `GoalPlanner.propose` не ведёт ни одного обхода, и это РЕШЕНИЕ. ADR-028
дословно: «LLM **не ведёт walk** (фаза-1 детерминирована); per-step `propose` сохранён для **M9.4
live/co-pilot**», и там же ОТКЛОНЁН «per-step goal-планировщик над all-pages меню». Причина —
контракт продукта: explore-once → replay-many. Обход, который ведёт модель, даёт на одном сайте
разные планы и разный `plan_hash`, то есть уносит воспроизводимость, ради которой инструмент и есть.

ЗАЧЕМ ГЕЙТ, ЕСЛИ РЕШЕНИЕ УЖЕ ЗАПИСАНО В ADR. Потому что запись в ADR не мешает следующему читателю
померить достижимость, увидеть «код не вызывается» и «починить» его — в любую из двух сторон, и обе
неверны: подключить значит откатить принятое решение, удалить значит выбросить половину co-pilot'а,
за которую уже заплачено (интерфейс, заземление по индексу ADR-022, деградация вне-диапазонного
индекса в `done`, откат на эвристику без модели). Ровно этот путь я и прошёл в этой волне, прежде
чем поднял ADR-028: замер показал «недостижим», и первым выводом было «оживить». Гейт ставит эту
развилку ПЕРЕД глазами того, кто попробует, а не после.

⚠ И ОДИН НАСТОЯЩИЙ ДЕФЕКТ ЗДЕСЬ ЖЕ. `--planner goal` не делает НИЧЕГО и до сих пор молчал об этом:
с `--goal` выигрывает ветка `if goal:` и жёстко ставит `HeuristicPlanner`, без `--goal` —
`GoalPlanner(goal="")`, чей `propose` первой же строкой откатывается на эвристику. Оператор,
попросивший `goal`, получал эвристику и не узнавал. Поведение при этом ПРАВИЛЬНОЕ (детерминированный
обход и предписан ADR-028), ошибочно было молчание — поэтому лечение объявлением, а не отказом.
"""
import ast
import json
import os
import pathlib
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        return 1
    print(f"  ok   {name}")
    return 0


def _walk_planner_in_goal_branch():
    """Какой обходчик СТРОИТСЯ в ветке `if goal:` — обходом AST, а не чтением глазами.

    Утверждение о ФОРМЕ исходника было бы суррогатом, если бы речь шла о поведении. Здесь речь
    именно о ДИСПЕТЧЕРИЗАЦИИ: вопрос «кто назначен обходчиком в goal-режиме» — это вопрос о том,
    что присваивается в этой ветке, и AST отвечает на него точно, а поведенческая проверка
    потребовала бы поднять настоящий браузер ради одного имени класса.
    """
    src = open(os.path.join(REPO, "brain", "__main__.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # ветка вида `if goal:` — тест на имя, а не на позицию: строки двигаются
        if not (isinstance(node.test, ast.Name) and node.test.id == "goal"):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            tgt = stmt.targets[0]
            names = [e.id for e in getattr(tgt, "elts", []) if isinstance(e, ast.Name)]
            if "planner" not in names:
                continue
            i = names.index("planner")
            val = stmt.value.elts[i] if isinstance(stmt.value, ast.Tuple) else stmt.value
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
                return val.func.id
    return None


def test_the_goal_walk_planner_stays_unwired():
    """Обходчиком в goal-режиме остаётся ЭВРИСТИКА — так предписывает ADR-028.

    KILLS: подключение `GoalPlanner` обходчиком. Мутация не «ломает тест», она заставляет автора
    прочитать причину и, если он всё же хочет модель за рулём, ЗАМЕСТИТЬ ADR-028 новым решением —
    то есть сделать это осознанно и с записью, а не походя.
    """
    who = _walk_planner_in_goal_branch()
    bad = check("ветка `if goal:` найдена обходом AST", who is not None,
                "не нашёл присваивания `planner` — изменилась форма диспетчеризации")
    bad += check("обходчик в goal-режиме — HeuristicPlanner", who == "HeuristicPlanner",
                 f"обходчиком назначен {who!r}: это откат ADR-028 (explore-once → replay-many). "
                 f"Если решение меняется осознанно — заместите ADR-028 и перепишите этот гейт")
    return bad


def test_propose_cannot_act_without_a_backend():
    """Без модели `propose` НЕ выдумывает действие, а отдаёт ход эвристике — поведенчески.

    Это половина обещания ADR-022, за которую заплачено и которую нельзя потерять при «уборке
    мёртвого кода». KILLS: удаление отката на эвристику.
    """
    from brain.planner import GoalPlanner, HeuristicPlanner
    state = {"current_url": "file:///s/index.html", "current_step": 1, "max_steps": 10,
             "visited_paths": [], "nav_frontier": [], "interactive_exercised": []}
    cands = [{"kind": "click", "role": "button", "name": "Go", "semantic_id": "b1",
              "intent": "click button 'Go'", "locator": {"role": "button", "name": "Go"}}]
    gp = GoalPlanner(goal="дойти до страницы действий", backend=None)
    got = gp.propose(dict(state), list(cands))
    want = HeuristicPlanner().propose(dict(state), list(cands))
    bad = check("без модели propose отдаёт ход эвристике", got == want, f"{got!r} != {want!r}")
    bad += check("и НЕ выдумывает действие вне списка кандидатов",
                 got.get("action") is None or got["action"] in cands, str(got.get("action")))
    return bad


def test_asking_for_the_goal_walker_is_announced():
    """`PLANNER=goal` ОБЪЯВЛЯЕТСЯ, а не игнорируется молча.

    Поведение при этом правильное — детерминированный обход предписан ADR-028, — поэтому лечение
    объявлением, а не отказом у двери, и поэтому у кода НЕТ `degrades`: ничего не потеряно.
    KILLS: снятие объявления; появление `degrades` (хаб нарисовал бы «прошло с потерей качества»
    на исправном прогоне — ошибка ADR-113).
    """
    src = open(os.path.join(REPO, "brain", "__main__.py"), encoding="utf-8").read()
    cat = json.loads((pathlib.Path(REPO) / "brain" / "events.json").read_text(encoding="utf-8"))["events"]
    e = cat.get("plan.goal_walker_reserved") or {}
    bad = check("код объявлен в каталоге", bool(e), "нет записи plan.goal_walker_reserved")
    bad += check("и двуязычен", bool(e.get("ru") and e.get("en")), str(sorted(e)))
    bad += check("и НЕ помечен degrades — прогон исправен", not e.get("degrades"), str(e))
    bad += check("__main__ действительно его произносит",
                 'log("plan.goal_walker_reserved")' in src)
    bad += check("условие смотрит именно на PLANNER=goal",
                 'os.environ.get("PLANNER")' in src and '"goal"' in src)
    # Причина названа в самом сообщении: читатель, увидевший строку, не должен идти за объяснением
    # в исходник — иначе объявление лишь заменяет одно молчание другим.
    bad += check("сообщение называет ПРИЧИНУ (ADR-028)", "ADR-028" in (e.get("en") or ""), e.get("en"))
    bad += check("и говорит, где цель ВСЁ-ТАКИ работает",
                 "scenario" in (e.get("en") or "").lower(), e.get("en"))
    return bad


def test_routing_is_untouched_and_honesty_comes_from_the_announcement():
    """Маршрутизация `make_planner` НЕ ТРОНУТА, а честность даёт ОБЪЯВЛЕНИЕ.

    ⚠ ЭТО ПИННИТ ОТМЕНЁННУЮ ПРАВКУ. Замер показал настоящую вещь: `PLANNER=goal` без цели даёт
    `GoalPlanner(goal="")`, чей `propose` откатывается на эвристику на КАЖДОМ шаге, и прогон печатает
    «Explore: planner goal» над обходом, все решения которого приняла эвристика (найдено ГЛАЗАМИ в
    выводе живого прогона). Первым лечением было вернуть здесь эвристику — «чтобы ярлык не врал».

    Эту правку отверг ЧУЖОЙ тест: `tests/test_m9_2_offline.py::test_make_planner_routing` пиннит
    `make_planner({"PLANNER": "goal"}).name == "goal"` с пометкой «explicit» — намеренный контракт
    маршрутизации (M9.2a, ADR-027). Функция отвечает на вопрос «какой планировщик выбран по
    окружению», и ответ именно такой; дефектом было МОЛЧАНИЕ, а не имя. Тест был прав, правка нет.

    KILLS: повторное «исправление ярлыка» в `make_planner` — оно снова уронит чужой гейт, и лучше
    встретить причину здесь, чем разбираться там во второй раз.
    """
    from brain.planner import make_planner, HeuristicPlanner, GoalPlanner, LLMPlanner
    bad = check("PLANNER=goal остаётся GoalPlanner (контракт M9.2a)",
                isinstance(make_planner({"PLANNER": "goal"}), GoalPlanner) and
                make_planner({"PLANNER": "goal"}).name == "goal",
                "маршрутизация изменена — это уронит tests/test_m9_2_offline.py")
    bad += check("PLANNER=llm не тронут", isinstance(make_planner({"PLANNER": "llm"}), LLMPlanner))
    bad += check("умолчание не тронуто", isinstance(make_planner({}), HeuristicPlanner))
    # И именно поэтому объявление ОБЯЗАНО существовать: без него «planner goal» над эвристическим
    # обходом остаётся тем, чем и было, — прогоном, который говорит не то, что делает.
    src = open(os.path.join(REPO, "brain", "__main__.py"), encoding="utf-8").read()
    bad += check("честность доставляется объявлением, а не переименованием",
                 'log("plan.goal_walker_reserved")' in src)
    return bad


def test_report_reads_the_name_the_run_actually_wrote():
    """Отчёт baseline-прогона ЧИТАЕТСЯ, а не объявляется отсутствующим.

    ⚠ ЗАМЕРЕНО: `brain/replay.py` пишет `baseline-report.json` в режиме baseline (одна строка,
    `name = ... if mode == "baseline"`), а читатели держали ЖЁСТКО `heal-report.json` — и `generate`,
    и страж `_run_report`, и ветка pushgateway. На baseline-прогоне `agentctl report` возвращал
    **exit 3** (`integrity`), то есть «прогон попросили запустить неправильно»: инструмент обвинял
    ОПЕРАТОРА в том, что сам же назвал файл иначе. Тот же класс, что и вся эта волна — продукт
    говорит не то, что произошло.

    Имя теперь ищет ОДНА функция `report.report_path`, а не три списка.
    KILLS: возврат жёсткого имени в любом из трёх мест.
    """
    import tempfile
    from brain.report import report_path
    from brain.__main__ import _run_report
    minimal = {"exit_code": 0, "verdict": "pass", "steps": [], "healed": 0, "failed": 0}
    bad = 0
    for name in ("heal-report.json", "baseline-report.json"):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, name), "w") as fh:
            json.dump(minimal, fh)
        bad += check(f"{name}: найден", report_path(d) is not None)
        bad += check(f"{name}: отчёт строится (НЕ exit 3)", _run_report(d) != 3,
                     "страж всё ещё знает одно имя — baseline-прогон объявляется битым")
        bad += check(f"{name}: report.json написан", os.path.exists(os.path.join(d, "report.json")))
    empty = tempfile.mkdtemp()
    bad += check("пустой каталог по-прежнему exit 3", _run_report(empty) == 3)
    return bad


def test_the_reason_lives_next_to_the_code():
    """Причина резерва записана в докстринге `GoalPlanner`, а не только в ADR и не только здесь.

    Следующий читатель приходит к КОДУ, померив достижимость, — и обязан встретить объяснение там.
    Ровно этим путём прошёл автор этой волны, и первым выводом было «оживить».
    KILLS: удаление записи из докстринга при «уборке».
    """
    from brain.planner import GoalPlanner
    doc = GoalPlanner.__doc__ or ""
    bad = check("докстринг называет ADR-028", "ADR-028" in doc)
    bad += check("и называет потребителя (live/co-pilot)", "co-pilot" in doc)
    bad += check("и запрещает «починку» в обе стороны",
                 ("удалить" in doc and "откатить" in doc), doc[-200:])
    return bad


def main():
    bad = 0
    print("обходчик остаётся детерминированным:")
    bad += test_the_goal_walk_planner_stays_unwired()
    print("заземление без модели:")
    bad += test_propose_cannot_act_without_a_backend()
    print("просьбу о goal-обходчике произносят вслух:")
    bad += test_asking_for_the_goal_walker_is_announced()
    print("маршрутизация не тронута, честность — объявлением:")
    bad += test_routing_is_untouched_and_honesty_comes_from_the_announcement()
    print("отчёт читает имя, которое прогон записал:")
    bad += test_report_reads_the_name_the_run_actually_wrote()
    print("причина лежит рядом с кодом:")
    bad += test_the_reason_lives_next_to_the_code()
    if bad:
        print(f"\ngoal walker reserved: {bad} FAILURE(S)")
        return 1
    print("\ngoal walker reserved: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
