#!/usr/bin/env python3
"""Gate for the event catalogue (brain/events.json) — offline, no network, no browser.

This is what makes "every human-facing message is catalogued" a CHECKED property rather than a
claim. It runs in both directions, because either direction alone rots:

  forward  — every log() call site in brain/ is claimed by exactly one catalogue entry.
             Without this, a new log line silently reverts to raw English jargon in the UI.
  backward — every catalogue entry points at call sites that still exist.
             Without this, deleted code leaves phantom entries and the count lies.

It also enforces the invariants the two streams depend on:
  * bilingual — `ru` AND `en` on every entry (the product ships RU/EN in parity);
  * a `degrades: true` entry carries a verdict hint in BOTH languages, since that is the one
    legitimate crossing from diagnostics into the run narrative — a run that exits 0 with the
    LLM absent must be able to say so on its verdict;
  * levels/categories/phases/exit codes are drawn from the declared vocabularies, so the UI's
    filters can be built from the catalogue instead of a hand-kept duplicate list;
  * the foreign-output patterns compile and end with a catch-all, so no line is ever unclassified.

Run: .venv/bin/python tests/test_event_catalog_offline.py
"""
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CATALOG = REPO / "brain" / "events.json"

# Modules that emit diagnostics through the module-level log()/_log() helpers. Kept explicit rather
# than globbed: a new brain module that logs must be added here deliberately, which is the point.
LOG_MODULES = ["__main__", "planner", "llm", "graph", "healing", "runcontrol",
               "record_bridge", "replay", "server", "budget"]

# A log emission is a statement STARTING with the helper call. An occurrence inside a longer
# expression (e.g. a comment or a nested call) is not an emission point and must not be counted,
# or the forward direction would demand catalogue entries for lines that print nothing.
LOG_CALL = re.compile(r"^_?log(_unparsed)?\(")

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def actual_sites() -> set[str]:
    """Every `<module>:<line>` in brain/ that emits a diagnostic."""
    found = set()
    for mod in LOG_MODULES:
        path = REPO / "brain" / f"{mod}.py"
        if not path.exists():
            fail(f"LOG_MODULES lists {mod}, but brain/{mod}.py does not exist")
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if LOG_CALL.match(line.strip()):
                found.add(f"{mod}:{lineno}")
    return found


def main() -> int:
    cat = json.loads(CATALOG.read_text())
    events = cat["events"]

    # --- both directions of coverage -----------------------------------------------------------
    claimed: dict[str, str] = {}
    for code, entry in events.items():
        for site in entry["sites"]:
            if site in claimed:
                fail(f"call site {site} is claimed twice: {claimed[site]} and {code}")
            claimed[site] = code

    real = actual_sites()
    for site in sorted(real - set(claimed)):
        fail(f"UNCATALOGUED: brain/{site.replace(':', '.py:')} logs, but no catalogue entry claims "
             f"it — it would render as raw English in the UI")
    for site in sorted(set(claimed) - real):
        fail(f"PHANTOM: catalogue entry {claimed[site]} points at {site}, which no longer logs")

    # --- bilingual, on every entry and every label table ----------------------------------------
    for code, entry in events.items():
        for lang in ("ru", "en"):
            if not entry.get(lang):
                fail(f"{code}: missing `{lang}` text (RU/EN parity is mandatory)")
    for table in ("category_labels", "level_labels", "phases", "modes", "exit_codes",
                  "narrative", "heal_strategies", "heal_outcomes"):
        for key, val in cat[table].items():
            for lang in ("ru", "en"):
                if not val.get(lang):
                    fail(f"{table}.{key}: missing `{lang}` text")

    # --- the diagnostics -> narrative crossing --------------------------------------------------
    degrading = {c: e for c, e in events.items() if e.get("degrades")}
    if not degrading:
        fail("no entry is marked `degrades` — the silent-degradation map is the reason this "
             "catalogue exists; an empty map means the flag was dropped")
    for code, entry in degrading.items():
        for lang in ("ru", "en"):
            if not entry.get(f"{lang}_verdict"):
                fail(f"{code}: `degrades` without `{lang}_verdict` — a run that exits 0 with the "
                     f"LLM absent could not say so on its verdict")

    # --- vocabularies (the UI builds its filters from these, so they must be closed sets) -------
    levels, cats, phases = set(cat["levels"]), set(cat["categories"]), set(cat["phases"])
    exits = set(cat["exit_codes"])
    for code, entry in events.items():
        if entry["lvl"] not in levels:
            fail(f"{code}: level {entry['lvl']!r} is not in `levels`")
        if entry["cat"] not in cats:
            fail(f"{code}: category {entry['cat']!r} is not in `categories`")
        if "phase" in entry and entry["phase"] not in phases:
            fail(f"{code}: phase {entry['phase']!r} is not in `phases`")
        if "exit" in entry and str(entry["exit"]) not in exits:
            fail(f"{code}: exit code {entry['exit']!r} is not in `exit_codes`")

    # Phases must match the graph's real nodes — a renamed node would otherwise leave the
    # narrative naming a phase that never occurs.
    graph_src = (REPO / "brain" / "graph.py").read_text()
    block = re.search(r"for name, fn in \[(.*?)\]:", graph_src, re.S)
    if not block:
        fail("could not locate the node list in brain/graph.py — the phase check went vacuous")
    else:
        nodes = set(re.findall(r'\("([a-z_]+)",', block.group(1)))
        if nodes != phases:
            fail(f"`phases` disagrees with brain/graph.py nodes: "
                 f"only in catalogue {sorted(phases - nodes)}, only in graph {sorted(nodes - phases)}")

    # Exit codes must match the contract comment in agentctl, which is what the UI's verdict reads.
    for expected in ("0", "1", "2", "3", "-1"):
        if expected not in exits:
            fail(f"exit code {expected} missing from `exit_codes` (contract: cmd/agentctl/main.go:11)")

    # --- foreign output: compiles, ordered, and can never leave a line unclassified -------------
    patterns = cat["foreign_patterns"]
    for p in patterns:
        try:
            re.compile(p["match"])
        except re.error as exc:
            fail(f"foreign pattern {p['code']}: bad regex {p['match']!r}: {exc}")
        if p["lvl"] not in levels:
            fail(f"foreign pattern {p['code']}: level {p['lvl']!r} is not in `levels`")
        if p["cat"] not in cats:
            fail(f"foreign pattern {p['code']}: category {p['cat']!r} is not in `categories`")
        for lang in ("ru", "en"):
            if not p.get(lang):
                fail(f"foreign pattern {p['code']}: missing `{lang}` text")
    if patterns and patterns[-1]["match"] != ".":
        fail("the last foreign pattern must be the catch-all `.` — otherwise a line from a tool we "
             "do not control ends up with no level and no category")

    # --- report ---------------------------------------------------------------------------------
    if failures:
        print(f"FAIL — {len(failures)} problem(s):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"event catalogue OK: {len(events)} codes cover {len(real)} log call sites "
          f"({len(degrading)} marked as silent degradations), "
          f"{len(cat['phases'])} phases, {len(cat['exit_codes'])} exit codes, "
          f"{len(patterns)} foreign patterns; RU/EN complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
