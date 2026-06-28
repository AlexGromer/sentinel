# Контракт M12 — единая config+chat консоль + OpenAI-compat shim (вариант i)

> 🌐 **Русский** (основная версия) · [English](M12_CONTRACT.en.md)

> **Статус**: Фаза-1 (shim) — ✅ **DELIVERED**; Фаза-2 (единая страница) — ✅ **DELIVERED** · **Дата**: 2026-06-28
> вводит **ADR-041** (OpenAI-compat shim) · опирается на ADR-032 (control-API security) + ADR-040 (SSE-машинерия)

---

## Цель

Одна страница на GitHub Pages, где **и чат, и управление прогонами — «одна модель»**: пользователь
описывает тест словами → Sentinel авторит сценарий и запускает прогон → прогресс стримится → готовый
`scenario.json` скачивается; рядом — подробный конфигуратор (RunConfig YAML/env + калькуляторы). Фундамент —
**вариант (i): OpenAI-compat shim** на control-API, поэтому ЛЮБОЙ OpenAI-клиент (Open WebUI,
DeepSeek/Mistral-клиенты, SDK, наша страница) драйвит Sentinel «как модель».

**Решения (подтверждены пользователем):**
- **(i) shim — сейчас** (фундамент-протокол); **(iii) AG-UI/CopilotKit — позже** (rich-фронт для co-pilot, фаза M9.8+).
- **Чат v1 = one-shot**: brain делает один проход (одно сообщение = один прогон → `scenario.json`).
  Мульти-тёрн диалог — отдельная brain-extension веха.
- Эта веха **раньше** M9.8-impl. Транспорт расширения M9.8 = **WS** (native-messaging — задокументированная альтернатива).

## Фаза-1 — OpenAI-compat shim (✅ DELIVERED, ADR-041)

`cmd/control-api`: новый **`POST /v1/chat/completions`** (stdlib, без новых зависимостей; token-gated через
`s.authed`, CORS через `s.cors`). Тело `handleCreateRun` вынесено в переиспользуемый **`spawnRun(req) *run`**
(build args + goroutine + `runStream`), который зовут и `POST /v1/runs`, и shim.

**Маппинг чат-тёрна → прогон:**
- **Режим**: из `model` (`sentinel` → describe · `sentinel-goal` → goal · `sentinel-explore` → explore)
  ИЛИ из ведущего префикса `goal:`/`explore:`/`describe:` (префикс приоритетнее). Дефолт — describe.
- **Цель**: самая свежая (последняя) `http(s)://`/`file://` ссылка среди всех сообщений (поддержана строка `target: <url>`).
- **Инструкция**: последний user-message (минус `target:`-строки).

**Ответ (OpenAI wire):**
- `stream:true` → SSE-кадры `chat.completion.chunk`: `delta.role` → построчные `delta.content` (лог прогона из
  `runStream`) → финальный `delta.content` с вердиктом → `finish_reason:"stop"` → `data: [DONE]`.
- `stream:false` → один `chat.completion`: `message.content` = лог + вердикт (по exit-коду: 0 pass / 1 нашёл
  проблему / 2 visual-golden / 3 config-error) + содержимое `scenario.json`.
- Нет цели/инструкции → дружелюбный chat-ответ-подсказка (200), не HTTP-ошибка.

**Безопасность**: тот же bearer-токен + CORS-allowlist + localhost-bind (ADR-032). Спавн — только известный
`agentctl`, target валидируется. Гейты: `go build/vet/test -race` + `gofmt` + 5 httptest
(`parseChatInstruction` unit · 403 без токена · non-stream · stream · no-target) + live curl smoke (stream +
non-stream + 403).

## Фаза-2 — единая `docs/index.html` (✅ DELIVERED)

**Реализовано** (`docs/index.html` 905→1469; калькуляторы нетронуты): 3 секции на neon-хаб — **#connect** (control-API URL+token, memory-only), **#build** (RunConfig-builder: YAML/env/cmd + download + ▶Run), **#chat** (describe/goal/explore → SSE-via-fetch → вердикт + скачать `scenario.json`); общий SSE/poll-драйвер; bilingual (`data-lang`/`setLang`/`sentinel_lang`); air-gapped; `setup`/`chat` — standalone advanced deep-links; заметка про OpenAI-shim (`/v1/chat/completions`) в чат-секции; `node --check` clean.

Эволюция neon-хаба (`docs/index.html`): добавить две секции, драйвящие control-API — **(a) RunConfig-builder**
(порт `docs/setup` `render()` → YAML/env/cmd + download) и **(b) chat-панель** (порт `docs/chat`
`streamEvents()` SSE-via-fetch + transcript + выгрузка артефактов). Общий connection-panel (URL + bearer,
memory-only) питает и live-run билдера, и чат. Билингва (`data-lang`/`setLang`/`sentinel_lang`), одна neon-палитра,
air-gapped. `docs/setup`+`docs/chat` остаются standalone «advanced» deep-link'ами. Калькуляторы хаба — как есть.

## Дальше по roadmap (порядок пользователя)
M12 → закрытие хвостов (M9-LIVE; оставшиеся GAP; GAP-RISK-009) → adopt LiteLLM (опц. роутер) + MCP Inspector →
**M9.8-impl** (расширение + WS) + AG-UI/CopilotKit (rich co-pilot) → Langfuse/DSPy после пользовательских тестов.

## Отложено
- Полный conversational-чат (мульти-тёрн) — нужна доработка brain (conversation-state).
- Проброс budget-флагов через `POST /v1/runs` (agentctl берёт бюджеты из env/`--run-config`, не из run-флагов) —
  билдер кладёт бюджеты в YAML/env; в M12 agentctl не трогаем.
