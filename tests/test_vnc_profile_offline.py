"""The `vnc` profile holds together across FOUR files, and every seam here is invisible to the others.

WHY THIS FILE EXISTS. `[LIVE-VNC]` is not one change in one place: the compose service says which
display to use, the entrypoint starts an X server and a VNC server on it, the browser service decides
whether Chromium is headed, and the healthcheck decides whether the container is honest about all
three. Each of those files is already covered by a gate — and not one of those gates can see the seam
between them.

The seam that matters most is worth naming in full, because it is the failure this profile would have
shipped with. `pw-executor/src/cdp-service.ts` called `chromium.launch({args})` with NO headless
option, so Playwright's default (headless) won. A headless Chromium draws nothing onto the virtual
display — so Xvfb would run, x11vnc would export the display, the container would report healthy,
`/live/status` would answer 200, every existing gate would stay green, and the operator would open the
live view and see A BLACK SCREEN. Nothing in the tree could have told them why.

The checks below therefore compare the TWO ENDS of each seam rather than asserting one end twice —
the same shape, and for the same reason, as `test_the_orchestrator_listens_where_control_api_dials`.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILT = REPO / "docker-compose.yml"
PULLED = REPO / "docker-compose.ghcr.yml"
ENTRYPOINT = REPO / "scripts" / "vnc-entrypoint.sh"
CDP_SERVICE = REPO / "pw-executor" / "src" / "cdp-service.ts"

# A floor on the derivation below: with no service carrying a DISPLAY, every assertion over that set
# is vacuously true, and this file would pass while the profile did not exist at all.
MIN_DISPLAY_SERVICES = 1


def _segments(path: pathlib.Path) -> "dict[str, str]":
    """service name -> the text of its own block. Same splitter as the parity gate."""
    text = path.read_text()
    m = re.search(r"(?m)^services:\s*$", text)
    assert m, f"{path.name} has no top-level `services:` block"
    body = text[m.end():]
    end = re.search(r"(?m)^[a-zA-Z_][\w-]*:\s*$", body)
    if end:
        body = body[: end.start()]
    starts = [(mm.group(1), mm.start(), mm.end())
              for mm in re.finditer(r"(?m)^  ([a-z0-9][\w-]*):\s*$", body)]
    out = {}
    for i, (name, _s, e) in enumerate(starts):
        stop = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        out[name] = body[e:stop]
    return out


def display_services(path: pathlib.Path) -> "dict[str, str]":
    """Services that hand their container an X display — DERIVED from the environment block.

    Keyed on `DISPLAY:` rather than on the name `browser-vnc`, for the same reason the CDP-port rule
    is: the property is "this container draws onto an X server", and a second such service would be
    covered by construction rather than by somebody remembering to extend a list.
    """
    return {n: seg for n, seg in _segments(path).items()
            if re.search(r"(?m)^      DISPLAY:\s*\S", seg)}


def test_a_service_on_a_virtual_display_asks_for_a_headed_browser():
    """THE seam. Without PW_HEADED the profile is an X server exporting an empty desktop."""
    for path in (BUILT, PULLED):
        svcs = display_services(path)
        assert len(svcs) >= MIN_DISPLAY_SERVICES, (
            f"{path.name}: derived {len(svcs)} service(s) with a DISPLAY, expected at least "
            f"{MIN_DISPLAY_SERVICES}. Either the `vnc` profile lost its browser, or this parser "
            f"stopped seeing it — and then every check below asserts nothing.")
        for name, seg in sorted(svcs.items()):
            assert re.search(r'(?m)^      PW_HEADED:\s*"?1"?\s*$', seg), (
                f"{path.name}: `{name}` runs on a virtual X display and does not set PW_HEADED=1. "
                f"Chromium would launch HEADLESS and draw nothing onto that display, while x11vnc "
                f"faithfully exported an empty desktop, /live/status answered 200 and the container "
                f"reported healthy. The operator would see a black screen with no way to find out why.")


def test_the_browser_service_lets_the_environment_decide_headedness():
    """The other end of the same seam, in the code that actually launches Chromium.

    Asserted INSIDE the `chromium.launch({...})` options object, not merely somewhere in the file: a
    file-wide search for the word would be satisfied by a comment, and it was a comment-shaped absence
    that produced this defect in the first place.
    """
    src = CDP_SERVICE.read_text()
    m = re.search(r"chromium\.launch\(\{(.+?)\}\);", src, re.S)
    assert m, "cdp-service.ts no longer calls chromium.launch({...}) in a shape this gate can read"
    opts = m.group(1)
    assert re.search(r"^\s*headless:", opts, re.M), (
        "chromium.launch() passes no `headless` option, so Playwright's default (headless) wins "
        "regardless of PW_HEADED — the black-screen defect this file exists to prevent.")
    assert "PW_HEADED" in opts or "PW_HEADLESS" in opts, (
        "the `headless` option does not depend on the environment, so it is a constant. A constant "
        "`true` breaks the vnc profile; a constant `false` breaks every golden ever taken, because "
        "screenshot_hash is byte-stable only in headless (docs/DETERMINISM.md).")


def test_a_headed_browser_is_told_how_big_the_screen_is():
    """FOUND BY LOOKING AT THE FIRST VNC FRAME, and by nothing else (2026-08-17).

    The first screenshot taken over RFB showed a real browser window on a 1280x800 display at about
    1060x790 — black bands down the right and along the bottom, ~17% of the screen. Every check was
    green: healthy container, 200 from /live/status, a frame with content in it. It just looked broken
    to a person. The cause is that the container has no window manager (deliberately), so nothing
    maximises or places a window and Chromium keeps its built-in default.

    Measured after the fix: non-black pixels went from 79.9% to 99.7%.

    Asserted as a SEAM, like the rest of this file: the geometry the entrypoint gives Xvfb and the
    geometry the browser is told must come from the same variable, or the window and the screen drift
    apart again with nothing to notice.
    """
    src = CDP_SERVICE.read_text()
    assert "SENTINEL_VNC_GEOMETRY" in src, (
        "cdp-service.ts does not read SENTINEL_VNC_GEOMETRY, so a headed Chromium keeps its default "
        "window size and leaves black bands on the exported screen — green everywhere, broken to look "
        "at.")
    assert "--window-size=" in src, (
        "no --window-size is passed to Chromium. There is no window manager in that container: "
        "nothing will resize the window afterwards, and --start-maximized needs a WM to act on.")

    ep = ENTRYPOINT.read_text()
    assert "SENTINEL_VNC_GEOMETRY" in ep, (
        "vnc-entrypoint.sh no longer sizes Xvfb from SENTINEL_VNC_GEOMETRY — the screen and the "
        "window would then take their size from two different places.")
    for path in (BUILT, PULLED):
        for name, seg in sorted(display_services(path).items()):
            assert re.search(r"(?m)^      SENTINEL_VNC_GEOMETRY:", seg), (
                f"{path.name}: `{name}` does not pass SENTINEL_VNC_GEOMETRY, so the X server and the "
                f"browser window each fall back to their own default and stop matching.")


def test_the_healthcheck_opens_the_socket_x11vnc_actually_serves():
    """Two ends again: the socket the entrypoint serves and the socket the healthcheck connects to.

    Both are read from their own file and COMPARED. Repeating the path in this test would assert that
    the file says what it says — and would stay green while the two halves disagreed, which is the only
    interesting failure.
    """
    ep = ENTRYPOINT.read_text()
    m = re.search(r'(?m)^:\s*"\$\{SENTINEL_VNC_SOCK:=([^}]+)\}"', ep)
    assert m, "vnc-entrypoint.sh no longer defaults SENTINEL_VNC_SOCK in a readable shape"
    sock = m.group(1)
    assert re.search(r'-unixsock\s+"?\$\{?SENTINEL_VNC_SOCK', ep), (
        "x11vnc is started without -unixsock $SENTINEL_VNC_SOCK — the default above would then be a "
        "path nobody reads, and the healthcheck below would be opening a socket by coincidence.")
    for path in (BUILT, PULLED):
        for name, seg in sorted(display_services(path).items()):
            assert re.search(r"(?m)^    healthcheck:\s*$", seg), (
                f"{path.name}: `{name}` has no healthcheck, so nothing notices when the screen dies "
                f"while the browser service keeps answering.")
            assert f"connect('{sock}'" in seg, (
                f"{path.name}: `{name}`'s healthcheck does not connect to {sock}, which is the socket "
                f"vnc-entrypoint.sh serves. Then the check passes on a container whose X server or VNC "
                f"server is gone — exactly the state where the live view shows black.")
            # ⚠ CONNECT, not stat. A stale socket FILE outlives the process that created it, so
            # "the path exists" would report health for a dead server — the difference between a check
            # about the world and a check about a directory entry.
            assert "existsSync" not in seg and "statSync" not in seg, (
                f"{path.name}: `{name}`'s healthcheck tests for the socket's EXISTENCE rather than "
                f"connecting to it; a stale socket file would then read as a healthy screen.")


def test_the_vnc_server_has_no_tcp_listener_at_all():
    """`-rfbport 0` is what turns "a socket as well" into "a socket INSTEAD".

    Measured 2026-08-18: with `-unixsock` alone x11vnc still opens 5900, so the socket was an addition
    and the TCP surface remained. With `-rfbport 0` /proc/net/tcp is empty. This is the flag that makes
    the whole transport claim true, so it is asserted rather than trusted.
    """
    ep = ENTRYPOINT.read_text()
    joined = re.sub(r"\\\n\s*", " ", ep)
    for line in joined.splitlines():
        code = line.split("#", 1)[0]
        if "x11vnc" in code and "-unixsock" in code:
            assert "-rfbport 0" in code, (
                f"x11vnc serves a unix socket but does not disable the TCP listener: {code.strip()[:110]}. "
                f"Measured: without `-rfbport 0` it also opens 5900, and the port is reachable from the "
                f"host at the container's bridge IP — so 'not published' would not mean 'not reachable'.")
            return
    raise AssertionError("no x11vnc invocation with -unixsock found — the transport is not what this file gates")


def test_the_socket_is_narrowed_and_no_password_scheme_creeps_back():
    """Access control is the socket's MODE, so the chmod is the security-critical line of the file.

    And the old scheme must not creep back: `-passwdfile`/`-rfbauth` would re-introduce VNC
    Authentication, i.e. DES over eight bytes of a password — the thing this transport was rebuilt to
    remove after CodeQL flagged it and the owner ruled that weak ciphers do not ship.
    """
    ep = ENTRYPOINT.read_text()

    assert re.search(r'chmod 600 "\$SENTINEL_VNC_SOCK"', ep), (
        "vnc-entrypoint.sh does not chmod 600 the socket. Its permissions ARE the authentication — "
        "measured: a foreign uid gets EACCES at 0600 — so an unnarrowed socket is an unguarded desktop.")

    # The old password scheme must not return through any shipped file.
    #
    # ⚠ COMMENTS ARE STRIPPED FIRST, and that is not a nicety — it is the second time in this PR that
    # a substring check fired on the PROSE EXPLAINING THE RULE. vnc-entrypoint.sh says "there is no
    # `-nopw` in this file and there will not be one", which is exactly the sentence a reader needs
    # and exactly what a naive search flags. A gate that fires on legitimate content gets an exception
    # carved into it, and the exception is how it stops applying to the case it was written for.
    for path in (ENTRYPOINT, BUILT, PULLED, REPO / "Dockerfile"):
        code = "\n".join(ln.split("#", 1)[0] for ln in path.read_text().splitlines())
        for flag in ("-passwdfile", "-rfbauth"):
            assert flag not in code, (
                f"{path.name} passes `{flag}` outside a comment, which turns VNC Authentication back "
                f"on — DES over the first eight bytes of a password, over an unencrypted channel. This "
                f"transport authenticates with the socket's 0600 permissions instead, and ships no "
                f"weak cipher.")

    assert re.search(r"(?m)^set -eu?\b", ep), (
        "vnc-entrypoint.sh does not `set -e`, so a failing step would be a printed warning and the "
        "server would start anyway — the difference between 'impossible by construction' and "
        "'unlikely'.")

    # The chmod must come BEFORE the container is considered up. Compared by position, because the
    # order is the mechanism: a socket served for even a second at 0755 is a socket anyone could have
    # connected to.
    chmod_at = ep.find('chmod 600 "$SENTINEL_VNC_SOCK"')
    exec_at = ep.find('exec "$@"')
    assert chmod_at != -1 and exec_at != -1 and chmod_at < exec_at, (
        "the socket is narrowed after the entrypoint hands over to the browser service, so there is a "
        "window in which it is world-reachable.")


def test_the_x_server_has_no_network_socket():
    """-nolisten tcp is what makes 'the display is reachable only from this container' true."""
    ep = ENTRYPOINT.read_text()
    m = re.search(r"(?m)^Xvfb .*$", ep)
    assert m, "vnc-entrypoint.sh no longer starts Xvfb in a readable shape"
    assert "-nolisten tcp" in m.group(0), (
        "Xvfb is started without `-nolisten tcp`, so the X server opens a TCP socket. X11 has no "
        "meaningful authentication here, and that socket would be a second, unguarded way into the "
        "same display the VNC password is protecting.")


def test_a_service_with_a_display_reaps_its_children():
    """`init: true` — without it the exec'd node process is pid 1 and never reaps Xvfb or x11vnc."""
    for path in (BUILT, PULLED):
        for name, seg in sorted(display_services(path).items()):
            assert re.search(r"(?m)^    init:\s*true\s*$", seg), (
                f"{path.name}: `{name}` runs three processes and does not set `init: true`. The "
                f"browser service becomes pid 1 by way of exec, and pid 1 does not reap children it "
                f"did not spawn — the container accumulates zombies for as long as it runs.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} vnc-profile tests passed")
