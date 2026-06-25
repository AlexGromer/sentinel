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

import os
from dataclasses import dataclass
from typing import Optional, Protocol

from .executor import log


@dataclass
class LLMResult:
    """Normalized completion. `model` is the model the provider actually used (MCP sampling sets
    this); for fixed backends it mirrors `backend.model`."""
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: Optional[str] = None


class LLMBackend(Protocol):
    """Provider-neutral chat surface. `name` is the provider ("anthropic"|"openai"|"sampling") —
    distinct from a planner's `name`. `model` is fixed per backend, never per call."""
    name: str
    model: str
    supports_vision: bool

    def complete(self, prompt: str, *, max_tokens: int, temperature: float) -> LLMResult: ...

    def complete_vision(self, prompt: str, image_b64: str, *, max_tokens: int,
                        temperature: float) -> LLMResult: ...


class AnthropicBackend:
    """Native Anthropic (the calibrated default). Vision-capable."""

    name = "anthropic"
    supports_vision = True

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


class OpenAICompatBackend:
    """Any OpenAI-compatible endpoint (base_url + key). Covers ChatGPT, DeepSeek, Qwen (DashScope
    compat), Gemini (OpenAI-compat endpoint), OpenRouter, Ollama, vLLM. Vision is opt-in via
    `supports_vision` because text-only models (e.g. DeepSeek-V3) must not attempt it."""

    name = "openai"

    def __init__(self, model: str, *, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, supports_vision: bool = False) -> None:
        import openai
        self.model = model
        self.supports_vision = supports_vision
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


# Per-role defaults preserve today's behaviour (ADR-007): Opus explore, Sonnet heal.
_DEFAULT_MODEL = {"planner": "claude-opus-4-8", "heal": "claude-sonnet-4-6"}


def _env(role: str, key: str) -> Optional[str]:
    """role-specific env (LLM_<KEY>_<ROLE>) overrides global (LLM_<KEY>)."""
    return os.environ.get(f"LLM_{key}_{role.upper()}") or os.environ.get(f"LLM_{key}")


def make_backend(role: str) -> Optional[LLMBackend]:
    """Build the backend for a role ("planner"|"heal") from env, or `None` to keep the offline
    fallback (heuristic / L1–L6). Never raises: any import/config problem returns `None`."""
    provider = (_env(role, "BACKEND") or "anthropic").lower()
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
            return OpenAICompatBackend(model, base_url=base_url, api_key=key,
                                       supports_vision=supports_vision)
        log(f"make_backend[{role}]: unknown LLM_BACKEND={provider!r} -> fallback")
        return None
    except Exception as e:  # missing SDK / bad config -> fallback, never crash a run
        log(f"make_backend[{role}]: {provider} unavailable -> fallback:", e)
        return None
