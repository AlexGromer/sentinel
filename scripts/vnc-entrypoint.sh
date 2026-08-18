#!/bin/sh
# browser-vnc entrypoint (W3 [LIVE-VNC]) — password, then display, then VNC, then BECOME the browser
# service. The order is the design, not a preference: each step is a precondition of the next, and the
# first one is the one that can refuse.
#
# ⚠ THE FIRST COMMAND IS THE SECRET, AND `set -e` IS WHAT MAKES "VNC WITHOUT A PASSWORD" IMPOSSIBLE
# BY CONSTRUCTION. `agentctl vnc-password` exits 2 when neither path yields a usable password, and
# nothing below runs. There is no `-nopw` in this file and there will not be one; that is asserted by
# a gate rather than promised by a comment.
#
# WHY NO PROCESS SUPERVISOR. Three processes live here, and the obvious answer — s6/supervisord plus a
# config file — would add a package, a SECOND place where "what runs in this container" is written
# down, and a component that restarts things quietly. It is not needed, because the three are not
# peers:
#
#   * the browser service is the one whose death makes the container useless, so it is the one this
#     script EXECs into. It becomes the container's main process; when it dies the container dies and
#     compose's restart policy applies — semantics that already exist and that a supervisor would hide.
#   * Xvfb and x11vnc are its preconditions. If either dies, the browser service goes on answering
#     /live/status perfectly while the screen it exists to show is gone. That is not left to a
#     supervisor to notice: the compose healthcheck opens BOTH the live port and the RFB port, so the
#     container goes UNHEALTHY on exactly that failure. A supervisor would have restarted x11vnc and
#     told nobody.
#   * zombie reaping is `init: true` in compose (docker's tini), not a supervisor. Without it the
#     EXEC'd node process would be pid 1 and would never reap the two children it did not spawn.
#
# ⚠ VNC AUTHENTICATION IS DES OVER THE FIRST EIGHT BYTES OF THE PASSWORD (RFB "VncAuth"). Measured
# 2026-08-17 against x11vnc 0.9.16: a server holding `ABCDEFGH12345678` accepts a client sending
# `ABCDEFGH` and refuses `ABCDEFGX`. Anything longer is truncated BY THE PROTOCOL, not by us — which
# is why the port is never published and why the trust boundary stays "who can reach this docker
# network".
set -eu

: "${DISPLAY:=:99}"
: "${SENTINEL_VNC_PORT:=5900}"
: "${SENTINEL_VNC_GEOMETRY:=1280x800x24}"
export DISPLAY

# No `set -x` in this file, ever: it would print the password into the container log.
umask 077

# --- the password, by EXACTLY TWO PATHS (Alex, 2026-08-16) ------------------------------------------
# Both converge HERE rather than on disk. `SENTINEL_VNC_PASSWORD` deliberately writes nothing to
# ./state (an operator who keeps the secret in their environment did not ask us to copy it onto their
# host), so x11vnc cannot simply be pointed at state/vnc.password — that file exists on one path and
# not the other. The verb prints whichever password is live, and this working copy lives on the
# container's own layer, NOT on the ./state bind mount, so it never reaches the host and dies with the
# container.
RFB_PASS_FILE=/tmp/vnc-rfb.pass
/app/bin/agentctl vnc-password --print > "$RFB_PASS_FILE"
chmod 600 "$RFB_PASS_FILE"
[ -s "$RFB_PASS_FILE" ] || { echo "vnc-entrypoint: the password file came out empty" >&2; exit 1; }

# --- the display ------------------------------------------------------------------------------------
# -nolisten tcp: the X server gets NO network socket at all. x11vnc reaches it over the unix socket
# below, which is why that path is what "the display is up" is tested on rather than a sleep.
Xvfb "$DISPLAY" -screen 0 "$SENTINEL_VNC_GEOMETRY" -nolisten tcp &
XVFB_PID=$!
sock="/tmp/.X11-unix/X${DISPLAY#:}"
i=0
while [ ! -e "$sock" ]; do
  i=$((i + 1))
  [ "$i" -le 100 ] || { echo "vnc-entrypoint: Xvfb never created $sock after 10s" >&2; exit 1; }
  # A dead Xvfb must fail FAST rather than after the full timeout: the difference between "slow" and
  # "gone" is the difference between waiting and reading the log.
  kill -0 "$XVFB_PID" 2>/dev/null || { echo "vnc-entrypoint: Xvfb exited during startup" >&2; exit 1; }
  sleep 0.1
done

# --- the VNC server ---------------------------------------------------------------------------------
# It binds all interfaces of the container's OWN network namespace because THE CLIENT IS ANOTHER
# CONTAINER (control-api's relay), and there is no way to bind "only the peer that will connect".
# `-localhost` would make the profile not work at all. The password is what stands there instead —
# which is exactly why it is mandatory and has no off switch.
#
#   -forever    keep serving after a viewer disconnects. Without it x11vnc EXITS on the first
#               disconnect and the container stays up, healthy-looking, with nothing behind the port.
#   -shared     a second viewer does not evict the first — takeover (ADR-054) means a human joining a
#               session somebody is already watching.
#   -noxdamage  XDAMAGE under Xvfb is a known source of stale tiles; the cost is a full-screen poll.
#   -passwdfile reads the FIRST LINE of the file as PLAINTEXT (measured: a file holding `sekret12\n`
#               was accepted as the password `sekret12`). The obfuscated `-rfbauth` form is NOT used:
#               it is fixed-key DES, publicly reversible, so it buys no confidentiality — while making
#               the file unreadable to the operator it was generated for.
x11vnc -display "$DISPLAY" -rfbport "$SENTINEL_VNC_PORT" -listen 0.0.0.0 \
       -passwdfile "$RFB_PASS_FILE" -forever -shared -noxdamage -q &
X11VNC_PID=$!

trap 'kill "$X11VNC_PID" "$XVFB_PID" 2>/dev/null || true' TERM INT

echo "vnc-entrypoint: display $DISPLAY ($SENTINEL_VNC_GEOMETRY) up; RFB on 0.0.0.0:$SENTINEL_VNC_PORT (password required, never published to a host)" >&2

# --- become the browser service ---------------------------------------------------------------------
# ⚠ The browser service must launch Chromium HEADED here, and it does that only when PW_HEADED=1 is
# set on this service (compose sets it). Without that variable the infrastructure above is perfect and
# the operator sees a BLACK SCREEN: a headless Chromium draws nothing onto :99, while /live/status
# answers 200 and this container reports healthy.
[ "$#" -gt 0 ] || set -- node /app/pw-executor/dist/cdp-service.js
exec "$@"
