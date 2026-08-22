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

W6 (ADR-129) ADDED TWO THINGS THIS GATE NOW GUARDS, and both are shapes in which the feature could
have defeated the check:

  1. The exemptions are PUBLISHED — handleReadyz serves componentsWithoutProbe under a top-level
     `unprobed` key, so the Health view can say "nobody asks about this component" instead of leaving
     it out entirely. The tempting alternative was `checks["webui"] = {status: "unprobed"}`, which
     would have made every unprobed service read as PROBED to the regex below — the one check whose
     entire job is to notice that it is not. So the disjointness is asserted, and the wiring is
     refused the ability to disappear silently.
  2. Each exemption carries BOTH language halves in one literal (componentNote), because a reason and
     its translation living in two maps drift the first time somebody edits one. That changed the
     declaration's TYPE, and this parser was rewritten to match it rather than granted an exception:
     a gate that goes red on a deliberate change is a gate doing its job, and the answer is to re-aim
     it at the new state with the reason written down.

The seventh probe (`agentctl`, [READYZ-BLIND-TO-AGENTCTL]) does not change the service arithmetic
here: agentctl is a binary this service SPAWNS, not a compose service, so it appears in the probe set
and in no compose file. That asymmetry is why the coverage floor moved and the cap did not.
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
# 6, just under the 7 measured after W6 added `agentctl`. 3 was the number when there were three
# probes; leaving it there meant a parser that lost half its matches would still pass, which is the
# vacuous shape the floor exists to prevent. A floor only ever goes UP.
MIN_PROBES = 6

# A CAP, and it may only ever go DOWN. Exemptions are meant to disappear; a number that can grow is a
# list of excuses with a gate attached.
MAX_UNPROBED = 2   # HEALTH-006 removed `browser`: the exemption that called itself a GAP is now a probe

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


def exempted() -> "dict[str, dict[str, str]]":
    """The recorded exemptions, read from the declaration beside the probes.

    ⚠ REWRITTEN FOR W6, not exempted from it. The declaration used to be map[string]string; it is now
    map[string]componentNote, because [HEALTH-REASON-EN] required the Russian half of each reason to
    live in the SAME literal as the English one — two parallel maps are two statements of one fact
    with nothing comparing them, and they drift the first time somebody edits one. This parser went
    red on that change, which is a parser doing its job; the answer is to re-aim it, and the reason
    is this paragraph.
    """
    src = read(READYZ)
    m = re.search(r"var componentsWithoutProbe = map\[string\]componentNote\{(.*?)\n\}", src, re.S)
    if not m:
        fail("cmd/control-api/readyz.go has no `componentsWithoutProbe = map[string]componentNote` "
             "declaration — without it an unprobed service has nowhere to record WHY, and this gate "
             "would have to accept silence")
        return {}
    # Parsed line by line rather than with one regex over the whole block. The first version matched
    # the concatenated Go string with a nested quantifier — `((?:"…"\s*\+?\s*)+)` — and CodeQL was
    # RIGHT about it: that shape backtracks exponentially on a crafted input. The input here is our own
    # source, so it was not exploitable; it was also unnecessary, which is the better reason to remove
    # it. A key is a quoted word at the start of a line, a field is `EN:`/`RU:`, and everything until
    # the next of either belongs to the one before it — no backtracking, and easier to read than the
    # regex it replaces.
    out: "dict[str, dict[str, str]]" = {}
    key, field = None, None
    for line in m.group(1).split("\n"):
        head = re.match(r'\s*"([a-z][\w-]*)":\s*\{?\s*$', line)
        if head:
            key, field = head.group(1), None
            out[key] = {"EN": "", "RU": ""}
            continue
        fld = re.match(r"\s*(EN|RU):\s*(.*)$", line)
        if fld and key is not None:
            field = fld.group(1)
            out[key][field] = fld.group(2)
            continue
        if key is not None and field is not None:
            out[key][field] += " " + line.strip()
    # A reason is the Go expression as written; strip the quoting and concatenation so an "empty
    # reason" is recognisable as empty rather than as the two characters `""`.
    return {k: {f: re.sub(r'["+,]', "", v).strip() for f, v in halves.items()}
            for k, halves in out.items()}


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
        else:
            # BOTH halves, because the Russian one is what a Russian reader is actually shown in the
            # Health view (W6), and an empty translation there is not a blank cell — it silently falls
            # back to English, which reads as "we translated this" and is not.
            for half in ("EN", "RU"):
                if not exempt[svc].get(half, "").strip():
                    fail(f"service {svc!r} is exempted with an empty {half} reason, which is an "
                         f"omission that learned to pass a gate")

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

    # A component is PROBED or DECLARED UNPROBED, never both — and this is the exact shape in which W6
    # could have defeated this gate. Publishing the exemptions in the /readyz body was the goal; doing
    # it by writing them into `checks["<name>"]` would have made every unprobed service read as probed
    # to the regex in probed(), and the check whose whole job is to notice an unprobed service would
    # report that there are none. The feature went into a top-level `unprobed` key instead, and this
    # line is what keeps it there.
    both = sorted(set(exempt) & checks)
    if both:
        fail(f"{both} are both probed and recorded in componentsWithoutProbe — one of the two is a "
             f"lie, and the declaration is the half that makes the other invisible to this gate")

    # And that the publishing itself cannot be deleted in silence. The BEHAVIOUR — everything declared
    # unprobed really is in the /readyz body, with its reason, and withheld from an anonymous caller —
    # belongs to cmd/control-api/readyz_test.go, which can call the handler; this gate cannot run a
    # server. What it can do is refuse the wiring being removed without anything going red, so the
    # deletion is loud in TWO languages and the second one names the first.
    if not re.search(r'body\["unprobed"\]\s*=', read(READYZ)):
        fail('handleReadyz no longer publishes componentsWithoutProbe under the top-level "unprobed" '
             'key — the declaration is back to being read by gates and by nobody else (ADR-129); the '
             'behaviour is owned by readyz_test.go::TestEveryComponentDeclaredWithoutAProbeIsNamedIn'
             'TheReadyzBody')

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
