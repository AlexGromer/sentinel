#!/usr/bin/env python3
"""Gate for the event catalogue (brain/events.json) — offline, no network, no browser.

This is what makes "every human-facing message is catalogued" a CHECKED property rather than a
claim. It runs in both directions, because either direction alone rots:

  forward  — every code a brain module actually emits exists in the catalogue, and the entry lists
             that module. Without this, a new log line silently reverts to raw English in the UI.
  backward — every catalogue entry names modules that still emit it.
             Without this, deleted code leaves phantom entries and the count lies.

ANCHORING IS PER MODULE, NOT PER LINE. The first version of this gate pinned `sites` to
`<module>:<line>`, and the very first real conversion invalidated all 64 entries at once — removing
four local logger definitions shifted every line number below them. A gate that fails on unrelated
edits above a log call is a gate someone switches off within a week. Module anchoring is also the
stronger check: it reads the code literals out of the source rather than counting call sites, so the
backward direction verifies a module really does emit what the catalogue claims, instead of
confirming a line number still holds some log call or other. To find the exact line, grep the code
string — unlike a line number, that never goes stale.

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
               "record_bridge", "replay", "server", "budget", "report", "store", "otel", "health",
               "frames"]
# `frames` joined with PROD-FAIL-MEDIA part A, and the gate is why the list is right. `capture_frame`
# moved out of `graph.py` into its own module so `replay.py` could take the picture of a failed step;
# the code it emits (`live.frame_failed`) did not change at all, but the FILE that emits it did — and
# this gate immediately called the catalogue entry a PHANTOM, "emitted by nothing I look at". That is
# exactly the answer a moved emitter should get from a list that is kept by hand.
# `store` and `otel` joined in HEALTH-002, when two long-silent handlers were made to declare: the
# chats projection (which had not been written by ANY deployment for months, and whose `except: pass`
# is why nobody noticed) and tracing setup (which left an operator believing spans were being
# collected). A module that emits and is not scanned is a module whose codes the catalogue cannot
# vouch for.
# `report` joined the list in ADR-097, when the report generator gained its first `log()` call. It is
# listed rather than special-cased: a module that emits and is not scanned is a module whose codes the
# catalogue cannot vouch for, and the gate said so — it called the new entry a PHANTOM, which was the
# correct answer to "catalogued, emitted by nothing I look at".

# Not every human-facing message comes from the brain. An entry may name an `emitter` instead of
# brain modules — the tested application's own console reaches us through the Playwright executor
# (ADR-067), which is TypeScript. Such an entry is still held to the same two-way rule: the code must
# actually appear in the named source, or the catalogue is claiming a message nothing sends.
# ADR-089 widened this to the Go control-api: its human-facing lines were outside the catalogue
# entirely, arriving in the UI through the `system.unclassified` catch-all at `info` — so a Go
# warning was filed at the same severity as "browser launched".
# A value may name a FILE or a DIRECTORY. control-api became a directory in HEALTH-005: the gate read
# only main.go, so a code emitted from any other file of the same binary was invisible to it — a blind
# spot in the one check whose job is "the catalogue cannot claim a message nothing sends". The service
# codes live in access.go/session.go/configfile.go, and the widening is what lets them be checked at
# all rather than a concession made to let them pass.
# HEALTH-005 PR-B added the third: `agentctl` emits `service.log_purged` when an operator destroys
# journal records. It is the one event the control-API cannot write, because the command that causes
# it does not go through the control-API — which is also why the catalogue could not vouch for it
# until this line existed. The gate said so, correctly, by calling the entry a PHANTOM.
# HEALTH-005 PR-C put a journal writer in the BROWSER service (pw-executor/src/cdp-service.ts, via
# svcjournal.ts), and this map still named one file — so those emissions were outside every scan the
# catalogue gate performs, exactly the blind spot the control-api widening had already fixed once.
# A DIRECTORY, therefore, for the same reason and by the same rule: a file added tomorrow is scanned
# because of where it lives, not because somebody remembered to add a line here.
EMITTER_FLOORS = {"pw-executor": 9, "control-api": 18, "agentctl": 2}   # control-api 14→18: LIVE-VNC added the four service.screen_* codes   # agentctl 1→2: LIVE-VNC added service.vnc_password_source

FILE_SUFFIXES = {"json", "jsonl", "ts", "js", "go", "py", "md", "html", "yml", "yaml", "log"}

EMITTERS = {"pw-executor": "pw-executor/src",
            "control-api": "cmd/control-api",
            "agentctl": "cmd/agentctl"}

# An emission is `log("<code>"` with a literal first argument. A non-literal call (a code built at
# runtime) is deliberately unmatched and reported separately: the catalogue cannot vouch for a code it
# cannot see, so building one dynamically has to be a conscious, visible choice.
LOG_CALL = re.compile(r"\blog\(\s*\"([a-z][\w.]*)\"")
LOG_DYNAMIC = re.compile(r"\blog\(\s*(?![\"#])[A-Za-z_]")

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def emitter_codes() -> dict[str, set[str]]:
    """code -> the set of non-brain emitters that reference it, read from their source."""
    found: dict[str, set[str]] = {}
    for name, rel in EMITTERS.items():
        path = REPO / rel
        if not path.exists():
            fail(f"EMITTERS names {name} -> {rel}, which does not exist")
            continue
        # ⚠ The directory branch used to glob "*.go" ALONE, which made it silently useless for a
        # TypeScript package: pointing it at pw-executor/src turned seven live codes into phantoms,
        # because the walk found no files at all and "never mentions that code" is what an empty scan
        # says about everything. Sources are taken by EXTENSION, both languages, so an emitter package
        # is scanned for what it is rather than for what the first emitter happened to be written in.
        exts = ("*.go", "*.ts")
        files = sorted(f for e in exts for f in path.glob(e)) if path.is_dir() else [path]
        # Test files are excluded on purpose: a code that appears only in a _test.go is emitted by
        # nothing the product ships, and counting it would let a phantom entry pass by being mentioned
        # in its own gate. Same for the TypeScript form (*.test.ts).
        files = [f for f in files if not f.name.endswith("_test.go") and not f.name.endswith(".test.ts")]
        src = "\n".join(f.read_text() for f in files)
        for code in re.findall(r"['\"]((?:app|test|ui|service)\.[\w.]+)['\"]", src):
            # ⚠ A quoted dotted string is not automatically a code. Widening the scan to the browser
            # service immediately produced `service.jsonl` — the journal's FILE NAME, which has the
            # exact shape of a code and is not one. Filtering by the last segment being a file
            # extension is narrow on purpose: it removes a class the shape cannot distinguish, and it
            # cannot hide a real code unless somebody names one `*.json`.
            if code.rsplit(".", 1)[-1] in FILE_SUFFIXES:
                continue
            found.setdefault(code, set()).add(name)
        # ⚠ A FLOOR PER EMITTER, and it was bought by a surviving mutation. Narrowing `pw-executor`
        # back from the package to `server.ts` alone passed every check: the browser service's
        # `service.started`/`service.stopped` are ALSO emitted by control-api, so the catalogue's
        # claim stayed vouched for BY ANOTHER PATH while the emissions this map is supposed to scan
        # went unread again. A phantom check cannot see coverage that is provided by somebody else —
        # only a count can. Floors are set just below the measured numbers and may only go UP.
        if len(found_here := {c for c, who in found.items() if name in who}) < EMITTER_FLOORS[name]:
            fail(f"emitter {name!r} yielded {len(found_here)} code(s), below the recorded floor of "
                 f"{EMITTER_FLOORS[name]} — the scan narrowed, and a narrowed scan reports "
                 f"'nothing to see' in exactly the same words as a clean one")
    return found


def emitted_codes() -> dict[str, set[str]]:
    """code -> the set of brain modules that emit it, read from the source."""
    found: dict[str, set[str]] = {}
    for mod in LOG_MODULES:
        path = REPO / "brain" / f"{mod}.py"
        if not path.exists():
            fail(f"LOG_MODULES lists {mod}, but brain/{mod}.py does not exist")
            continue
        src = path.read_text()
        for code in LOG_CALL.findall(src):
            found.setdefault(code, set()).add(mod)
        for line in src.splitlines():
            stripped = line.strip()
            if LOG_DYNAMIC.search(stripped) and not stripped.startswith(("#", "def ", "*")):
                fail(f"brain/{mod}.py builds a log code at runtime — the catalogue cannot vouch for "
                     f"a code it cannot see: {stripped[:90]}")
    return found



def check_exit_promises_match_the_code(events):
    """ADR-087: an entry that declares `exit: N` promises the process really exits N.

    Nothing checked this, and four entries were wrong in the direction that misleads hardest: they
    declared 3 ("нужен человек: план или настройки не сходятся") while the code returned 2 ("страница
    отличается от сохранённого эталона"). A missing plan.json therefore reached every exit-code reader
    as a visual regression, sending them to look at the UI instead of at their config.

    Matched from the source rather than executed: the four paths need a browser, a plan and a live
    executor between them. A text assertion is weaker than a call and is what is available — and it is
    strictly stronger than the nothing that guarded these before.
    """
    src = (REPO / "brain" / "__main__.py").read_text()
    declared = {c: e["exit"] for c, e in events.items() if "exit" in e}
    if not declared:
        fail("no entry declares an exit — this check would be vacuous")
    checked = 0
    for code, want in declared.items():
        # find `log("<code>" ...)` and the first `return N` within the next few lines
        for m in re.finditer(r'log\("' + re.escape(code) + r'"', src):
            tail = src[m.end(): m.end() + 400]
            rm = re.search(r'\breturn (-?\d+)\b', tail)
            if not rm:
                continue
            got = int(rm.group(1))
            checked += 1
            if got != want:
                fail(f"{code}: catalogue promises exit {want}, code returns {got} — a reader of the "
                     f"exit code is told the wrong thing about whose problem this is")
    if checked == 0:
        fail("no declared exit could be matched to a return — the check is not looking at anything")
    return checked


def check_source_overrides(cat, events):
    """HEALTH-004: an event may override the SOURCE its category implies, and must justify it.

    The source axis is derived (cat -> sources -> audiences) precisely so the two cannot disagree, and
    every override weakens that guarantee. So the override is allowed, narrow, and has to argue for
    itself: without `src_why` the gate refuses it. An unexplained override is how a second, quietly
    diverging classification starts — this page already carries one (`lvKindOf`).

    The case it exists for: `heal.drift_*` are emitted by the healer and are statements about the
    APPLICATION. Their category stays `heal` — drift is a healing concept and a `heal` filter must
    show it — but with the derived source the `business` audience hid the product's only report that
    the interface under test had moved.
    """
    valid_sources = set(cat["sources"])
    overrides = {c: e for c, e in events.items() if e.get("src")}
    for code, e in overrides.items():
        if e["src"] not in valid_sources:
            fail(f"{code}: src override {e['src']!r} is not a declared source ({sorted(valid_sources)})")
        if not (e.get("src_why") or "").strip():
            fail(f"{code}: overrides its source with no `src_why` — an override that cannot argue for "
                 f"itself is how a second, silently diverging classification begins")
        if e["src"] == cat_source(cat, e["cat"]):
            fail(f"{code}: overrides its source to {e['src']!r}, which is what the category already "
                 f"implies — a no-op override is noise that will outlive the reason it was added")

    # The drift codes by name: this is the whole point of the mechanism, and a future edit that
    # re-derives them has to argue with this line rather than quietly change a filter's meaning.
    for code in ("heal.drift_rebind", "heal.drift_reground", "heal.drift_summary"):
        if events[code].get("src") != "application":
            fail(f"{code} must be sourced to the application: «the interface changed» is a fact about "
                 f"the application under test, and with the derived source the business filter hides "
                 f"the only place the product says so")
    return len(overrides)


def cat_source(cat, category):
    for src, meta in cat["sources"].items():
        if category in meta["cats"]:
            return src
    return ""


def check_fault_axis(cat, events):
    """HEALTH-004: every code that can END a run says WHOSE problem the ending is.

    The rule is derived, not listed: an entry declaring `exit` is by definition one that terminates a
    run, so that same set must declare `fault`. A hand-kept list would let the next terminal code
    arrive without one, and a run whose fault nobody declared falls back to the exit code — which is
    precisely the conflation this axis exists to remove (exit 3 was `integrity` for a corrupt plan AND
    for a refusal to start because ollama was down; the hub told the second one to go check plan_hash).

    `none` is deliberately a MEMBER of the vocabulary rather than an absent field: "this run harmed
    nobody" is an answer, and an empty string would be indistinguishable from "we never decided".
    """
    faults = cat.get("faults")
    if not faults:
        fail("no `faults` vocabulary — the fault axis cannot be a closed set without one")
    if "none" not in faults:
        fail("`faults` has no `none` member — a clean run has no fault, and that must be sayable")
    for name, val in faults.items():
        for lang in ("ru", "en"):
            if not val.get(f"{lang}_hint"):
                fail(f"faults.{name}: missing `{lang}_hint` — the hub renders the hint next to the "
                     f"verdict, and a domain nobody can explain is a label, not an answer")

    for code, entry in cat["exit_codes"].items():
        if entry.get("fault") not in faults:
            fail(f"exit_codes.{code}: fault {entry.get('fault')!r} is not in `faults`")

    terminal = {c: e for c, e in events.items() if "exit" in e}
    if not terminal:
        fail("no entry declares an exit — this check would be vacuous")
    for code, entry in terminal.items():
        if entry.get("fault") not in faults:
            fail(f"{code}: declares `exit` but its fault {entry.get('fault')!r} is not in `faults` — "
                 f"a code that ends a run must say whose problem the ending is")

    # The whole point is DISCRIMINATION: an axis whose every member is the same word answers nothing.
    # This caught nothing when written and is here so that collapsing the map into a constant fails.
    distinct = {e["fault"] for e in terminal.values()}
    if len(distinct) < 3:
        fail(f"terminal codes name only {sorted(distinct)} — the fault axis is supposed to tell "
             f"'we broke' apart from 'your application did', and it currently cannot")

    # The one the product could not say before HEALTH-004, pinned by name so a future edit that
    # re-attributes it to the application has to argue with this line.
    if events["fatal.llm_required_unreachable"]["fault"] != "tool":
        fail("fatal.llm_required_unreachable must be `tool`: a run refused BECAUSE OUR MODEL IS "
             "UNREACHABLE is not a finding about the application under test")
    if events["fatal.internal_error"]["fault"] != "tool":
        fail("fatal.internal_error must be `tool` — its own message says so in both languages")

    # A code may name a fault WITHOUT declaring an exit, and these five have to. Measured live on
    # 2026-08-04: a goal run whose model endpoint answered 404 emitted plan.scenario_error_empty
    # (degrades, src=tool, the 404 quoted in the message), authored zero steps and exited 1 — and the
    # verdict blamed `app`, because exit 1 alone means "the test found a problem in the application".
    # The product KNEW whose problem it was and the badge said the opposite. These codes end a run in
    # every sense except declaring a number, so they carry the answer.
    for code in ("plan.scenario_error_empty", "plan.scenario_budget_empty",
                 "plan.describe_error_empty", "plan.describe_budget_empty",
                 "plan.output_unparseable"):
        if events[code].get("fault") != "tool":
            fail(f"{code} must be `tool`: authoring that produced nothing failed on OUR endpoint, OUR "
                 f"budget or OUR parser — attributing it to the application sends the reader to debug "
                 f"the one thing that was working")
    extra = sum(1 for c, e in events.items() if e.get("fault") and "exit" not in e)
    return len(terminal), extra


def main() -> int:
    cat = json.loads(CATALOG.read_text())
    events = cat["events"]

    # --- both directions of coverage -----------------------------------------------------------
    # Entries split by WHO emits them: brain modules (`modules`) or a foreign source (`emitter`).
    brain_entries = {c: e for c, e in events.items() if "emitter" not in e}
    foreign_entries = {c: e for c, e in events.items() if "emitter" in e}

    real = emitted_codes()
    for code in sorted(set(real) - set(brain_entries)):
        fail(f"UNCATALOGUED: brain/{'/'.join(sorted(real[code]))}.py emits {code!r}, which is not in "
             f"the catalogue — it would render as a bare code in the UI")
    for code in sorted(set(brain_entries) - set(real)):
        fail(f"PHANTOM: catalogue entry {code!r} is emitted by nothing — dead entry, or the code was "
             f"renamed in the source only")
    for code in sorted(set(brain_entries) & set(real)):
        declared, actual = set(events[code]["modules"]), real[code]
        if declared != actual:
            fail(f"{code}: `modules` disagrees with the source — declared {sorted(declared)}, "
                 f"actually emitted from {sorted(actual)}")

    # The same two-way rule for a foreign emitter: the code must be present in the source it names.
    foreign_real = emitter_codes()
    for code, entry in sorted(foreign_entries.items()):
        emitter = entry["emitter"]
        if emitter not in EMITTERS:
            fail(f"{code}: unknown emitter {emitter!r} (known: {', '.join(sorted(EMITTERS))})")
        elif emitter not in foreign_real.get(code, set()):
            fail(f"PHANTOM: {code!r} claims emitter {emitter!r}, but {EMITTERS[emitter]} never "
                 f"mentions that code")
        if "modules" in entry:
            fail(f"{code}: an entry with an `emitter` must not also declare brain `modules`")
    for code in sorted(set(foreign_real) - set(events)):
        fail(f"UNCATALOGUED: {'/'.join(sorted(foreign_real[code]))} emits {code!r}, which is not in "
             f"the catalogue")

    # --- bilingual, on every entry and every label table ----------------------------------------
    for code, entry in events.items():
        for lang in ("ru", "en"):
            if not entry.get(lang):
                fail(f"{code}: missing `{lang}` text (RU/EN parity is mandatory)")
    for table in ("category_labels", "level_labels", "phases", "modes", "exit_codes",
                  "narrative", "heal_strategies", "heal_outcomes", "sources", "audiences", "faults"):
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

    # ADR-087: the vocabulary check above proves the code EXISTS; this proves the process
    # really returns it. Four entries promised 3 while returning 2 until it was written.
    check_exit_promises_match_the_code(events)

    # HEALTH-004: the FAULT axis — whose problem the outcome is. An exit code alone cannot answer it
    # (exit 3 is a refusal to start, a corrupt plan AND a malformed request), so the code that ENDED
    # the run carries the answer and the exit_codes entry is only the fallback. Both must declare it,
    # or a run whose outcome nobody can attribute reaches the dashboard as the coarse word `problem`.
    terminal_codes, extra_faults = check_fault_axis(cat, events)
    overrides = check_source_overrides(cat, events)

    # A foreign emitter renders the ENGLISH text itself, and the UI recovers the placeholder values by
    # matching that template against the rendered string. If the two drift, the UI silently falls back
    # to English — a degradation with no error, which is the failure mode this milestone is about.
    # APP_MESSAGES lives in ONE file, so this check names it rather than reusing the emitter entry —
    # which is now a directory, because journal emissions are spread across the package (cdp-service.ts,
    # svcjournal.ts). The two are different questions: "which code emits" scans the package, "where is
    # the template table" is a single declaration.
    exec_src = (REPO / "pw-executor/src/server.ts").read_text()
    block = re.search(r"const APP_MESSAGES: Record<string, string> = \{(.*?)\n\};", exec_src, re.S)
    if not block:
        fail("pw-executor no longer declares APP_MESSAGES — the template equality check went vacuous")
    else:
        declared = dict(re.findall(r"'([\w.]+)':\s*'((?:[^'\\]|\\.)*)'", block.group(1)))
        for code, entry in foreign_entries.items():
            if entry["emitter"] != "pw-executor":
                continue
            if code not in declared:
                fail(f"{code}: APP_MESSAGES has no template, so the emitter cannot render it")
            elif declared[code] != entry["en"]:
                fail(f"{code}: the emitter's English text differs from the catalogue —\n"
                     f"       emitter:   {declared[code]!r}\n"
                     f"       catalogue: {entry['en']!r}")
        for code in declared:
            if code not in events:
                fail(f"APP_MESSAGES declares {code!r}, which is not in the catalogue")

    # The source axis must partition the categories exactly: a category in two sources would make the
    # dropdown ambiguous, and one in none would be invisible under every source filter.
    seen_cats: dict[str, str] = {}
    for src, meta in cat["sources"].items():
        for c in meta["cats"]:
            if c not in cats:
                fail(f"sources.{src} lists category {c!r}, which is not in `categories`")
            if c in seen_cats:
                fail(f"category {c!r} belongs to two sources: {seen_cats[c]} and {src}")
            seen_cats[c] = src
    for c in sorted(cats - set(seen_cats)):
        fail(f"category {c!r} belongs to no source — it would vanish under any source filter")

    # And the audience axis must partition the SOURCES exactly, for the same reason one level up: the
    # coarse choice a tester makes first ("my application, or the tool?") has to cover every record
    # exactly once, or the top-level filter quietly hides a whole source.
    seen_srcs: dict[str, str] = {}
    for aud, meta in cat["audiences"].items():
        for s in meta["sources"]:
            if s not in cat["sources"]:
                fail(f"audiences.{aud} lists source {s!r}, which is not in `sources`")
            if s in seen_srcs:
                fail(f"source {s!r} belongs to two audiences: {seen_srcs[s]} and {aud}")
            seen_srcs[s] = aud
    for s in sorted(set(cat["sources"]) - set(seen_srcs)):
        fail(f"source {s!r} belongs to no audience — it would vanish under any audience filter")
    # An audience name that collides with a category or level name would make `src == <name>` in the
    # filter language ambiguous to a reader, since all three share the value space of one field.
    for aud in cat["audiences"]:
        if aud in cats and aud not in cat["sources"]:
            fail(f"audience {aud!r} collides with a category name — `src == {aud}` would read two ways")
        if aud in levels:
            fail(f"audience {aud!r} collides with a level name")

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

    print(f"event catalogue OK: {len(events)} codes — "
          f"{len(brain_entries)} from {len({m for ms in real.values() for m in ms})} brain modules, "
          f"{len(foreign_entries)} from {len(EMITTERS)} foreign emitter(s); "
          f"{len(degrading)} silent degradations, {len(cat['sources'])} sources in "
          f"{len(cat['audiences'])} audiences, "
          f"{len(cat['phases'])} phases, {len(cat['exit_codes'])} exit codes, "
          f"{terminal_codes} terminal + {extra_faults} decisive codes attributed across "
          f"{len(cat['faults'])} faults, {overrides} source override(s), "
          f"{len(patterns)} foreign patterns; RU/EN complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
