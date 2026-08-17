"""ADR-110 — the built stack and the pulled stack must offer the SAME things.

`docker-compose.yml` builds from a checkout; `docker-compose.ghcr.yml` pulls a released image. They
are two files because their image resolution genuinely differs — but everything a person LEARNS from
one has to hold in the other. Someone told `--profile control-api` by the README must not discover
that the profile exists only in the file they are not using.

Two files that must agree and are maintained by hand always drift; the question is only whether the
drift is noticed. So this compares SETS derived from the files rather than checking for known names:
a service added to one file fails here without anyone remembering to extend a list. That is the same
shape as the wizard schema-drift gate, and for the same reason — its predecessor compared 2 blocks
out of 8 and stayed green through a real divergence.

Deliberately NOT asserted: that the service bodies match. They must not — one carries `build:`, the
other `image:` from GHCR, and pinning the bodies would forbid the very difference the files exist for.

The offline suite has no docker, so this reads the YAML rather than running `docker compose config`;
that also means it gates on a PR, where the drift is introduced.

2026-08-03 — the second half. Profile parity said the two files AGREED; it could not say the agreed
answer was any good. Every long-running service sat behind its own profile, so `docker compose up`
started nothing a person could open, and the four profiles had to be typed in the right combination
before the product existed. Alex's decision: the default IS the product. That is a property of the
files, so it is asserted here rather than left to a README that nobody diffs.

The wiring is asserted for the same reason and with better evidence: the two files had ALREADY
diverged on it silently. `control-api` in the pulled stack named PW_CDP_ENDPOINT; in the built stack
it did not — and a YAML merge key replaces the `environment:` mapping wholesale rather than
deepening it, so the anchor's value never reached that service at all. Runs started through the API
launched their own browser, and the live view could only ever show the browser service's idle page.
Nothing was red. Set parity does not see inside a service body, which is exactly where this lived.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILT = REPO / "docker-compose.yml"
PULLED = REPO / "docker-compose.ghcr.yml"

# The services `docker compose up` must start with no flags — the product, not a menu of parts.
DEFAULT_STACK = {"control-api", "store-gateway", "browser", "webui", "orchestrator"}
# ⚠ `orchestrator` joined at ADR-126, and the gate made it a deliberate edit — which is what it is
# for. It belongs in the DEFAULT set rather than behind a profile because it is not "heavier than
# the product": it is the same image, one Go process and one unix socket, and without it three
# things the product ADVERTISES are silently absent — the budget ceiling (ADR-021), operator
# takeover (ADR-054) and the map gate (ADR-108c). A profile would have kept the honest default
# smaller and the advertised product a fiction.


def _services(path: pathlib.Path) -> "dict[str, set[str]]":
    """service name -> its profiles, parsed from the `services:` block.

    A hand-rolled parser rather than PyYAML: the offline suite installs no third-party deps, and the
    shape needed here (top-level keys under `services:`, plus one flow-sequence line each) is
    unambiguous at two-space indentation.
    """
    text = path.read_text()
    m = re.search(r"(?m)^services:\s*$", text)
    assert m, f"{path.name} has no top-level `services:` block"
    body = text[m.end():]
    # Stop at the next top-level key (e.g. `volumes:`) — otherwise its children parse as services.
    end = re.search(r"(?m)^[a-zA-Z_][\w-]*:\s*$", body)
    if end:
        body = body[: end.start()]

    starts = [(mm.group(1), mm.end()) for mm in re.finditer(r"(?m)^  ([a-z0-9][\w-]*):\s*$", body)]
    out: "dict[str, set[str]]" = {}
    for i, (name, pos) in enumerate(starts):
        seg = body[pos: starts[i + 1][1] if i + 1 < len(starts) else len(body)]
        # Only the service's own `profiles:` line, not one from a later service.
        pm = re.search(r"(?m)^    profiles:\s*\[([^\]]*)\]", seg)
        profiles = set()
        if pm:
            profiles = {p.strip().strip("\"'") for p in pm.group(1).split(",") if p.strip()}
        out[name] = profiles
    assert out, f"{path.name}: parsed zero services — the parser, not the file, is what broke"
    return out


def _segments(path: pathlib.Path) -> "dict[str, str]":
    """service name -> the text of its own block, ending where the next service begins.

    Separate from _services() because the questions below are about what is INSIDE a service body,
    and the boundary has to be exact for that: a segment that ran one line into its neighbour would
    let an assertion pass on the neighbour's key.
    """
    text = path.read_text()
    m = re.search(r"(?m)^services:\s*$", text)
    assert m, f"{path.name} has no top-level `services:` block"
    body = text[m.end():]
    end = re.search(r"(?m)^[a-zA-Z_][\w-]*:\s*$", body)
    if end:
        body = body[: end.start()]
    starts = [(mm.group(1), mm.start(), mm.end()) for mm in re.finditer(r"(?m)^  ([a-z0-9][\w-]*):\s*$", body)]
    out: "dict[str, str]" = {}
    for i, (name, _s, e) in enumerate(starts):
        stop = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        out[name] = body[e:stop]
    return out


def test_both_stacks_offer_the_same_services():
    built, pulled = _services(BUILT), _services(PULLED)
    # A floor: if the parser silently degrades to finding almost nothing, equal-but-empty sets would
    # agree perfectly and this gate would pass while checking nothing.
    assert len(built) >= 6, f"only {len(built)} services parsed from {BUILT.name} — parser drift"

    missing = sorted(set(built) - set(pulled))
    extra = sorted(set(pulled) - set(built))
    assert not missing, (
        f"{PULLED.name} is missing services that {BUILT.name} offers: {missing}. Someone who "
        f"cannot build from source would silently not have them.")
    assert not extra, (
        f"{PULLED.name} offers services {BUILT.name} does not: {extra}. The pulled stack must not "
        f"be a superset either — then the documented one is the incomplete one.")


def test_a_service_carries_the_same_profile_in_both_stacks():
    """A profile name IS the user interface. `--profile store` has to mean the same thing in both."""
    built, pulled = _services(BUILT), _services(PULLED)
    for name in sorted(set(built) & set(pulled)):
        assert built[name] == pulled[name], (
            f"service '{name}' is behind profiles {sorted(built[name]) or ['(default)']} in "
            f"{BUILT.name} but {sorted(pulled[name]) or ['(default)']} in {PULLED.name} — the same "
            f"command would start different stacks.")


def _cdp_services(path: pathlib.Path) -> "dict[str, str]":
    """service name -> its block, for every service that RUNS cdp-service.js — DERIVED, never listed.

    The rule this feeds is about a PROPERTY (an unauthenticated CDP relay), not about a name. Its
    predecessor asked for the literal service `browser`, which was true and sufficient for exactly as
    long as there was one such service — and `[LIVE-VNC]` adds a second one running the same relay.

    Read from the `entrypoint:`/`command:` FLOW SEQUENCES specifically, not from the segment text: a
    comment written ABOVE a service belongs, to a splitter that cuts at the service key, to the
    PREVIOUS service's block — and docker-compose.yml really does explain cdp-service.js in the
    comment above `browser`, i.e. inside `store-gateway`'s segment. A substring search over the
    segment would classify store-gateway as a CDP service and quietly inflate the floor below.

    BOTH keys are read because compose CLEARS the image's CMD when `entrypoint:` is overridden, so a
    service may legitimately split "what runs" across the two.
    """
    out: "dict[str, str]" = {}
    for name, seg in _segments(path).items():
        argv = " ".join(m.group(1) for m in
                        re.finditer(r"(?m)^    (?:entrypoint|command):\s*\[(.+)\]\s*$", seg))
        if "cdp-service.js" in argv:
            out[name] = seg
    return out


# A FLOOR on that derivation, in the shape DEFAULT_STACK uses: a number somebody edits on purpose,
# not a count that follows whatever the file says today. TWO services relay CDP: `browser` (headless,
# the default stack) and `browser-vnc` (headed, behind the `vnc` profile, LIVE-VNC). A floor only ever
# goes UP, and it moving is the point — it made "a second browser arrived" a thing somebody stated
# rather than a thing that happened.
CDP_SERVICES = 2


def test_the_pulled_stack_never_builds_and_no_cdp_service_publishes_a_port():
    """Two properties that make the file what it claims to be.

    A stray `build:` would send someone with no checkout into a build failure, which is the exact
    situation this file exists to remove.

    And the CDP port has no authentication and cannot be given any (see pw-executor/src/
    cdp-service.ts): whoever reaches it drives the browser and reads its cookies. Reachability is the
    ONLY control there is, so a `ports:` key on a service that relays CDP is a security regression,
    not a convenience.

    ⚠ THE RULE IS DERIVED FROM WHAT A SERVICE RUNS, NOT FROM ITS NAME, and that is the whole edit
    (2026-08-17). The previous version searched for the literal service `browser` and checked that
    one block. It was correct and sufficient for exactly as long as there was one CDP service;
    `[LIVE-VNC]` adds `browser-vnc`, which relays the same unauthenticated protocol and would have
    been covered by nothing at all — this gate would have gone on passing, about a service it could
    not see. Note what is NOT forbidden: a service that merely sits near one, or a future bridge that
    carries its own credential, may publish a port. Only a derived rule can tell those apart.
    """
    pulled_text = PULLED.read_text()
    assert not re.search(r"(?m)^\s+build:\s*$", pulled_text), (
        f"{PULLED.name} contains a `build:` key — the pulled stack must resolve every image from "
        f"the registry, or it is just the built stack with extra steps.")

    sets = {}
    for path in (BUILT, PULLED):
        cdp = _cdp_services(path)
        # Every assertion under this loop is vacuously true over an empty dict, and a regex that
        # stops matching yields exactly that — the failure the derivation itself cannot see.
        assert len(cdp) >= CDP_SERVICES, (
            f"{path.name}: derived only {len(cdp)} CDP service(s) {sorted(cdp)}, expected at least "
            f"{CDP_SERVICES}. Either a service that relays CDP stopped being visible to this parser "
            f"— in which case the rule below silently stopped applying to it — or one was removed "
            f"and this number must come down with it, as an edit somebody makes on purpose.")
        for name, seg in sorted(cdp.items()):
            # ⚠ THE KEY AT ITS OWN INDENT, not the substring anywhere in the block. A substring search
            # is what the previous version did, and it was fine only while no service body EXPLAINED
            # the rule: the moment `browser-vnc` carried the comment "NO `ports:` KEY, for the same
            # reason as browser", the gate failed on the very sentence promising it would not publish
            # a port. A check that fires on legitimate content gets an exception carved into it, and
            # an exception is how a gate stops applying to the thing it was written for. Narrowed to
            # the shape compose actually emits — four spaces, the key, end of line.
            assert not re.search(r"(?m)^    ports:\s*$", seg), (
                f"{path.name}: the `{name}` service relays CDP and publishes ports. CDP is "
                f"unauthenticated BY CONSTRUCTION — anything that reaches that port drives the "
                f"browser and reads its cookies, and there is no token to add because the protocol "
                f"has none. Reachability is the only control there is.")
        sets[path.name] = set(cdp)

    built_set, pulled_set = sets[BUILT.name], sets[PULLED.name]
    assert built_set == pulled_set, (
        f"the two stacks relay CDP from different services: {sorted(built_set)} in {BUILT.name} vs "
        f"{sorted(pulled_set)} in {PULLED.name}. One file has a browser the other does not, and the "
        f"port rule above was applied to a different set in each.")


def test_up_with_no_flags_starts_the_product():
    """The default set is a product decision, so it is pinned rather than merely compared.

    Asserted as an EQUALITY, not a subset. A subset check would stay green if someone dropped the
    last profile off `ollama` and made `docker compose up` pull a multi-gigabyte model image — the
    failure mode on the other side of the same decision.
    """
    for path in (BUILT, PULLED):
        svc = _services(path)
        assert len(svc) >= 6, f"only {len(svc)} services parsed from {path.name} — parser drift"
        default = {name for name, profiles in svc.items() if not profiles}
        assert default == DEFAULT_STACK, (
            f"{path.name}: `docker compose up` would start {sorted(default) or ['nothing']}, "
            f"not {sorted(DEFAULT_STACK)}. The default has to BE the product: a person who types "
            f"the command from the README must end up with something they can open, and anything "
            f"heavier than the product (a local model, a fixture run) has to stay behind a profile.")


def test_the_default_stack_is_wired_to_itself():
    """Four services on one network are not a stack until each knows the others' addresses.

    Every value is checked in the `${VAR-fallback}` form (ONE dash): with `:-`, an operator who
    exports an empty value to opt out of the store or the browser gets the fallback substituted
    anyway and has no way to say "no store" from the environment at all.
    """
    wiring = {
        "CONTROL_API_STORE_ADDR": "unix:/app/state/store.sock",
        "CONTROL_API_CDP_LIVE": "http://browser:9224",
        # The one that was missing from the built stack entirely, which is why runs started through
        # the API never attached to the browser service they were meant to share.
        "PW_CDP_ENDPOINT": "http://browser:9223",
        # ADR-126. Absent from every compose file until then, which is the whole reason the budget
        # ceiling, operator takeover and the map gate were dead in each shipped deployment.
        "CONTROL_API_ORCH_ADDR": "unix:/app/state/orch.sock",
    }
    for path in (BUILT, PULLED):
        seg = _segments(path)["control-api"]
        for var, target in wiring.items():
            m = re.search(r"(?m)^      " + var + r":\s*(\S.*)$", seg)
            assert m, (
                f"{path.name}: control-api does not name {var} in its OWN environment block. A YAML "
                f"merge key replaces that mapping rather than deepening it, so inheriting it from "
                f"the anchor does not work — this exact omission shipped once and cost the live view.")
            value = m.group(1).strip()
            assert value == "${" + var + "-" + target + "}", (
                f"{path.name}: control-api sets {var} to {value!r}, expected "
                f"'${{{var}-{target}}}' — the sibling service as the default, in the single-dash "
                f"form so an explicit empty value still opts out.")


def test_the_orchestrator_listens_where_control_api_dials(path=None):
    """One socket path, written in TWO places, with nothing joining them — until this.

    ⚠ THIS TEST EXISTS BECAUSE A MUTATION SURVIVED. Dropping `--addr /app/state/orch.sock` from the
    orchestrator's entrypoint broke nothing: every gate stayed green, `docker compose ps` reported the
    service HEALTHY, and control-api silently never reached it. The healthcheck is the reason it looks
    so convincing — it tests the path the ENTRYPOINT chose, so the two wrong halves agree with each
    other and disagree only with the third.

    That failure is the exact shape of the defect ADR-126 was written to remove: two ends naming a
    thing in two files, each correct on its own. So the check is a COMPARISON of what is written,
    never a repetition of the expected value — a hand-written path here would be a fourth place to
    get wrong.
    """
    for p in (BUILT, PULLED):
        seg = _segments(p)
        assert "orchestrator" in seg, f"{p.name}: no orchestrator service to check"

        m = re.search(r"(?m)^      CONTROL_API_ORCH_ADDR:\s*\$\{CONTROL_API_ORCH_ADDR-unix:(\S+?)\}\s*$",
                      seg["control-api"])
        assert m, f"{p.name}: control-api does not name CONTROL_API_ORCH_ADDR in its own environment block"
        dialled = m.group(1)

        e = re.search(r'(?m)^    entrypoint:\s*\[(.+)\]\s*$', seg["orchestrator"])
        assert e, f"{p.name}: the orchestrator service has no entrypoint to read a socket path from"
        argv = [a.strip().strip('"') for a in e.group(1).split(",")]
        assert "--serve" in argv, (
            f"{p.name}: the orchestrator entrypoint is {argv} — without --serve the binary refuses to "
            f"start at all (ADR-126), so the service would restart-loop while looking configured")
        assert "--addr" in argv, (
            f"{p.name}: the orchestrator entrypoint names no --addr, so it listens on its built-in "
            f"default while control-api dials {dialled!r}. The healthcheck would still pass — it "
            f"tests the path the entrypoint chose — and the two would never meet")
        listens = argv[argv.index("--addr") + 1]
        assert listens == dialled, (
            f"{p.name}: the orchestrator listens on {listens!r} and control-api dials {dialled!r}. "
            f"The stack comes up healthy and the supervision is silently absent — no ceiling, no "
            f"takeover, no map gate, and nothing in any log to say so")

        # The healthcheck is the third writer of the same path, and the same trap: green over a
        # socket nobody dials proves only that the service is talking to itself.
        assert listens in seg["orchestrator"].split("healthcheck:", 1)[-1], (
            f"{p.name}: the healthcheck does not test {listens!r} — the path the service actually "
            f"listens on. A probe of some other path is a green tick with no subject")


def test_control_api_waits_for_what_it_only_probes_once():
    """`depends_on` here is correctness, not tidiness.

    control-api probes the store-gateway ONCE at startup and remembers a miss for the lifetime of
    the process (cmd/control-api/store.go::newStoreClient), and an unreachable CDP endpoint fails a
    run hard rather than falling back to a browser nobody asked for. Starting all four at once
    without a condition would therefore produce a stack that is up and quietly half-wired — the
    exact failure this whole wave exists to remove.
    """
    for path in (BUILT, PULLED):
        seg = _segments(path)
        dep = seg["control-api"]
        assert re.search(r"(?m)^    depends_on:\s*$", dep), (
            f"{path.name}: control-api declares no depends_on, so it races the two services it "
            f"cannot re-probe later.")
        for name in ("store-gateway", "browser"):
            assert re.search(r"(?m)^      " + re.escape(name) + r":\s*\n\s+condition: service_healthy\s*$", dep), (
                f"{path.name}: control-api does not wait for {name} to be HEALTHY. "
                f"`service_started` is not enough — the container exists long before the socket or "
                f"the CDP relay does.")
            assert re.search(r"(?m)^    healthcheck:\s*$", seg[name]), (
                f"{path.name}: {name} has no healthcheck, so `condition: service_healthy` on it "
                f"can never be satisfied and the stack would hang instead of starting.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} compose-parity tests passed")
