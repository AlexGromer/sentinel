#!/usr/bin/env python3
"""Офлайн-гейт: перечень элементов доезжает до модели ЦЕЛЫМИ записями, и остаток НАЗВАН (ADR-136).

Run:  .venv/bin/python tests/test_prompt_budget_offline.py

ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ (2026-08-24, `testdata/site-spa`, heuristic, дефолтный бюджет 40 шагов —
тот же прогон, что даёт 12 страниц и coverage 0.2701):

  * карта — 184 элемента, 27 108 символов; срез `json.dumps(...)[:8000]` оставлял 55 элементов;
  * оставленное — НЕ ВАЛИДНЫЙ JSON: `Unterminated string starting at char 7998`. Модель получала
    массив без закрывающей скобки, оборванный посреди строкового литерала;
  * представлены были ТРИ страницы целиком и одна частично; ВОСЕМЬ из двенадцати не имели в промпте
    ни одного элемента — при том, что промпт просит собрать сценарий «по всему сайту»;
  * рост карты делал хуже: при потолке 200 шагов элементов стало 284, а доля дошедшего упала
    с 29 % до 19 %;
  * в журнал не писалось НИЧЕГО — ни о срезе, ни о потере.

И вторая половина, замеренная на механизме: запись фронтира стоит ~130 символов промпта НА КАЖДОМ
шаге (N=0 → 1 752 симв., N=100 → 14 552, N=500 → 66 552, N=1000 → 131 562), а потолка у фронтира
нет. ⚠ На нашем корпусе это НЕ ВОСПРОИЗВОДИТСЯ — максимум 14 якорей на страницу и фронтир ≤ 2, —
поэтому цена и кеп проверяются здесь СИНТЕТИЧЕСКИМ перечнем, а живьём — на `l14-frontier.html`.

⚠ ПОЧЕМУ НЕ ПРОВЕРКА ФОРМЫ ИСХОДНИКА. Утверждение «в planner.py нет подстроки `[:8000]`» —
суррогат: мутация `[:MAP_CHARS]` прошла бы насквозь. Здесь проверяется то, что получает модель.
"""
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from brain.llm import LLMResult                                    # noqa: E402
from brain.sanitize import fit_json_list, partial_note             # noqa: E402
from brain import planner as P                                     # noqa: E402
from brain.planner import (GoalPlanner, LLMPlanner, MAP_CHARS,     # noqa: E402
                           STEP_MENU_CHARS, _spread_by_page)
from brain import budget                                           # noqa: E402

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


class FakeBackend:
    """Тот же образец, что в tests/test_m9_2b_offline.py. Промпт сохраняется целиком: он и есть предмет."""
    name, model, supports_vision, supports_structured = "fake", "fake-model", False, False

    def __init__(self, reply='{"steps": []}'):
        self.reply, self.calls = reply, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.calls.append(prompt)
        return LLMResult(self.reply, 10, 10)

    def complete_vision(self, *a, **k):
        raise NotImplementedError


def _map(pages: int, per_page: int) -> list:
    """Плоская карта: `pages` страниц по `per_page` элементов, ключи страниц отсортированы."""
    return [{"semantic_id": f"p{pg:02d}-e{i:02d}", "page": f"file:///s/page-{pg:02d}.html",
             "role": "button", "name": f"Control number {i} on page {pg}"}
            for pg in range(pages) for i in range(per_page)]


# --- 1. Механизм укладки ---------------------------------------------------------------------------

def test_the_packer_never_hands_over_broken_json():
    """ГЛАВНОЕ свойство: при ЛЮБОМ бюджете результат разбирается как JSON.

    Прежний срез строки этим свойством не обладал ни при каком нетривиальном входе, и именно оно
    отличает «карта без хвоста» от «сломанный JSON». Бюджеты подобраны так, чтобы один из них был
    МЕНЬШЕ одной записи: это законный вход, а не ошибка, и ответ на него — пустой массив плюс честное
    `dropped`, а не исключение.
    """
    recs = [{"ref": f"r{i}", "name": f"Кнопка номер {i}"} for i in range(40)]
    for b in (0, 1, 2, 5, 50, 300, 1000, 10 ** 6):
        text, dropped = fit_json_list(recs, b)
        try:
            back = json.loads(text)
        except Exception as e:
            fail(f"бюджет {b}: укладка отдала неразбираемый JSON ({e})")
            continue
        if len(back) + dropped != len(recs):
            fail(f"бюджет {b}: сохранено {len(back)} + выброшено {dropped} != {len(recs)} — "
                 f"счёт выброшенного лжёт, и человек прочтёт неполный перечень как полный")
        if b >= 2 and len(text) > max(b, 2):
            fail(f"бюджет {b}: результат {len(text)} символов — бюджет не соблюдён")
    print("  ok  укладка отдаёт валидный JSON при любом бюджете, и счёт выброшенного сходится")


def test_the_packer_drops_whole_records_not_characters():
    """Встречное к первому. Валидный JSON можно отдать и обрезав поле внутри записи — тогда модель
    получит элемент с испорченным `ref`, которого нет в карте, и заземление отвергнет его уже ПОСЛЕ
    того, как за него заплатили. Здесь требуется, чтобы каждая уцелевшая запись совпадала с исходной
    ПОБАЙТОВО."""
    recs = [{"ref": f"r{i}", "name": "x" * 40} for i in range(30)]
    text, dropped = fit_json_list(recs, 900)
    back = json.loads(text)
    if dropped == 0:
        fail("бюджет 900 не вызвал ни одного выброса — проверка ничего не утверждает")
    for got, want in zip(back, recs):
        if got != want:
            fail(f"уцелевшая запись изменена: {got} против {want} — резали внутри записи")
    print(f"  ok  выброшены {dropped} записей ЦЕЛИКОМ, уцелевшие не тронуты")


# --- 2. Карта авторинга ----------------------------------------------------------------------------

def test_every_page_of_the_map_reaches_the_model():
    """ADR-136. Бюджет раскладывается по ВСЕМ страницам, а не достаётся алфавитному началу.

    ⚠ ЭТО НЕ КОСМЕТИКА ПОРЯДКА. Промпт авторинга просит собрать сценарий, выбирая ТОЛЬКО из
    предъявленных элементов, и называет их «real elements discovered across the whole site». Пока
    отбор был префиксным, две трети сайта не были предъявлены вовсе — то есть модель отвечала на
    вопрос, которого ей не задавали, а человек читал результат как «по всему сайту».

    KILLS: удаление `_spread_by_page` (замерено: 12 страниц → 4).
    """
    # ⚠ ЧЕРЕЗ `build_scenario`, А НЕ ВЫЗОВОМ ПОМОЩНИКА. Первая редакция звала `_spread_by_page` в
    # теле теста — и мутация «убрать раскладку из build_scenario» прошла ЗЕЛЁНОЙ: проверялся
    # помощник, а не проводка. Утверждать надо о том, что получает модель, то есть о ПРОМПТЕ.
    flat = _map(pages=12, per_page=16)
    fb = FakeBackend('{"steps": []}')
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    GoalPlanner(goal="pay the bill", backend=fb).build_scenario(flat)
    prompt = fb.calls[-1]
    if "PARTIAL" not in prompt:
        fail(f"карта из {len(flat)} элементов уместилась целиком — проверка отбора ничего "
             f"не утверждает; увеличьте карту")
    all_pages = {e["page"] for e in flat}
    missing = sorted(pg for pg in all_pages if pg not in prompt)
    if missing:
        fail(f"в промпт не попало ни одного элемента {len(missing)} страниц из {len(all_pages)} "
             f"({missing[:3]}) — бюджет снова достаётся алфавитному началу, и остальной сайт "
             f"модель не видит")
    print(f"  ok  бюджет разложен: все {len(all_pages)} страниц представлены в промпте авторинга")


def test_a_cut_map_says_so_in_the_prompt_and_a_whole_one_stays_silent():
    """Пара. Одно «есть примечание» удовлетворяется кодом, который печатает его всегда, — и тогда
    полный перечень тоже объявлен неполным, что хуже молчания: человек перестаёт верить примечанию.

    KILLS: `partial_note`, возвращающая текст безусловно.
    KILLS: молчаливый кеп (примечания нет вовсе).
    """
    fb = FakeBackend('{"steps": []}')
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    GoalPlanner(goal="pay the bill", backend=fb).build_scenario(_map(pages=12, per_page=16))
    big = fb.calls[-1]
    if "PARTIAL" not in big:
        fail("карта не поместилась, а промпт не сказал об этом ни слова — модель считает "
             "предъявленное исчерпывающим и выбирает «лучшее» из подмножества")
    fb2 = FakeBackend('{"steps": []}')
    GoalPlanner(goal="pay the bill", backend=fb2).build_scenario(_map(pages=1, per_page=2))
    small = fb2.calls[-1]
    if "PARTIAL" in small:
        fail("перечень уместился целиком, а промпт всё равно объявил его неполным")
    if "elements:" not in small:
        fail(f"промпт авторинга потерял перечень элементов: {small[:200]!r}")
    print("  ok  урезанная карта объявлена, полная — нет")


# --- 3. Перечень кандидатов шага -------------------------------------------------------------------

def _candidates(clicks: int, navs: int) -> list:
    out = [{"kind": "click", "semantic_id": f"b{i}", "role": "button",
            "name": f"Button number {i}", "target": None, "intent": f"click button {i}"}
           for i in range(clicks)]
    out += [{"kind": "navigate", "semantic_id": f"n{i}", "role": None, "name": None,
             "target": f"https://app.example/section-{i}/detail?tab=overview",
             "intent": f"navigate to section {i}"} for i in range(navs)]
    return out


def test_the_step_prompt_is_bounded_no_matter_how_large_the_frontier_grows():
    """Цена фронтира замерена на механизме: ~130 символов на запись, на КАЖДОМ шаге.

    ⚠ Утверждение о ЧИСЛЕ (например «промпт меньше 9000») — не про механизм: его удовлетворит и код
    с жёстко вписанным пределом, и код, которому просто не досталось большого фронтира. Поэтому здесь
    сравниваются ДВА размера фронтира: рост в двадцать раз не имеет права поднять промпт даже вдвое.

    KILLS: удаление кепа (замерено без него: N=50 → 8 142 симв., N=1000 → 131 562).
    """
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    sizes = {}
    for n in (50, 1000):
        fb = FakeBackend('{"done": true}')
        LLMPlanner(backend=fb).propose({"current_url": "https://app.example/",
                                        "coverage_achieved": 0.3, "coverage_target": 0.85},
                                       _candidates(clicks=10, navs=n))
        sizes[n] = len(fb.calls[-1])
    if sizes[1000] > STEP_MENU_CHARS + 2000:
        fail(f"фронтир в 1000 адресов дал промпт {sizes[1000]} символов при бюджете перечня "
             f"{STEP_MENU_CHARS} — кепа нет")
    if sizes[1000] > sizes[50] * 2:
        fail(f"рост фронтира с 50 до 1000 поднял промпт с {sizes[50]} до {sizes[1000]} символов — "
             f"цена по-прежнему линейна по фронтиру")
    print(f"  ok  промпт шага ограничен: фронтир 50 → {sizes[50]} симв., 1000 → {sizes[1000]}")


def test_the_page_keeps_its_own_controls_when_the_frontier_is_huge():
    """Встречное к предыдущему, и без него кеп удовлетворяется кодом, который выбрасывает ВСЁ.

    Клики ограничены страницей, фронтир — нет; выбрасывать надо второе. Простой срез «первые N» по
    общему списку дал бы обратное на длинной странице, потому что клики стоят в списке ПЕРВЫМИ.

    KILLS: префиксный срез общего перечня.
    """
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    fb = FakeBackend('{"done": true}')
    cands = _candidates(clicks=20, navs=1000)
    LLMPlanner(backend=fb).propose({"current_url": "https://app.example/",
                                    "coverage_achieved": 0.1, "coverage_target": 0.85}, cands)
    prompt = fb.calls[-1]
    missing = [c["semantic_id"] for c in cands
               if c["kind"] == "click" and f'"{c["name"]}"' not in prompt]
    if missing:
        fail(f"из перечня выпали клики самой страницы ({len(missing)} из 20) — кеп режет не то: "
             f"контролов на экране десятки, а фронтир не ограничен ничем")
    if '"navigate"' not in prompt:
        fail("фронтир выброшен ЦЕЛИКОМ — модель лишилась единственного способа уйти со страницы")
    print("  ok  при фронтире в 1000 адресов все 20 кликов страницы остались в перечне")


def test_a_pick_lands_on_the_element_the_model_actually_saw():
    """⚠ САМОЕ ОПАСНОЕ МЕСТО ЭТОГО PR, и оно молчаливое.

    Модель отвечает ИНДЕКСОМ, вызывающий берёт `candidates[idx]`. Если отбор перенумерует записи,
    индекс останется В ГРАНИЦАХ списка — и обход пойдёт на ЧУЖОЙ элемент, не выдав ни одного
    признака. Поэтому: индекс из показанной части обязан выбрать ИМЕННО тот кандидат, а индекс из
    выброшенной — быть отвергнут, а не исполнен.

    KILLS: срез меню ДО `enumerate` (перенумерация).
    KILLS: проверка `0 <= idx < len(candidates)` вместо принадлежности показанному.
    """
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    cands = _candidates(clicks=5, navs=1000)
    picked = 3
    fb = FakeBackend(json.dumps({"index": picked}))
    out = LLMPlanner(backend=fb).propose({"current_url": "u", "coverage_achieved": 0.1,
                                          "coverage_target": 0.85}, cands)
    if out.get("action") is not cands[picked]:
        fail(f"выбор #{picked} привёл к {out.get('action')} вместо {cands[picked]} — "
             f"отбор перенумеровал записи, и обход пошёл на чужой элемент")

    # ⚠ ПЛОТНАЯ СТРАНИЦА — СЛУЧАЙ, РАДИ КОТОРОГО ЭТА ПРОВЕРКА И СУЩЕСТВУЕТ. Пока перечень был
    # ПРЕФИКСОМ исходного списка, перенумерация не меняла ничего, и мутация «срезать до enumerate»
    # проходила зелёной как эквивалентная. С забронированной долей фронтира (её появление само
    # найдено мутацией) перечень прификсом быть перестал: часть кликов выброшена, а навигации
    # оставлены, — и вот тут перенумерация отправляет обход на ЧУЖОЙ элемент.
    dense = _candidates(clicks=100, navs=50)
    nav_i = next(i for i, c in enumerate(dense) if c["kind"] == "navigate")
    fb_d = FakeBackend(json.dumps({"index": nav_i}))
    out_d = LLMPlanner(backend=fb_d).propose({"current_url": "u", "coverage_achieved": 0.1,
                                              "coverage_target": 0.85}, dense)
    if out_d.get("action") is not dense[nav_i]:
        fail(f"на плотной странице выбор #{nav_i} привёл к {out_d.get('action')} вместо "
             f"{dense[nav_i]} — записи перенумерованы, и обход пошёл на чужой элемент")

    # Индекс из ВЫБРОШЕННОЙ части: он в границах списка, но модель его не видела.
    hidden = len(cands) - 1
    fb2 = FakeBackend(json.dumps({"index": hidden}))
    out2 = LLMPlanner(backend=fb2).propose({"current_url": "u", "coverage_achieved": 0.1,
                                            "coverage_target": 0.85}, cands)
    if out2.get("action") is not None:
        fail(f"выбран индекс {hidden}, которого не было в промпте, и он ИСПОЛНЕН — проверка "
             f"смотрит в диапазон, а не в предъявленное")
    print("  ok  показанный индекс выбирает свой элемент, непоказанный — отвергнут")


def test_the_frontier_is_never_starved_by_a_control_dense_page():
    """⚠ ЭТОТ СЛУЧАЙ НАЙДЕН МУТАЦИЕЙ, А НЕ ПРЕДУСМОТРЕН, и он про дефект в САМОЙ этой волне.

    Первая редакция кепа отдавала бюджет кликам целиком, а навигациям — остаток. Замерено: на
    странице со ста контролами в перечень попадали 89 кликов и НОЛЬ навигаций. То есть модель на
    плотной странице теряла единственный способ уйти с неё, а обход — фронтир, ради которого две
    предыдущие правки волны и делались. Дефект был бы тихим: прогон продолжается, просто перестаёт
    ходить.

    KILLS: отдача всего бюджета кликам (замерено: навигаций 0 из 50).
    """
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    fb = FakeBackend('{"done": true}')
    LLMPlanner(backend=fb).propose({"current_url": "u", "coverage_achieved": 0.1,
                                    "coverage_target": 0.85}, _candidates(clicks=100, navs=50))
    prompt = fb.calls[-1]
    if '"navigate"' not in prompt:
        fail("на странице со ста контролами в перечень не попало ни одной навигации — фронтир "
             "вытеснен кликами, и модель не может уйти со страницы")
    if '"click"' not in prompt:
        fail("клики страницы вытеснены целиком — резерв фронтира съел то, что резать не должен")
    print("  ok  на плотной странице в перечне есть и клики, и навигации")


def _checks():
    found = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    if len(found) < 8:
        fail(f"найдено {len(found)} проверок вместо восьми — вывод перечня сломался")
    return [f for _, f in found]


def main() -> int:
    for fn in _checks():
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("prompt budget: OK (укладка целыми записями, остаток назван, индекс не перенумерован)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
