"""Offline gate: сценарий говорит, следует ли он за собственными действиями (ADR-151, W13 вторая половина).

Run:  .venv/bin/python tests/test_route_consistency_offline.py

Заземление отвечает на «нашлись ли элементы» и молчит о том, СОГЛАСОВАН ли сценарий сам с собой.
Замерено на живом goal-прогоне — зелёном, `unmatched=0`, вердикт `pass`: один лишний переход и один
прыжок туда, куда со страницы пути нет. Продукт объявлял этот сценарий безупречным.

⚠ ПЕРВЫЙ ЗАМЫСЕЛ БЫЛ ОТМЕНЁН ЗАМЕРОМ. Проверять СВЯЗНОСТЬ по рёбрам бессмысленно: `_nav_step`
вставляется АВТОМАТИЧЕСКИ между шагами на разных страницах, поэтому сценарий связен по построению и
такая проверка не поймала бы ничего. Рёбра (ADR-150) дают другое — видно, что сценарий делает ПОСЛЕ
клика, о котором уже известно, куда он ведёт.

Это ОБЪЯВЛЕНИЕ, а не приговор, и гейт утверждает в том числе это: провалить прогон за телепорт
нельзя, потому что цель — направление, а не спецификация (HEALTH-004), и приложение может законно не
иметь ссылки туда, куда пользователь попадает закладкой. Судит человек; продукт обязан сказать.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from brain.scenario import route_consistency  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        return 1
    print(f"  ok   {name}")
    return 0


A, B, C = "file:///a", "file:///b", "file:///c"


def _map(**pages):
    return {p: els for p, els in pages.items()}


def _nav(sid, to):
    return {"step_id": sid, "action_type": "navigate", "target": to, "locator": None}


def _click(sid, role, name):
    return {"step_id": sid, "action_type": "click", "locator": {"role": role, "name": name}}


SITE = {A: [{"semantic_id": "1", "role": "link", "name": "ToB", "href_to": B}],
        B: [{"semantic_id": "2", "role": "button", "name": "Go", "leads_to": C}],
        C: [{"semantic_id": "3", "role": "button", "name": "Done"}]}


def test_a_scenario_that_follows_itself_is_silent():
    """Узкая половина, и без неё «жаловаться всегда» прошло бы весь остальной файл."""
    bad = 0
    steps = [_nav(1, A), _click(2, "link", "ToB"), _click(3, "button", "Go"), _click(4, "button", "Done")]
    r = route_consistency(steps, SITE)
    bad += check("маршрут, идущий по рёбрам, не вызывает ни одной жалобы",
                 r == {"redundant_navigations": [], "teleports": []}, f"got {r}")
    return bad


def test_redundant_navigation_is_named():
    """Клик уже привёл сюда — идти сюда снова лишнее. Не ошибка исполнения, но лишний шаг в тесте,
    который человек будет читать."""
    bad = 0
    steps = [_nav(1, A), _click(2, "link", "ToB"), _nav(3, B), _click(4, "button", "Go")]
    r = route_consistency(steps, SITE)
    bad += check("лишний переход назван по номеру шага", r["redundant_navigations"] == [3], f"got {r}")
    bad += check("и он НЕ засчитан телепортом", r["teleports"] == [], f"got {r}")
    return bad


def test_teleport_is_named():
    """Со страницы A ребра до C нет: значит это прыжок по адресной строке, а не маршрут пользователя.
    Тест «работает» и при этом не проверяет тот путь, которым ходят."""
    bad = 0
    steps = [_nav(1, A), _nav(2, C)]
    r = route_consistency(steps, SITE)
    bad += check("прыжок без ребра назван по номеру шага", r["teleports"] == [2], f"got {r}")
    bad += check("и он НЕ засчитан лишним переходом", r["redundant_navigations"] == [], f"got {r}")
    return bad


def test_unknown_position_accuses_nobody():
    """⚠ Обвинение по незнанию хуже молчания.

    Клик, про который карта ничего не знает, делает положение НЕИЗВЕСТНЫМ. Следующий navigate тогда
    не обвиняется: «ребра нет» означало бы лишь, что мы эту страницу не обходили, а не что сценарий
    прыгнул. Без этой половины проверка сыпала бы жалобами на всяком сайте, обойденном частично, —
    и её научились бы игнорировать."""
    bad = 0
    steps = [_nav(1, A), _click(2, "button", "Неизвестная"), _nav(3, C)]
    r = route_consistency(steps, SITE)
    bad += check("после неизвестного клика переход не обвиняется",
                 r == {"redundant_navigations": [], "teleports": []}, f"got {r}")

    # И симметрично: со страницы, которой в карте нет вовсе, тоже не обвиняем.
    r2 = route_consistency([_nav(1, "file:///never-seen"), _nav(2, C)], SITE)
    bad += check("переход с неизвестной страницы не обвиняется", r2["teleports"] == [], f"got {r2}")
    return bad


def test_the_first_navigate_is_never_blamed():
    """Первому шагу неоткуда прыгать: положения ещё нет."""
    bad = 0
    r = route_consistency([_nav(1, C)], SITE)
    bad += check("стартовый переход не обвиняется", r["teleports"] == [], f"got {r}")
    return bad


def test_it_is_an_announcement_not_a_verdict():
    """⚠ Провалить прогон за телепорт нельзя: цель — направление, а не спецификация (HEALTH-004), и
    приложение может законно не иметь ссылки туда, куда пользователь попадает закладкой.

    Утверждается на КАТАЛОГЕ, а не на коде: `degrades: true` покрасил бы вердикт «прошло с потерей
    качества» на исправном прогоне — ровно та ошибка, что уже сфотографирована в ADR-113. И утверждать
    это надо здесь, потому что поставить флаг может кто угодно и позже."""
    import json
    bad = 0
    ev = json.load(open(os.path.join(REPO, "brain", "events.json")))["events"]
    code = ev.get("plan.route_not_followed")
    bad += check("код объявления есть в каталоге", code is not None)
    if not code:
        return bad
    bad += check("он НЕ помечен degrades — маршрут потерян, а покрытие нет",
                 not code.get("degrades"), "вердикт покрасился бы на исправном прогоне")
    bad += check("он предупреждение, а не ошибка", code.get("lvl") == "warn", f"lvl={code.get('lvl')!r}")
    for lang in ("ru", "en"):
        bad += check(f"фраза {lang} называет ОБА числа",
                     "{redundant}" in code.get(lang, "") and "{teleports}" in code.get(lang, ""),
                     f"{lang}={code.get(lang)!r}")
    return bad


def main():
    bad = 0
    print("согласованный сценарий молчит:")
    bad += test_a_scenario_that_follows_itself_is_silent()
    print("лишний переход:")
    bad += test_redundant_navigation_is_named()
    print("телепорт:")
    bad += test_teleport_is_named()
    print("неизвестное положение:")
    bad += test_unknown_position_accuses_nobody()
    bad += test_the_first_navigate_is_never_blamed()
    print("объявление, а не приговор:")
    bad += test_it_is_an_announcement_not_a_verdict()
    if bad:
        print(f"\nroute consistency: {bad} FAILURE(S)")
        return 1
    print("\nroute consistency: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
