"""Sentinel brain — JSON-RPC client over the pw-executor subprocess (stdio).

M1 transport stays the M0 newline-delimited JSON-RPC 2.0 (MCP-SDK migration = M2).
"""
import json
import shlex
import subprocess
import sys


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


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
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"executor closed during '{method}'")
        resp = json.loads(line)
        if resp.get("error"):
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
            raise RuntimeError("MCP session not initialized")
        tool = method.replace("browser.", "browser_")
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, params), self._loop)
        res = fut.result(timeout=60)
        if getattr(res, "isError", False):
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
    """McpExecutor when MCP_TRANSPORT=mcp (ADR-016), else the default JSON-RPC Executor."""
    import os
    if os.environ.get("MCP_TRANSPORT") == "mcp":
        return McpExecutor(cmd)
    return Executor(cmd)
