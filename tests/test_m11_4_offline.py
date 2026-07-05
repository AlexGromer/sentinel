#!/usr/bin/env python3
"""M11.4 air-gapped bundle — offline structural test (no Docker, no network, stdlib only).

Runs inside the CI `build` job's offline suite loop (`.venv/bin/python tests/test_m11_4_offline.py`).
It does NOT build/run containers — the heavy save/load/`--network none` proof lives in the `airgap`
CI job (scripts/offline-verify.sh --local). Here we cheaply guard the invariants that make the offline
compose correct on a TRUE air-gapped host, so a regression reddens fast even if the Docker job is skipped:

  * the offline anchor carries NO `build:` key   (else compose would build+pull base images offline)
  * the airgap network is `internal: true`        (structural "zero external calls")
  * `pull_policy: never` is set                    (fail closed instead of reaching a registry)
  * the `demo` service is `network_mode: none`     (strict isolation for the LLM-free smoke)
  * the ollama image tag is PARAMETERIZED          (pinned at bundle time, never a floating :latest)
  * both scripts exist and are executable          (single-source verifier + maintainer assembler)
  * the .dockerignore hub-page gap is fixed        (`!docs/index.html`, so /app/docs has a landing page)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(cond, msg):
    (print("PASS", msg) if cond else FAILURES.append(msg))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# --- docker-compose.offline.yml -------------------------------------------------------------------
compose_path = os.path.join(ROOT, "docker-compose.offline.yml")
check(os.path.isfile(compose_path), "docker-compose.offline.yml exists")
if os.path.isfile(compose_path):
    compose = read("docker-compose.offline.yml")

    # Slice the offline base anchor block: from its declaration to the first top-level `services:`.
    anchor_re = re.search(r"^x-sentinel-offline-base:.*?(?=^\S)", compose, re.S | re.M)
    check(anchor_re is not None, "x-sentinel-offline-base anchor is defined")
    if anchor_re:
        check("build:" not in anchor_re.group(0),
              "offline anchor has NO build: key (would pull base images on an air-gapped host)")

    check("internal: true" in compose, "airgap network is internal: true")
    check(compose.count("pull_policy: never") >= 2, "pull_policy: never on the image services")
    check("network_mode: none" in compose, "demo service is network_mode: none")
    check("${OLLAMA_IMAGE_TAG" in compose,
          "ollama image tag is parameterized (${OLLAMA_IMAGE_TAG}, pinned at bundle time)")
    check(not re.search(r"image:\s*ollama/ollama:latest\b", compose),
          "ollama image is not a hardcoded floating :latest")

# --- scripts --------------------------------------------------------------------------------------
for rel in ("scripts/offline-verify.sh", "scripts/build-airgap-bundle.sh"):
    p = os.path.join(ROOT, rel)
    check(os.path.isfile(p), f"{rel} exists")
    check(os.path.isfile(p) and os.access(p, os.X_OK), f"{rel} is executable")

if os.path.isfile(os.path.join(ROOT, "scripts/offline-verify.sh")):
    ov = read("scripts/offline-verify.sh")
    check("--local" in ov and "--bundle" in ov, "offline-verify.sh has both --local and --bundle modes")
    check("--network none" in ov, "offline-verify.sh isolates the runtime with --network none")
    check("gethostbyname" in ov, "offline-verify.sh includes the negative DNS probe")
    check("cosign verify-blob" in ov, "offline-verify.sh verifies cosign bundles offline")
    check("--offline" not in ov,
          "offline-verify.sh does NOT use the removed cosign --offline flag (v3.x: use --bundle)")

# --- .dockerignore hub-page fix -------------------------------------------------------------------
if os.path.isfile(os.path.join(ROOT, ".dockerignore")):
    check("!docs/index.html" in read(".dockerignore"),
          ".dockerignore whitelists docs/index.html (image hub landing page present)")

# --- verdict --------------------------------------------------------------------------------------
if FAILURES:
    print("\nFAIL — %d check(s):" % len(FAILURES), file=sys.stderr)
    for f in FAILURES:
        print("  -", f, file=sys.stderr)
    sys.exit(1)
print("\nOK: M11.4 offline structural checks passed")
