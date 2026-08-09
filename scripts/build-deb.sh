#!/bin/sh
# Build the Sentinel .deb from ALREADY-BUILT binaries (ADR-110).
#
#   scripts/build-deb.sh <version> <arch> <bin-dir> [out-dir]
#     version   0.1.0            (a leading "v" is accepted and stripped)
#     arch      amd64 | arm64    (Debian arch names, not GOARCH-by-accident)
#     bin-dir   directory holding the four control-plane binaries
#     out-dir   default: dist/
#
# Deliberately dpkg-deb and nothing else: no nfpm, no goreleaser, no fpm. The binaries
# are already produced by the release matrix, so packaging them is a file layout plus a
# control file — a new build-tool dependency would buy nothing and would have to be
# pinned, verified and carried into the air-gapped bundle.
#
# The package ships the control plane ONLY; a browser run additionally needs
# Python/uv + Node + Playwright browsers on the host. That is stated in the package
# description and in README.Debian rather than left for the user to discover.
set -eu

VERSION="${1:?usage: build-deb.sh <version> <arch> <bin-dir> [out-dir]}"
ARCH="${2:?missing arch (amd64|arm64)}"
BIN_DIR="${3:?missing bin-dir}"
OUT_DIR="${4:-dist}"
VERSION="${VERSION#v}"

case "$ARCH" in
  amd64|arm64) : ;;
  *) echo "ERROR: unsupported arch '$ARCH' (expected amd64 or arm64)" >&2; exit 1 ;;
esac

# A Debian version must not start with a letter and must contain no '/' or whitespace.
# Catching it here beats a dpkg-deb error three steps later that names the temp dir.
case "$VERSION" in
  ''|*[!0-9A-Za-z.+~-]*|[!0-9]*) echo "ERROR: '$VERSION' is not a usable Debian version" >&2; exit 1 ;;
esac

# CDPATH is unset rather than prefixed onto `cd`: the prefix form is correct sh but shellcheck reads
# it as a typo (SC1007), and shellcheck is a HARD gate in ci.yml's install-smoke job.
unset CDPATH
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
SRC="$ROOT/packaging/deb"
command -v dpkg-deb >/dev/null 2>&1 || { echo "ERROR: dpkg-deb is required" >&2; exit 1; }

# The four control-plane binaries. A package that silently ships three of them would
# install cleanly and fail at the moment someone tries to use the missing one, so a
# missing input is a HARD error here, not a warning.
BINARIES="agentctl control-api store-gateway orchestrator"
missing=
for b in $BINARIES; do
  [ -f "$BIN_DIR/$b" ] || missing="$missing $b"
done
if [ -n "$missing" ]; then
  echo "ERROR: missing binaries in '$BIN_DIR':$missing" >&2
  exit 1
fi

PKG="sentinel_${VERSION}_${ARCH}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
TREE="$STAGE/$PKG"

mkdir -p "$TREE/DEBIAN" \
         "$TREE/usr/bin" \
         "$TREE/usr/lib/systemd/system" \
         "$TREE/etc/sentinel" \
         "$TREE/usr/share/doc/sentinel"

for b in $BINARIES; do
  install -m 0755 "$BIN_DIR/$b" "$TREE/usr/bin/$b"
done

install -m 0644 "$SRC/sentinel-control-api.service" "$TREE/usr/lib/systemd/system/"
install -m 0644 "$SRC/control-api.env"              "$TREE/etc/sentinel/"
install -m 0644 "$SRC/README.Debian"                "$TREE/usr/share/doc/sentinel/"
install -m 0644 "$ROOT/LICENSE"                     "$TREE/usr/share/doc/sentinel/copyright"

sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" "$SRC/control.in" > "$TREE/DEBIAN/control"

# Declaring the env file a conffile is what makes an operator's edits survive an
# upgrade instead of being silently overwritten.
printf '/etc/sentinel/control-api.env\n' > "$TREE/DEBIAN/conffiles"

# systemd only learns about a unit file after a daemon-reload, and dpkg will not do it
# for us here (no debhelper). Without these three scripts the unit would be installed
# and invisible, then left running after the package was removed.
cat > "$TREE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
EOF
cat > "$TREE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = deconfigure ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop sentinel-control-api >/dev/null 2>&1 || true
    fi
fi
exit 0
EOF
cat > "$TREE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
    case "$1" in
        remove|purge) systemctl disable sentinel-control-api >/dev/null 2>&1 || true ;;
    esac
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 "$TREE/DEBIAN/postinst" "$TREE/DEBIAN/prerm" "$TREE/DEBIAN/postrm"

# md5sums lets `dpkg -V sentinel` report a tampered or truncated file after install.
( cd "$TREE" && find usr etc -type f -print0 | LC_ALL=C sort -z \
    | xargs -0 md5sum > DEBIAN/md5sums )

mkdir -p "$OUT_DIR"
# --root-owner-group: package contents belong to root:root regardless of who built
# them, so the .deb is identical from a CI runner and from a maintainer's laptop.
dpkg-deb --build --root-owner-group "$TREE" "$OUT_DIR/$PKG.deb" >/dev/null
echo "$OUT_DIR/$PKG.deb"
