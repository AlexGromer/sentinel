#!/usr/bin/env python3
"""UI-BI-INTO-TEXTCONTENT — markup must never be handed to a destination that renders plain text.

THE CLASS. `bi(ru, en)` in docs/index.html RETURNS markup:

    function bi(ru, en){ return '<span data-lang="ru">' + ru + '</span><span data-lang="en">' + en + '</span>'; }

because the language toggle works by showing one span and hiding the other. Assign that to
`textContent`, to `title` or to `placeholder` and the reader is shown the tags.

WHY A GATE AND NOT A THIRD FIX. The class appeared three times. The first two were fixed one site at
a time and the file carries a comment about each (`index.html`, at `bi`'s call site in the run
controls, and at `hz-when`: "Caught by a screenshot, and by nothing else"). When the third was found
— again by a screenshot, in the PERSISTENT rail, i.e. on every view — a grep found SEVEN live sites,
not one. Three point fixes in a row is the signal that the point fix is the wrong instrument.

WHY TWO GATES, AND WHY THEY ARE NOT REDUNDANT. `scripts/hub-dom-check.mjs` walks every view and
asserts nothing a reader can see LOOKS like markup. That is the stronger statement — it holds however
the markup got there, including via a helper that does not exist yet. But it only covers states the
sweep actually drives, and four of the seven sites were on the import panel's FAILURE branches: a
non-2xx response and a thrown exception. Nothing drives those, so nothing behavioural can see them.
This file reads the source instead, and therefore covers exactly what the sweep cannot.

(The reverse is also true, which is why neither replaces the other: this file cannot see markup that
reaches the reader without passing through `bi(`.)

Offline, stdlib only, no browser.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

# Destinations that render TEXT. `value` is deliberately absent: an <input> value is text too, but
# `bi()` into a value is not reachable in this hub and adding it would only widen the surface for a
# false positive, which is how a check gets deleted.
TEXT_SINKS = ("textContent", "title", "placeholder", "alt", "label")
SINK_ASSIGN = re.compile(r"\.(%s)\s*(?:\+=|=)(?!=)" % "|".join(TEXT_SINKS))
SET_ATTR = re.compile(r"""setAttribute\(\s*['"](title|placeholder|alt)['"]\s*,""")

# Floors. A regex that stops matching finds nothing, and "no violations" over an empty scan is
# indistinguishable from a pass — the failure mode this repository keeps meeting in its own tests.
MIN_BI_CALLS = 100
# 20, not "the 31 found today". A floor set at the current count is a maintenance tax that goes red
# on an honest refactor — and the thing it must catch is a pattern that matches NOTHING, which is
# nowhere near 20.
MIN_SINK_ASSIGNMENTS = 20

errors = []


def statement_after(src, idx, limit=600):
    """The rest of the assignment: everything up to the terminating `;`.

    Naive on purpose. These are plain assignments, and the alternative — a JavaScript parser — is a
    dependency this suite does not carry. The floors above are what catches the day this stops being
    true, because a broken scan shows up as too few assignments rather than as too few violations.
    """
    end = src.find(";", idx)
    if end < 0 or end - idx > limit:
        end = min(idx + limit, len(src))
    return src[idx:end]


def html_files():
    out = []
    for name in sorted(os.listdir(DOCS)):
        if not name.endswith(".html"):
            continue
        # *.internal.html is gitignored and never shipped; it is not a surface a reader reaches.
        if name.endswith(".internal.html"):
            continue
        out.append(os.path.join(DOCS, name))
    return out


def line_of(src, idx):
    return src.count("\n", 0, idx) + 1


def main():
    files = html_files()
    if not files:
        print("FAIL: no docs/*.html found — this gate scanned nothing")
        return 1

    total_bi = 0
    total_sinks = 0

    for path in files:
        rel = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        total_bi += len(re.findall(r"\bbi\(", src))

        for m in SINK_ASSIGN.finditer(src):
            total_sinks += 1
            stmt = statement_after(src, m.end())
            if re.search(r"\bbi\(", stmt):
                errors.append(
                    "%s:%d assigns bi(...) to .%s — bi() RETURNS `<span data-lang>` markup, so this "
                    "prints the tags at the reader. In the page use innerHTML (esc() every dynamic "
                    "part); biPlain() is for native dialogs, which show both languages at once: %s"
                    % (rel, line_of(src, m.start()), m.group(1), stmt.strip().splitlines()[0][:100]))

        for m in SET_ATTR.finditer(src):
            stmt = statement_after(src, m.end())
            if re.search(r"\bbi\(", stmt):
                errors.append(
                    "%s:%d passes bi(...) to setAttribute(%r) — an attribute renders plain text"
                    % (rel, line_of(src, m.start()), m.group(1)))

        # The other half of the same fix, and a real one: moving these sites to innerHTML means any
        # FOREIGN text concatenated in is now markup. The three sources that actually appear here are
        # a response body, an error message and a status — each must go through esc().
        for m in re.finditer(r"\.innerHTML\s*(?:\+=|=)(?!=)", src):
            stmt = statement_after(src, m.end())
            foreign = re.search(r"\.text\(\)|\.message\b|\bawait r\b", stmt)
            if foreign and "esc(" not in stmt:
                errors.append(
                    "%s:%d writes innerHTML from foreign text (%s) without esc() — that is how a "
                    "message becomes markup: %s"
                    % (rel, line_of(src, m.start()), foreign.group(0),
                       stmt.strip().splitlines()[0][:100]))

        # ТРЕТИЙ случай того же класса, найденный КАДРОМ, а не этим гейтом: `esc(bi(...))`.
        # Экранирование съедает СВОЮ разметку и печатает читателю теги — визуально это тот же отказ,
        # что и `textContent = bi(...)`, но по коду он выглядел безопасным, потому что `esc()` здесь
        # обычно и есть правильное действие. Замер: кнопка отчёта в панели артефактов печатала
        # `<span data-lang="ru">Отчёт (report.html)</span>…` буквально, а гейт был зелёным.
        for m in re.finditer(r"\besc\(\s*(?:[A-Za-z0-9_.]+\s*(?:===?|!==?)[^?]*\?\s*)?bi\(", src):
            errors.append(
                "%s:%d wraps bi(...) in esc() — esc() escapes OUR OWN markup and prints the tags at "
                "the reader. Escape only the dynamic part, never the bi() label: %s"
                % (rel, line_of(src, m.start()), src[m.start():m.start() + 100].splitlines()[0]))

    if total_bi < MIN_BI_CALLS:
        errors.append("only %d bi() calls found across %d file(s) (floor %d) — the scan is not "
                      "reading the hub, so every check above passed over nothing"
                      % (total_bi, len(files), MIN_BI_CALLS))
    if total_sinks < MIN_SINK_ASSIGNMENTS:
        errors.append("only %d text-sink assignments found (floor %d) — the sink pattern has stopped "
                      "matching, which is the silent way this gate dies"
                      % (total_sinks, MIN_SINK_ASSIGNMENTS))

    if errors:
        print("FAIL: %d markup-as-text error(s)" % len(errors))
        for e in errors:
            print("  - " + e)
        return 1

    print("markup-not-text: OK (%d file(s), %d bi() calls, %d text-sink assignments; none hands "
          "markup to a text destination, no innerHTML carries unescaped foreign text)"
          % (len(files), total_bi, total_sinks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
