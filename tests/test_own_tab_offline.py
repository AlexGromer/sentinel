#!/usr/bin/env python3
"""ADR-128 (LIVE-OWN-TAB) — what a run owns inside a browser it did not launch.

Run:  .venv/bin/python tests/test_own_tab_offline.py

WHAT THIS COVERS THAT NOTHING ELSE DID. `browser.tabs` and `browser.switchTab` (M9.4 A6) shipped
with no test of any kind — measured while writing this, by grepping the whole tree for either name
outside the executor itself. They were the surface ADR-128 changed most: until it, the run adopted
`pages()[0]`, so in CDP-attach mode "the run's tabs" began as *the human's* tabs, and `switchTab`
could point the run at one of them.

WHY IT DRIVES THE EXECUTOR OVER JSON-RPC RATHER THAN RUNNING `agentctl`. The property is about which
pages the executor takes into `pages[]`, and an explore run never opens a popup — it clicks what the
planner picks. Driving the RPC directly is what makes "click the thing that opens a tab" a step this
check can take, and it is the same transport the brain uses, not a test-only door.

⚠ tests/test_live_video_offline.py covers the neighbouring half — two concurrent runs each getting
their own picture — through `agentctl`. The two are not redundant: that one proves runs are separable
from the OUTSIDE (the live view), this one proves the executor's own bookkeeping from the INSIDE.
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
CDP_SERVICE = REPO / "pw-executor" / "dist" / "cdp-service.js"
EXECUTOR = REPO / "pw-executor" / "dist" / "server.js"
FIXTURE = REPO / "testdata" / "fixtures" / "l6-newtab.html"
BYSTANDER = REPO / "testdata" / "site-v2" / "index.html"

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def _skip(reason: str) -> None:
    print(f"     SKIP — {reason}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _prereq() -> "str | None":
    for f in (CDP_SERVICE, EXECUTOR):
        if not f.exists():
            return f"{f.name} not built (cd pw-executor && npm run build)"
    if not shutil.which("node"):
        return "node not on PATH"
    return None


class _Service:
    """A real browser service — the deployment shape ADR-110 introduced and ADR-128 is about."""

    def __init__(self) -> None:
        self.cdp_port = _free_port()
        self.live_port = _free_port()
        env = {**os.environ,
               "CDP_LISTEN_PORT": str(self.cdp_port),
               "CDP_LIVE_PORT": str(self.live_port),
               # Its journal must not land in the repository's real state/ during a test run.
               "SENTINEL_STATE_DIR": os.path.join(os.environ.get("TMPDIR", "/tmp"), "own-tab-gate-state")}
        self.proc = subprocess.Popen(["node", str(CDP_SERVICE)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def ready(self, timeout: float = 120.0) -> bool:
        line = (self.proc.stdout.readline() or "").strip() if self.proc.stdout else ""
        if not line.startswith("CDP_SERVICE_READY"):
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.live_port}/live/status", timeout=3) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def tabs(self) -> "list[str]":
        """The tabs the BROWSER really holds — the ground truth `browser.tabs` is checked against."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=10) as r:
                return [t["url"] for t in json.loads(r.read()) if t.get("type") == "page"]
        except Exception:
            return []

    def open_bystander(self, url: str) -> bool:
        """A tab nobody's run opened. Chrome wants PUT since 111; GET is the older spelling."""
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.cdp_port}/json/new?{url}", method=method)
                with urllib.request.urlopen(req, timeout=10):
                    return True
            except Exception:
                continue
        return False

    def close(self) -> None:
        self.proc.kill()
        try:
            self.proc.wait(timeout=15)
        except Exception:
            pass


class _Executor:
    """The real dist/server.js over its real stdio JSON-RPC transport."""

    def __init__(self, cdp_port: int) -> None:
        env = {**os.environ, "PW_CDP_ENDPOINT": f"http://127.0.0.1:{cdp_port}", "PW_NO_TRACE": "1"}
        self.proc = subprocess.Popen(["node", str(EXECUTOR)], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.n = 0

    def call(self, method: str, params: "dict | None" = None, timeout: float = 60.0):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"the executor died during {method}: {self.proc.stderr.read()[-400:]}")
        return json.loads(line)

    def tab_urls(self) -> "list[str]":
        return [t["url"] for t in self.call("browser.tabs")["result"]["tabs"]]

    def shutdown(self) -> None:
        try:
            self.call("shutdown")
            self.proc.wait(timeout=40)
        except Exception:
            self.proc.kill()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


def _await(predicate, timeout: float = 20.0) -> bool:
    """Wait on STATE, never on a guessed sleep — a page event and an RPC reply race by design."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return False


def test_a_run_opens_its_own_page_and_leaves_the_tab_that_was_already_open_alone():
    """THE promise ADR-128 withdrew from ADR-037, stated as a property.

    ADR-037 said the run reuses the user's session AND their open tab. The session half stands; the
    tab half is gone, and this is what that means concretely: a tab that was open before the run
    started is neither driven by it nor visible to `browser.switchTab`."""
    why = _prereq()
    if why:
        return _skip(why)
    svc, ex = _Service(), None
    try:
        if not svc.ready():
            return _skip("cdp-service did not come up")
        assert svc.open_bystander(f"file://{BYSTANDER}"), "could not open the pre-existing tab"
        assert _await(lambda: any("site-v2" in u for u in svc.tabs())), "the pre-existing tab never appeared"

        ex = _Executor(svc.cdp_port)
        ex.call("initialize")
        ex.call("browser.navigate", {"url": f"file://{FIXTURE}"})

        urls = ex.tab_urls()
        assert len(urls) == 1, f"the run took more than its own page into its tab list: {urls}"
        assert "l6-newtab.html" in urls[0], f"the run is driving a page it did not open: {urls[0]}"
        assert not any("site-v2" in u for u in urls), (
            "the tab that was already open is in the run's tab list — `browser.switchTab` can point "
            f"the run at somebody else's page, which is exactly what ADR-128 removes: {urls}")

        # And it really is a SECOND tab in the browser, not a navigation of the first one.
        real = svc.tabs()
        assert any("site-v2" in u for u in real), f"the run navigated the pre-existing tab away: {real}"
        assert any("l6-newtab.html" in u for u in real), f"the run's own page is not in the browser: {real}"
    finally:
        if ex:
            ex.shutdown()
        svc.close()


def test_the_run_tracks_the_popups_it_opened_and_hands_every_one_of_them_back():
    """Ownership has two halves, and the second one was missing at first.

    ⚠ MEASURED, and it is why this check exists in this shape: teardown originally closed only the
    page the run opened FIRST, so a run driving l6-newtab.html left its popup behind — the browser
    still held `l1.html` after shutdown. A run that opens three tabs would abandon three, into a
    service that outlives every run.

    ⚠ The popup is opened by `target=_blank rel="noopener"`, deliberately the strictest form: it
    severs `window.opener` in the DOM, and the ownership rule reads the TARGET's opener instead —
    measured to survive noopener, which is the fact the rule stands on."""
    why = _prereq()
    if why:
        return _skip(why)
    svc, ex = _Service(), None
    try:
        if not svc.ready():
            return _skip("cdp-service did not come up")
        assert svc.open_bystander(f"file://{BYSTANDER}"), "could not open the pre-existing tab"

        ex = _Executor(svc.cdp_port)
        ex.call("initialize")
        ex.call("browser.navigate", {"url": f"file://{FIXTURE}"})
        ex.call("browser.click", {"locator": {"css": "#ext"}})
        assert _await(lambda: len(ex.tab_urls()) == 2), (
            f"the popup this run's own page opened was not tracked: {ex.tab_urls()} — with it missing, "
            "`browser.switchTab` cannot reach the tab the application just put the user in")

        # A tab appearing DURING the run, opened by nobody's page: still not ours.
        assert svc.open_bystander(f"file://{REPO}/testdata/site/index.html"), "could not open the second tab"
        assert not _await(lambda: len(ex.tab_urls()) > 2, timeout=6), (
            f"a tab opened outside the run was adopted mid-run: {ex.tab_urls()}")

        before = svc.tabs()
        ex.shutdown()
        ex = None
        assert _await(lambda: len(svc.tabs()) == len(before) - 2, timeout=30), (
            f"the run did not hand back both of its pages: {before} -> {svc.tabs()}")
        after = svc.tabs()
        assert any("site-v2" in u for u in after), (
            f"the run closed a tab it did not open — that is the browser's lifetime, not the run's: {after}")
        assert not any("l6-newtab" in u or "l1.html" in u for u in after), (
            f"a page the run opened survived its teardown: {after}")
    finally:
        if ex:
            ex.kill()
        svc.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            fail(f"{fn.__name__}: {e}")
        else:
            print("  ok  ", fn.__name__)
    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print(f"OK — {len(fns)} own-tab tests passed")
