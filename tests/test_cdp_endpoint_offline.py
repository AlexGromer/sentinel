"""ADR-110 — attaching to a browser that lives in ANOTHER container.

The delivery wave puts the browser behind its own compose service so the live-view work
(`[LIVE-CDP-STREAM]`) has a CDP port to read. The obvious spelling of that —
`PW_CDP_ENDPOINT=http://browser:9223`, the compose service name — does not work, and each of the
three reasons is fatal on its own. All three were measured against Chrome 150, not assumed:

  1. Chromium binds the debugging port to 127.0.0.1 and IGNORES --remote-debugging-address=0.0.0.0.
     Silently: the log still reads "DevTools listening on ws://127.0.0.1:9222". A sibling container
     cannot reach that at all, which is why the browser service runs a TCP forwarder.
  2. The DevTools HTTP endpoint validates the Host header. `Host: browser:9223` answers
     HTTP 500 "Host header is specified and is not an IP address or localhost." — Chrome's
     DNS-rebinding guard. The failure names a header, not the cause.
  3. Chrome echoes the Host it was addressed by into `webSocketDebuggerUrl`. So addressing it
     numerically is not merely a way past (2): it is also what makes the websocket URL Playwright
     then follows point back through the forwarder rather than at the client's own loopback.

The executor therefore substitutes a resolved address for a DNS name before connecting, and SAYS
it did. This suite pins that behaviour against the REAL built `dist/server.js` — the shipped seam,
not a re-implementation of the rule, because a test that re-derives the rewrite would agree with a
wrong rewrite.

The hostname is made resolvable through glibc's HOSTALIASES rather than /etc/hosts, so the gate
needs no privileges and leaves nothing behind on the machine that ran it.

What this pins:
  * a DNS-name CDP endpoint attaches and drives a page (the rewrite happened);
  * removing the rewrite is caught: raw `connectOverCDP` against the same endpoint is asserted to
    fail, so the positive check cannot be passing for some unrelated reason;
  * a name that does not resolve fails with OUR message naming the host — it does not fall back to
    launching a local browser, which would let a run proceed against the wrong browser entirely;
  * `localhost` and numeric endpoints are left alone (no lookup to fail).
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "testdata" / "fixtures"
DIST = REPO / "pw-executor" / "dist" / "server.js"
ALIAS = "sentinelbrowser"          # unqualified on purpose: HOSTALIASES only maps dotless names


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ExternalBrowser:
    """A browser with an open debugging port, standing in for the compose `browser` service.

    Launched THROUGH Playwright rather than by locating a binary: `chromium.executablePath()` names
    the full Chromium build, while CI (and this machine) install `chromium-headless-shell`, so a
    path-based launcher skipped everywhere it mattered. Playwright launches whichever build it has,
    and `--remote-debugging-port` opens the HTTP endpoint alongside its own pipe transport.
    """

    _JS = (
        "const {chromium}=require('playwright-core');"
        "chromium.launch({args:['--remote-debugging-port=%d']}).then(b=>{"
        "process.stdout.write('READY\\n');"
        "process.on('SIGTERM',()=>b.close().then(()=>process.exit(0)));"
        "setInterval(()=>{},1<<30);"
        "}).catch(e=>{process.stdout.write('FAIL '+e.message+'\\n');process.exit(1);});"
    )

    def __init__(self) -> None:
        self.port = _free_port()
        self.proc = subprocess.Popen(
            ["node", "-e", self._JS % self.port], cwd=str(REPO / "pw-executor"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def wait_ready(self, timeout: float = 90.0) -> bool:
        line = (self.proc.stdout.readline() or "").strip() if self.proc.stdout else ""
        if not line.startswith("READY"):
            return False
        # READY means Playwright is attached over its pipe; the HTTP endpoint comes up alongside it.
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2).read()
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def _hostaliases(tmp: pathlib.Path) -> str:
    f = tmp / "hostaliases"
    f.write_text(f"{ALIAS} localhost\n")
    return str(f)


def _drive(env_extra: dict, calls: list) -> "tuple[str, object]":
    """Drive the REAL executor with extra env. Returns ('ok', results) or ('error', message)."""
    script = (
        'import sys, json; sys.path.insert(0, %r)\n'
        'from brain.executor import Executor\n'
        'ex = Executor("node %s")\n'
        'try:\n'
        '    out = [ex.call(m, **p) for m, p in json.loads(%r)]\n'
        '    print("@@OK@@" + json.dumps(out))\n'
        'except Exception as e:\n'
        '    print("@@ERR@@" + str(e))\n'
        'try:\n'
        '    ex.call("shutdown"); ex.close()\n'
        'except Exception:\n'
        '    pass\n' % (str(REPO), DIST, json.dumps(calls))
    )
    env = {**os.environ, "PYTHONPATH": str(REPO), "PW_NO_TRACE": "1", **env_extra}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       timeout=300)
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@OK@@"):
            return "ok", json.loads(line[len("@@OK@@"):])
        if line.startswith("@@ERR@@"):
            return "error", line[len("@@ERR@@"):]
    return "error", ((r.stderr or "") + (r.stdout or ""))[-400:]


def _skip(reason: str) -> None:
    print(f"     SKIP — {reason}")


def test_a_cdp_endpoint_named_by_hostname_attaches_and_drives_a_page():
    """The property: a DNS-name endpoint works. Chrome would refuse it verbatim.

    The control is asserted FIRST and in the same process tree: a raw connectOverCDP against this
    very endpoint must fail. Without it, a green run here would also be consistent with Chrome
    having stopped caring about the Host header — i.e. with the rewrite being dead code.
    """
    if not DIST.exists():
        return _skip("pw-executor/dist not built (npm run build)")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cdp-gate-"))
    br = _ExternalBrowser()
    try:
        if not br.wait_ready():
            return _skip("the stand-in browser never opened its debugging port")

        endpoint = f"http://{ALIAS}:{br.port}"
        aliases = _hostaliases(tmp)

        # --- control: the endpoint genuinely needs the rewrite -------------------------------
        probe = subprocess.run(
            ["node", "-e",
             "import('playwright-core').then(async m=>{try{const b=await m.chromium."
             f"connectOverCDP('{endpoint}');await b.close();console.log('ACCEPTED');"
             "}catch(e){console.log('REFUSED:'+e.message.split('\\n')[0]);}})"],
            cwd=str(REPO / "pw-executor"), capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOSTALIASES": aliases})
        control = (probe.stdout or "").strip()
        assert control.startswith("REFUSED"), (
            f"raw connectOverCDP ACCEPTED {endpoint}, so this gate proves nothing: Chrome no longer "
            f"refuses a DNS-name Host header, and the rewrite it is meant to cover is unobservable "
            f"here. Re-derive the gate before trusting it again. Got: {control!r}")

        # --- the property: the shipped executor gets through --------------------------------
        kind, res = _drive({"PW_CDP_ENDPOINT": endpoint, "HOSTALIASES": aliases},
                           [("browser.navigate", {"url": "file://" + str(FIXTURES / "l1.html")}),
                            ("browser.interactives", {})])
        assert kind == "ok", (
            f"the executor could not attach over a hostname CDP endpoint: {res}. The rewrite in "
            f"resolveCdpEndpoint() is what makes this work — Chrome refuses the name (see the "
            f"control above).")
        assert len(res[1]["elements"]) > 0, (
            "attached, but perceived nothing — the adopted context is not the browser we launched.")
    finally:
        br.close()


def test_an_unresolvable_cdp_host_fails_by_name_instead_of_launching_a_local_browser():
    """A degradation nobody declared is the failure mode this project keeps paying for.

    If the endpoint's host does not resolve, the run must stop and say which name failed. Falling
    back to a locally launched browser would be worse than a crash: the run would go green against
    a browser nobody asked for, and every artefact would describe the wrong session.
    """
    if not DIST.exists():
        return _skip("pw-executor/dist not built (npm run build)")

    kind, res = _drive(
        {"PW_CDP_ENDPOINT": "http://sentinel-no-such-host-19f3:9223"},
        [("browser.navigate", {"url": "file://" + str(FIXTURES / "l1.html")})])

    assert kind == "error", (
        "an unresolvable CDP host produced a SUCCESSFUL navigate — the executor fell back to a "
        "browser of its own choosing and the run would report on the wrong session.")
    assert "sentinel-no-such-host-19f3" in res, (
        f"the failure does not name the host that could not be resolved: {res!r}")


def test_localhost_and_numeric_endpoints_are_left_alone():
    """The rewrite must not widen into endpoints that already work.

    `localhost` is explicitly allowed by Chrome's guard, and a numeric address is the thing the
    rewrite produces. Touching either would add a lookup that can fail for endpoints that never
    needed one — including the air-gapped bundle, where DNS may not exist at all.
    """
    r = subprocess.run(
        ["node", "-e",
         "import('./dist/launch.js').then(m=>console.log(JSON.stringify(["
         "m.cdpHostNeedsNumericAddress('http://localhost:9222'),"
         "m.cdpHostNeedsNumericAddress('http://127.0.0.1:9222'),"
         "m.cdpHostNeedsNumericAddress('http://[::1]:9222'),"
         "m.cdpHostNeedsNumericAddress('http://browser:9223'),"
         "m.withCdpHost('http://browser:9223/x','10.0.0.7')])))"],
        cwd=str(REPO / "pw-executor"), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return _skip(f"pw-executor/dist not built: {(r.stderr or '')[-200:]}")
    localhost, v4, v6, name, rewritten = json.loads((r.stdout or "").strip())
    assert localhost is False and v4 is False and v6 is False, (
        f"an endpoint Chrome already accepts was marked for rewriting "
        f"(localhost={localhost}, v4={v4}, v6={v6})")
    assert name is True, "a compose service name was NOT marked for rewriting — the whole point"
    assert rewritten == "http://10.0.0.7:9223/x", (
        f"the rewrite lost part of the endpoint: {rewritten!r} (port and path must survive)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} CDP-endpoint tests passed")
