# M7 Contract — "MCP-Server Exposure" (PROPOSED, frozen 2026-06-25)

> 🌐 **Русский** (основная версия) · [English](M7_CONTRACT.en.md)

Статус: **Proposed** (ADR-020). Docs-first — контракт заморожен сейчас, имплементация — следующий
milestone (требует живого MCP-host для прогона, user-run).

Цель: второе направление запроса пользователя — Sentinel **вызывается из** агентов-хостов
(OpenCode, Kilocode, Claude Desktop), которые сами поставляют модель. Экспонируем «мозг» как
**MCP-сервер**; host драйвит его и через MCP `sampling/createMessage` поставляет модель.

## Ключевой рычаг
B1 (M6/ADR-019) уже даёт абстракцию `LLMBackend`. M7 = ещё одна её реализация:
`SamplingBackend(LLMBackend)`, маршрутизирующая LLM-вызовы в host. **Никаких изменений
planner/healing** — они уже ходят через `make_backend` / инжектируемый backend.

## Scope (имплементация — следующая сессия)
- **Sentinel-as-MCP-server** (НОВЫЙ, отдельный от MCP-сервера pw-executor): tools
  `explore` / `heal` / `replay` / `report` со схемами входа; stdout зарезервирован под протокол.
- **`SamplingBackend`**: `complete()` → `sampling/createMessage` (single user message → текст);
  `supports_vision=False` (basic sampling без vision ⇒ heal деградирует в L1–L6); токены `0`;
  `LLMResult.model` = реальная модель хоста. Sync↔async мост — как `McpExecutor`
  (`executor.py:99–114`, background loop + `run_coroutine_threadsafe`).
- Выбор: env `LLM_BACKEND=sampling` (или авто-детект server-mode).

## Honest constraints (VERIFY при имплементации, anti-hallucination)
- Поддержка MCP `sampling` неравномерна по хостам (Claude Desktop — да; **OpenCode / Kilocode —
  VERIFY до кодирования**). Если host не даёт sampling → backend недоступен → fallback на heuristic/L1–L6.
- OpenCode / Kilocode — это агенты-клиенты / агрегаторы провайдеров, **не model-API**. «Работать с
  ними» здесь = они драйвят Sentinel как MCP-tool, модель — их.
- Инверсия архитектуры: в server-mode Sentinel перестаёт быть автономным CLI. Контракт
  explore-once / replay-many и determinism сохраняются (replay LLM-free), но explore инициирует host.

## Гейт (когда реализуем)
- `tools/list` MCP-сервера Sentinel возвращает `explore`/`heal`/`replay`/`report`.
- Прогон из реального MCP-host (user-run): host драйвит explore, sampling поставляет модель, артефакты
  идентичны CLI-режиму.
- Offline: contract-тест схем tools + `SamplingBackend` через fake sampling-session (паттерн `FakeBackend`).

## ADR
ADR-020 (Proposed). Опирается на ADR-019 (`LLMBackend`). MCP-сервер **отдельный** от ADR-016 (pw-executor).

## Вне scope
Имплементация в этом сеансе (только контракт + ADR). Vision через sampling (basic sampling без vision).
