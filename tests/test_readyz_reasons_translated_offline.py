#!/usr/bin/env python3
"""[HEALTH-REASON-EN] / W6 — every reason the product AUTHORS is authored in both languages.

Run:  .venv/bin/python tests/test_readyz_reasons_translated_offline.py

WHY THIS EXISTS, AND WHY IT DERIVES ITS LIST. /readyz reasons are what the Health view puts in front
of a Russian reader, and until W6 every one of them was English. The first fix drew the boundary on
the WRONG AXIS — translate `skipped`, leave `error` alone — which sounds principled and is not: a
SCREENSHOT of the finished view showed `config: ОТКАЗ / no config stored; run the setup wizard` as the
single English line in an otherwise Russian table, and that sentence is ours from end to end, with no
error anywhere in it.

The honest boundary is PROVENANCE:

    ours     — composed only of our own literals and our own interpolations (a path, an address, an
               HTTP status). It must carry DetailRU.
    not ours — carries err.Error(). Translating the wrapper and leaving a Go error string as the tail
               puts two languages inside one sentence, which is worse than either; the view states
               this once, in a footnote under the table.

And the list is DERIVED from cmd/control-api/readyz.go rather than written here (principle 5,
docs/DEVELOPMENT.md §0): a hand-kept list of "reasons that need translating" shows the entry that is
WRONG and never the one that is ABSENT, and absent is the whole failure mode — a probe added next
month with an English-only skip is exactly what this must catch by construction.

⚠ COMPANION FLOOR. A parser that stops matching yields an empty set and passes perfectly. MIN_CHECKED
is just under the number measured on 2026-08-22.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READYZ = os.path.join("cmd", "control-api", "readyz.go")

# 38 readyCheck literals measured 2026-08-22, 19 of them ours. Floors sit just under. Only ever UP.
MIN_CHECKED = 34
MIN_TRANSLATED = 17

# An expression is NOT ours once a Go error is spliced into it. `.Error()` is the only marker needed:
# every foreign string in this file arrives that way, and a broader test (say, any identifier) would
# exempt our own constants — which is how storeUnavailableMsg would have escaped.
FOREIGN = ".Error()"

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def literals(src: str) -> "list[tuple[int, str]]":
    """Every `readyCheck{...}` composite literal, with the line it starts on.

    Brace-matched rather than regexed: these literals span up to six lines and contain braces in the
    prose. A regex would either stop at the first `}` (truncating the fields this gate reads) or run
    to the last one (swallowing the next literal whole), and both failures look like a clean pass.
    """
    out = []
    for m in re.finditer(r"readyCheck\{", src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((src.count("\n", 0, m.start()) + 1, src[m.end():i]))
    return out


def main() -> int:
    with open(os.path.join(REPO, READYZ), encoding="utf-8") as fh:
        src = fh.read()
    lits = literals(src)
    if len(lits) < MIN_CHECKED:
        fail(f"found only {len(lits)} readyCheck literal(s) in {READYZ} — the brace matcher stopped "
             f"working, and an empty set satisfies every assertion below")

    translated = 0
    for line, body in lits:
        m = re.search(r"\bDetail:\s*(.*?)(?:,\s*\n|\}$|,\s*DetailRU:)", body + "}", re.S)
        if not m:
            continue  # an ok probe with nothing to report — success is silent, by design
        detail = m.group(1)
        has_ru = "DetailRU:" in body
        if FOREIGN in detail:
            # Not ours. It must NOT claim to be translated either: a Russian wrapper around a Go error
            # is the mixed-language sentence this boundary exists to prevent.
            if has_ru:
                fail(f"{READYZ}:{line} carries DetailRU beside a reason built from err.Error() — half a "
                     f"sentence in each language is worse than one whole sentence in either")
            continue
        if not has_ru:
            fail(f"{READYZ}:{line} authors an English reason with no DetailRU beside it: {detail.strip()[:90]} "
                 f"— this is what a Russian reader is shown in the Health view")
            continue
        translated += 1

    if translated < MIN_TRANSLATED:
        fail(f"only {translated} authored reason(s) carry a Russian half, below the floor of "
             f"{MIN_TRANSLATED} — the walk narrowed, and a narrowed walk reports 'all translated' in "
             f"exactly the same words as a complete one")

    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"readyz reasons: OK ({len(lits)} readyCheck literals, {translated} authored reasons and each "
          f"carries both language halves)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
