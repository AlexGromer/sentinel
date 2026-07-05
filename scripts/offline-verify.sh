#!/usr/bin/env bash
# Sentinel — offline / air-gapped verification (M11.4, ADR-030).
#
# The SINGLE source of offline-verification truth: the CI `airgap` job AND a maintainer both call
# this, so the check that gates a PR is byte-identical to the one that blesses a real bundle.
#
#   offline-verify.sh --local
#       Testable NOW, no release required. Builds sentinel:local, round-trips it through
#       `docker save`/`docker load`, and proves the RUNTIME makes zero external calls: agentctl runs
#       under `--network none`, the LLM-free demo explore completes, the bundled docs are present,
#       and a DNS lookup from inside the isolated container FAILS (negative probe). Lints the offline
#       compose file. This is what gates every push/PR. amd64/native — no multi-arch here.
#
#   offline-verify.sh --bundle <dir>
#       Maintainer / post-tag. Verifies a REAL assembled bundle (built by build-airgap-bundle.sh from
#       a v* release): MANIFEST checksums, Cosign bundles (offline — the SET rides in the .cosign.bundle
#       and the Sigstore trust root ships as trusted-root.json, so NO Rekor/TUF network call), then
#       brings up the offline stack and probes the local Ollama /v1/models endpoint from a peer on the
#       internal airgap network. Cannot pass until a v* tag exists (the GHCR image + signed archives are
#       release-gated) — the same maintainer-gated boundary as M11.1's signed release.
#
# Exit 0 = every check passed; non-zero = a check failed (message on stderr). No arguments prints usage.
set -euo pipefail

IMAGE="sentinel:local"
COMPOSE="docker-compose.offline.yml"
PROBE_HOST="${PROBE_HOST:-github.com}"        # host the negative DNS probe must FAIL to resolve

# Cosign identities are PARAMETERS, not hardcoded: the carried-forward M11.1 release assets and the
# new bundle MANIFEST are signed by different identities (a release workflow ref vs an interactive
# maintainer keyless flow), so a single hardcoded regex would silently mis-verify one of them.
COSIGN_ISSUER="${COSIGN_ISSUER:-https://token.actions.githubusercontent.com}"
COSIGN_RELEASE_ID_RE="${COSIGN_RELEASE_ID_RE:-https://github.com/AlexGromer/sentinel/.github/workflows/release.yml@refs/tags/v.*}"
COSIGN_MANIFEST_ID_RE="${COSIGN_MANIFEST_ID_RE:-.*}"                 # maintainer identity; SET THIS to pin the signer
COSIGN_MANIFEST_ISSUER="${COSIGN_MANIFEST_ISSUER:-$COSIGN_ISSUER}"

# Optional buildx cache backend for a from-scratch `--local` build OUTSIDE CI, e.g.
# AIRGAP_CACHE='--cache-from type=gha --cache-to type=gha,mode=max'. CI does NOT use this: it prebuilds
# via docker/build-push-action (which drives the gha cache) and sets AIRGAP_SKIP_BUILD=1, so the build
# branch below is skipped and the already-loaded image is reused.
AIRGAP_CACHE="${AIRGAP_CACHE:-}"

info() { printf '\033[1;34m==\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32mPASS\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$0")")"

# ---------------------------------------------------------------------------------------------------
verify_local() {
  info "M11.4 offline-verify --local (build -> save/load -> --network none runtime, no external calls)"

  if [ -n "${AIRGAP_SKIP_BUILD:-}" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    info "reusing existing $IMAGE (AIRGAP_SKIP_BUILD set)"
  else
    info "building $IMAGE (docker buildx, amd64/native)"
    # shellcheck disable=SC2086  # AIRGAP_CACHE is intentionally word-split into separate flags
    docker buildx build -f Dockerfile -t "$IMAGE" --load $AIRGAP_CACHE . \
      || fail "image build failed"
  fi

  info "docker save -> load round-trip (proves the tar is self-contained)"
  tmptar="$(mktemp -t sentinel-XXXXXX.tar)"
  docker save "$IMAGE" -o "$tmptar"
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  docker load -i "$tmptar"
  rm -f "$tmptar"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "$IMAGE absent after save/load round-trip"
  pass "save/load round-trip"

  # agentctl is a subcommand CLI: `--help` (no valid subcommand) prints usage and exits 2 BY DESIGN
  # (cmd/agentctl/main.go: default -> usage(); code=2). So assert it EXECUTES and emits usage under
  # --network none (the isolation smoke), independent of the CLI's help exit code. The demo run below
  # is the exit-0 proof that a full offline explore succeeds.
  info "agentctl executes under --network none (usage smoke)"
  help_out="$(docker run --rm --network none "$IMAGE" --help 2>&1 || true)"
  printf '%s' "$help_out" | grep -qiE "agentctl (run|baseline)|^usage:" \
    || fail "agentctl produced no usage output under --network none (binary failed to execute)"
  pass "agentctl runs with no network"

  info "LLM-free demo explore under --network none (heuristic planner)"
  runs_dir="$(mktemp -d -t sentinel-runs-XXXXXX)"
  docker run --rm --network none -v "$runs_dir:/app/runs" "$IMAGE" \
    run --target "file:///app/testdata/site/index.html" --planner heuristic --artifact-dir /app/runs/demo \
    || fail "offline demo explore failed"
  plan="$runs_dir/demo/plan.json"
  [ -s "$plan" ] || fail "demo produced no plan.json"
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$plan" || fail "plan.json is not valid JSON"
  rm -rf "$runs_dir" 2>/dev/null || sudo rm -rf "$runs_dir" 2>/dev/null || true
  pass "demo explore completed offline -> valid plan.json"

  info "bundled docs present in the image (/app/docs/index.html)"
  docker run --rm --network none --entrypoint /usr/bin/test "$IMAGE" -f /app/docs/index.html \
    || fail "/app/docs/index.html missing — the .dockerignore hub-page gap is not fixed"
  pass "docs static copy present in the image"

  # setdefaulttimeout does not bind gethostbyname (OS resolver), but under --network none there is no
  # reachable nameserver, so the lookup fails FAST with a resolver error — which is exactly the signal
  # we assert. The timeout is belt-and-suspenders for a misconfigured host, not the primary mechanism.
  info "negative DNS probe — resolving $PROBE_HOST from inside --network none MUST fail"
  if docker run --rm --network none --entrypoint /app/.venv/bin/python "$IMAGE" \
       -c "import socket; socket.setdefaulttimeout(2); socket.gethostbyname('$PROBE_HOST')" >/dev/null 2>&1; then
    fail "$PROBE_HOST resolved under --network none — the container is NOT network-isolated"
  fi
  pass "no external DNS reachable from the isolated container"

  info "offline compose lints ($COMPOSE config -q)"
  docker compose -f "$COMPOSE" config -q || fail "$COMPOSE is not valid"
  pass "$COMPOSE is valid"

  info "ALL --local checks passed"
}

# ---------------------------------------------------------------------------------------------------
verify_bundle() {
  local dir="$1"
  [ -d "$dir" ] || fail "bundle dir not found: $dir"
  command -v cosign >/dev/null 2>&1 || fail "cosign not installed (required for --bundle)"
  info "M11.4 offline-verify --bundle $dir (real assembled bundle — maintainer / post-tag)"
  cd "$dir"

  info "checksums verify offline (sha256sum -c MANIFEST.sha256)"
  [ -f MANIFEST.sha256 ] || fail "MANIFEST.sha256 missing"
  sha256sum -c MANIFEST.sha256 || fail "checksum mismatch"
  pass "all checksums verified"

  # Offline cosign needs the Sigstore trust root (Fulcio CA + Rekor/CT keys) to validate the cert chain
  # in each bundle. build-airgap-bundle.sh ships it as trusted-root.json so no TUF CDN fetch happens on
  # a fresh air-gapped host. If it's absent (older bundle), cosign falls back to its embedded/ambient
  # root — which on a never-initialized offline host would try the network; warn loudly.
  local trust=(); if [ -f trusted-root.json ]; then trust=(--trusted-root trusted-root.json); else
    warn "trusted-root.json absent — cosign may attempt a TUF fetch; on a fresh air-gapped host run 'cosign initialize' while connected first"; fi
  [ "$COSIGN_MANIFEST_ID_RE" = ".*" ] && \
    warn "COSIGN_MANIFEST_ID_RE=.* accepts ANY signer for MANIFEST.sha256 — set it to the maintainer identity to pin the signer"

  info "Cosign bundles verify offline (SET in the bundle, trust root local — no Rekor/TUF network call)"
  local b f id iss
  for b in *.cosign.bundle; do
    [ -e "$b" ] || fail "no .cosign.bundle files found in $dir"
    f="${b%.cosign.bundle}"
    [ -f "$f" ] || fail "signed file for $b not found ($f)"
    if [ "$f" = "MANIFEST.sha256" ]; then id="$COSIGN_MANIFEST_ID_RE"; iss="$COSIGN_MANIFEST_ISSUER";
    else id="$COSIGN_RELEASE_ID_RE"; iss="$COSIGN_ISSUER"; fi
    cosign verify-blob "$f" --bundle "$b" "${trust[@]}" \
      --certificate-identity-regexp "$id" --certificate-oidc-issuer "$iss" \
      || fail "cosign verify-blob failed for $f"
    pass "verified $f"
  done

  info "loading bundled images (docker load)"
  local tar
  for tar in *.image.tar; do
    [ -e "$tar" ] || fail "no *.image.tar found — cannot bring the stack up offline"
    docker load -i "$tar" || fail "docker load failed for $tar"
  done

  local have_model=""
  if [ -f ollama-models.tar.gz ]; then
    have_model=1
    info "restoring the ollama model volume from the bundle (-> sentinel_ollama-models)"
    docker volume create sentinel_ollama-models >/dev/null
    docker run --rm -v sentinel_ollama-models:/root/.ollama -v "$PWD:/backup" alpine \
      tar xzf /backup/ollama-models.tar.gz -C /root/.ollama || fail "model restore failed"
  fi

  info "bringing up the offline stack + probing Ollama /v1/models over the airgap network"
  docker compose -f "$COMPOSE" --profile ollama up -d ollama || fail "offline ollama service failed to start"
  # Wait for the ollama server to answer its own API (dependency-free — the ollama binary is in the image).
  local up=""
  for _ in $(seq 1 30); do
    if docker compose -f "$COMPOSE" exec -T ollama ollama list >/dev/null 2>&1; then up=1; break; fi
    sleep 2
  done
  [ -n "$up" ] || fail "ollama server did not become ready"
  # Probe the OpenAI-compat /v1/models from a sentinel PEER on the internal airgap network (no host
  # port exists — internal networks can't publish ports; a peer proves the endpoint answers offline).
  if docker compose -f "$COMPOSE" run --rm --entrypoint /app/.venv/bin/python sentinel \
       -c "import urllib.request,sys; d=urllib.request.urlopen('http://ollama:11434/v1/models', timeout=15).read().decode(); print(d[:200]); sys.exit(0 if '\"object\"' in d or 'data' in d else 1)"; then
    pass "Ollama /v1/models responds offline over the airgap network"
  elif [ -n "$have_model" ]; then
    docker compose -f "$COMPOSE" down -v || true
    fail "Ollama /v1/models did not respond — the preloaded model volume is not attached correctly"
  else
    warn "/v1/models not reachable (no model was bundled — skipping)"
  fi

  info "offline demo (heuristic, LLM-free)"
  docker compose -f "$COMPOSE" --profile demo run --rm demo || { docker compose -f "$COMPOSE" down -v || true; fail "offline demo run failed"; }
  docker compose -f "$COMPOSE" down -v || true

  info "ALL --bundle checks passed"
}

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }

case "${1:-}" in
  --local)  verify_local ;;
  --bundle) [ $# -ge 2 ] || fail "--bundle requires a <dir> argument"; verify_bundle "$2" ;;
  -h|--help|"") usage 0 ;;
  *) fail "unknown mode: $1 (use --local or --bundle <dir>)" ;;
esac
