# M0 Contract — "Hello Browser" (frozen 2026-06-23)

> 🌐 **Русский** (основная версия) · [English](M0_CONTRACT.en.md)

Цель M0: **проверить передачу данных через все три языка и сформировать `trace.zip`** — НЕ сбор разведывательных данных.
Scope исключает: gRPC, store-gateway, LLM, healing, полный 9-узловой LangGraph (они относятся к M1+).

```
agentctl run --target <URL> [--artifact-dir DIR] [--mode explore]   (Go)
  └─ spawns: python3 -m brain      (env contract, Boundary A)        (Python)
       └─ spawns: node pw-executor/dist/server.js  (stdio)           (TypeScript)
            ↑ newline-delimited JSON-RPC 2.0  (Boundary B, MCP-aligned)
```

## Boundary A — agentctl → brain (subprocess + env)
agentctl генерирует `RUN_ID`, создаёт директорию артефактов, затем запускает `python3 -m brain`
(cwd = repo root, `PYTHONPATH` = repo root) со следующими переменными окружения:

| Env var | Значение |
|---------|---------|
| `TARGET_URL` | URL для восприятия (обязательный) |
| `RUN_ID` | случайный hex-идентификатор (генерируется agentctl) |
| `RUN_MODE` | `explore` (только M0) |
| `ARTIFACT_DIR` | абсолютный путь к директории вывода (agentctl создаёт её; по умолчанию `./runs/<RUN_ID>`) |
| `PW_EXECUTOR_CMD` | команда, которую brain использует для запуска pw-executor (например, `node <repo>/pw-executor/dist/server.js`) — Go владеет этой конфигурацией |

brain транслирует stdout/stderr в agentctl (унаследованные потоки). Exit 0 = успех (trace.zip присутствует и непустой), иначе ненулевой. agentctl передаёт код завершения.

## Boundary B — brain → pw-executor (JSON-RPC 2.0 over stdio)
**Транспорт:** один JSON-объект на строку. **КРИТИЧНО:** stdout pw-executor несёт ТОЛЬКО JSON-RPC; все логи идут в stderr.
- Request: `{"jsonrpc":"2.0","id":<n>,"method":"<m>","params":{...}}\n`
- Response: `{"jsonrpc":"2.0","id":<n>,"result":{...}}\n` или `{...,"error":{"code":<c>,"message":"<m>"}}\n`

| Method | Params | Result |
|--------|--------|--------|
| `initialize` | — | `{name, version, capabilities[]}` — также лениво запускает Chromium и начинает трассировку |
| `browser.navigate` | `{url}` | `{url, title, status}` |
| `browser.snapshot` | — | `{ariaSnapshot: <string>, nodeCount}` (Playwright `ariaSnapshot()`) |
| `browser.traceStop` | `{path}` | `{path}` — останавливает трассировку, записывает `path` (trace.zip) |
| `shutdown` | — | `{ok:true}` — закрывает браузер, сервер завершается с exit 0 |

Сессия pw-executor: лениво при первом вызове → `chromium.launch({headless:true})` → `newContext()` → `context.tracing.start({screenshots:true, snapshots:true})` → `newPage()`.

## brain «perceive» flow (M0)
`initialize` → `browser.navigate(TARGET_URL)` → `browser.snapshot()` (вывести aria-дерево в stdout + записать `ARTIFACT_DIR/snapshot.aria.yaml`) → `browser.traceStop(ARTIFACT_DIR/trace.zip)` → `shutdown`. Проверить, что `trace.zip` существует и непустой → exit 0/1.

## Acceptance gate (Given/When/Then)
- **GIVEN** доступный целевой URL и собранные pw-executor + brain + agentctl,
- **WHEN** выполняется `agentctl run --target https://example.com`,
- **THEN** дерево доступности выведено в stdout И `runs/<RUN_ID>/trace.zip` существует с размером > 0 И exit code = 0.

## M1 deltas (NOT in M0)
- Заменить написанный вручную JSON-RPC на **MCP SDK** (`@modelcontextprotocol/sdk` server + Python MCP client) — GAP-VERIFY-002.
- Обернуть `perceive` как один узел в реальный **LangGraph StateGraph** (затем добавить остальные 8 узлов).
- Ввести `RunState`, узел plan (Opus 4.8), сходимость покрытия, `plan_hash`.
