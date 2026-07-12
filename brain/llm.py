"""Sentinel brain — provider-agnostic LLM backends (M6, ADR-019).

The brain calls an LLM in three places: the `LLMPlanner` (explore) and the `HealingEngine`'s
text re-grounding + set-of-marks vision. Each goes through an `LLMBackend`, so Sentinel can run
on Anthropic OR any OpenAI-compatible endpoint (ChatGPT, DeepSeek, Qwen, Gemini-compat,
OpenRouter, Ollama, vLLM) — selected per role via env. A later milestone (M7) adds a
`SamplingBackend` (MCP sampling: the host supplies the model) as just another backend.

Determinism (ADR-019): the LLM path is BEST-EFFORT — different models produce different plans,
so `plan_hash` is NOT guaranteed across models. `HeuristicPlanner` stays the deterministic anchor
and golden baselines stay heuristic-only. See ../docs/M6_CONTRACT.md.

`make_backend(role)` returns `None` when unconfigured or the SDK is missing, so a missing key or
package never breaks a run: the planner falls back to the heuristic, healing to L1–L6.

Env precedence (per call to `make_backend`): role-specific `LLM_<KEY>_<ROLE>` > global `LLM_<KEY>`.
Roles: "planner", "heal". Keys: BACKEND, MODEL, BASE_URL, API_KEY, VISION.
With NO env set the behaviour is identical to before: Anthropic, Opus (planner) / Sonnet (heal),
keyed off ANTHROPIC_API_KEY, falling back to heuristic / L1–L6 when the key is absent.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from .executor import log


@dataclass
class LLMResult:
    """Normalized completion. `model` is the model the provider actually used (MCP sampling sets
    this); for fixed backends it mirrors `backend.model`. `data` is the parsed JSON object when the
    completion came from a structured-output call (native tool_use/json_schema) or was extracted
    downstream; None for a plain text completion."""
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: Optional[str] = None
    data: Optional[dict] = None


class LLMBackend(Protocol):
    """Provider-neutral chat surface. `name` is the provider ("anthropic"|"openai"|"sampling") —
    distinct from a planner's `name`. `model` is fixed per backend, never per call.

    `supports_structured` advertises native structured output (Anthropic tool_use / OpenAI
    json_schema): when True, callers use `complete_json` for a guaranteed JSON object; when False
    (MCP sampling, local models with it disabled) they fall back to `complete` + `extract_json`."""
    name: str
    model: str
    supports_vision: bool
    supports_structured: bool

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult: ...

    def complete_vision(self, prompt: str, image_b64: str, *, max_tokens: int,
                        temperature: float) -> LLMResult: ...

    def complete_json(self, prompt: str, *, schema: dict, max_tokens: int,
                      temperature: float) -> LLMResult: ...


class AnthropicBackend:
    """Native Anthropic (the calibrated default). Vision-capable + native structured output."""

    name = "anthropic"
    supports_vision = True
    supports_structured = True

    def __init__(self, model: str, api_key: Optional[str] = None) -> None:
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    @staticmethod
    def _result(msg) -> LLMResult:
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        u = getattr(msg, "usage", None)
        pt = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        ct = int(getattr(u, "output_tokens", 0) or 0) if u else 0
        return LLMResult(text, pt, ct, model=getattr(msg, "model", None))

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult:
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": prompt}])
        return self._result(msg)

    def complete_vision(self, prompt: str, image_b64: str, *, max_tokens: int,
                        temperature: float) -> LLMResult:
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": prompt}]}])
        return self._result(msg)

    def complete_json(self, prompt: str, *, schema: dict, max_tokens: int,
                      temperature: float) -> LLMResult:
        """Structured output via a single forced tool call: the model MUST return `emit(input=…)`
        matching `schema`, so `.data` is the parsed object (no text-slicing)."""
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            tools=[{"name": "emit", "description": "Return the result as structured JSON.",
                    "input_schema": schema}],
            tool_choice={"type": "tool", "name": "emit"},
            messages=[{"role": "user", "content": prompt}])
        data = next((getattr(b, "input", None) for b in msg.content
                     if getattr(b, "type", "") == "tool_use"), None)
        u = getattr(msg, "usage", None)
        pt = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        ct = int(getattr(u, "output_tokens", 0) or 0) if u else 0
        text = json.dumps(data) if data is not None else \
            "".join(getattr(b, "text", "") for b in msg.content).strip()
        return LLMResult(text, pt, ct, model=getattr(msg, "model", None), data=data)


class OpenAICompatBackend:
    """Any OpenAI-compatible endpoint (base_url + key). Covers ChatGPT, DeepSeek, Qwen (DashScope
    compat), Gemini (OpenAI-compat endpoint), OpenRouter, Ollama, vLLM. Vision is opt-in via
    `supports_vision` because text-only models (e.g. DeepSeek-V3) must not attempt it."""

    name = "openai"

    def __init__(self, model: str, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, supports_vision: bool = False,
                 supports_structured: bool = False) -> None:
        import openai
        self.model = model
        self.supports_vision = supports_vision
        # opt-in: many OpenAI-compatible endpoints (Ollama/vLLM) reject response_format=json_schema,
        # so structured output is OFF by default and those fall back to complete()+extract_json.
        self.supports_structured = supports_structured
        kwargs: dict = {}
        if base_url:
            kwargs["base_url"] = base_url
        # the openai SDK requires a non-empty key even when the endpoint ignores it (e.g. Ollama)
        kwargs["api_key"] = api_key or "noauth"
        self._client = openai.OpenAI(**kwargs)

    def _result(self, resp) -> LLMResult:
        choice = resp.choices[0]
        text = (getattr(choice.message, "content", None) or "").strip()
        u = getattr(resp, "usage", None)
        pt = int(getattr(u, "prompt_tokens", 0) or 0) if u else 0
        ct = int(getattr(u, "completion_tokens", 0) or 0) if u else 0
        return LLMResult(text, pt, ct, model=getattr(resp, "model", None))

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult:
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": prompt}])
        return self._result(resp)

    def complete_vision(self, prompt: str, image_b64: str, *, max_tokens: int,
                        temperature: float) -> LLMResult:
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": prompt}]}])
        return self._result(resp)

    def complete_json(self, prompt: str, *, schema: dict, max_tokens: int,
                      temperature: float) -> LLMResult:
        """Structured output via `response_format=json_schema`: the reply content is the JSON object.
        Only used when `supports_structured` (opt-in `LLM_STRUCTURED=1`); `.data` is None if the
        endpoint returned non-JSON, letting `complete_structured` salvage via `extract_json`."""
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "out", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        r = self._result(resp)
        try:
            r.data = json.loads(r.text) if r.text else None
        except Exception:
            r.data = None
        return r


# MCP sampling session, set by the brain MCP server (M7, ADR-020) for the duration of a tool call.
# Holds (event_loop, mcp.ServerSession). `asyncio.to_thread` copies this contextvar into the worker
# thread that runs the synchronous graph, so SamplingBackend can reach the host's sampling capability.
_sampling_ctx: contextvars.ContextVar = contextvars.ContextVar("sentinel_sampling", default=None)


def set_sampling_session(loop, session):
    """Server entrypoint: bind the host sampling session for this context; returns a reset token."""
    return _sampling_ctx.set((loop, session))


def reset_sampling_session(token) -> None:
    _sampling_ctx.reset(token)


class SamplingBackend:
    """LLMBackend backed by MCP `sampling/createMessage` — the HOST supplies the model (M7, ADR-020).

    Used when the brain runs as an MCP server (`RUN_MODE=mcp-server`): each `complete()` is a
    server→host request bridged onto the host's event loop (mirrors `executor.McpExecutor`). Text-only:
    basic sampling has no vision, so `supports_vision=False` and heal degrades to deterministic L1–L6."""

    name = "sampling"
    supports_vision = False
    supports_structured = False

    def __init__(self, loop, session, model: str = "mcp-sampling", timeout: float = 120.0) -> None:
        self._loop, self._session, self.model, self._timeout = loop, session, model, timeout

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult:
        from mcp.types import SamplingMessage, TextContent
        coro = self._session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=max_tokens, temperature=temperature)
        res = asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=self._timeout)
        c = getattr(res, "content", None)
        text = c.text if c is not None and getattr(c, "type", "") == "text" else ""
        return LLMResult(text.strip(), 0, 0, model=getattr(res, "model", None) or self.model)

    def complete_vision(self, prompt: str, image_b64: str, *, max_tokens: int,
                        temperature: float) -> LLMResult:
        raise NotImplementedError("MCP sampling backend is text-only (supports_vision=False)")


# Per-role defaults preserve today's behaviour (ADR-007): Opus explore, Sonnet heal.
_DEFAULT_MODEL = {"planner": "claude-opus-4-8", "heal": "claude-sonnet-4-6"}


def _env(role: str, key: str) -> Optional[str]:
    """role-specific env (LLM_<KEY>_<ROLE>) overrides global (LLM_<KEY>)."""
    return os.environ.get(f"LLM_{key}_{role.upper()}") or os.environ.get(f"LLM_{key}")


def make_backend(role: str) -> Optional[LLMBackend]:
    """Build the backend for a role ("planner"|"heal") from env, or `None` to keep the offline
    fallback (heuristic / L1–L6). Never raises: any import/config problem returns `None`."""
    provider = (_env(role, "BACKEND") or "anthropic").lower()
    # MCP-server mode (M7): an active sampling session takes precedence — the host supplies the model.
    sampling = _sampling_ctx.get()
    if sampling is not None or provider == "sampling":
        if sampling is None:
            log(f"make_backend[{role}]: sampling requested but no MCP session -> fallback")
            return None
        loop, session = sampling
        return SamplingBackend(loop, session)
    model = _env(role, "MODEL") or _DEFAULT_MODEL.get(role)
    base_url = _env(role, "BASE_URL")
    try:
        if provider == "anthropic":
            key = _env(role, "API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                log(f"make_backend[{role}]: no Anthropic key -> offline fallback")
                return None
            return AnthropicBackend(model, api_key=key)
        if provider == "openai":
            if not model:
                log(f"make_backend[{role}]: openai provider needs LLM_MODEL -> fallback")
                return None
            key = _env(role, "API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not key and not base_url:
                log(f"make_backend[{role}]: openai needs a key or base_url -> fallback")
                return None
            supports_vision = (_env(role, "VISION") or "") == "1"
            supports_structured = (_env(role, "STRUCTURED") or "") == "1"
            return OpenAICompatBackend(model, base_url=base_url, api_key=key,
                                       supports_vision=supports_vision,
                                       supports_structured=supports_structured)
        log(f"make_backend[{role}]: unknown LLM_BACKEND={provider!r} -> fallback")
        return None
    except Exception as e:  # missing SDK / bad config -> fallback, never crash a run
        log(f"make_backend[{role}]: {provider} unavailable -> fallback:", e)
        return None


def extract_json(text: str) -> dict:
    """Parse the first complete JSON object out of a (possibly noisy) model reply.

    Robust replacement for the fragile `text[text.find("{"): text.rfind("}") + 1]` slice: scans
    balanced braces (string-aware) from the first `{` to its matching `}`, so a markdown code fence,
    trailing commentary, or a stray `}` in prose no longer corrupts the parse. Raises on a missing or
    invalid object — every caller already degrades to heuristic/empty on exception."""
    s = text.strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in text")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    # unbalanced (truncated reply) -> last-ditch: the original first-{ .. last-} span
    return json.loads(s[start:s.rfind("}") + 1])


def complete_structured(backend, prompt: str, schema: dict, *, max_tokens: int,
                        temperature: float) -> LLMResult:
    """Obtain a JSON object from `backend`, preferring native structured output.

    When the backend advertises `supports_structured`, use `complete_json` (Anthropic tool_use /
    OpenAI json_schema) — the reply is a guaranteed object, no text-slicing. Otherwise (MCP sampling,
    local models with structured OFF) fall back to `complete` + `extract_json`. The returned
    NEVER raises on an unparseable reply: it returns the `LLMResult` with `.data=None` so the CALLER
    can still charge `budget.tracker().add(role, result)` with the tokens the model already spent
    (ADR-021 over-budget guard) and THEN degrade to the heuristic / an empty result on `data is None`.
    Only a backend (network/SDK) error propagates — and there no tokens were produced to book."""
    if getattr(backend, "supports_structured", False):
        r = backend.complete_json(prompt, schema=schema, max_tokens=max_tokens,
                                  temperature=temperature)
    else:
        r = backend.complete(prompt, max_tokens=max_tokens, temperature=temperature)
    if r.data is None:  # fallback path, or a native reply with no structured payload -> salvage
        try:
            r.data = extract_json(r.text)
        except Exception:
            r.data = None  # caller charges budget, then degrades on `data is None`
    return r
