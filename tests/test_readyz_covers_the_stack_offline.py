#!/usr/bin/env python3
"""HEALTH-006 precondition — every service of the deployment is probed, or its absence is recorded.

Run:  .venv/bin/python tests/test_readyz_covers_the_stack_offline.py

WHY THIS EXISTS BEFORE HEALTH-006 AND NOT INSIDE IT. The probes were written one at a time, and a
hand-written set of probes cannot show the service nobody thought about. Measured before this gate:

    default stack (docker-compose.yml, .ghcr.yml):  browser · control-api · store-gateway · webui
    /readyz checks:                                 store · config · llm

Exactly ONE check (`store`) corresponds to a service at all. `browser` and `webui` were probed by
nothing — while control-api declares `depends_on: browser: service_healthy` and therefore refuses to
START without a healthy browser. A deployment that demands a component at boot and never asks about it
again reports itself ready in precisely the case where it is not.

The same failure had already happened twice elsewhere in one week: the UI smoke kept its own list of
views and screenshotted seven of nine, and the capability registry kept one access path per entry and
so could not say whether anything was reachable three ways. The mechanism is identical — a hand-kept
list shows what is WRONG in it and never what is ABSENT from it — which is why the rule is now
principle 5 in docs/DEVELOPMENT.md and why this gate derives both sides instead of listing either.

WHAT IT DOES NOT DO. It does not add a probe; HEALTH-006 does. It makes the omission blocking and
named, so the probe cannot be forgotten and cannot be quietly dropped later.
"""
import os
import re
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.offline.yml"]
READYZ = os.path.join("cmd", "control-api", "readyz.go")

# Floors, because deriving a list removes "somebody forgot" and introduces a quieter failure: a parser
# that stops matching yields an EMPTY set, and every assertion over it passes perfectly.
MIN_SERVICES = 4
MIN_PROBES = 3

# A CAP, and it may only ever go DOWN. Exemptions are meant to disappear; a number that can grow is a
# list of excuses with a gate attached.
MAX_UNPROBED = 3

# The name a service carries in compose is not always the name a probe carries in the code — the
# gateway is the `store-gateway` service and the `store` check. Mapped explicitly, because guessing by
# prefix would silently pair the wrong things the first time two services shared one.
PROBE_NAME_OF_SERVICE = {
    "store-gateway": "store",
}

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(rel: str) -> str:
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def default_stack() -> "set[str]":
    """Services that `docker compose up` starts with no flags, across every shipped compose file.

    Derived, never listed: a service added to any of the three arrives here by existing, which is the
    whole point. Profiled services are excluded — they are what a deployment may legitimately not want,
    and demanding a readiness probe for a component nobody started would make /readyz permanently 503.
    """
    out: "set[str]" = set()
    for name in COMPOSE:
        doc = yaml.safe_load(read(name))
        for svc, body in (doc.get("services") or {}).items():
            if not (body.get("profiles") or []):
                out.add(svc)
    return out


def probed() -> "set[str]":
    """Component names /readyz actually reports on, read from the source that reports them."""
    src = read(READYZ)
    return set(re.findall(r'checks\["([a-z][\w-]*)"\]', src))


def exempted() -> "dict[str, str]":
    """The recorded exemptions, read from the declaration beside the probes."""
    src = read(READYZ)
    m = re.search(r"var componentsWithoutProbe = map\[string\]string\{(.*?)\n\}", src, re.S)
    if not m:
        fail("cmd/control-api/readyz.go has no `componentsWithoutProbe` declaration — without it an "
             "unprobed service has nowhere to record WHY, and this gate would have to accept silence")
        return {}
    out = {}
    for key, reason in re.findall(r'"([a-z][\w-]*)":\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+),', m.group(1)):
        out[key] = reason
    return out


def main() -> int:
    services = default_stack()
    checks = probed()
    exempt = exempted()

    if len(services) < MIN_SERVICES:
        fail(f"derived only {len(services)} default-stack service(s) from {len(COMPOSE)} compose files "
             f"— the parser, not the stack, is what regressed; every check below would be vacuous")
    if len(checks) < MIN_PROBES:
        fail(f"derived only {len(checks)} probe(s) from {READYZ} — the regex stopped matching, and an "
             f"empty probe set makes every service look exempt")

    unprobed = []
    for svc in sorted(services):
        probe = PROBE_NAME_OF_SERVICE.get(svc, svc)
        if probe in checks:
            continue
        unprobed.append(svc)
        if svc not in exempt:
            fail(f"service {svc!r} starts with `docker compose up`, is probed by nothing, and has no "
                 f"entry in componentsWithoutProbe. Add the probe, or record WHY there is none — a "
                 f"component nobody asks about is one the deployment will call healthy while it is not")
        elif not exempt[svc].strip(' "'):
            fail(f"service {svc!r} is exempted with an empty reason, which is an omission that learned "
                 f"to pass a gate")

    # The cap must EQUAL the recorded exemptions. A cap that merely bounds them can be raised on its
    # own and nobody sees it — measured: MAX_UNPROBED 3 -> 9 passed, which turns the ratchet into a
    # place where a regression is quieter to allow than to fix (the same defect the capability gate's
    # floor had, found the same way). Tying the two together makes raising the allowance require
    # writing down WHAT is being allowed, and removing an exemption force the cap down with it.
    if MAX_UNPROBED != len(exempt):
        fail(f"MAX_UNPROBED is {MAX_UNPROBED} but {len(exempt)} exemption(s) are recorded. The cap is "
             f"not a budget to spend — it is the count of decisions already written down, and it moves "
             f"only when one is added or removed")

    if len(unprobed) > MAX_UNPROBED:
        fail(f"{len(unprobed)} services are unprobed, above the recorded cap of {MAX_UNPROBED}: "
             f"{unprobed}. The cap may only go DOWN — a number that grows is a list of excuses")

    # And the reverse direction: an exemption naming a service that no longer exists is a stale excuse
    # that makes the cap look tighter than it is.
    for name in sorted(exempt):
        if name not in services:
            fail(f"componentsWithoutProbe names {name!r}, which is not a default-stack service in any "
                 f"compose file — a stale exemption inflates the allowance without covering anything")

    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"readyz coverage: OK ({len(services)} default-stack services from {len(COMPOSE)} compose "
          f"files, {len(checks)} probes, {len(unprobed)}/{MAX_UNPROBED} unprobed and each with a "
          f"recorded reason)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
