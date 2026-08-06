#!/usr/bin/env python3
"""HEALTH-005 PR-B — the service journal must be reachable ALL THREE ways, or it is not reachable.

Run:  .venv/bin/python tests/test_service_log_surface_offline.py

WHY THIS GATE EXISTS AND WHY IT IS NOT THREE GATES. The M16 measurement (docs/M16_MATRIX.md) counted
49 capabilities and found 5 present on all three surfaces: 20 missing from the CLI, 18 from HTTP, 23
from the UI. Every one of those had a per-surface test that passed. What nothing checked was the
CONJUNCTION — and the conjunction is the property Alex stated: "everything three ways". A capability
with two of three surfaces has no failing test anywhere; it simply cannot be found from the third.

PR-A produced the extreme case of that: a journal with THREE zeros. Written by the product, reachable
by nothing.

The refs are resolved against the real sources, never against a copy of them, so renaming the route,
the verb or the view breaks this instead of leaving a page that says all three exist.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: "list[str]" = []


def read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def fail(msg: str) -> None:
    failures.append(msg)


def main() -> int:
    access = read(os.path.join("cmd", "control-api", "access.go"))
    api = read(os.path.join("cmd", "agentctl", "api.go"))
    main_go = read(os.path.join("cmd", "agentctl", "main.go"))
    hub = read(os.path.join("docs", "index.html"))
    catalog = json.loads(read(os.path.join("brain", "events.json")))

    # ---- surface 1: HTTP ---------------------------------------------------------------------
    #
    # Declared in the routes TABLE, which is the only place a route can be registered (ADR-109) — so
    # this also proves the route is actually served, not merely that a handler exists.
    route = re.search(r'\{pattern:\s*"GET /v1/service-log",\s*access:\s*(\w+)', access)
    if not route:
        fail("GET /v1/service-log is not declared in cmd/control-api/access.go routes() — a handler "
             "that is not in the table is not served at all")
    elif route.group(1) != "accessAuthed":
        fail(f"GET /v1/service-log declares {route.group(1)}: an account must be able to read the "
             "record of its OWN sign-ins, which is half of what an audit journal is for; the per-record "
             "scoping is what protects the rest")

    # The scoping must be applied by the handler, since the route declares no `domain` (it names no
    # single row for the guard to resolve). A route that lost its scoping would answer every account
    # the whole deployment's history, and nothing about the response shape would look wrong.
    svcread = read(os.path.join("cmd", "control-api", "svcjournal_read.go"))
    if "journalScope" not in svcread or "s.callerOf" not in svcread:
        fail("svcjournal_read.go no longer scopes by caller — with no `domain` on the route, the "
             "handler is the ONLY thing standing between an account and the deployment's journal")

    # ---- surface 2: CLI ----------------------------------------------------------------------
    if not re.search(r'Verb:\s*"service-log".*?Path:\s*"/v1/service-log"', api, re.S):
        fail('agentctl has no `service-log` verb over /v1/service-log — reading the journal would '
             'need a browser, on a deployment whose whole point is that CI and a remote operator '
             'already hold the machine token')
    if 'case "purge-service"' not in main_go:
        fail('agentctl has no `purge-service` subcommand — the journal could be written and read but '
             'never bounded by anything an operator chooses')

    # ---- surface 3: UI -----------------------------------------------------------------------
    #
    # Three separate things, because two of them can be present while the view is unreachable: the
    # section can exist with no way in, and the router refuses any name not in VIEWS.
    if 'data-view="journal"' not in hub:
        fail("docs/index.html has no data-view=\"journal\" section")
    if not re.search(r"var VIEWS\s*=\s*\[[^\]]*'journal'", hub):
        fail("'journal' is not in VIEWS, so setView() would refuse to open it and the rail link is dead")
    if not re.search(r'href="#v=journal"\s+data-nav="journal"', hub):
        fail("no rail entry for the journal — a view nothing navigates to is a view nobody finds")
    if "/v1/service-log" not in hub:
        fail("the hub never calls /v1/service-log — the view would render an empty list forever")

    # ---- the DOM gate must cover the new view ------------------------------------------------
    #
    # hub-dom-check.mjs keeps its OWN list of views (it is a second, independent statement of the
    # same set), and its neighbour-leak check iterates that list. A view missing from it is a view
    # no browser check ever opens.
    gate = read(os.path.join("scripts", "hub-dom-check.mjs"))
    if not re.search(r"const VIEWS\s*=\s*\[[^\]]*'journal'", gate):
        fail("scripts/hub-dom-check.mjs does not list 'journal' in its VIEWS — the view would be "
             "opened by no browser check, and the leak check would not know it exists")

    # ---- the destruction path names itself ---------------------------------------------------
    purge = read(os.path.join("cmd", "agentctl", "purge_service.go"))
    if '"service.log_purged"' not in purge:
        fail("purge-service does not emit service.log_purged — a journal whose deletion leaves no "
             "trace answers 'was anything removed?' with silence")
    if "service.log_purged" not in catalog["events"]:
        fail("service.log_purged is not in the event catalogue, so it would reach the UI through the "
             "system.unclassified catch-all, at the wrong level and in English only")
    if "--yes" not in purge and "yes" not in purge:
        fail("purge-service does not require a confirmation flag")

    # ---- the catalogue entry and the reader agree on the level vocabulary --------------------
    entry = catalog["events"].get("service.log_purged", {})
    if entry.get("lvl") != "warn":
        fail(f"service.log_purged is catalogued at {entry.get('lvl')!r}: destroying audit records is "
             "exactly the line somebody comes looking for, which is what `warn` means in this stream")

    # ---- and the capability catalogue lists all three, so the reader is told ------------------
    caps = json.loads(read(os.path.join("docs", "capabilities.json")))["capabilities"]
    kinds = {c["access"]["kind"] for c in caps if "journal" in c["id"] or "service-journal" in c["id"]}
    for want in ("http", "cli", "ui"):
        if want not in kinds:
            fail(f"docs/capabilities.json advertises no {want} path to the service journal — the "
                 f"catalogue exists precisely so a working capability is not invisible")

    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print("service journal: OK — HTTP route (scoped), agentctl verb, purge subcommand, hub view "
          "(section + VIEWS + rail + fetch), DOM gate coverage, catalogued self-recording purge, "
          "and all three paths advertised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
