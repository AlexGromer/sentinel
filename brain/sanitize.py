"""Sanitize AUT-derived strings before they enter LLM prompts (#37, THREAT_MODEL §6 rec #6).

A hostile or buggy application-under-test controls the text of its own elements (role, name,
aria-label) and the URLs it navigates to. That text flows into the planner and healing prompts
(brain/planner.py, brain/healing.py). Interpolated raw it enables two problems:

  * prompt injection — embedded directives, or control/format characters (newlines, BiDi overrides,
    zero-width joiners) that restructure or spoof the prompt;
  * unbounded token cost — a single multi-megabyte element name inflates every LLM call.

`safe_text` neutralizes both for one field; `safe_json` maps it recursively over a candidate menu so
the json.dumps that follows only ever sees cleaned values. User-authored text (the run goal /
description) is NOT AUT-controlled and is deliberately out of scope here.
"""
from __future__ import annotations

import json
import re
import unicodedata

# Default per-field cap. Element labels / per-step intents are short; anything longer is either an
# accident or an attack, and the model gains nothing from the tail.
MAX_FIELD = 300

# Unicode general categories dropped entirely: C* = control / format / surrogate / private-use /
# unassigned. Plain spaces (Zs) survive; every whitespace char is first folded to U+0020 so tabs and
# newlines stay as spacing without letting an element name inject a new prompt line.
_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}


def safe_text(value: object, maxlen: int = MAX_FIELD) -> str:
    """Coerce to str, fold whitespace to spaces, drop control/format chars, collapse runs, cap length."""
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    s = "".join(" " if ch.isspace() else ch for ch in s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _DROP)
    s = re.sub(r" +", " ", s).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rstrip() + "…"
    return s


def safe_json(value, maxlen: int = MAX_FIELD):
    """Recursively sanitize every string in a JSON-shaped value (dict/list/scalars); non-str untouched."""
    if isinstance(value, str):
        return safe_text(value, maxlen)
    if isinstance(value, dict):
        return {k: safe_json(v, maxlen) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_json(v, maxlen) for v in value]
    return value


def fit_json_list(records: list, budget: int, maxlen: int = MAX_FIELD) -> "tuple[str, int]":
    """Сериализовать столько ЦЕЛЫХ записей, сколько влезает в `budget` символов.

    Возвращает `(json_text, dropped)` — валидный JSON-массив и число выброшенных записей.

    ⚠ ЗАЧЕМ ЭТО ВООБЩЕ ЕСТЬ. До ADR-136 оба промпта, несущих перечень элементов, обрезались срезом
    УЖЕ СЕРИАЛИЗОВАННОЙ строки: `json.dumps(...)[:8000]` в авторинге сценария и `[:3000]` в
    перезаземлении. Срез по символам не знает границ записи и рубит посреди строкового литерала, а
    значит модель получает НЕ «карту без хвоста», а СЛОМАННЫЙ JSON. Замерено на `testdata/site-spa`
    (карта из 184 элементов, 27 108 символов): до модели доезжали 55 элементов, последний оборван, и
    `json.loads` на этом входе падает `Unterminated string starting at char 7998`. В журнал не
    писалось ничего — ни о срезе, ни о том, что 70 % карты не уехало.

    ⚠ ПОЧЕМУ КЕП ПО ЗАПИСЯМ, А НЕ ПО СИМВОЛАМ. Кеп по символам, поставленный на список, воспроизводит
    ту же болезнь на следующем поле: стоит записи стать длиннее — и обрыв снова окажется посреди
    неё. Единственная форма, переживающая рост карты, — выбрасывать записи ЦЕЛИКОМ и называть,
    сколько выброшено. Число `dropped` возвращается, а не логируется здесь, потому что имя события и
    его текст принадлежат вызывающему: у авторинга и у перезаземления это разные новости.

    ⚠ БЮДЖЕТ МЕНЬШЕ ОДНОЙ ЗАПИСИ — законный вход, а не ошибка: тогда `dropped == len(records)`, текст
    равен `[]`, и вызывающий обязан это произнести. Молчаливый `[]` неотличим от «элементов нет».
    """
    parts = [json.dumps(safe_json(r, maxlen)) for r in records]
    used, kept = 2, 0            # 2 — сами скобки массива
    for p in parts:
        add = len(p) + (2 if kept else 0)   # ", " перед каждой записью, кроме первой
        if used + add > budget:
            break
        used += add
        kept += 1
    return "[" + ", ".join(parts[:kept]) + "]", len(records) - kept

def partial_note(total: int, dropped: int) -> str:
    """Строка промпта, ПРОИЗНОСЯЩАЯ остаток. Пусто, когда перечень полон.

    ⚠ КЕП, О КОТОРОМ НЕ СКАЗАНО МОДЕЛИ, — ЭТО ТИХАЯ ЛОЖЬ. Промпт подаёт перечень как исчерпывающий и
    просит выбрать ЛУЧШЕЕ действие; урезанный без объявления перечень заставляет модель выбирать
    лучшее из подмножества, считая его целым, — и «лучшее» становится ответом на другой вопрос. Тот
    же приём, что у `safe_text` (`sanitize.py`), который дописывает «…» вместо молчаливого среза.
    """
    if dropped <= 0:
        return ""
    return (f"NOTE: this list is PARTIAL — {total - dropped} of {total} candidates are shown "
            f"(the rest did not fit the prompt budget). Choose only from what is shown.\n")


