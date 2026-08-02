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
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILT = REPO / "docker-compose.yml"
PULLED = REPO / "docker-compose.ghcr.yml"


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


def test_the_pulled_stack_never_builds_and_the_browser_port_is_never_published():
    """Two properties that make the file what it claims to be.

    A stray `build:` would send someone with no checkout into a build failure, which is the exact
    situation this file exists to remove. And the CDP port has no authentication and cannot be given
    any (see pw-executor/src/cdp-service.ts), so publishing it would hand whoever reaches the host a
    browser to drive — a `ports:` key on that service is a security regression, not a convenience.
    """
    pulled_text = PULLED.read_text()
    assert not re.search(r"(?m)^\s+build:\s*$", pulled_text), (
        f"{PULLED.name} contains a `build:` key — the pulled stack must resolve every image from "
        f"the registry, or it is just the built stack with extra steps.")

    for path in (BUILT, PULLED):
        text = path.read_text()
        m = re.search(r"(?m)^  browser:\s*$", text)
        assert m, f"{path.name} has no `browser` service"
        seg = text[m.end():]
        nxt = re.search(r"(?m)^  [a-z0-9][\w-]*:\s*$|^[a-zA-Z_][\w-]*:\s*$", seg)
        seg = seg[: nxt.start()] if nxt else seg
        assert "ports:" not in seg, (
            f"{path.name}: the `browser` service publishes ports. CDP is unauthenticated by "
            f"construction — anything that reaches that port drives the browser and reads its "
            f"cookies. Reachability is the ONLY control there is.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} compose-parity tests passed")
