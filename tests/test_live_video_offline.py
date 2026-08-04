"""ADR-111 — the live VIDEO mode, end to end through the browser service.

The live area has claimed three modes since ADR-108d and shipped two. The third rendered a paragraph
saying it was not built — honest, and useless. What blocked it was never the browser: the executor
has carried screencast tools all along. It was the absence of a CHANNEL — the executor runs inside
the brain's process on stdio, so control-api has no address for it and never will.

ADR-110 made the browser a service, which inverts the problem: the process holding the browser is
long-lived and already listening, so it serves its own screencast and control-api puts a credential
in front of it.

Every check here drives the REAL cdp-service and the REAL control-api binary, because the two defects
this path actually had were both invisible to any test that stubbed either end:

  * `chromium.launch()` returns a Browser that only tracks contexts created through ITS OWN
    connection. A run attaches over CDP as a separate client, so the launched handle reported
    `contexts() == []` for the whole run. A second, ADOPTING connection (connectOverCDP) is what an
    observer needs. It DOES track pages created after it connects — measured, after a mutation showed
    the "reconnect if empty" workaround was unkillable and the workaround turned out to be the thing
    causing the symptom it was written for. Opening the live view BEFORE starting a run — the obvious
    order — is still the order this suite uses, because that is the order that exposed all of it.
  * the status route swallowed its lookup error, and answered has_page:false during a run whose very
    next request returned a frame. "Could not look" and "looked and found nothing" cannot share a
    field.

What this pins:
  * a hostname/port with no browser service answers `available:false` WITH A REASON, not an error —
    the single-container deployment is normal, not broken;
  * the live routes require a credential (they proxy an endpoint that has none of its own);
  * a frame arrives even when the page is IDLE (measured: a screencast emits on repaint, so an idle
    page yields ~1 frame per 4s — the first frame is seeded with an explicit capture);
  * the MJPEG endpoint delivers MULTIPLE frames while a real run drives the browser;
  * status reports the page even when it was asked before the run existed.
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
CDP_SERVICE = REPO / "pw-executor" / "dist" / "cdp-service.js"
CONTROL_API = REPO / "bin" / "control-api"
AGENTCTL = REPO / "bin" / "agentctl"
TOKEN = "live-video-gate-token"
# How long a run may take to attach to the browser service and open its first page. ONE constant for
# every wait on that one condition: the two waits in the streaming test used to be 12s and 60s, and
# the shorter one failed the suite under load while the longer one was still patiently succeeding.
PAGE_WAIT_S = 60


def _skip(reason: str) -> None:
    print(f"     SKIP — {reason}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _get(url: str, token: "str | None" = None, timeout: float = 20.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class _Stack:
    """cdp-service + control-api, wired to each other, both real binaries."""

    def __init__(self) -> None:
        self.cdp_port = _free_port()
        self.live_port = _free_port()
        self.api_port = _free_port()
        env = {**os.environ,
               "CDP_LISTEN_PORT": str(self.cdp_port),
               "CDP_LIVE_PORT": str(self.live_port)}
        self.svc = subprocess.Popen(["node", str(CDP_SERVICE)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.api = None

    def wait_service(self, timeout: float = 120.0) -> bool:
        line = (self.svc.stdout.readline() or "").strip() if self.svc.stdout else ""
        if not line.startswith("CDP_SERVICE_READY"):
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                code, _ = _get(f"http://127.0.0.1:{self.live_port}/live/status", timeout=3)
                if code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def start_api(self, live_base: "str | None") -> bool:
        env = {**os.environ,
               "CONTROL_API_ADDR": f"127.0.0.1:{self.api_port}",
               "CONTROL_API_TOKEN": TOKEN,
               "CONTROL_API_AGENTCTL": str(AGENTCTL)}
        env["CONTROL_API_CDP_LIVE"] = live_base or ""
        self.api = subprocess.Popen([str(CONTROL_API)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                code, _ = _get(f"http://127.0.0.1:{self.api_port}/healthz", timeout=3)
                if code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def run_explore(self, artifact_dir: str) -> subprocess.Popen:
        env = {**os.environ,
               "PW_CDP_ENDPOINT": f"http://127.0.0.1:{self.cdp_port}",
               "PW_NO_TRACE": "1",
               "BRAIN_PYTHON": str(REPO / ".venv" / "bin" / "python")}
        return subprocess.Popen(
            [str(AGENTCTL), "run", "--target", f"file://{REPO}/testdata/site/index.html",
             "--planner", "heuristic", "--artifact-dir", artifact_dir],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self) -> None:
        for p in (self.api, self.svc):
            if p:
                p.kill()
                try:
                    p.wait(timeout=15)
                except Exception:
                    pass


def _prereq() -> "str | None":
    if not CDP_SERVICE.exists():
        return "pw-executor/dist not built (npm run build)"
    for b in (CONTROL_API, AGENTCTL):
        if not b.exists():
            return f"{b.name} not built (go build ./cmd/...)"
    if not shutil.which("node"):
        return "node not on PATH"
    return None


def test_a_deployment_without_a_browser_service_says_so_rather_than_failing():
    """The single-container deployment is the normal one. It must not look broken.

    `available:false` with a REASON, at HTTP 200 — because "what is the state of the live view" has
    a true answer here, and it is not an error. A 5xx would send someone hunting a fault that does
    not exist, which is precisely the confusion this whole ADR is about removing.
    """
    why = _prereq()
    if why:
        return _skip(why)
    st = _Stack()
    try:
        if not st.start_api(None):          # deliberately NO CONTROL_API_CDP_LIVE
            return _skip("control-api did not start")
        code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/status", TOKEN)
        assert code == 200, f"status answered HTTP {code} with no browser service — it must answer"
        doc = json.loads(body)
        assert doc.get("available") is False, f"claimed availability with no service configured: {doc}"
        assert doc.get("reason"), "said unavailable without saying why — the reason IS the feature"

        # The stream route, by contrast, is 501: the route exists, this deployment does not implement
        # it. A 404 would read as a wrong URL and send the reader looking for a typo.
        code, _ = _get(f"http://127.0.0.1:{st.api_port}/v1/live/mjpeg", TOKEN)
        assert code == 501, f"expected 501 Not Implemented with no browser service, got {code}"
    finally:
        st.close()


def test_an_idle_page_answers_with_a_frame_immediately():
    """The case the seeded first frame exists for, and the one a busy run hides.

    Measured: a CDP screencast emits on REPAINT, not on a timer — an idle page produced 1 frame in
    4 seconds where an active one produced 17. So opening the live view on a run that is between
    steps, paused, or simply looking at a static page would show an empty box until something moved,
    and "empty" is indistinguishable from "broken".

    The assertion is on LATENCY, not on eventual arrival: without the seed a frame does eventually
    turn up when the page happens to repaint, so a test that merely waited would pass either way.
    A seeded capture answers at once.
    """
    why = _prereq()
    if why:
        return _skip(why)
    st = _Stack()
    ex = None
    try:
        if not st.wait_service():
            return _skip("the browser service never came up")
        if not st.start_api(f"http://127.0.0.1:{st.live_port}"):
            return _skip("control-api did not start")

        # Open a page and then leave it completely alone. The executor is held open so the page is
        # not closed, but nothing touches it — this is a genuinely idle browser.
        ex = subprocess.Popen(
            [str(REPO / ".venv" / "bin" / "python"), "-c",
             "import sys, time; sys.path.insert(0, %r)\n"
             "from brain.executor import Executor\n"
             "ex = Executor('node %s')\n"
             "ex.call('browser.navigate', url='file://%s/testdata/fixtures/l1.html')\n"
             "print('READY', flush=True)\n"
             "time.sleep(60)\n" % (str(REPO), REPO / "pw-executor" / "dist" / "server.js", REPO)],
            env={**os.environ, "PYTHONPATH": str(REPO),
                 "PW_CDP_ENDPOINT": f"http://127.0.0.1:{st.cdp_port}", "PW_NO_TRACE": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = ex.stdout.readline() if ex.stdout else ""
        if not line.startswith("READY"):
            return _skip("could not open an idle page")
        time.sleep(5)   # let every initial paint finish; from here nothing repaints

        t0 = time.time()
        code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/frame.jpg", TOKEN, timeout=30)
        took = time.time() - t0
        assert code == 200, f"an idle page answered {code}: {body[:200]!r}"
        assert body[:3] == b"\xff\xd8\xff", "the idle-page frame is not a JPEG"
        assert took < 2.0, (
            f"the first frame of an IDLE page took {took:.1f}s. It is seeded with an explicit "
            f"capture precisely so it does not have to wait for a repaint that may never come.")
    finally:
        if ex:
            ex.kill()
            try:
                ex.wait(timeout=15)
            except Exception:
                pass
        st.close()


def test_the_live_routes_require_a_credential():
    """They proxy an endpoint that has NONE of its own.

    The browser service's live port is unauthenticated for the same reason its CDP port is: it lives
    on an internal network and that is the whole control. This route is where the credential is, and
    a screencast shows whatever the browser has open — including an application someone is logged
    into.
    """
    why = _prereq()
    if why:
        return _skip(why)
    st = _Stack()
    try:
        if not st.start_api("http://127.0.0.1:1"):   # never answers; auth must be decided first
            return _skip("control-api did not start")
        for path in ("/v1/live/status", "/v1/live/frame.jpg", "/v1/live/mjpeg"):
            code, _ = _get(f"http://127.0.0.1:{st.api_port}{path}")
            assert code in (401, 403), (
                f"{path} answered {code} without a credential — it proxies an unauthenticated "
                f"endpoint, so this route IS the authentication")
    finally:
        st.close()


def test_a_frame_and_a_stream_arrive_while_a_run_drives_the_browser():
    """The load-bearing check, in the order that used to fail.

    Status is asked BEFORE the run exists on purpose: that is what made the observer adopt an empty
    browser and stay blind. Then a real explore drives the service's browser, and the same endpoints
    must produce a JPEG and a multi-frame stream.
    """
    why = _prereq()
    if why:
        return _skip(why)
    st = _Stack()
    proc = None
    try:
        if not st.wait_service():
            return _skip("the browser service never came up")
        if not st.start_api(f"http://127.0.0.1:{st.live_port}"):
            return _skip("control-api did not start")

        # The poisoning order: look before there is anything to see.
        code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/status", TOKEN)
        assert code == 200, f"status HTTP {code}"
        assert json.loads(body).get("available") is True, "a configured browser service reported unavailable"

        out = tempfile.mkdtemp(prefix="live-video-gate-")
        proc = st.run_explore(out)

        # Collect the stream IN PARALLEL with the run, not after it. A live view is only live while
        # something is happening: an explore of this fixture finishes in about ten seconds, and by
        # then the page is closed and the screencast has stopped — so a sequential test measured the
        # quiet after the run and concluded the stream was empty.
        collected: "list[bytes]" = []

        def collect() -> None:
            # Retry while the answer is 503 "no page yet": the run needs a moment to attach and open
            # one, and a viewer who opened the live view first is the ordinary case, not an error.
            # This mirrors what the UI does rather than giving the test an easier path than a person.
            #
            # The budget is the SAME 60s the main thread gives the identical condition below. It was 12s
            # against the main thread's 60s — two waits for one state with different patience — so on a
            # loaded machine this thread gave up at 12s, the main thread waited happily to 60s, and the
            # run failed with "the stream never became available (503 throughout)". That reads as a
            # broken product; it was an impatient test. Measured on this repo 2026-08-04: fails under
            # concurrent suite load, passes in isolation.
            open_deadline = time.time() + PAGE_WAIT_S
            r = None
            while time.time() < open_deadline and r is None:
                req = urllib.request.Request(f"http://127.0.0.1:{st.api_port}/v1/live/mjpeg")
                req.add_header("Authorization", "Bearer " + TOKEN)
                try:
                    r = urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as e:
                    if e.code != 503:
                        collected.append(f"ERR:{e}".encode())
                        return
                    time.sleep(0.5)
                except Exception as e:
                    collected.append(f"ERR:{e}".encode())
                    return
            if r is None:
                collected.append(b"ERR:the stream never became available (503 throughout)")
                return
            deadline = time.time() + 14
            try:
                with r:
                    collected.append(("CT:" + r.headers.get("Content-Type", "")).encode())
                    while time.time() < deadline:
                        try:
                            b = r.read(16384)
                        except (TimeoutError, socket.timeout):
                            break
                        if not b:
                            break
                        collected.append(b)
            except Exception as e:                      # reported, not swallowed
                collected.append(f"ERR:{e}".encode())

        streamer = threading.Thread(target=collect, daemon=True)
        streamer.start()

        # WAIT FOR THE STATE, not for a guessed number of seconds. This slept 6s and measured a page
        # that appears at about 6s — a bet, and one that loses on a loaded machine or a slower
        # runner. The project has paid for that shape before (CI-FLAKE-HUB: two guessed sleeps of 25
        # and 22 seconds). The ceiling is generous and, when it is reached, says plainly that the run
        # never opened a page rather than blaming the thing being tested.
        deadline, doc, up = time.time() + PAGE_WAIT_S, {}, {}
        while time.time() < deadline:
            code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/status", TOKEN)
            doc = json.loads(body)
            up = doc.get("upstream") or {}
            if up.get("has_page"):
                break
            time.sleep(0.5)
        assert not up.get("error"), f"the page lookup failed and said so: {up.get('error')}"
        assert up.get("has_page") is True, (
            f"no page seen while a run was driving the browser: {doc}. The observer connection "
            f"adopts what exists AT CONNECT TIME, so a status call before the run must not poison it.")

        code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/frame.jpg", TOKEN, timeout=30)
        assert code == 200, f"frame.jpg answered {code}: {body[:200]!r}"
        assert body[:3] == b"\xff\xd8\xff", "the frame is not a JPEG"
        assert len(body) > 1000, f"the frame is {len(body)} bytes — too small to be a screenshot"

        # The stream. Read with a deadline rather than to EOF: it is open-ended by design.
        req = urllib.request.Request(f"http://127.0.0.1:{st.api_port}/v1/live/mjpeg")
        req.add_header("Authorization", "Bearer " + TOKEN)
        streamer.join(timeout=25)
        blob = b"".join(collected)
        assert b"multipart/x-mixed-replace" in blob, (
            f"the stream is not multipart: {blob[:120]!r}")
        frames = blob.count(b"--sentinelframe")
        assert frames >= 5, (
            f"only {frames} frame(s) in {len(blob)} bytes while a run was driving the browser. The "
            f"threshold is 5 because it was 2 and a mutation walked through it: the seeded first "
            f"frame plus ONE unacked screencast frame reaches 2, and without the ack Chromium sends "
            f"exactly one. A real explore of this fixture delivers 10-11.")
    finally:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
        st.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} live-video tests passed")
