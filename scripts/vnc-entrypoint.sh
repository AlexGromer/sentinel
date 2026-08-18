#!/bin/sh
# browser-vnc entrypoint (LIVE-VNC, ADR-127) — display, then VNC on a UNIX SOCKET, then BECOME the
# browser service.
#
# ⚠ THERE IS NO PASSWORD HERE, AND THAT IS THE SECURITY DECISION, NOT AN OMISSION.
#
# The first version served RFB over TCP with the protocol's classic "VNC Authentication", and CodeQL
# was right to call it out: that scheme is DES over the FIRST EIGHT BYTES of the password, over an
# unencrypted channel. The owner's rule is that weak ciphers do not ship — so the fix had to remove
# the algorithm, not reclassify the alert.
#
# Measured 2026-08-18 on this image (x11vnc 0.9.16), which is what made the fix possible:
#
#   x11vnc -unixsock <path> -rfbport 0   →  /proc/net/tcp EMPTY (no listening port at all)
#                                        →  security types [1] = None (no DES anywhere)
#                                        →  full session works: ServerInit + 655 360 bytes of pixels
#   socket at 0600                       →  a foreign uid gets EACCES on connect; the owner connects
#
# So authentication became FILE PERMISSIONS, which is strictly stronger than eight bytes of DES: the
# kernel refuses the connection before a byte of RFB is spoken. It also removes a surface measured the
# day before — the RFB port was reachable from the host at the container's bridge IP, so "not
# published" never meant "not reachable from this machine". A unix socket cannot be reached that way
# at all.
set -eu

: "${DISPLAY:=:99}"
: "${SENTINEL_VNC_SOCK:=/app/state/vnc.sock}"
: "${SENTINEL_VNC_GEOMETRY:=1280x800x24}"
export DISPLAY

# 0600 on the socket is the whole access-control story, so it is set before anything can create it.
umask 077

# --- the display ------------------------------------------------------------------------------------
# -nolisten tcp: the X server gets NO network socket either. x11vnc reaches it over
# /tmp/.X11-unix/X<n>, which is why that path is what "the display is up" is tested on rather than a
# sleep.
Xvfb "$DISPLAY" -screen 0 "$SENTINEL_VNC_GEOMETRY" -nolisten tcp &
XVFB_PID=$!
xsock="/tmp/.X11-unix/X${DISPLAY#:}"
i=0
while [ ! -e "$xsock" ]; do
  i=$((i + 1))
  [ "$i" -le 100 ] || { echo "vnc-entrypoint: Xvfb never created $xsock after 10s" >&2; exit 1; }
  # A dead Xvfb must fail FAST rather than after the full timeout: "slow" and "gone" need different
  # answers from whoever reads the log.
  kill -0 "$XVFB_PID" 2>/dev/null || { echo "vnc-entrypoint: Xvfb exited during startup" >&2; exit 1; }
  sleep 0.1
done

# --- the VNC server, on a unix socket and NOTHING else ----------------------------------------------
#   -unixsock   serve RFB on an AF_UNIX socket in the shared ./state mount, where control-api's relay
#               reaches it as a peer with the same uid.
#   -rfbport 0  no TCP listener AT ALL. Without this x11vnc opens 5900 as well and the socket is merely
#               an addition — measured, and the reason this flag is not optional.
#   -forever    keep serving after a viewer disconnects; without it x11vnc EXITS on the first
#               disconnect and the container stays up, healthy-looking, with nothing behind the socket.
#   -shared     a second viewer does not evict the first — takeover (ADR-054) means a human joining a
#               session somebody is already watching.
#   -noxdamage  XDAMAGE under Xvfb is a known source of stale tiles; the cost is a full-screen poll.
rm -f "$SENTINEL_VNC_SOCK"
x11vnc -display "$DISPLAY" -unixsock "$SENTINEL_VNC_SOCK" -rfbport 0 \
       -forever -shared -noxdamage -q &
X11VNC_PID=$!

# Wait for the socket, then narrow it. x11vnc creates it 0755 regardless of umask on some builds, so
# the mode is SET rather than assumed — this line is the access control, and a check asserts it.
i=0
while [ ! -S "$SENTINEL_VNC_SOCK" ]; do
  i=$((i + 1))
  [ "$i" -le 100 ] || { echo "vnc-entrypoint: x11vnc never created $SENTINEL_VNC_SOCK after 10s" >&2; exit 1; }
  kill -0 "$X11VNC_PID" 2>/dev/null || { echo "vnc-entrypoint: x11vnc exited during startup" >&2; exit 1; }
  sleep 0.1
done
chmod 600 "$SENTINEL_VNC_SOCK"

trap 'kill "$X11VNC_PID" "$XVFB_PID" 2>/dev/null || true; rm -f "$SENTINEL_VNC_SOCK"' TERM INT

echo "vnc-entrypoint: display $DISPLAY ($SENTINEL_VNC_GEOMETRY) up; RFB on $SENTINEL_VNC_SOCK (0600, no TCP port, no password — the socket's permissions ARE the access control)" >&2

# --- become the browser service ---------------------------------------------------------------------
# ⚠ The browser service must launch Chromium HEADED, and it does that only when PW_HEADED=1 is set on
# this service (compose sets it). Without that variable the infrastructure above is perfect and the
# operator sees a BLACK SCREEN: a headless Chromium draws nothing onto the display, while
# /live/status answers 200 and this container reports healthy.
[ "$#" -gt 0 ] || set -- node /app/pw-executor/dist/cdp-service.js
exec "$@"
