#!/bin/sh
# Sentinel — single-command installer (M11.5, ADR-030 / ADR-059).
#
#   curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh
#
# Downloads the `agentctl` binary from the latest GitHub Release, verifies its checksum (hard fail on
# mismatch) and — if `cosign` is installed — its keyless signature (pinned identity), then installs it to
# ~/.local/bin (no root). For an air-gapped host use the offline bundle instead (docs/DISTRIBUTION.md §6).
#
# Env overrides (also used by the CI install-smoke against a locally-built fake release):
#   SENTINEL_VERSION   pin a tag (default: latest)      SENTINEL_BIN_DIR   install dir (default ~/.local/bin)
#   SENTINEL_BASE_URL  release download base            SENTINEL_API_URL   latest-release API URL
#   SENTINEL_REPO      owner/repo (default AlexGromer/sentinel)
#   SENTINEL_COSIGN_ID_RE / SENTINEL_COSIGN_ISSUER   override the pinned keyless verify identity
set -eu

REPO="${SENTINEL_REPO:-AlexGromer/sentinel}"
BIN_DIR="${SENTINEL_BIN_DIR:-$HOME/.local/bin}"
VERSION="${SENTINEL_VERSION:-latest}"
BASE_URL="${SENTINEL_BASE_URL:-https://github.com/$REPO/releases/download}"
API_URL="${SENTINEL_API_URL:-https://api.github.com/repos/$REPO/releases/latest}"
COSIGN_ID_RE="${SENTINEL_COSIGN_ID_RE:-https://github.com/AlexGromer/sentinel/.github/workflows/release.yml@refs/tags/v.*}"
COSIGN_ISSUER="${SENTINEL_COSIGN_ISSUER:-https://token.actions.githubusercontent.com}"

info() { printf '\033[1;34m==\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar  >/dev/null 2>&1 || die "tar is required"
if   command -v sha256sum >/dev/null 2>&1; then SHACMD="sha256sum"
elif command -v shasum    >/dev/null 2>&1; then SHACMD="shasum -a 256"
else die "sha256sum or shasum is required"; fi

# --- platform -----------------------------------------------------------------------------------
os="$(uname -s)"; arch="$(uname -m)"
case "$os"   in Linux) OS=linux ;; Darwin) OS=darwin ;; *) die "unsupported OS '$os' — use Docker/WSL on Windows" ;; esac
case "$arch" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) die "unsupported arch '$arch'" ;; esac

# --- resolve version ----------------------------------------------------------------------------
if [ "$VERSION" = latest ]; then
  info "resolving the latest release ($REPO)"
  VERSION="$(curl -fsSL "$API_URL" | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name" *: *"([^"]+)".*/\1/')"
  [ -n "$VERSION" ] || die "could not resolve the latest release tag from $API_URL (network/rate-limit, or no release cut yet)"
fi
info "installing Sentinel agentctl $VERSION ($OS/$ARCH)"

# --- download -----------------------------------------------------------------------------------
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ARCHIVE="sentinel-$VERSION-$OS-$ARCH.tar.gz"
base="$BASE_URL/$VERSION"
info "downloading $ARCHIVE + checksums"
curl -fsSL "$base/$ARCHIVE"         -o "$TMP/$ARCHIVE"         || die "download failed: $base/$ARCHIVE"
curl -fsSL "$base/checksums.sha256" -o "$TMP/checksums.sha256" || die "download failed: $base/checksums.sha256"

# --- checksum (hard fail — acceptance criterion) ------------------------------------------------
info "verifying checksum"
( cd "$TMP" && grep " $ARCHIVE\$" checksums.sha256 > archive.sha256 \
    && [ -s archive.sha256 ] && $SHACMD -c archive.sha256 ) \
  || die "checksum verification FAILED for $ARCHIVE — refusing to install"

# --- cosign signature (optional; pinned keyless identity) ---------------------------------------
if command -v cosign >/dev/null 2>&1; then
  if curl -fsSL "$base/$ARCHIVE.cosign.bundle" -o "$TMP/$ARCHIVE.cosign.bundle" 2>/dev/null; then
    info "verifying Cosign signature"
    cosign verify-blob "$TMP/$ARCHIVE" --bundle "$TMP/$ARCHIVE.cosign.bundle" \
      --certificate-identity-regexp "$COSIGN_ID_RE" --certificate-oidc-issuer "$COSIGN_ISSUER" \
      || die "Cosign verification FAILED for $ARCHIVE"
  else
    warn "no .cosign.bundle for $ARCHIVE — skipping signature check"
  fi
else
  warn "cosign not installed — skipping signature verification (install cosign v3+ for supply-chain assurance)"
fi

# --- extract + install --------------------------------------------------------------------------
tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
SRC="$(find "$TMP" -type f -name agentctl | head -n1)"
[ -n "$SRC" ] || die "agentctl not found inside $ARCHIVE"
mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR"
if ! install -m 0755 "$SRC" "$BIN_DIR/agentctl" 2>/dev/null; then
  cp "$SRC" "$BIN_DIR/agentctl"  || die "failed to write $BIN_DIR/agentctl (permission denied / disk full / read-only mount?)"
  chmod 0755 "$BIN_DIR/agentctl" || die "failed to chmod $BIN_DIR/agentctl"
fi
# Hard gate: the install/sanity result MUST decide the exit code — a swallowed write/exec failure that
# still printed "installed" + exit 0 would give a false success to `curl|sh` and any CI gating on it.
got="$("$BIN_DIR/agentctl" --version 2>&1)" || die "the installed agentctl at $BIN_DIR/agentctl failed to run: $got"
info "installed: $BIN_DIR/agentctl ($got)"
case ":$PATH:" in *":$BIN_DIR:"*) : ;; *) warn "$BIN_DIR is not on \$PATH — add:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac

cat <<EOF

Sentinel agentctl $VERSION installed. (agentctl is the CLI; a full explore run also needs the Docker image,
built from the repo — the setup-WebUI + 'docker compose run' path assumes a checkout.)
Next steps:
  Full quickstart (git clone -> docker compose):  docs/QUICKSTART.md
  Air-gapped / offline install:                   docs/DISTRIBUTION.md §6
EOF
