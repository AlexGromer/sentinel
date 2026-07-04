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
