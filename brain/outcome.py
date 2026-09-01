"""Единственный автор пары «код выхода + слово вердикта» (ADR-139).

ЗАЧЕМ ЭТОТ МОДУЛЬ СУЩЕСТВУЕТ. «Провалился ли прогон» вычислялось ПЯТЬЮ независимыми выражениями:

  1. `__main__._run_explore`   `ok = plan_file.exists() and len(steps) >= 5` → 0/1
  2. `__main__._write_scenario` `describe и unmatched → 1; ноль заземлённых → 1; иначе 0`
  3. `graph.py`, узел `report`  `failed_run = bool(errors) or not_grounded` → кадр `verdict`
  4. `__main__._run_chat`      `0 if scenario_steps else 1`
  5. `__main__.main`, внешний `except` → `rc = 4`, и кадра нет вовсе

Замерено на шипнутых функциях: (1) и (3) не имеют НИ ОДНОГО общего входа — кадр читает
`{errors, scenario_unmatched, scenario_steps}`, код читает `{plan_file.exists(), len(steps)}`, —
поэтому они не «разъезжаются со временем», а отвечают на разные вопросы и совпадают случайно.
Расхождение идёт в ОБЕ стороны: короткий обход даёт кадр `ok/0` при коде 1, а сошедшийся обход с
одним упавшим кликом — кадр `failed/1` при коде 0.

ЧТО ФИЗИЧЕСКИ МЕШАЕТ СЛЕДУЮЩЕМУ СЛУЧАЮ ТОГО ЖЕ КЛАССА РАЗОЙТИСЬ. Не аккуратность, а устройство:

  · слова `"verdict"` в `brain/graph.py` больше НЕТ — второго эмиттера пришлось бы писать заново,
    а не дописывать строку;
  · `decide()` — чистая тотальная функция от `Facts`: новый повод покраснеть нельзя добавить, не
    расширив `Facts`, а расширение видно в диффе одной строкой;
  · `Outcome.verdict` ВЫВЕДЕНО из `exit_code` таблицей, а не записано рядом с ним — тот же приём,
    которым путь `replay` пользуется давно и потому не расходится;
  · **число нельзя получить, не напечатав кадр**: `announce()` печатает кадр И возвращает код,
    другого способа получить код в `__main__` не осталось.

ПОРОГ «ПЯТЬ ШАГОВ» УБРАН, И ЭТО ЗАМЕР, А НЕ ВКУС. Он наказывал РАЗМЕР приложения: одностраничный
сайт с одной кнопкой, обойдённый ЦЕЛИКОМ (`converged`, `complete: true`, coverage 1.00), давал 2
шага и `exit 1` — то есть `fault: app`, «тест нашёл проблему», при том что проблемы не было.
Двухстраничный сайт (кнопка + ссылка) — 3 шага и тот же исход. Порог заводился как приёмочный гейт
вехи M1 под конкретную фикстуру (`docs/M1_CONTRACT.md`), объяснявшая его строка `GATE NOT MET` из
кода удалена, и сегодня `exit 1` по этой причине не объясняется человеку ничем.

⚠ ЧТО ПОРОГ ВСЁ-ТАКИ ЛОВИЛ — «обход не увидел НИЧЕГО» (сломанный исполнитель отвечает пустотой:
1 шаг, coverage 0.0). Эта ловля СОХРАНЕНА, но переведена на факты, которые её действительно
описывают, и это тоже замер: у сломанного исполнителя и у ЗАКОННОЙ страницы без интерактивных
элементов (текст, заголовки) совпадает ВСЁ — шагов 0, `interactive_seen` пуст, `any(site_map.values())`
ложно, `errors` пуст, `reason` = `no_candidates`. Различает их РОВНО ОДНО: адрес. Вакуум не отвечает
ни на `browser.currentUrl`, ни ключом карты (`site_map` = `{'': []}`), живой браузер отвечает всегда.
Поэтому третий подпункт предиката — `saw_an_address`, и без него законная текстовая страница
получила бы `exit 4` «инструмент сломался», то есть мы обвинили бы себя вместо честного «смотреть
нечего».
"""
from __future__ import annotations

from dataclasses import dataclass

from . import agui
from .eventlog import exit_codes, log

# Коды выхода. Числа читают agentctl, control-api и хаб; источник правды — блок `exit_codes`
# в `brain/events.json`, здесь только имена, чтобы литералов в коде не осталось.
EXIT_PASS = 0
EXIT_FINDING = 1
EXIT_INTEGRITY = 3
EXIT_TOOL_FAILURE = 4
EXIT_TOOL_FAILURE_SALVAGED = 5

# Слово вердикта ВЫВОДИТСЯ ИЗ КАТАЛОГА (ADR-141), а не переписывается здесь.
#
# ⚠ ЗДЕСЬ СТОЯЛ СЛОВАРЬ-ЛИТЕРАЛ, И ЕГО СОБСТВЕННЫЙ КОММЕНТАРИЙ ВЫШЕ НАЗЫВАЛ КАТАЛОГ ИСТОЧНИКОМ
# ПРАВДЫ, КАТАЛОГ ПРИ ЭТОМ НЕ ЧИТАЯ. Это была ВОСЬМАЯ рукописная таблица кодов выхода в дереве, и
# ни одна проверка не сверяла её с остальными семью. Замер 2026-08-31 показал, чем это кончилось на
# другом конце провода: `cmd/control-api` держал свою копию из четырёх слов, и три слова, которые
# рождались ЗДЕСЬ — `tool_failure` (4), `tool_failure_salvaged` (5), `not_started` (-1) — при записи
# в хранилище превращались в `problem`. То есть прогон, про который МЫ уже знали «сломался наш
# инструмент», доезжал до человека как «тест нашёл проблему в приложении».
VERDICT_WORD = {int(_code): _entry["verdict"] for _code, _entry in exit_codes().items()}


@dataclass(frozen=True)
class Facts:
    """ФАКТЫ прогона — то, что наблюдалось. Ни одного решения: решение принимает `decide`.

    Заморожен намеренно: поле, которое можно поправить по дороге, — это второй автор."""

    mode: str                       # "explore" | "goal" | "describe" | "chat"
    config_conflict: bool = False   # прогон отвергнут У ДВЕРИ (несовместимые настройки)
    crashed: bool = False
    salvaged: bool = False
    errors: int = 0                 # len(state["errors"]) — отказы ШАГОВ обхода
    interactive_seen: int = 0
    elements_mapped: int = 0        # sum(len(v) for v in site_map.values())
    saw_an_address: bool = True     # обход вообще получил адрес? см. докстринг модуля
    plan_written: bool = True       # `plan.json` лёг на диск (наш собственный артефакт)
    crawl_complete: "bool | None" = None
    grounded: "int | None" = None   # только пути авторинга
    unmatched: "int | None" = None
    failed_steps: int = 0


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    verdict: str      # ВЫВЕДЕНО: VERDICT_WORD[exit_code]
    degraded: bool
    reason: str       # код каталога или "" — ПОЧЕМУ получилось это число
    failed: int
    healed: int = 0


def _outcome(code: int, reason: str, f: Facts) -> Outcome:
    """Единственное место, где рождается `Outcome`: слово и признак деградации выводятся здесь."""
    return Outcome(
        exit_code=code,
        verdict=VERDICT_WORD.get(code, "problem"),
        degraded=(code != EXIT_PASS or f.crawl_complete is False or bool(f.unmatched)),
        reason=reason,
        failed=f.failed_steps,
    )


def decide(f: Facts) -> Outcome:
    """Факты → исход. ЧИСТАЯ и ТОТАЛЬНАЯ: ни env, ни файлов, ни состояния графа.

    Порядок вопросов — часть конструкции, как в `cmd/control-api/fault.go`: сначала «сломались ли
    МЫ», потом «нашли ли мы что-то про приложение», и только потом «всё хорошо».
    """
    # 0. Отказ У ДВЕРИ: настройки несовместимы, прогон не начинался. Вина `test` — не приложение и
    #    не инструмент, а то, КАК прогон попросили запустить.
    if f.config_conflict:
        return _outcome(EXIT_INTEGRITY, "", f)

    # 1. Инструмент упал. Различие 5/4 — успели ли записать найденное (ADR-131).
    if f.crashed:
        return _outcome(EXIT_TOOL_FAILURE_SALVAGED if f.salvaged else EXIT_TOOL_FAILURE,
                        "explore.crashed", f)

    # 2. Обход не увидел НИЧЕГО — и не потому, что смотреть было нечего. Три подпункта, и третий
    #    (адрес) — единственное, что отличает сломанный исполнитель от законной текстовой страницы.
    if not f.interactive_seen and not f.elements_mapped and not f.saw_an_address:
        return _outcome(EXIT_TOOL_FAILURE, "explore.saw_nothing", f)

    # 3. Наш собственный артефакт не написан. Прежний критерий требовал `plan_file.exists()` и при
    #    его отсутствии возвращал 1, то есть `fault: app` — но не написать СВОЙ файл это отказ
    #    ИНСТРУМЕНТА, и вина здесь исправлена заодно.
    if f.mode == "explore" and not f.plan_written:
        return _outcome(EXIT_TOOL_FAILURE, "explore.saw_nothing", f)

    # ⚠ ПОРЯДОК ветки 2 и ветки 4 НЕНАБЛЮДАЕМ, и это замерено, а не предположено: отказы шагов
    #    рождаются кликами, клики — кандидатами, кандидаты — картой, поэтому у вакуума (карта пуста)
    #    `errors` ровно ноль. Мутация, меняющая их местами, ЭКВИВАЛЕНТНА; запись об этом стоит и
    #    рядом с гейтом (`_HalfBroken` в tests/test_run_outcome_offline.py).
    # 4. Обход наткнулся на отказы шагов — это находка ПРО ПРИЛОЖЕНИЕ, и сегодня она теряется:
    #    `errors` не входит в критерий успеха вовсе, поэтому прогон с упавшими кликами выходит 0.
    #    На путях авторинга отказ шага обхода вердикт НЕ красит: их результат — `scenario.json`,
    #    и заземлённый сценарий остаётся годным.
    if f.mode == "explore" and f.errors:
        return _outcome(EXIT_FINDING, "test.explore_errors", f)

    # 5-6. Авторинг: правило перенесено из `_write_scenario` ДОСЛОВНО и теперь живёт в одном месте.
    if f.mode == "describe" and f.unmatched:
        return _outcome(EXIT_FINDING, "plan.not_grounded", f)
    if f.grounded is not None and f.grounded == 0:
        return _outcome(EXIT_FINDING, "plan.not_grounded", f)

    # 7. Всё остальное — прогон сделал то, о чём его просили. Неполнота обхода при этом НЕ молчит:
    #    её произносит `explore.incomplete` с `degrades: true`, и она доезжает в `degradations`.
    return _outcome(EXIT_PASS, "", f)


def facts_from(state: dict, *, mode: str, crashed: bool = False, salvaged: bool = False,
               config_conflict: bool = False,
               grounded: "int | None" = None, unmatched: "int | None" = None,
               plan_written: bool = True) -> Facts:
    """Состояние графа → `Facts`. Только ЧИТАЕТ и СЧИТАЕТ; ничего не решает и не нормализует."""
    site_map = state.get("site_map") or {}
    # `any(values)`, а не `if site_map`: `perceive` заводит ключ на каждый посещённый путь, поэтому
    # страница без интерактивных элементов даёт `{path: []}` — НЕПУСТОЙ словарь, описывающий НОЛЬ
    # элементов. Та же ловушка уже названа в `graph.py` у записи `site-map.json`.
    mapped = sum(len(v or []) for v in site_map.values())
    seen = state.get("interactive_seen") or []
    # Адрес: либо браузер ответил `current_url`, либо в карте есть непустой ключ страницы. Вакуумный
    # исполнитель не даёт ни того, ни другого (замерено: `current_url` = '', ключ карты = '').
    saw_addr = bool(state.get("current_url")) or any(bool(k) for k in site_map)
    return Facts(
        mode=mode,
        config_conflict=config_conflict,
        crashed=crashed,
        salvaged=salvaged,
        errors=len(state.get("errors") or []),
        interactive_seen=len(seen) if isinstance(seen, (list, tuple, set, dict)) else int(seen or 0),
        elements_mapped=mapped,
        saw_an_address=saw_addr,
        crawl_complete=(state.get("completeness") or {}).get("complete"),
        plan_written=plan_written,
        grounded=grounded,
        unmatched=unmatched,
        failed_steps=state.get("failed_steps", 0) or 0,
    )


# Коды, которые произносит ИМЕННО этот модуль. Прочие причины из `decide` уже сказаны там, где они
# случились (`explore.crashed` — в обработчике падения, `plan.not_grounded` — в `_write_scenario`),
# и повторить их здесь значило бы завести второго автора одного сообщения — ровно ту болезнь, от
# которой лечит весь этот файл.
_OWNED_CODES = {"explore.saw_nothing"}


def announce(o: Outcome, run_id: str) -> int:
    """Печатает кадр `verdict` И ВОЗВРАЩАЕТ код выхода.

    ⚠ Другого способа получить код в `__main__` нет, и это не удобство, а ЗАПРЕТ: кадр перестаёт
    быть отдельным утверждением о том же прогоне и становится побочным продуктом получения числа.
    Расхождение, которое чинит ADR-139, физически требует двух авторов; здесь автор один.

    Кадр уходит и на путях падения/спасения, где раньше его не было ВООБЩЕ — узел `report` до них
    не доезжает, а эмиттер жил в нём.
    """
    # ⚠ ЛИТЕРАЛ, а не `log(o.reason)`. Гейт каталога справедливо отверг переменную: код, собранный
    # в рантайме, он подтвердить не может, а значит и человек не может — «эмитится ниоткуда» это
    # ровно тот класс, за которым гейт и следит. Владеющий код здесь один, и он написан буквой.
    if o.reason == "explore.saw_nothing":
        log("explore.saw_nothing")
    try:
        # Best-effort, как у прежнего эмиттера в графе: кадр — это наблюдаемость поверх stdout, и
        # он не имеет права уронить прогон. ⚠ Но код выхода возвращается В ЛЮБОМ случае — иначе
        # отказ печати снова развёл бы кадр и число.
        agui.emit("verdict", run_id, verdict=o.verdict, exit_code=o.exit_code,
                  healed=o.healed, failed=o.failed)
    except Exception as e:
        log("system.agui_emit_failed", error=e)
    return o.exit_code
