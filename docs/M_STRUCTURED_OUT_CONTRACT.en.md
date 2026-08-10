# Contract M-STRUCTURED-OUT — strict LLM structured output (`tool_use`/`json_schema`) + robust JSON fallback

> 🌐 [Русский](M_STRUCTURED_OUT_CONTRACT.md) (основная версия) · **English**

> Status: **Accepted** (ADR-057). Mini-milestone: one wave on top of `main` (after PR #67). Implements LLM-problem #2.

## 1. Why

The brain extracted JSON from LLM replies with a fragile `json.loads(text[text.find("{"): text.rfind("}") + 1])`
slice at **6 sites** (`brain/planner.py`×4, `brain/healing.py`×2). The slice breaks on: prose after the
JSON with a stray `}`, markdown ` ```json ` fences, a truncated reply, and any model that wraps the
object in commentary. This milestone makes the output strict: native structured output where the
provider supports it, and a robust extractor as the universal fallback for models without tool-use.

## 2. Layered design

| Backend | `supports_structured` | JSON path |
|---|---|---|
| `AnthropicBackend` (default) | `True` (always) | native forced `tool_use` |
| `OpenAICompatBackend` | opt-in `LLM_STRUCTURED[_ROLE]=1` (default **False**) | `response_format=json_schema` if ON, else robust extract |
| `SamplingBackend` (MCP) | `False` | robust extract |

Default-OFF for OpenAI-compat is deliberate: many local endpoints (Ollama/vLLM) reject
`response_format=json_schema` → they get the robust extractor (strictly better than the old
`find('{')`). Anthropic (the default) always gets the reliable native path.

## 3. `brain/llm.py` surface

- `LLMResult.data: Optional[dict]` — the parsed object (native or downstream); `None` for plain text.
- `LLMBackend`: +`supports_structured: bool` +`complete_json(prompt, *, schema, max_tokens, temperature) -> LLMResult`.
- `AnthropicBackend.complete_json` — `tools=[{name:"emit", input_schema}]` + forced `tool_choice`; `.data` = the `tool_use` block's `input`.
- `OpenAICompatBackend.complete_json` — `response_format={type:"json_schema", …}`; `.data = json.loads(content)` (None on non-JSON → salvage).
- `extract_json(text) -> dict` — string-aware balanced-brace scan from the first `{` to its matching `}`; last-ditch the original `find/rfind` slice; raises on a missing object.
- `complete_structured(backend, prompt, schema, *, max_tokens, temperature) -> LLMResult` — native when `supports_structured` (salvage via `extract_json` if `.data is None`), else `complete`+`extract_json`. Always carries `.data` + real token counts.

## 4. Migrated consumers (prompts stay byte-identical)

5 text sites → `complete_structured(self._backend, prompt, _SCHEMA, …)`; `j = result.data`:
- `planner.py`: `LLMPlanner` (`_SCHEMA_PICK`) · `GoalPlanner.propose` (`_SCHEMA_PICK`) · `build_scenario` (`_SCHEMA_STEPS`) · `DescribePlanner.draft` (`_SCHEMA_DRAFT`).
- `healing.py`: `_llm_reground` (`_SCHEMA_PICK` — an index into the live element list, ADR-082).

1 vision site → `_visual_reground`: `complete_vision(…)` + `extract_json(result.text)`.

The `budget.tracker().exceeded(…)` guard, grounding (`int(j["index"])` → `candidates[idx]`, OOB → done)
and the fallback-to-heuristic-on-exception are all unchanged.

## 5. Tests

`tests/test_m_structured_out_offline.py` (self-executing, in the CI loop `m_structured_out`): extract_json
units (fences/prose/nested/brace-in-string/no-object→raise) · complete_structured routing (native/fallback/salvage) ·
planner ×4 + heal css/visual integration under BOTH backends · budget over-budget skip + token accounting.
Invariant: `StructuredBackend.complete()` raises — if a native path ever text-parses by accident, the test fails.

## 6. Deferred

- Native vision+structured for `_visual_reground` (the least-portable provider combo) — stays on `extract_json`.
- `strict:true` json_schema (all-required / `additionalProperties:false`) — permissive schemas for now (the union is validated in Python).
- Auto-detecting `supports_structured` for OpenAI-compat via a capability probe — an explicit env opt-in for now.

## 7. Acceptance criteria

1. 0 fragile `find('{')` slices in consumers; text → `complete_structured`, vision → `extract_json`.
2. Anthropic native `tool_use`; OpenAI-compat `json_schema` when `LLM_STRUCTURED=1`, else robust; sampling/local robust. No run crashes on a non-JSON reply (→ heuristic/empty).
3. Grounding (index/refs) + `plan_hash` unchanged; the existing 17 offline tests stay green.
4. Budget accounting + over-budget skip preserved.
5. New self-executing test in the ci.yml loop; all gates green (go · python×18 · bilingual · gitleaks).
6. ADR-057 in both ARCHITECTURE files; bilingual contract pair; FILEMAP updated.

> ADR-057 — see `ARCHITECTURE.en.md` §3.
