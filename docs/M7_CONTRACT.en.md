# M7 Contract — "MCP-Server Exposure" (PROPOSED, frozen 2026-06-25)

> 🌐 [Русский](M7_CONTRACT.md) (основная версия) · **English**

Status: **Proposed** (ADR-020). Docs-first — the contract is frozen now, implementation is the next
milestone (needs a live MCP host to exercise, user-run).

Goal: the second direction of the user ask — Sentinel **is driven by** host agents (OpenCode,
Kilocode, Claude Desktop) that supply the model themselves. We expose the brain as an **MCP server**;
the host drives it and supplies the model via MCP `sampling/createMessage`.

## Key lever
B1 (M6/ADR-019) already provides the `LLMBackend` abstraction. M7 = one more implementation:
`SamplingBackend(LLMBackend)` routing LLM calls back to the host. **No planner/healing changes** —
they already go through `make_backend` / an injected backend.

## Scope (implementation — next session)
- **Sentinel-as-MCP-server** (NEW, distinct from the pw-executor MCP server): tools
  `explore` / `heal` / `replay` / `report` with input schemas; stdout reserved for the protocol.
- **`SamplingBackend`**: `complete()` → `sampling/createMessage` (single user message → text);
  `supports_vision=False` (basic sampling has no vision ⇒ heal degrades to L1–L6); tokens `0`;
  `LLMResult.model` = the host's real model. Sync↔async bridge like `McpExecutor`
  (`executor.py:99–114`, background loop + `run_coroutine_threadsafe`).
- Selection: env `LLM_BACKEND=sampling` (or server-mode auto-detect).

## Honest constraints (VERIFY at implementation, anti-hallucination)
- MCP `sampling` support is uneven across hosts (Claude Desktop — yes; **OpenCode / Kilocode —
  VERIFY before coding**). If a host offers no sampling → the backend is unavailable → fallback to heuristic/L1–L6.
- OpenCode / Kilocode are agent clients / provider aggregators, **not model APIs**. "Work with them"
  here = they drive Sentinel as an MCP tool, with their model.
- Architecture inversion: in server-mode Sentinel stops being an autonomous CLI. The
  explore-once / replay-many and determinism contract are preserved (replay is LLM-free), but the host initiates explore.

## Gate (when implemented)
- The Sentinel MCP server's `tools/list` returns `explore`/`heal`/`replay`/`report`.
- A run from a real MCP host (user-run): the host drives explore, sampling supplies the model, the
  artifacts are identical to CLI mode.
- Offline: a contract test for the tool schemas + `SamplingBackend` via a fake sampling session (the `FakeBackend` pattern).

## ADR
ADR-020 (Proposed). Builds on ADR-019 (`LLMBackend`). The MCP server is **distinct** from ADR-016 (pw-executor).

## Out of scope
Implementation this session (contract + ADR only). Vision over sampling (basic sampling has no vision).
