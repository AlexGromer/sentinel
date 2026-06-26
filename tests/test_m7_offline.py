"""Offline M7 tests — MCP-server exposure + SamplingBackend (no live host, no network).

Run:  .venv/bin/python tests/test_m7_offline.py
Proves: SamplingBackend bridges a sync `complete()` onto a host event loop and maps the host's
`create_message` result to an `LLMResult` (host model, tokens 0, text-only); the sampling contextvar
drives `LLMPlanner` via `make_backend`; and the brain MCP server exposes explore/heal/replay/report.
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import llm                                   # noqa: E402
from brain.llm import LLMResult, SamplingBackend        # noqa: E402
from brain.planner import LLMPlanner                     # noqa: E402


class _Text:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Result:
    """Stand-in for mcp.types.CreateMessageResult (single content block + model)."""
    def __init__(self, text, model):
        self.content, self.model = _Text(text), model


class FakeSamplingSession:
    """Stand-in for an MCP ServerSession; records create_message calls, returns a canned reply."""
    def __init__(self, reply='{"index": 0}', model="host-model"):
        self.reply, self.model, self.calls = reply, model, []

    async def create_message(self, *, messages, max_tokens, temperature, **kw):
        self.calls.append((max_tokens, temperature))
        return _Result(self.reply, self.model)


def _loop_in_thread():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def test_sampling_backend_bridges_to_host():
    loop = _loop_in_thread()
    try:
        fs = FakeSamplingSession('{"index": 0}', model="host-model")
        sb = SamplingBackend(loop, fs)
        assert sb.name == "sampling" and sb.supports_vision is False
        r = sb.complete("pick one", max_tokens=200, temperature=0)
        assert r.text == '{"index": 0}', r
        assert r.model == "host-model", r                       # host supplies the real model
        assert (r.prompt_tokens, r.completion_tokens) == (0, 0)  # sampling has no usage
        assert fs.calls == [(200, 0)], fs.calls                 # max_tokens/temperature passed through
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_sampling_backend_vision_unsupported():
    sb = SamplingBackend(None, None)
    raised = False
    try:
        sb.complete_vision("x", "b64", max_tokens=10, temperature=0)
    except NotImplementedError:
        raised = True
    assert raised, "complete_vision must raise (supports_vision=False)"


def test_sampling_contextvar_drives_planner():
    loop = _loop_in_thread()
    token = llm.set_sampling_session(loop, FakeSamplingSession('{"index": 1}'))
    try:
        p = LLMPlanner()                                        # make_backend('planner') -> SamplingBackend
        assert p._backend is not None and p._backend.name == "sampling", p._backend
        cands = [{"kind": "click", "role": "b", "name": "A", "target": None},
                 {"kind": "click", "role": "b", "name": "B", "target": None}]
        d = p.propose({"current_url": "u", "coverage_achieved": 0.0, "coverage_target": 0.85}, cands)
        assert d["action"] is cands[1], d                       # host picked index 1 via sampling
    finally:
        llm.reset_sampling_session(token)
        loop.call_soon_threadsafe(loop.stop)


def test_make_backend_sampling_without_session_falls_back():
    # provider=sampling but no active session -> None (caller keeps heuristic/L1-L6 fallback)
    os.environ["LLM_BACKEND"] = "sampling"
    try:
        assert llm.make_backend("planner") is None
    finally:
        os.environ.pop("LLM_BACKEND", None)


def test_mcp_server_exposes_tools():
    import pathlib
    from brain.server import build_app
    app = build_app(pathlib.Path("/tmp"), "test")
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert {"explore", "heal", "replay", "report"} <= names, names
    # the injected Context param must NOT leak into a tool's input schema
    explore = next(t for t in tools if t.name == "explore")
    props = set((explore.inputSchema or {}).get("properties", {}))
    assert "ctx" not in props, props
    assert "target_url" in props, props


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
