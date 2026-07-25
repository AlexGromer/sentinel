"""Sentinel brain as an MCP server (M7, ADR-020).

Exposes the brain's capabilities as MCP tools so an MCP host (OpenCode / Kilocode / Claude Desktop)
can drive it; the host supplies the model via MCP `sampling/createMessage` (see `SamplingBackend` in
`brain/llm.py`). Distinct from the pw-executor MCP server (ADR-016): this server IS the brain.

The graph/replay run synchronously (LangGraph `app.invoke`), so each tool runs that work in a worker
thread (`asyncio.to_thread`, which copies the sampling contextvar) — keeping the event loop free to
service the reverse `sampling/createMessage` requests. stdout is reserved for the MCP protocol; all
logs go to stderr.

NOTE: `Context`/`FastMCP` are imported at module level (no `from __future__ import annotations`) so
FastMCP's `get_type_hints` can resolve the `Context` param and inject it / exclude it from the schema.
"""
import asyncio
import contextlib
import json
import os
import sys

from mcp.server.fastmcp import Context, FastMCP

from .eventlog import log
from .executor import make_executor
from .llm import reset_sampling_session, set_sampling_session


def build_app(out, run_id: str) -> FastMCP:
    """Build (but do not run) the FastMCP server with the brain tools. Testable in isolation."""
    app = FastMCP("sentinel-brain")

    async def _drive(ctx, use_sampling: bool, work) -> str:
        """Bind the host sampling session (if needed) and run the sync `work` off the event loop.

        stdout is the MCP JSON-RPC channel here (module docstring), but the brain's run code prints to
        stdout: `@@AGUI` AG-UI events (graph.py/replay.py, M14) and the replay tail summary
        (__main__.py). Left alone, those non-protocol lines interleave with the framed JSON-RPC the host
        is reading and can desync it. Redirect stdout→stderr for the duration of `work`: the MCP SDK's
        writer holds the ORIGINAL stdout buffer (captured when the stdio transport was set up), so framed
        responses still reach the host, while the brain's diagnostics land on stderr (harmless — no
        control-API line-reader consumes @@AGUI in mcp-server mode; the host drives via JSON-RPC)."""
        token = set_sampling_session(asyncio.get_running_loop(), ctx.session) if use_sampling else None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                rc = await asyncio.to_thread(work)
        finally:
            if token is not None:
                reset_sampling_session(token)
        return json.dumps({"exit_code": rc, "run_id": run_id, "artifact_dir": str(out)})

    @app.tool()
    async def explore(target_url: str, coverage_target: float = 0.85,
                      max_steps: int = 40, ctx: Context = None) -> str:
        """Autonomously explore the app and freeze a deterministic plan.json (LLM planner via host sampling)."""
        def work():
            from .__main__ import _run_explore
            ex = make_executor(os.environ["PW_EXECUTOR_CMD"])
            # MCP explore drives the LLM planner via host sampling (make_planner reads PLANNER; default
            # is heuristic). setdefault keeps an explicit operator override. `_run_explore` takes 6 args
            # (planner is env-selected, not positional) — passing a 7th raised TypeError before this fix.
            os.environ.setdefault("PLANNER", "llm")
            try:
                return _run_explore(ex, run_id, out, target_url, coverage_target, max_steps)
            finally:
                ex.close()
        return await _drive(ctx, True, work)

    @app.tool()
    async def heal(plan_file: str, target_url: str = "", aut_version: str = "",
                   ctx: Context = None) -> str:
        """Replay a frozen plan WITH LLM self-healing (broken-locator re-grounding via host sampling)."""
        def work():
            from .__main__ import _run_replay
            ex = make_executor(os.environ["PW_EXECUTOR_CMD"])
            try:
                return _run_replay(ex, run_id, out, target_url, plan_file, True,
                                   baseline=False, aut_version=aut_version, ci=False, force=False)
            finally:
                ex.close()
        return await _drive(ctx, True, work)

    @app.tool()
    async def replay(plan_file: str, target_url: str = "", aut_version: str = "",
                     ci: bool = False, ctx: Context = None) -> str:
        """Deterministic replay of a frozen plan (no LLM; L1–L6 heal only). For CI."""
        def work():
            from .__main__ import _run_replay
            ex = make_executor(os.environ["PW_EXECUTOR_CMD"])
            try:
                return _run_replay(ex, run_id, out, target_url, plan_file, False,
                                   baseline=False, aut_version=aut_version, ci=ci, force=False)
            finally:
                ex.close()
        return await _drive(ctx, False, work)

    @app.tool()
    async def report(run_dir: str = "", ctx: Context = None) -> str:
        """Generate report.html + report.json + metrics.prom from a run's heal-report.json."""
        def work():
            from .__main__ import _run_report
            return _run_report(run_dir or str(out))
        return await _drive(ctx, False, work)

    return app


def run_mcp_server(out, run_id: str) -> int:
    """Entrypoint for RUN_MODE=mcp-server: serve the brain tools over MCP stdio (host drives, host samples)."""
    log("run.mcp_serving", run_id=run_id)
    build_app(out, run_id).run("stdio")
    return 0
