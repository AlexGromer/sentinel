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

    def run_explore(self, artifact_dir: str, run_id: "str | None" = None,
                    target: str = "testdata/site/index.html") -> subprocess.Popen:
        env = {**os.environ,
               "PW_CDP_ENDPOINT": f"http://127.0.0.1:{self.cdp_port}",
               "PW_NO_TRACE": "1",
               "BRAIN_PYTHON": str(REPO / ".venv" / "bin" / "python")}
        if run_id:
            # LIVE-PER-RUN. In compose the executor DERIVES the claim endpoint from PW_CDP_ENDPOINT
            # plus the default live port; here the ports are random, so the override exists and is
            # exercised — a derivation nobody can override is a derivation nobody can test.
            env["SENTINEL_RUN_ID"] = run_id
            env["PW_LIVE_CLAIM"] = f"http://127.0.0.1:{self.live_port}/live/claim"
        # ADR-128: the target is a PARAMETER because two concurrent runs have to be told apart by
        # what is on their screens. Same fixture twice would produce two frames that look alike, and
        # a check that cannot fail on a swapped picture is not checking the thing it is named for.
        return subprocess.Popen(
            [str(AGENTCTL), "run", "--target", f"file://{REPO}/{target}",
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


# ---------------------------------------------------------------------- LIVE-PER-RUN (ADR-111 follow-on)
#
# The defect these four exist for: the live view was a fact about the SERVICE. With two runs in the
# same browser it showed whichever page was created LAST, and the hub said so in prose because the
# topology could not say it in data. A picture of the wrong run is worse than no picture — it is
# indistinguishable from the right one.


def test_a_named_run_gets_its_own_page_not_the_newest():
    """THE defect, stated as a property: a second page must not steal the first run's picture."""
    why = _prereq()
    if why:
        return _skip(why)
    st, proc = _Stack(), None
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        with tempfile.TemporaryDirectory() as d:
            proc = st.run_explore(d, run_id="run-alpha")
            # Wait for the claim to land — on STATE, never on a guessed sleep.
            deadline = time.time() + 90
            claimed = None
            while time.time() < deadline:
                code, body = _get(f"http://127.0.0.1:{st.live_port}/live/status?run_id=run-alpha", timeout=5)
                if code == 200:
                    j = json.loads(body)
                    if j.get("has_page") and j.get("url"):
                        claimed = j
                        break
                time.sleep(0.4)
            assert claimed, "run-alpha never resolved to a page — the claim never reached the service"
            assert claimed["scoped"] is True, f"a NAMED request answered unscoped: {claimed}"
            assert claimed["run_id"] == "run-alpha"

            # A second page in the same browser: this is what used to hijack the picture.
            code, second = _get(f"http://127.0.0.1:{st.live_port}/live/status", timeout=5)
            unscoped = json.loads(second)
            assert unscoped["scoped"] is False, (
                "an UNNAMED request claimed to be scoped — that field is how a caller learns the "
                f"picture is 'the newest page' rather than 'your run': {unscoped}")

            # And the frame path agrees with status about whose page it is.
            code, _ = _get(f"http://127.0.0.1:{st.live_port}/live/frame.jpg?run_id=run-alpha", timeout=20)
            assert code == 200, f"the claimed run could not be photographed: HTTP {code}"
    finally:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
        st.close()


def test_an_unclaimed_run_is_refused_rather_than_answered_with_somebody_elses_page():
    """The decision this task exists for: refuse, never substitute.

    A run that named itself and cannot be resolved gets a 503 that SAYS which of the three cases it
    is. Answering it with the newest page would be the original defect wearing a run_id."""
    why = _prereq()
    if why:
        return _skip(why)
    st, proc = _Stack(), None
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        with tempfile.TemporaryDirectory() as d:
            # A real page EXISTS — that is the point. The refusal must not be "no page anywhere".
            proc = st.run_explore(d, run_id="run-present")
            deadline = time.time() + 90
            while time.time() < deadline:
                code, body = _get(f"http://127.0.0.1:{st.live_port}/live/status?run_id=run-present", timeout=5)
                if code == 200 and json.loads(body).get("has_page"):
                    break
                time.sleep(0.4)

            code, body = _get(f"http://127.0.0.1:{st.live_port}/live/status?run_id=run-ghost", timeout=5)
            j = json.loads(body)
            assert j["has_page"] is False, (
                "a run that never claimed a page was answered with one — that is showing somebody "
                f"else's picture under this run's name: {j}")
            assert j["scoped"] is True and j["run_id"] == "run-ghost"
            assert j["reason"] and "claim" in j["reason"], (
                f"the refusal does not say WHY, so a reader cannot tell 'never started' from "
                f"'already finished': {j}")

            code, body = _get(f"http://127.0.0.1:{st.live_port}/live/frame.jpg?run_id=run-ghost", timeout=10)
            assert code == 503, f"an unclaimed run got a picture: HTTP {code}"
    finally:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
        st.close()


def test_the_claim_endpoint_refuses_half_a_claim():
    """A claim missing either half is not a claim. Accepting one would map a run to nothing, and the
    live view would then refuse that run forever with the wrong reason."""
    why = _prereq()
    if why:
        return _skip(why)
    st = _Stack()
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        base = f"http://127.0.0.1:{st.live_port}/live/claim"
        for body, label in (({"run_id": "r"}, "no target_id"),
                            ({"target_id": "t"}, "no run_id"),
                            ({}, "empty")):
            req = urllib.request.Request(base, data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError(f"a claim with {label} was accepted")
            except urllib.error.HTTPError as e:
                assert e.code == 400, f"a claim with {label} answered HTTP {e.code}, want 400"
        # GET must not write: the only writer on this surface states its method rather than assuming.
        code, _ = _get(base, timeout=10)
        assert code == 405, f"GET /live/claim answered {code}, want 405"
    finally:
        st.close()


def test_control_api_carries_run_id_through_to_the_browser_service():
    """The proxy used to build its target from a LITERAL path, so `?run_id=` added by the hub or the
    CLI reached it and was dropped on the floor. Nothing failed — the answer was simply about a
    different page. Measured through the REAL control-api, because the defect lived in the proxy and
    a test of the service alone would pass over it."""
    why = _prereq()
    if why:
        return _skip(why)
    st, proc = _Stack(), None
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        if not st.start_api(f"http://127.0.0.1:{st.live_port}"):
            return _skip("control-api did not come up")
        with tempfile.TemporaryDirectory() as d:
            proc = st.run_explore(d, run_id="run-through-proxy")
            deadline = time.time() + 90
            seen = None
            while time.time() < deadline:
                code, body = _get(
                    f"http://127.0.0.1:{st.api_port}/v1/live/status?run_id=run-through-proxy",
                    token=TOKEN, timeout=5)
                if code == 200:
                    # control-api wraps the service document so a caller never infers availability
                    # from the shape of what came back.
                    j = (json.loads(body) or {}).get("upstream") or {}
                    if j.get("has_page"):
                        seen = j
                        break
                time.sleep(0.4)
            assert seen, "the run never resolved THROUGH control-api"
            assert seen.get("run_id") == "run-through-proxy" and seen.get("scoped") is True, (
                "control-api dropped run_id on the way to the browser service — the caller asked "
                f"about a run and was answered about the newest page: {seen}")

            # A ghost run must be refused THROUGH the proxy too, not just at the service.
            code, body = _get(f"http://127.0.0.1:{st.api_port}/v1/live/status?run_id=run-ghost",
                              token=TOKEN, timeout=5)
            assert (json.loads(body).get("upstream") or {})["has_page"] is False

            # The PICTURE says whose it is, and the proxy must carry that through. Found by mutation:
            # emptying the header list in proxyLive left every assertion above green, because they all
            # read the JSON document — a JPEG has nowhere to put a run id except a header, and a
            # caller that cannot check the header has to trust the routing it just asked about.
            req = urllib.request.Request(
                f"http://127.0.0.1:{st.api_port}/v1/live/frame.jpg?run_id=run-through-proxy",
                headers={"Authorization": "Bearer " + TOKEN})
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 200, f"the claimed run could not be photographed through the proxy: {resp.status}"
                assert resp.headers.get("X-Sentinel-Run") == "run-through-proxy", (
                    "the frame arrived without naming its run — the proxy ate the header, and the "
                    f"caller cannot tell whose picture it got: {dict(resp.headers)}")
                assert resp.headers.get("X-Sentinel-Scoped") == "true", (
                    f"the frame does not say it was scoped: {dict(resp.headers)}")
    finally:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
        st.close()

# ------------------------------------------------------------------------------ ADR-128 (LIVE-OWN-TAB)
#
# The remainder LIVE-PER-RUN could not reach: the two runs above are one real run plus a name that
# was never claimed, because until ADR-128 two REAL concurrent runs adopted the same tab and the
# service — correctly — refused them both. A run opens its own page now, so the case the whole live
# view exists for is finally expressible as a check.


def _get_with_headers(url: str, timeout: float = 20.0):
    """Like _get, but keeps the headers: whose frame this is cannot be read from a JPEG's body.

    ⚠ The headers object is returned AS IS rather than as a dict. The browser service writes
    `x-sentinel-run` in lower case and control-api forwards it in title case, so a plain dict makes
    this check depend on which of the two answered — measured here, by writing it the wrong way
    first. `http.client.HTTPMessage` matches case-insensitively, which is what HTTP actually means.
    """
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _await_page(live_port: int, run_id: str, timeout: float = PAGE_WAIT_S + 30):
    """Wait for a run to resolve to a page — on STATE, never on a guessed sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, body = _get(f"http://127.0.0.1:{live_port}/live/status?run_id={run_id}", timeout=5)
        if code == 200:
            j = json.loads(body)
            if j.get("has_page") and j.get("url"):
                return j
        time.sleep(0.4)
    return None


def test_two_concurrent_runs_each_get_their_own_page_and_their_own_picture():
    """THE property of ADR-128: two runs, two pages, two pictures, each answering for itself.

    Before it, both runs announced ONE targetId (measured live: 84DC6185) and `resolve` had to refuse
    them both — honest, and the live view worked for nobody whenever two runs overlapped."""
    why = _prereq()
    if why:
        return _skip(why)
    st, a, b = _Stack(), None, None
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
            # DIFFERENT fixtures on purpose — see run_explore. Same fixture twice would make the
            # frame comparison below pass even if both runs shared one page.
            a = st.run_explore(da, run_id="run-alpha", target="testdata/site/index.html")
            b = st.run_explore(db, run_id="run-beta", target="testdata/site-v2/index.html")
            ja = _await_page(st.live_port, "run-alpha")
            jb = _await_page(st.live_port, "run-beta")
            assert ja, "run-alpha never resolved to a page"
            assert jb, "run-beta never resolved to a page"
            assert ja["scoped"] is True and jb["scoped"] is True, f"a named request answered unscoped: {ja} {jb}"

            # Each run is shown ITS OWN fixture. This is the assertion that fails if the two runs
            # end up on one page: they would report the same URL, whichever it was.
            assert "/testdata/site/" in ja["url"], f"run-alpha is looking at somebody else's page: {ja['url']}"
            assert "/testdata/site-v2/" in jb["url"], f"run-beta is looking at somebody else's page: {jb['url']}"

            ca, fa, ha = _get_with_headers(f"http://127.0.0.1:{st.live_port}/live/frame.jpg?run_id=run-alpha")
            cb, fb, hb = _get_with_headers(f"http://127.0.0.1:{st.live_port}/live/frame.jpg?run_id=run-beta")
            assert ca == 200 and cb == 200, f"a claimed run could not be photographed: {ca} {cb}"
            assert ha.get("X-Sentinel-Run") == "run-alpha" and hb.get("X-Sentinel-Run") == "run-beta", (
                f"the frame does not say whose it is: {ha.get('X-Sentinel-Run')} {hb.get('X-Sentinel-Run')}")
            assert ha.get("X-Sentinel-Scoped") == "true" and hb.get("X-Sentinel-Scoped") == "true"
            # The pixels themselves differ. Byte equality here would mean one page served twice —
            # the defect, wearing two correct run ids.
            assert fa and fb and fa != fb, (
                f"both runs were served the SAME image ({len(fa)} vs {len(fb)} bytes) — the label is "
                "right and the picture is one, which is exactly what ADR-128 removes")
    finally:
        for p in (a, b):
            if p:
                p.kill()
                try:
                    p.wait(timeout=20)
                except Exception:
                    pass
        st.close()


def _open_bystander_tab(cdp_port: int, url: str) -> "str | None":
    """A tab nobody's run owns — the human's, in the topology ADR-128 exists for.

    Deliberately NOT a second run: a run has a lifetime of its own, and a check that depends on one
    still being alive races its own subject. This tab stays until something closes it, which is
    precisely the fact under test.
    """
    target = f"http://127.0.0.1:{cdp_port}/json/new?{url}"
    for method in ("PUT", "GET"):  # Chrome requires PUT since 111; GET is the older spelling
        try:
            req = urllib.request.Request(target, method=method)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()).get("id")
        except Exception:
            continue
    return None


def _tab_ids(cdp_port: int) -> "list[str]":
    code, body = _get(f"http://127.0.0.1:{cdp_port}/json/list", timeout=10)
    if code != 200:
        return []
    return [t["id"] for t in json.loads(body) if t.get("type") == "page"]


def test_a_run_that_is_stopped_hands_its_page_back_and_leaves_every_other_tab_alone():
    """The other half of owning a page: giving it back, on EVERY exit path — and only that one.

    A run stopped by a signal — a budget kill, `agentctl` cancel, `docker compose down` — must leave
    no tab behind, or the browser service accumulates one per run and the newest of them keeps
    answering unnamed requests on a finished run's behalf. It must also close NOTHING ELSE: the
    difference between owning a page and owning the browser is the entire ADR-037 guard."""
    why = _prereq()
    if why:
        return _skip(why)
    st, a = _Stack(), None
    try:
        if not st.wait_service():
            return _skip("cdp-service did not come up")
        bystander = _open_bystander_tab(st.cdp_port, f"file://{REPO}/testdata/site-v2/index.html")
        assert bystander, "could not open a bystander tab — the rest of this check would prove nothing"
        with tempfile.TemporaryDirectory() as da:
            a = st.run_explore(da, run_id="run-going", target="testdata/site/index.html")
            claimed = _await_page(st.live_port, "run-going")
            assert claimed, "run-going never resolved to a page"
            # ⚠ The run did NOT adopt the tab that was already open. Before ADR-128 it would have:
            # `pages()[0]` is exactly this tab, and the run would be driving somebody else's page.
            assert "/testdata/site/" in claimed["url"], (
                f"the run adopted the tab that was already open instead of opening its own: {claimed['url']}")
            before = _tab_ids(st.cdp_port)
            assert bystander in before and len(before) >= 2, f"expected the run's tab beside the bystander: {before}"

            # SIGTERM, not SIGKILL: this is the signal a stopped run really receives, and the point
            # is that the executor's teardown runs and hands the page back.
            a.terminate()
            try:
                a.wait(timeout=30)
            except Exception:
                pass

            deadline, gone = time.time() + 60, None
            while time.time() < deadline:
                code, body = _get(f"http://127.0.0.1:{st.live_port}/live/status?run_id=run-going", timeout=5)
                if code == 200 and json.loads(body).get("has_page") is False:
                    gone = json.loads(body)
                    break
                time.sleep(0.5)
            assert gone, ("the stopped run's page is still open — a killed run leaked its tab into a "
                          "service that outlives it")
            assert gone["reason"] and "no longer open" in gone["reason"], (
                f"the refusal does not distinguish 'finished' from 'never claimed': {gone}")

            code, _ = _get(f"http://127.0.0.1:{st.live_port}/live/frame.jpg?run_id=run-going", timeout=10)
            assert code == 503, (f"a finished run was handed a picture anyway — with a tab still open in "
                                 f"the browser, that picture would be the bystander's: HTTP {code}")

            after = _tab_ids(st.cdp_port)
            assert bystander in after, ("the run closed a tab it did not open — that is the browser's "
                                        f"lifetime, not the run's: {before} -> {after}")
            assert len(after) < len(before), (f"the run's own tab survived its teardown: {before} -> {after}")
    finally:
        if a:
            a.kill()
            try:
                a.wait(timeout=20)
            except Exception:
                pass
        st.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} live-video tests passed")
