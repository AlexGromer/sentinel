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

from .eventlog import log


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
    # Why the completion stopped: "stop" = the model finished on its own; "length" = it hit max_tokens
    # mid-thought (for a reasoning model, often BEFORE any answer content). None when the backend does
    # not report it. The planner uses this to tell "model gave no JSON" apart from "budget too small".
    finish_reason: Optional[str] = None


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

    def complete_chat(self, messages: list, *, max_tokens: int, temperature: float,
                      system: Optional[str] = None) -> LLMResult:
        """ADR-108b: a turn of CONVERSATION — the whole exchange, with its roles intact.

        `complete` exists and takes one string, so a conversation could have been flattened into it.
        That would have been a transcript pretending to be a dialogue: the model loses which words were
        its own, and every provider's multi-turn handling (caching, role-aware safety, tool state) is
        bypassed. Anthropic's `system` is a separate parameter, not a message, which is exactly why it
        travels separately here too."""
        kw = dict(model=self.model, max_tokens=max_tokens, temperature=temperature, messages=messages)
        if system:
            kw["system"] = system
        return self._result(self._client.messages.create(**kw))

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
        return LLMResult(text, pt, ct, model=getattr(resp, "model", None),
                         finish_reason=getattr(choice, "finish_reason", None))

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult:
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature,
            messages=[{"role": "user", "content": prompt}])
        return self._result(resp)

    def complete_chat(self, messages: list, *, max_tokens: int, temperature: float,
                      system: Optional[str] = None) -> LLMResult:
        """ADR-108b, the OpenAI-compatible half. Here `system` IS a message, and it goes first — the
        one place the two providers genuinely differ in shape rather than in spelling."""
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        return self._result(self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=temperature, messages=msgs))

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
_DEFAULT_MODEL = {
    "planner": "claude-opus-4-8",
    "heal": "claude-sonnet-4-6",
    # ADR-108b: conversation is its own role, and NOT because a third model was wanted. A role is what
    # lets an operator point talking and planning at different endpoints — the planner may be a large
    # remote model while the chat that answers "what does this test cover?" runs on whatever is local.
    # It defaults to the cheaper of the two: answering a person is not the work planning is.
    "chat": "claude-sonnet-4-6",
}


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
            log("llm.sampling_no_session", role=role)
            return None
        loop, session = sampling
        return SamplingBackend(loop, session)
    model = _env(role, "MODEL") or _DEFAULT_MODEL.get(role)
    base_url = _env(role, "BASE_URL")
    try:
        if provider == "anthropic":
            key = _env(role, "API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                log("llm.no_anthropic_key", role=role)
                return None
            return AnthropicBackend(model, api_key=key)
        if provider == "openai":
            if not model:
                log("llm.openai_model_missing", role=role)
                return None
            key = _env(role, "API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not key and not base_url:
                log("llm.openai_endpoint_missing", role=role)
                return None
            supports_vision = (_env(role, "VISION") or "") == "1"
            supports_structured = (_env(role, "STRUCTURED") or "") == "1"
            return OpenAICompatBackend(model, base_url=base_url, api_key=key,
                                       supports_vision=supports_vision,
                                       supports_structured=supports_structured)
        log("llm.backend_unknown", provider=provider)
        return None
    except Exception as e:  # missing SDK / bad config -> fallback, never crash a run
        log("llm.provider_unavailable", provider=provider, error=e)
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


def _int_env(key: str, default: int) -> int:
    try:
        v = int(os.environ.get(key, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Adaptive structured-output budget (M9-LIVE, 2026-07-23). A reasoning model can spend a tight
# max_tokens entirely on THINK tokens and stop before emitting any answer (finish_reason="length",
# empty content), which the caller reads as "no JSON" and silently degrades. Rather than fail, retry
# the SAME call with a doubled ceiling until the model produces content or we hit the hard cap — then
# remember the working ceiling PER MODEL so the next run starts there and pays the escalation once.
# Bounded and budget-aware: escalates ONLY on finish_reason="length", never past _TOKEN_HARD_MAX, and
# stops when the role's ADR-021 plan budget is exhausted. Disable with LLM_ADAPTIVE_TOKENS=0.
_TOKEN_HARD_MAX = _int_env("LLM_MAX_TOKENS_HARD", 16384)
_ADAPTIVE = os.environ.get("LLM_ADAPTIVE_TOKENS", "1").strip().lower() not in ("0", "false", "no", "off")
_BUDGET_FILE = os.environ.get("SENTINEL_LLM_BUDGET_FILE") or os.path.join("state", "llm-budget.json")
_learned_cache: Optional[dict] = None


def _learned_budgets() -> dict:
    """Per-model learned ceilings {model: max_tokens}, loaded once (best-effort)."""
    global _learned_cache
    if _learned_cache is None:
        try:
            with open(_BUDGET_FILE) as f:
                loaded = json.load(f)
            _learned_cache = {k: int(v) for k, v in loaded.items() if isinstance(v, (int, float))}
        except Exception:
            _learned_cache = {}
    return _learned_cache


def _remember_budget(model: Optional[str], tokens: int) -> None:
    """Persist a newly-learned working ceiling for `model` (best-effort, atomic, never raises)."""
    if not model:
        return
    store = _learned_budgets()
    if store.get(model, 0) >= tokens:
        return
    store[model] = tokens
    try:
        d = os.path.dirname(_BUDGET_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = _BUDGET_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f)
        os.replace(tmp, _BUDGET_FILE)
    except Exception:
        pass


def _one_structured(backend, prompt: str, schema: dict, max_tokens: int, temperature: float) -> LLMResult:
    if getattr(backend, "supports_structured", False):
        r = backend.complete_json(prompt, schema=schema, max_tokens=max_tokens, temperature=temperature)
    else:
        r = backend.complete(prompt, max_tokens=max_tokens, temperature=temperature)
    if r.data is None:  # fallback path, or a native reply with no structured payload -> salvage
        try:
            r.data = extract_json(r.text)
        except Exception:
            r.data = None
    return r


def complete_structured(backend, prompt: str, schema: dict, *, max_tokens: int,
                        temperature: float, role: Optional[str] = None) -> LLMResult:
    """Obtain a JSON object from `backend`, preferring native structured output, self-calibrating the
    token ceiling for reasoning models.

    When the backend advertises `supports_structured`, use `complete_json` (Anthropic tool_use /
    OpenAI json_schema) — the reply is a guaranteed object, no text-slicing. Otherwise (MCP sampling,
    local models with structured OFF) fall back to `complete` + `extract_json`. NEVER raises on an
    unparseable reply: returns the `LLMResult` with `.data=None` so the CALLER can still charge
    `budget.tracker().add(role, result)` with the tokens the model spent (ADR-021) and THEN degrade.

    If the reply is truncated (finish_reason="length" with no parseable payload), retry with a doubled
    ceiling up to _TOKEN_HARD_MAX and remember the working value per model (see the module note above).
    `role` (e.g. "plan") makes the retries respect that budget and is optional; without it the loop is
    still bounded by _TOKEN_HARD_MAX. The returned result's token counts are SUMMED across attempts so
    the caller charges the true total. Only a backend (network/SDK) error propagates."""
    model = getattr(backend, "model", None)
    cap = max_tokens
    if _ADAPTIVE:
        cap = min(max(cap, _learned_budgets().get(model, 0)), _TOKEN_HARD_MAX)
    total_pt = total_ct = 0
    attempt = 0
    while True:
        r = _one_structured(backend, prompt, schema, cap, temperature)
        total_pt += r.prompt_tokens or 0
        total_ct += r.completion_tokens or 0
        if r.data is not None:
            if cap > max_tokens:  # succeeded only after escalation -> teach the next run
                _remember_budget(model, cap)
            break
        # Only a length-truncation is worth retrying; a genuine bad reply won't improve with room.
        if not _ADAPTIVE or r.finish_reason != "length" or cap >= _TOKEN_HARD_MAX:
            break
        if role is not None:
            from . import budget
            if budget.tracker().exceeded(role):
                log("llm.budget_ceiling_reached", role=role, cap=cap)
                break
        new_cap = min(cap * 2, _TOKEN_HARD_MAX)
        log("llm.output_truncated_retry", cap=cap, new_cap=new_cap)
        cap = new_cap
        attempt += 1
    r.prompt_tokens, r.completion_tokens = total_pt, total_ct  # true total across attempts (ADR-021)
    return r


# --- ADR-108b: conversation -----------------------------------------------------------------------
# Everything above this line asks the model for a STRUCTURE — a plan, a pick, a locator — and the
# whole apparatus (schemas, extract_json, adaptive ceilings) exists to make the reply machine-usable.
# None of it can answer a person. `complete` was the only unconstrained call in the module and it had
# exactly one caller: the fallback inside _one_structured, for backends that cannot do schemas. So the
# product whose centre is a chat had no way to say anything back.

CONVERSATION_SYSTEM = (
    "You are Sentinel, an autonomous UI-testing agent. You are talking to the person who runs you.\n"
    "Answer their question directly and briefly. Plain prose, no JSON, no code fences unless they ask "
    "for code.\n"
    "You author and replay browser tests: you explore an application, freeze a deterministic plan, and "
    "repair locators when the DOM drifts.\n"
    "To actually TEST something you need an objective for this conversation (a goal, or a description "
    "of the flow) and a target URL. If the person has not given one and seems to want work done, say "
    "plainly what you need."
)


def converse(backend, messages: list, *, max_tokens: int = 800, temperature: float = 0.3,
             system: Optional[str] = None) -> LLMResult:
    """One turn of conversation: the exchange goes in, prose comes out.

    `messages` is the OpenAI/Anthropic shape — [{"role": "user"|"assistant", "content": str}, …] —
    and the ROLES are preserved wherever the backend understands them (complete_chat). A backend that
    does not (MCP sampling, which is text-only by contract) gets the exchange flattened into a single
    prompt with the speakers labelled; that is a real loss of fidelity, which is why it is the fallback
    rather than the implementation.

    Returns the LLMResult unchanged so the caller charges budget.tracker().add("chat", result) exactly
    as the structured callers do — a conversation spends tokens like everything else, and a turn that
    escaped accounting would make the budget a number about part of the work.
    """
    sys_prompt = system or CONVERSATION_SYSTEM
    chat = getattr(backend, "complete_chat", None)
    if callable(chat):
        return chat(messages, max_tokens=max_tokens, temperature=temperature, system=sys_prompt)
    flat = [sys_prompt, ""]
    for m in messages:
        who = "User" if (m.get("role") == "user") else "Assistant"
        flat.append(f"{who}: {m.get('content', '')}")
    flat.append("Assistant:")
    return backend.complete("\n".join(flat), max_tokens=max_tokens, temperature=temperature)
