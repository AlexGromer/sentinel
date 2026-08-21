#!/usr/bin/env python3
"""[GATE-DISTRIBUTION-SERVICES] — the §2 service table of docs/DISTRIBUTION.md against the compose
file it claims to describe, in BOTH language halves.

Run:  .venv/bin/python tests/test_distribution_services_table_offline.py

WHY THIS EXISTS, AND WHAT IT ALREADY COST. The table is kept by hand (principle 5) and it drifted:
`orchestrator` — a service of the DEFAULT stack, the one `docker compose up` starts with no flags —
had no row from the day the service was added (2026-08-16, `41917ac`, ADR-126) until the row was
written by hand two days later (2026-08-18, `e9a9d4b`), after the gap was noticed and measured into
BACKLOG on 2026-08-17. For those two days the document a person reads to learn what the delivery
consists of was missing a part of the delivery, and nothing was red. The note under the table had already named
the fix — "this table is hand-kept and should be gated through the `_services()` parser" — and the
gate did not exist. This is that gate.

WHY A MISSING ROW IS THE HARD CASE. An EXTRA row is self-reporting: a reader looks the service up in
compose, does not find it, and the document is wrong in a way that announces itself. A missing row is
not: the nine rows that remain are each correct, the table looks complete, and only somebody who
already knows the service exists can tell. So the check is SET EQUALITY against a set DERIVED from
`docker-compose.yml`, never a list of names kept here — a list here would need the same edit the
table needed, on the same day, by the same person who did not make it. Same asymmetry, same answer as
[DOC-ARCH-EN-DRIFT]: what is enumerated by hand loses entries silently.

WHY IT IMPORTS `_services` RATHER THAN PARSING COMPOSE AGAIN. A second parser is a second thing to
get wrong, and two parsers can disagree while both stay green. Importing also means this gate and the
parity gate see exactly the same services: if the parser degrades, the parity gate's own floors
(`>= 6` services, `>= 2` CDP services) fail there rather than letting this one pass over an empty set.

CHECKED BY MUTATION — 7 of 7 killed, and each by a DIFFERENT assertion (2026-08-21):
  1. RU: the `orchestrator` row deleted — the historical defect verbatim → red, "documents no row".
  2. EN: the `browser-vnc` row deleted → red on the same assertion, in the mirror half.
  3. RU: `browser-vnc` moved from profile `vnc` to `cli` — row present, wrong command → red.
  4. RU: the anchoring heading renamed, so the parser reads nothing → red on the heading count,
     not on a silently empty table.
  5. EN: a profile cell written as prose instead of a backticked name → red; unrecognised does not
     become "(always)".
  6. EN: the last two rows swapped — sets still equal to each other AND to compose → red ONLY on the
     mirror-order assertion, with the three other tests passing.
  7. RU: a row duplicated — still set-equal to compose → red ONLY on the duplicate assertion.
The last two are why the mirror test is kept alongside the compose comparison: they measure which
defect each check catches instead of assuming the pair is non-redundant.

DELIBERATELY NOT ASSERTED: `docker-compose.ghcr.yml`. `test_compose_parity_offline` already ties the
two files as sets of names AND of profiles, so comparing the table to both would restate that tie
rather than catch anything it does not — and two checks are non-redundant only when it is measured
which defect each one catches.
"""
import pathlib
import re
import sys

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))

# After the sys.path line above, deliberately: the import must resolve when the file is run as a
# script from the repo root, which is how the CI loop runs every tests/test_*_offline.py.
from test_compose_parity_offline import BUILT, _services

RU = REPO / "docs" / "DISTRIBUTION.md"
EN = REPO / "docs" / "DISTRIBUTION.en.md"

# The heading that opens the table, per half. Anchored on the heading rather than on "the first table
# in the file": §2 carries a second table right below this one (the three UI deployment modes), and a
# gate that drifts onto a neighbouring table would still be green while checking the wrong subject.
HEADING = {RU: "### Сервисы", EN: "### Services"}

# How the table writes "no profile — this one starts by default". A cell that is neither this nor a
# backticked profile name is an ERROR below, not a default: "unrecognised" must never quietly become
# "starts with the product", which is the single most expensive thing the table can get wrong.
DEFAULT_MARKER = {RU: "(всегда)", EN: "(always)"}

# A floor on the parse. Ten services exist today. Equal-but-empty sets agree perfectly, so a heading
# renamed, a table turned into a list, or a row shape changed would otherwise pass this file while
# reading nothing at all. The number moves only when somebody decides it does.
MIN_ROWS = 8


def _rows(path: pathlib.Path) -> "list[tuple[str, set[str]]]":
    """(service name, its profiles) for every row of the §2 table, IN FILE ORDER.

    A list rather than a dict: order and duplicates are both evidence, and a dict silently discards
    the second of two rows naming the same service.
    """
    text = path.read_text()
    heading = HEADING[path]
    hits = re.findall(r"(?m)^" + re.escape(heading) + r"\s*$", text)
    assert len(hits) == 1, (
        f"{path.name}: found {len(hits)} `{heading}` headings, expected exactly 1. With none there is "
        f"nothing to read and every assertion below is vacuous; with two, this gate would silently "
        f"pick one table and leave the other ungated.")
    m = re.search(r"(?m)^" + re.escape(heading) + r"\s*$", text)
    body = text[m.end():]
    # Stop at the next heading of any level — the table ends where the section does.
    nxt = re.search(r"(?m)^#{1,6} ", body)
    if nxt:
        body = body[: nxt.start()]

    out: "list[tuple[str, set[str]]]" = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        assert len(cells) >= 4, f"{path.name}: table row has fewer than 3 cells: {line!r}"
        name_cell, profile_cell = cells[1].strip(), cells[2].strip()
        if set(name_cell) <= {"-", ":"} and name_cell:  # the |---|---|---| separator
            continue
        if name_cell in ("Сервис", "Service"):  # the header row
            continue
        nm = re.fullmatch(r"`([a-z0-9][\w-]*)`", name_cell)
        # Not `continue`: a row this parser cannot read is a row this gate does not check, and that
        # is the failure mode the whole file is about. It has to be loud.
        assert nm, (
            f"{path.name}: cannot read a service name from the first cell of {line[:120]!r}. The row "
            f"is inside the §2 service table and would otherwise be skipped — i.e. ungated.")
        out.append((nm.group(1), _profiles(profile_cell, path, nm.group(1))))

    assert len(out) >= MIN_ROWS, (
        f"{path.name}: parsed only {len(out)} rows from the `{heading}` table, expected at least "
        f"{MIN_ROWS}. Either the table shrank by half, or this parser stopped seeing it — and in the "
        f"second case every comparison below runs over almost nothing and passes.")
    return out


def _profiles(cell: str, path: pathlib.Path, name: str) -> "set[str]":
    """The profile cell as a set, in the same shape `_services()` returns: default == empty set."""
    marker = DEFAULT_MARKER[path]
    ticked = set(re.findall(r"`([^`]+)`", cell))
    if cell == marker:
        return set()
    assert marker not in cell, (
        f"{path.name}: the profile cell for `{name}` is {cell!r} — it says both {marker!r} and a "
        f"profile name. A service is either in the default stack or behind a profile; a cell saying "
        f"both leaves the reader to guess which command starts it.")
    assert ticked, (
        f"{path.name}: the profile cell for `{name}` is {cell!r}, which is neither {marker!r} nor a "
        f"backticked profile name. Read as a default it would claim the service starts with "
        f"`docker compose up`; this gate refuses to guess.")
    return ticked


def test_the_two_halves_list_the_same_services():
    """RU is the primary half and EN the mirror; a service documented in one only is documented for
    half the readers. Order is compared too, because the mirror is a mirror — and because a row moved
    or duplicated during a translation edit is invisible to a set comparison."""
    ru, en = _rows(RU), _rows(EN)
    for path, rows in ((RU, ru), (EN, en)):
        names = [n for n, _ in rows]
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, (
            f"{path.name}: services listed twice in the §2 table: {dupes}. Two rows for one service "
            f"are two answers to the same question, and the row count floor above counted both.")

    ru_names, en_names = [n for n, _ in ru], [n for n, _ in en]
    only_ru = sorted(set(ru_names) - set(en_names))
    only_en = sorted(set(en_names) - set(ru_names))
    assert not only_ru and not only_en, (
        f"the §2 service table differs between the halves: only in {RU.name}: {only_ru}; only in "
        f"{EN.name}: {only_en}. The two halves must agree in CONTENT — an English reader must not get "
        f"a different delivery than a Russian one.")
    assert ru_names == en_names, (
        f"the two halves list the same services in a different order: {RU.name} {ru_names} vs "
        f"{EN.name} {en_names}. The mirror is maintained row by row; a reordering is a sign that one "
        f"half was edited without the other, which is exactly how a row goes missing.")


def test_the_two_halves_give_every_service_the_same_profile():
    """A profile name IS the command a person types. `--profile vnc` in one half and nothing in the
    other means one of the two documents promises a service the reader's `docker compose up` will not
    start — and the reader has no way to know which half is wrong."""
    ru, en = dict(_rows(RU)), dict(_rows(EN))
    for name in sorted(set(ru) & set(en)):
        assert ru[name] == en[name], (
            f"service `{name}` is behind profiles {sorted(ru[name]) or ['(default)']} in {RU.name} "
            f"but {sorted(en[name]) or ['(default)']} in {EN.name} — the same table tells two "
            f"readers to type two different commands.")


def test_every_compose_service_has_a_row_and_no_row_invents_one():
    """The set equality this whole file is for. `orchestrator` failed exactly this, for a day."""
    compose = _services(BUILT)
    for path in (RU, EN):
        documented = {n for n, _ in _rows(path)}
        missing = sorted(set(compose) - documented)
        invented = sorted(documented - set(compose))
        assert not missing, (
            f"{path.name} §2 documents no row for services that {BUILT.name} defines: {missing}. "
            f"A reader learning what the delivery consists of would not learn about them — the way "
            f"`orchestrator`, a service of the DEFAULT stack, went undocumented from ADR-126 until "
            f"it was caught by hand.")
        assert not invented, (
            f"{path.name} §2 documents services {BUILT.name} does not define: {invented}. The table "
            f"promises something the stack cannot start; the source of truth is the compose file.")


def test_a_row_names_the_profile_compose_actually_gives_the_service():
    """Naming the service is half the row. A service moved behind a profile — or out from behind one,
    the way `webui` was in #190 (ADR-112) — changes which command starts it, and a table that still
    says the old thing sends the reader to a stack missing exactly the part they came for."""
    compose = _services(BUILT)
    for path in (RU, EN):
        for name, profiles in _rows(path):
            if name not in compose:
                continue  # named by the test above; not restated here
            assert profiles == compose[name], (
                f"{path.name} §2 says `{name}` is behind profiles "
                f"{sorted(profiles) or ['(default)']}, but {BUILT.name} puts it behind "
                f"{sorted(compose[name]) or ['(default)']}. Either the table is stale or the service "
                f"moved without the document following it.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} distribution-services-table tests passed")
