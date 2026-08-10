# Контракт M-STRUCTURED-OUT — строгий structured-output LLM (`tool_use`/`json_schema`) + устойчивый JSON-fallback

> 🌐 **Русский** (основная версия) · [English](M_STRUCTURED_OUT_CONTRACT.en.md)

> Статус: **Accepted** (ADR-057). Мини-веха: одна волна поверх `main` (после PR #67). Реализует LLM-problem #2.

## 1. Зачем

Мозг извлекал JSON из ответа LLM хрупким срезом `json.loads(text[text.find("{"): text.rfind("}") + 1])`
в **6 сайтах** (`brain/planner.py`×4, `brain/healing.py`×2). Срез падает на: prose после JSON со
случайной `}`, markdown-фенсах ` ```json `, усечённом ответе, любой модели, оборачивающей объект в
комментарий. Веха делает вывод строгим: native structured-output там, где провайдер умеет, и
устойчивый extractor как универсальный fallback для моделей без tool-use.

## 2. Слоистый дизайн

| Backend | `supports_structured` | Путь до JSON |
|---|---|---|
| `AnthropicBackend` (дефолт) | `True` (всегда) | native forced `tool_use` |
| `OpenAICompatBackend` | opt-in `LLM_STRUCTURED[_ROLE]=1` (default **False**) | `response_format=json_schema` если ON, иначе robust extract |
| `SamplingBackend` (MCP) | `False` | robust extract |

Default-OFF для OpenAI-compat — намеренно: многие локальные эндпоинты (Ollama/vLLM) отвергают
`response_format=json_schema` → им достаётся robust extractor (строго лучше прежнего `find('{')`).
Anthropic (дефолт) всегда получает надёжный native-путь.

## 3. Поверхность `brain/llm.py`

- `LLMResult.data: Optional[dict]` — распарсенный объект (native или downstream); `None` для plain-text.
- `LLMBackend`: +`supports_structured: bool` +`complete_json(prompt, *, schema, max_tokens, temperature) -> LLMResult`.
- `AnthropicBackend.complete_json` — `tools=[{name:"emit", input_schema}]` + forced `tool_choice`; `.data` = `input` блока `tool_use`.
- `OpenAICompatBackend.complete_json` — `response_format={type:"json_schema", …}`; `.data = json.loads(content)` (None при не-JSON → salvage).
- `extract_json(text) -> dict` — string-aware balanced-brace scan от первой `{` до парной `}`; last-ditch — исходный `find/rfind`-срез; raise при отсутствии объекта.
- `complete_structured(backend, prompt, schema, *, max_tokens, temperature) -> LLMResult` — native при `supports_structured` (salvage через `extract_json` если `.data is None`), иначе `complete`+`extract_json`. Всегда несёт `.data` + реальные токены.

## 4. Мигрированные потребители (промпты байт-идентичны)

5 text-сайтов → `complete_structured(self._backend, prompt, _SCHEMA, …)`; `j = result.data`:
- `planner.py`: `LLMPlanner` (`_SCHEMA_PICK`) · `GoalPlanner.propose` (`_SCHEMA_PICK`) · `build_scenario` (`_SCHEMA_STEPS`) · `DescribePlanner.draft` (`_SCHEMA_DRAFT`).
- `healing.py`: `_llm_reground` (`_SCHEMA_PICK` — индекс в живом списке элементов, ADR-082).

1 vision-сайт → `_visual_reground`: `complete_vision(…)` + `extract_json(result.text)`.

Гейт `budget.tracker().exceeded(…)`, grounding (`int(j["index"])` → `candidates[idx]`, OOB → done) и
fallback-к-heuristic-при-исключении — без изменений.

## 5. Тесты

`tests/test_m_structured_out_offline.py` (self-executing, в CI-loop `m_structured_out`): extract_json-юниты
(фенсы/prose/nested/brace-in-string/no-object→raise) · complete_structured-роутинг (native/fallback/salvage) ·
интеграция планеров ×4 + heal css/visual под ОБОИМ backend'ом · budget over-budget-skip + токен-учёт.
Инвариант: `StructuredBackend.complete()` кидает — если native-путь случайно text-парсит, тест падает.

## 6. Отложено

- Native vision+structured для `_visual_reground` (наименее переносимый провайдер-комбо) — остаётся на `extract_json`.
- `strict:true` json_schema (all-required / `additionalProperties:false`) — сейчас permissive-схемы (union валидируется в Python).
- Авто-детект `supports_structured` для OpenAI-compat по capability-пробе — сейчас явный env-opt-in.

## 7. Критерии приёмки

1. 0 хрупких `find('{')`-срезов в потребителях; text → `complete_structured`, vision → `extract_json`.
2. Anthropic native `tool_use`; OpenAI-compat `json_schema` при `LLM_STRUCTURED=1`, иначе robust; sampling/local robust. Ни один прогон не падает на не-JSON (→ heuristic/empty).
3. Grounding (индекс/refs) + `plan_hash` без изменений; 17 существующих offline-тестов зелёные.
4. Budget-учёт + over-budget-skip сохранены.
5. Новый self-executing тест в ci.yml-loop; все гейты зелёные (go · python×18 · bilingual · gitleaks).
6. ADR-057 в обоих ARCHITECTURE; bilingual contract-пара; FILEMAP обновлён.

> ADR-057 — см. `ARCHITECTURE.md` §3.
