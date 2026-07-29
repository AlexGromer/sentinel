"""Offline gate: FILEMAP must not answer "—" about a file it already describes.

Run:  .venv/bin/python tests/test_filemap_hygiene_offline.py

FILEMAP.md is read INSTEAD of Glob/Grep — that is its stated purpose, and the project rule says to
check it before searching. A post-commit hook appends a row for every new file with an empty `—`
description, expecting a human to fill it in. When the human fills in a row but the hook has already
appended a second one for the same path, the file ends up described TWICE: once correctly, once as
`—`. A reader who finds the stub first gets "nothing is known about this file" from an index that
knows the answer — worse than no index, because it is trusted.

Measured on main 2026-07-29 before this gate: 222 paths, 10 of them carrying both a filled row and a
hook stub. They also cost real work — both merge conflicts while rebasing the PROD stack onto main
were in exactly these duplicate rows.

Deliberately NARROW, and the narrowness is the design:

  - a stub is only wrong when a FILLED row for the same path exists. That is unambiguous noise.
  - a LONE stub (a new file nobody has documented yet) is NOT failed here. The hook writes it AFTER
    the commit, so gating it would fail every piece of in-progress work the moment a file is added —
    a gate that fires on correct behaviour gets disabled, and a disabled gate protects nothing.
    Those are tracked as debt in BACKLOG ([DOC-FILEMAP-STUB-DUPES]) and are a separate decision.
  - two genuinely FILLED rows for one path are also not failed: brain/record_bridge.py carries two
    correct descriptions from different milestones, and merging that prose is a judgement call, not
    something a gate should force.

⚠ The parser here is the reason this file exists rather than a one-line grep. Rows in FILEMAP end
with a trailing `|` inconsistently, so a row without one has only three separators — splitting on the
fourth field silently yields an empty description and reports FILLED rows as stubs. The first
measurement of this problem did exactly that and called four correct rows broken. The description is
therefore everything after the third pipe, with an optional trailing pipe stripped.
"""
import collections
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILEMAP = os.path.join(REPO, "FILEMAP.md")

# Table furniture, not paths.
_NOT_PATHS = {"Path", "Файл", "------", "---", ""}


def rows(text):
    """(path, description, line_number) for every table row that names a file."""
    out = []
    for n, line in enumerate(text.split("\n"), start=1):
        if not line.startswith("| "):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        path = parts[1].strip()
        if path in _NOT_PATHS or path.startswith("-"):
            continue
        # Everything after the third pipe. A trailing `|` is optional in this file, and assuming it
        # is present is precisely the bug that made the first measurement of this wrong.
        desc = "|".join(parts[3:]).strip()
        if desc.endswith("|"):
            desc = desc[:-1].strip()
        out.append((path, desc, n))
    return out


def is_stub(desc):
    return desc in ("", "—", "-", "–")


def main():
    text = open(FILEMAP, encoding="utf-8").read()
    parsed = rows(text)

    # A gate over an empty set passes vacuously. FILEMAP is a large table; if the parser stops
    # matching (the table gains a column, the furniture changes), that must be loud.
    assert len(parsed) > 100, (
        f"only {len(parsed)} rows parsed out of FILEMAP.md — the parser is not matching the table, "
        "and this gate would be checking almost nothing"
    )
    filled = [p for p in parsed if not is_stub(p[1])]
    assert filled, "every parsed row looks like a stub — the description field is being read wrong"

    by_path = collections.defaultdict(list)
    for path, desc, n in parsed:
        by_path[path].append((desc, n))

    offenders = []
    for path, entries in sorted(by_path.items()):
        if len(entries) < 2:
            continue
        stubs = [n for d, n in entries if is_stub(d)]
        real = [n for d, n in entries if not is_stub(d)]
        if stubs and real:
            offenders.append((path, stubs, real))

    if offenders:
        lines = [
            f"  {path}: stub row(s) at line {stubs} while line {real} already describes it"
            for path, stubs, real in offenders
        ]
        raise AssertionError(
            "FILEMAP.md describes these paths twice, once as an empty hook stub — a reader who finds "
            "the stub first is told nothing about a file the index documents:\n"
            + "\n".join(lines)
            + "\n\nDelete the stub row (the hook appends one per new file; the filled row is the answer)."
        )

    lone = sum(1 for p, e in by_path.items() if len(e) == 1 and is_stub(e[0][0]))
    doubled = sum(1 for p, e in by_path.items() if len(e) > 1)
    print(
        f"filemap hygiene: OK ({len(by_path)} paths, {len(parsed)} rows; no path carries both a "
        f"description and a stub; {doubled} path(s) legitimately listed twice; "
        f"{lone} undocumented stub(s) outstanding — tracked, not gated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
