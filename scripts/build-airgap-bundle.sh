#!/usr/bin/env bash
# Sentinel — air-gapped bundle assembler (M11.4, ADR-030). MAINTAINER-ONLY, run on a CONNECTED
# machine. This is the one place external calls happen (pull images/models, download the Release) —
# the RESULT is a self-contained bundle whose consumer never touches the network. NOT run in CI.
#
#   scripts/build-airgap-bundle.sh <vTAG> [outdir]
#
# Requires: a REAL published `v<TAG>` GitHub Release (from release.yml / M11.1 — none exists until a
# maintainer cuts the first tag, so this cannot run before then, exactly like M11.1's signed-release
# E2E), plus: docker (+ buildx), gh (authenticated), cosign v2+, curl, network access, AND a browser
# or device-code path for the interactive keyless signature of the bundle manifest (see step 8).
#
# Env overrides: MODEL (default qwen2.5-vl:7b — docs/LOCAL_MODELS.md §3.2, the ✅-verified Ollama VLM),
#                OLLAMA_IMAGE (default ollama/ollama:latest — the exact tag pulled is PINNED into .env),
#                IMAGE (default ghcr.io/alexgromer/sentinel).
set -euo pipefail

TAG="${1:?usage: build-airgap-bundle.sh <vTAG> [outdir]}"
OUTDIR="${2:-sentinel-airgap-${TAG}}"
IMAGE="${IMAGE:-ghcr.io/alexgromer/sentinel}"
MODEL="${MODEL:-qwen2.5-vl:7b}"
OLLAMA_IMAGE="${OLLAMA_IMAGE:-ollama/ollama:latest}"
COSIGN_ISSUER="${COSIGN_ISSUER:-https://token.actions.githubusercontent.com}"
COSIGN_RELEASE_ID_RE="${COSIGN_RELEASE_ID_RE:-https://github.com/AlexGromer/sentinel/.github/workflows/release.yml@refs/tags/v.*}"

ROOT="$(git rev-parse --show-toplevel)"
say() { printf '\033[1;34m==\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

for t in docker gh cosign curl; do command -v "$t" >/dev/null 2>&1 || die "missing required tool: $t"; done
gh release view "$TAG" >/dev/null 2>&1 || die "no GitHub Release '$TAG' — cut the release first (release.yml)"

mkdir -p "$OUTDIR"
OUT="$(cd "$OUTDIR" && pwd)"
say "assembling air-gapped bundle for $TAG -> $OUT"

# 1) Release artifacts (agentctl archives + checksums + cosign bundles + SBOM) --------------------
say "downloading Release assets ($TAG)"
gh release download "$TAG" --dir "$OUT" --pattern '*.tar.gz' --pattern 'checksums.sha256' \
  --pattern '*.cosign.bundle' --pattern 'sbom.cdx.json' --clobber

# 2) The SIGNED GHCR image: verify while connected, THEN retag + save. `docker save` does NOT carry
#    the attached cosign signature, so verification MUST happen here (online); what survives into the
#    bundle is the maintainer's manifest hash-of-the-tar (signed in step 8), not a cosign image object.
say "verifying the signed image $IMAGE:$TAG (online) before saving"
cosign verify "$IMAGE:$TAG" \
  --certificate-identity-regexp "$COSIGN_RELEASE_ID_RE" \
  --certificate-oidc-issuer "$COSIGN_ISSUER" >/dev/null \
  || die "cosign verify failed for $IMAGE:$TAG — refusing to bundle an unverified image"
docker pull "$IMAGE:$TAG"
docker tag "$IMAGE:$TAG" sentinel:local
say "docker save sentinel:local"
docker save sentinel:local -o "$OUT/sentinel.image.tar"

# 3) Ollama runtime image — pin the EXACT pulled tag into .env (never a floating :latest at deploy) --
say "pulling + saving the Ollama runtime image ($OLLAMA_IMAGE)"
case "$OLLAMA_IMAGE" in *:latest) say "WARN: OLLAMA_IMAGE is :latest — pass OLLAMA_IMAGE=ollama/ollama:<version> for a reproducible pin" ;; esac
docker pull "$OLLAMA_IMAGE"
OLLAMA_TAG="${OLLAMA_IMAGE##*:}"; [ "$OLLAMA_TAG" = "$OLLAMA_IMAGE" ] && OLLAMA_TAG="latest"
OLLAMA_DIGEST="$(docker inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$OLLAMA_IMAGE" 2>/dev/null || true)"
docker tag "$OLLAMA_IMAGE" "ollama/ollama:${OLLAMA_TAG}"
docker save "ollama/ollama:${OLLAMA_TAG}" -o "$OUT/ollama.image.tar"
{ printf 'OLLAMA_IMAGE_TAG=%s\n' "$OLLAMA_TAG"; [ -n "$OLLAMA_DIGEST" ] && printf '# ollama digest (reproducibility): %s\n' "$OLLAMA_DIGEST"; } > "$OUT/.env"

# 4) Preload the model + export the ollama models volume ------------------------------------------
say "preloading model '$MODEL' and exporting the volume"
docker volume create sentinel_ollama-bundle >/dev/null
docker run -d --name sentinel-ollama-bundle -v sentinel_ollama-bundle:/root/.ollama \
  "ollama/ollama:${OLLAMA_TAG}" >/dev/null
trap 'docker rm -f sentinel-ollama-bundle >/dev/null 2>&1 || true; docker volume rm sentinel_ollama-bundle >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do docker exec sentinel-ollama-bundle ollama list >/dev/null 2>&1 && break; sleep 2; done
docker exec sentinel-ollama-bundle ollama pull "$MODEL" || die "ollama pull '$MODEL' failed"
docker run --rm -v sentinel_ollama-bundle:/data -v "$OUT:/backup" alpine \
  tar czf /backup/ollama-models.tar.gz -C /data . || die "model export failed"
docker rm -f sentinel-ollama-bundle >/dev/null 2>&1 || true
docker volume rm sentinel_ollama-bundle >/dev/null 2>&1 || true
trap - EXIT

# 5) Offline compose + static docs subset (the same webui subset baked into the image) ------------
say "copying docker-compose.offline.yml + the static docs subset"
cp "$ROOT/docker-compose.offline.yml" "$OUT/"
mkdir -p "$OUT/docs-static"
cp -r "$ROOT/docs/setup" "$ROOT/docs/calculators" "$OUT/docs-static/" 2>/dev/null || true
cp "$ROOT/docs/index.html" "$ROOT/docs/LOCAL_MODELS.md" "$OUT/docs-static/" 2>/dev/null || true

# 6) Transfer + bring-up README ------------------------------------------------------------------
cat > "$OUT/README.md" <<EOF
# Sentinel air-gapped bundle — $TAG

Assembled on a connected machine; move this whole directory to the isolated host via physical media
or an internal transfer (it is NOT a GitHub Release asset). Nothing below touches the network.

## Verify + bring up (on the air-gapped host)
\`\`\`bash
# 1. verify integrity + signatures offline (no Rekor call):
scripts/offline-verify.sh --bundle "\$PWD"
# 2. load images + restore the model, then run:
docker load -i sentinel.image.tar && docker load -i ollama.image.tar
docker volume create sentinel_ollama-models
docker run --rm -v sentinel_ollama-models:/root/.ollama -v "\$PWD:/backup" alpine \\
  tar xzf /backup/ollama-models.tar.gz -C /root/.ollama
docker compose -f docker-compose.offline.yml --profile demo run --rm demo    # zero-dep smoke
docker compose -f docker-compose.offline.yml --profile ollama up -d ollama   # local model ($MODEL)
\`\`\`
Model: $MODEL · Ollama image tag (pinned in .env): $OLLAMA_TAG
EOF

# 6c) Sigstore trust root snapshot — lets offline-verify.sh --bundle validate cert chains on a fresh
#     air-gapped host with NO TUF CDN fetch (generated here, online, shipped in the bundle).
say "capturing the Sigstore trust root (trusted-root.json)"
cosign trusted-root create --out "$OUT/trusted-root.json" 2>/dev/null \
  || say "WARN: 'cosign trusted-root create' unavailable — offline verify may need 'cosign initialize' on the target host"

# 7) MANIFEST = superset checksum over EVERY DISTINCT bundle artifact. `*.tar.gz` already covers the
#    release archives AND ollama-models.tar.gz, so neither is listed again (no duplicate lines).
say "writing MANIFEST.sha256 over the whole bundle"
(
  cd "$OUT"
  files=(sentinel.image.tar ollama.image.tar sbom.cdx.json docker-compose.offline.yml .env)
  [ -f trusted-root.json ] && files+=(trusted-root.json)
  sha256sum "${files[@]}" ./*.tar.gz > MANIFEST.sha256   # *.tar.gz covers release archives + ollama-models
)

# 8) Sign the manifest — INTERACTIVE keyless (opens a browser / device-code flow; not unattended) ---
say "signing MANIFEST.sha256 (Cosign keyless — follow the browser/device prompt)"
cosign sign-blob --yes --bundle "$OUT/MANIFEST.sha256.cosign.bundle" "$OUT/MANIFEST.sha256" \
  || die "manifest signing failed (needs an interactive OIDC/browser session)"

say "DONE. Bundle: $OUT ($(du -sh "$OUT" | cut -f1)). Transfer it, then: scripts/offline-verify.sh --bundle <dir>"
