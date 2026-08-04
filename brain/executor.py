"""Sentinel brain — JSON-RPC client over the pw-executor subprocess (stdio).

M1 transport stays the M0 newline-delimited JSON-RPC 2.0 (MCP-SDK migration = M2).
"""
import json
import shlex
import subprocess
import sys


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


class ExecutorTransportError(RuntimeError):
    """We could not TALK to the executor — as distinct from the executor telling us something failed.

    HEALTH-004: the two are opposite answers to "whose problem is this" and used to be the same
    exception. A dead subprocess or an unreadable response is OUR failure and says nothing whatever
    about the application under test; a remote error means the executor is alive, reached the page,
    and is reporting what it found there.

    Asked at the boundary where the two genuinely differ — which raise site fired — rather than by
    matching driver text later. A step classifier reading `str(e)` for "Timeout" or "ECONNRESET" is a
    SURROGATE for that question: it happens to correlate today, drifts the first time Playwright
    rewords a message, and gives a confident wrong answer when it does.

    Subclasses RuntimeError so every existing `except Exception` and `except RuntimeError` around a
    call keeps behaving exactly as before; only code that asks the new question sees a difference.
    """


class Executor:
    def __init__(self, cmd: str) -> None:
        self.proc = subprocess.Popen(
            shlex.split(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, **params: object) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        from .otel import inject_context           # M8: W3C trace-context → pw-executor (no-op if off)
        meta = inject_context({})
        if meta:
            params = {**params, "_meta": meta}
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            # The subprocess is gone. Nothing was learned about the page.
            raise ExecutorTransportError(f"executor closed during '{method}'")
        try:
            resp = json.loads(line)
        except ValueError as e:
            # A response we cannot read is a broken channel, not a finding — and it used to surface as
            # a bare JSONDecodeError that read like a malformed page.
            raise ExecutorTransportError(f"executor sent an unreadable response to '{method}': {e}") from e
        if resp.get("error"):
            # The executor answered. Whatever it reports is about the browser and the page it drove.
            raise RuntimeError(f"{method}: {resp['error']['message']}")
        return resp.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class McpExecutor:
    """MCP-SDK transport (M2b-2, ADR-016), wrapped behind the same sync `call()` interface.

    Runs a persistent MCP ClientSession on a background asyncio loop and dispatches each call
    synchronously via run_coroutine_threadsafe, so graph/healing/replay stay unchanged. Spawns
    pw-executor with MCP_TRANSPORT=mcp (so the TS server serves over the MCP SDK). Tool names map
    `browser.<x>` -> `browser_<x>`; `initialize`/`shutdown` are MCP lifecycle (no-ops here).
    """

    def __init__(self, cmd: str) -> None:
        import asyncio
        import shlex
        import threading
        self._cmd = shlex.split(cmd)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._err: Exception | None = None
        self._session = None
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("MCP session did not become ready in 30s")
        if self._err:
            raise self._err

    def _run(self) -> None:
        import asyncio
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # surface startup errors to __init__
            self._err = e
            self._ready.set()

    async def _serve(self) -> None:
        import asyncio
        import os
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._stop = asyncio.Event()
        params = StdioServerParameters(
            command=self._cmd[0], args=self._cmd[1:],
            env={**os.environ, "MCP_TRANSPORT": "mcp"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()

    def call(self, method: str, **params: object) -> dict:
        import asyncio
        import json
        if method in ("initialize", "shutdown"):
            return {}
        if self._session is None:
            raise ExecutorTransportError("MCP session not initialized")
        tool = method.replace("browser.", "browser_")
        from .otel import inject_context           # M8: W3C trace-context → pw-executor (no-op if off)
        meta = inject_context({})
        if meta:
            params = {**params, "_meta": meta}
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, params), self._loop)
        try:
            res = fut.result(timeout=60)
        except TimeoutError as e:
            # OUR RPC deadline, not the page's. A caller that reads this as a slow application would
            # be debugging the wrong system.
            raise ExecutorTransportError(f"the executor did not answer '{method}' within 60s") from e
        if getattr(res, "isError", False):
            # The executor answered — the report is about the browser and the page.
            raise RuntimeError(f"{method}: {''.join(getattr(c, 'text', '') for c in res.content)}")
        for c in res.content:
            if getattr(c, "type", "") == "text":
                return json.loads(c.text)
        return {}

    def close(self) -> None:
        try:
            if self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(timeout=10)
        except Exception:
            pass


def make_executor(cmd: str):
    """McpExecutor when MCP_TRANSPORT=mcp (ADR-016), else the default JSON-RPC Executor.

    HEALTH-001: the command is validated HERE, in the function that uses it, rather than in a
    start-up health check. That placement matters twice over. It is the only place that sees the
    command actually being used, so a caller who substitutes this function — the tests do, and so
    would an injected executor — bypasses the validation naturally instead of tripping over a check
    that was reasoning about a string nobody was going to run. And it turns the failure a person
    actually hits (a fresh clone where `npm run build` was never run) from a Node module-resolution
    error into a sentence naming the missing file.
    """
    import os
    import shlex
    import pathlib as _pathlib
    import shutil

    why = None
    try:
        parts = shlex.split(cmd or "")
    except ValueError as exc:
        parts, why = [], f"PW_EXECUTOR_CMD is not parseable: {exc}"
    if not why and not parts:
        why = "PW_EXECUTOR_CMD is empty"
    if not why and not (shutil.which(parts[0]) or _pathlib.Path(parts[0]).exists()):
        why = f"{parts[0]!r} is not on PATH and is not a file"
    if not why:
        for arg in parts[1:]:
            if arg.startswith("-"):
                continue
            if not _pathlib.Path(arg).exists():
                why = f"{arg!r} does not exist — has pw-executor been built? (npm --prefix pw-executor run build)"
            break
    if why:
        raise RuntimeError(f"the executor cannot be started: {why}")

    if os.environ.get("MCP_TRANSPORT") == "mcp":
        return McpExecutor(cmd)
    return Executor(cmd)
