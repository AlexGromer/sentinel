"""Catalogued diagnostics — stream B of `brain/events.json` (M9-LIVE).

Every human-facing diagnostic goes through `log(code, **fields)`. The level, category and phase are
NOT arguments: they live in the catalogue, so 72 call sites cannot drift apart on what "warn" means.
The catalogue is also where the Russian text lives — the wire stays English (greppable, portable, no
Windows codepage trouble) and the UI resolves `code` to the reader's language.

WIRE FORMAT — one line, readable by a human AND parseable by a machine, with no mode flag:

    [warn|llm] llm.no_anthropic_key: No AI key (planner) — running without AI

That shape is deliberate. A `@@LOG {json}` line would leave a CLI user staring at JSON, and emitting
both a readable line and a JSON line would double the volume; a plain readable line would leave
control-api guessing. This carries level, category, code and message in one pass, so `agentctl run`
in a terminal reads fine while control-api parses it exactly. `mod` is not on the wire — control-api
resolves it from the catalogue's `sites`, which the CI gate keeps truthful.

Module named `eventlog`, not `logging`: a `brain/logging.py` would shadow the standard library for
anything that ends up importing by absolute name — a footgun with no upside.

CAPTURE vs VIEW. This module is the capture side and defaults to `debug` — the whole file is a few
hundred lines and a run cannot be re-created after the fact, so dropping detail here would cost a
repeat run for nothing. The UI hides debug by default on the VIEW side, where it costs nothing to
change your mind. Override per run with SENTINEL_LOG_LEVEL, or per category with
SENTINEL_LOG_LEVELS=heal=info,llm=debug. The `SENTINEL_` prefix already reaches the brain through
agentctl's env allowlist (cmd/agentctl/main.go), so nothing needs plumbing.

Nothing here may ever break a run. A missing catalogue, an unknown code, a bad format field: each
degrades to something visible and keeps going. An unknown code is reported at `error` rather than
swallowed, because the CI gate makes it unreachable in a released build — if it ever prints, the
gate was bypassed and that is worth shouting about.
"""
import json
import os
import pathlib
import sys

_CATALOG_PATH = pathlib.Path(__file__).with_name("events.json")

# Ordered so a threshold comparison is a plain integer compare.
_RANK = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_DEFAULT_LEVEL = "debug"

_events: dict | None = None
_thresholds: tuple[int, dict[str, int]] | None = None


def _catalog() -> dict:
    """The `events` block, loaded once. A broken/absent catalogue yields {} — see module docstring."""
    global _events
    if _events is None:
        try:
            _events = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))["events"]
        except Exception as exc:  # noqa: BLE001 — a logger must not raise
            print(f"[error|system] eventlog.catalog_unreadable: {_CATALOG_PATH}: {exc}",
                  file=sys.stderr, flush=True)
            _events = {}
    return _events


def _levels() -> tuple[int, dict[str, int]]:
    """(default threshold, per-category thresholds) from the environment, parsed once per process."""
    global _thresholds
    if _thresholds is None:
        base = _RANK.get((os.environ.get("SENTINEL_LOG_LEVEL") or "").strip().lower(),
                         _RANK[_DEFAULT_LEVEL])
        per: dict[str, int] = {}
        for pair in (os.environ.get("SENTINEL_LOG_LEVELS") or "").split(","):
            cat, _, lvl = pair.partition("=")
            rank = _RANK.get(lvl.strip().lower())
            if cat.strip() and rank:
                per[cat.strip().lower()] = rank
        _thresholds = (base, per)
    return _thresholds


def reset_cache() -> None:
    """Drop the memoized catalogue and thresholds. For tests that vary the environment."""
    global _events, _thresholds
    _events, _thresholds = None, None


class _Lenient(dict):
    """Leaves an unsupplied placeholder visible instead of raising — a missing field must not cost
    the whole message."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(template: str, fields: dict) -> str:
    try:
        return template.format_map(_Lenient(fields))
    except Exception:  # noqa: BLE001 — an unbalanced brace in the catalogue must not kill the run
        return template


def _emit(lvl: str, cat: str, code: str, msg: str) -> None:
    # The protocol is line-oriented, so a message may not contain newlines. Exception strings and
    # tracebacks routinely do, and an embedded newline would split one event into several unparseable
    # fragments at the boundary.
    flat = " ".join(str(msg).split())
    print(f"[{lvl}|{cat}] {code}: {flat}", file=sys.stderr, flush=True)


# Codes that fired during this run and carry `degrades: true` in the catalogue, in order of first
# occurrence. ADR-077: a run that finished with the LLM absent, the budget spent or the locators
# re-grounded has LOST QUALITY, and until now that fact lived only in a log file — the verdict, the
# report and the JUnit all said the same thing they would have said for a clean run. The catalogue
# already knew which codes mean degradation and already carried the sentence to print; nothing read it.
#
# Module-level, like the catalogue itself: one brain process is one run.
_degraded: "list[str]" = []


def degradations() -> "list[str]":
    """Codes with `degrades: true` that fired this run, deduplicated, in order of first occurrence."""
    return list(_degraded)


def reset_degradations() -> None:
    """Clear the tally. For tests, which run several 'runs' inside one process."""
    _degraded.clear()


def verdict_sentence(code: str, lang: str = "en") -> str:
    """The catalogue's plain sentence for a degrading `code`, for the verdict rather than the log.

    `{ru,en}_verdict` exists precisely because the log line and the verdict line answer different
    questions: the log says what happened at that moment ("No AI key (planner)"), the verdict says what
    it MEANS for the result ("the run completed WITHOUT AI: the plan came from simple rules"). Falls
    back to the code so an artifact never renders a blank where a sentence was expected."""
    entry = _catalog().get(code) or {}
    return str(entry.get(f"{lang}_verdict") or entry.get("en_verdict") or code)


def log(code: str, **fields: object) -> None:
    """Emit the catalogued event `code`, rendering its English text with `fields`."""
    entry = _catalog().get(code)
    if entry is None:
        _emit("error", "system", "eventlog.uncatalogued",
              f"code {code!r} is not in the catalogue (fields={fields!r})")
        return
    # BEFORE the threshold check on purpose. Whether the run degraded is a fact about the RUN; whether
    # the reader wanted to see warnings is a fact about the log VIEW. Recording after the filter would
    # mean `SENTINEL_LOG_LEVEL=error` silently produced a cleaner-looking verdict than the same run at
    # default verbosity — the exact class of silent degradation this is closing.
    if entry.get("degrades") and code not in _degraded:
        _degraded.append(code)
    base, per = _levels()
    cat = entry["cat"]
    if _RANK[entry["lvl"]] < per.get(cat, base):
        return
    _emit(entry["lvl"], cat, code, _render(entry["en"], fields))


def would_log(code: str) -> bool:
    """Whether `code` would pass the current thresholds. For skipping expensive field computation."""
    entry = _catalog().get(code)
    if entry is None:
        return True  # an uncatalogued code always reports
    base, per = _levels()
    return _RANK[entry["lvl"]] >= per.get(entry["cat"], base)
