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
        acc = c["access"]
        kind, ref = acc["kind"], acc["ref"]

        if kind == "cli":
            assert f'case "{ref}":' in agentctl, (
                f"{cid}: cli subcommand {ref!r} is not in cmd/agentctl/main.go's switch")
        elif kind == "http":
            assert f'HandleFunc("{ref}"' in control_api, (
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
        elif kind == "env":
            assert ref in product_src, (
                f"{cid}: env var {ref!r} is read by no non-test product code — dead or renamed?")
        elif kind == "code":
            src = _read(acc["file"])
            assert ref in src, f"{cid}: token {ref!r} not found in {acc['file']}"
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
