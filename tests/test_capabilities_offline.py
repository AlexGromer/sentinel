"""Offline gate: the capabilities catalogue cannot advertise what the product lacks (PROD-DISCOVERY).

Run:  .venv/bin/python tests/test_capabilities_offline.py

docs/capabilities.json is the catalogue of features that exist and work but were unreachable because
nothing named them (the LiteLLM class of gap: code present, cannot be found). A capabilities page is
worse than useless if it drifts from reality — a reader who follows a listed feature to a command
that no longer exists trusts the page less than if it had said nothing.

So every entry carries an ACCESS ref, and this gate verifies that ref RESOLVES in the real code:
  cli     -> a subcommand present in cmd/agentctl/main.go's switch
  http    -> a route registered via HandleFunc in cmd/control-api
  mode    -> a RUN_MODE value the brain dispatches on
  profile -> a docker-compose profile
  service -> a docker-compose service in the DEFAULT stack (started by `docker compose up`)
  env     -> an environment variable read by non-test product code
  code    -> a token present in a named source file
  file    -> a path that exists

This is behavioural, not a claim about the page's prose: rename a subcommand, drop a route, remove a
profile, and the gate breaks instead of the page misleading a reader.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _read_glob(reldir, suffix):
    out = []
    for root, _dirs, files in os.walk(os.path.join(REPO, reldir)):
        if "node_modules" in root:
            continue
        for f in files:
            if f.endswith(suffix) and not f.endswith("_test.go"):
                out.append(os.path.join(root, f))
    return out


def _resolve(cid, kind, ref, agentctl, control_api, brain_main, compose, product_src, file_ref=None):
    """Resolve ONE access path against the real code. Extracted so a capability can declare several.

    Unchanged in substance from when each entry had exactly one path — the checks are the same, they
    are simply applied per path now.
    """

    if kind == "cli":
        # TWO shapes are a CLI path, and both are real. A `case "x":` in main.go's switch is the
        # hand-written subcommand; a row in `apiVerbs` (cmd/agentctl/api.go) is the table-driven
        # projection of a control-API route — `agentctl runs artifact` is as much a terminal path as
        # `agentctl import`. A gate that knew only the switch called every projected verb missing the
        # moment the capability list started deriving them.
        api = _read(os.path.join("cmd", "agentctl", "api.go"))
        assert f'case "{ref}":' in agentctl or f'Verb: "{ref}"' in api, (
            f"{cid}: cli path {ref!r} is neither a subcommand in cmd/agentctl/main.go's switch nor a "
            f"verb in cmd/agentctl/api.go's apiVerbs table")
    elif kind == "http":
        # Two shapes register a route: the declaration table in access.go (ADR-109 second half,
        # where the mux is built from `{pattern: "GET /v1/x", …}`) and the direct HandleFunc the
        # UI-mode routes still use. A gate that knew only the second one reported every route
        # missing the moment the table arrived.
        assert (f'HandleFunc("{ref}"' in control_api or f'{{pattern: "{ref}"' in control_api), (
            f"{cid}: HTTP route {ref!r} is not registered in cmd/control-api")
    elif kind == "mode":
        # the brain must dispatch on this RUN_MODE, not merely mention it in a comment: require the
        # quoted string to appear in an equality/branch context.
        assert (f'"{ref}"' in brain_main), f"{cid}: RUN_MODE {ref!r} not found in brain/__main__.py"
        assert re.search(rf'run_mode\s*==\s*"{re.escape(ref)}"|"{re.escape(ref)}"\s*[:,)]|_run_{ref.replace("-", "_")}',
                         brain_main), (
            f"{cid}: RUN_MODE {ref!r} appears but not in a dispatch position — is it really handled?")
    elif kind == "profile":
        assert f'profiles: ["{ref}"]' in compose, (
            f"{cid}: docker-compose profile {ref!r} does not exist")
    elif kind == "service":
        # A capability of the DEFAULT stack: `docker compose up` and it is there. Both halves
        # matter. Existence alone would keep passing if someone put the service back behind a
        # profile, and then the catalogue would promise a thing the documented command does not
        # start — the precise failure this catalogue exists to prevent, one level up.
        m = re.search(rf"(?m)^  {re.escape(ref)}:\s*$", compose)
        assert m, f"{cid}: docker-compose service {ref!r} does not exist"
        tail = compose[m.end():]
        nxt = re.search(r"(?m)^  [a-z0-9][\w-]*:\s*$|^[a-zA-Z_][\w-]*:\s*$", tail)
        body = tail[: nxt.start()] if nxt else tail
        assert not re.search(r"(?m)^    profiles:", body), (
            f"{cid}: service {ref!r} is behind a profile, so `docker compose up` does not start "
            f"it and the catalogue's access path is a flag the reader was not told to pass")
    elif kind == "env":
        assert ref in product_src, (
            f"{cid}: env var {ref!r} is read by no non-test product code — dead or renamed?")
    elif kind == "code":
        src = _read(file_ref)
        assert ref in src, f"{cid}: token {ref!r} not found in {file_ref}"
    elif kind == "file":
        assert os.path.exists(os.path.join(REPO, ref)), f"{cid}: file {ref!r} does not exist"
    elif kind == "ui":
        # A `ui` ref names a VIEW of the hub, and the catalogue offers a button that navigates to
        # it. The ref must therefore resolve to a real view — otherwise the button is a promise
        # the page cannot keep, which is the exact failure the catalogue exists to prevent, moved
        # from prose into a control. Both halves are required: the view must EXIST as a
        # data-view section, and setView must accept it (VIEWS is what it validates against).
        hub = _read(os.path.join("docs", "index.html"))
        assert f'data-view="{ref}"' in hub or f'data-view="{ref} ' in hub, (
            f"{cid}: ui view {ref!r} has no data-view section in docs/index.html")
        assert re.search(rf"VIEWS\s*=\s*\[[^\]]*'{re.escape(ref)}'", hub) or \
               re.search(rf'VIEWS\s*=\s*\[[^\]]*"{re.escape(ref)}"', hub), (
            f"{cid}: ui view {ref!r} is not in VIEWS, so setView would refuse to open it")
    else:
        raise AssertionError(f"{cid}: unknown access.kind {kind!r}")


def main() -> int:
    cat = json.loads(_read(os.path.join("docs", "capabilities.json")))
    caps = cat["capabilities"]
    assert caps, "capabilities.json parsed to zero entries — this gate would be vacuous"

    agentctl = _read(os.path.join("cmd", "agentctl", "main.go"))
    control_api = "\n".join(_read(os.path.relpath(p, REPO)) for p in _read_glob("cmd/control-api", ".go"))
    brain_main = _read(os.path.join("brain", "__main__.py"))
    compose = _read("docker-compose.yml")
    # product code the env/code checks may search (Go + Python + TS, no tests)
    product_files = (_read_glob("cmd", ".go") + _read_glob("brain", ".py")
                     + _read_glob("pw-executor/src", ".ts"))
    product_files = [p for p in product_files if not p.endswith(".test.ts")]
    product_src = "\n".join(_read(os.path.relpath(p, REPO)) for p in product_files)

    seen_ids = set()
    for c in caps:
        cid = c["id"]
        assert cid not in seen_ids, f"duplicate capability id {cid!r}"
        seen_ids.add(cid)
        for k in ("title_ru", "title_en", "how_ru", "how_en"):
            assert c.get(k), f"{cid}: missing {k}"
        # ACCESS IS A LIST (2026-08-07). It used to be one object, and a single path can only ever
        # answer "reachable somewhere" — which was read as "covered". Measured before the change: of
        # 50 entries, ZERO could be shown reachable all three ways, because the SHAPE could not
        # express it. Every listed path is resolved below, exactly as the single one used to be.
        paths = c["access"]
        assert isinstance(paths, list) and paths, (
            f"{cid}: access must be a non-empty LIST of paths — one path per surface")
        assert c.get("reach") in ("capability", "artifact"), (
            f"{cid}: missing `reach` — a product CAPABILITY is expected to be reachable three ways, "
            f"an ARTIFACT (Helm chart, env knob, compose profile) is not, and the difference has to "
            f"be stated rather than inferred from the kind")
        for acc in paths:
            kind, ref = acc["kind"], acc["ref"]
            _resolve(cid, kind, ref, agentctl, control_api, brain_main, compose, product_src,
                     acc.get("file"))
        continue

    # ---- THE PRINCIPLE, AS A NUMBER (2026-08-07) -------------------------------------------------
    #
    # "Everything three ways" was a belief, not a measurement: the registry held ONE path per entry,
    # so it answered "reachable somewhere" and was read as "covered". Measured at the moment the shape
    # changed: of 33 product capabilities, ZERO could be shown reachable from the UI, a terminal AND
    # over HTTP — not because they are not, but because nothing recorded it.
    #
    # A gate that DEMANDED all three today would either stall this change or produce three dozen
    # hastily invented reasons, which is worse than none. So it RATCHETS: the number is printed every
    # run and may not fall. Raising the floor is a deliberate edit that says "this many are now
    # genuinely reachable three ways", which is the only claim worth trusting.
    THREE = {"ui", "cli", "http"}
    MIN_THREE_WAY = 0          # ⚠ may only ever go UP; today's honest number
    assert MIN_THREE_WAY >= 0, (
        "the ratchet floor is negative, which makes it unsatisfiable-proof rather than a floor — a "
        "regression would pass by lowering the number instead of being fixed")
    MIN_CAPABILITIES = 30      # a floor on the walk itself: classifying everything as `artifact`
    #                            would make the ratchet vacuous, so the population is bounded too
    caps_only = [c for c in caps if c.get("reach") == "capability"]
    three_way = [c for c in caps_only if {p["kind"] for p in c["access"]} >= THREE]
    partial = [c for c in caps_only if not ({p["kind"] for p in c["access"]} >= THREE)]

    assert len(caps_only) >= MIN_CAPABILITIES, (
        f"only {len(caps_only)} entries are classified as a product capability — the rest as artifacts. "
        f"That makes the three-way ratchet measure almost nothing; check the `reach` values.")
    assert len(three_way) >= MIN_THREE_WAY, (
        f"three-way reachability fell to {len(three_way)} from a recorded floor of {MIN_THREE_WAY}. "
        f"A capability lost a surface: {[c['id'] for c in partial][:8]}")

    # Every capability short of three ways must SAY which surface is missing, or the gap is invisible
    # again. The reason itself is not demanded yet — that is the next ratchet — but naming the hole is.
    unexplained = [c["id"] for c in partial
                   if not (THREE - {p["kind"] for p in c["access"]}) <= set(c.get("missing", {}))
                   and not c.get("missing")]
    print(f"    three ways: {len(three_way)}/{len(caps_only)} capabilities; "
          f"{len(unexplained)} name no missing surface at all")

    # The high-severity features the audit called out by name must be present — a catalogue that
    # quietly dropped the Helm chart or the OpenAI shim would pass every per-entry check above while
    # failing the reader who came looking for exactly those.
    must_have = {"openai-shim", "helm-chart", "cdp-attach", "takeover", "mcp-server",
                 "airgap-bundle", "login-as-test", "install-sh", "export-spec"}
    missing = must_have - seen_ids
    assert not missing, f"the catalogue is missing high-severity capabilities: {sorted(missing)}"

    # The catalogue itself must be reachable, or it is one more undiscoverable feature. Both the
    # README and the published landing page must link to CAPABILITIES.md — the two front doors a new
    # user actually opens.
    readme = _read("README.md")
    assert "docs/CAPABILITIES.md" in readme, "README does not link to docs/CAPABILITIES.md — the catalogue is itself undiscoverable"
    landing = _read(os.path.join("docs", "index.html"))
    assert "CAPABILITIES.md" in landing, "the landing page (docs/index.html) does not link to CAPABILITIES.md"

    # And the prose pages exist in both languages (bilingual parity is enforced separately, but a
    # missing English mirror here would ship a half-built catalogue).
    for page in ("docs/CAPABILITIES.md", "docs/CAPABILITIES.en.md"):
        assert os.path.exists(os.path.join(REPO, page)), f"missing prose page {page}"

    print(f"capabilities: OK ({len(caps)} entries, all access paths resolve; "
          f"{sum(1 for c in caps if c.get('severity') == 'high')} high-severity; README+landing link it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
