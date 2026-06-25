# M2b Contract — "Service Layer" (frozen 2026-06-24)

> 🌐 **Русский** (основная версия) · [English](M2b_CONTRACT.en.md)

Цель: погасить долг из временного отклонения ADR-012 — ввести **Go `store-gateway` (gRPC)** как
единственного SQLite-писателя (восстанавливает ADR-007) и мигрировать транспорт brain↔pw-executor на **MCP SDK**
(закрывает GAP-VERIFY-002). Чистая инфраструктура: **никакой новой пользовательской ценности**, и **наименее
offline-тестируемый** milestone (требует живых процессов Go+Python+Node). Разбит на две независимые части.

## Разбивка scope (выполнять M2b-1 первым)
- **M2b-1 — Go store-gateway + gRPC + proto.** Заменить SQLite в `brain/store.py` Go-сервисом,
  владеющим БД; brain общается с ним через gRPC. Восстанавливает single-writer (ADR-007).
- **M2b-2 — MCP-SDK transport.** Заменить написанный вручную newline JSON-RPC (brain↔pw-executor)
  MCP SDK; `pw-executor` становится MCP-сервером, brain — MCP-клиентом. Закрывает GAP-VERIFY-002,
  реализует ADR-002 «LangGraph связывает MCP-инструменты нативно».

## Ключевой рычаг (замены с низким риском)
`brain/store.py` и `brain/executor.py` — чистые интерфейсы. M2b заменяет только их **реализации** —
`healing.py` / `replay.py` / `calibrate.py` / `graph.py` продолжают вызывать те же
`store.<method>` / `ex.call(method, **params)` и остаются **неизменными**. Это ограничивает радиус взрыва.

## M2b-1 — proto / store-gateway / gRPC
**proto (`proto/persistence.proto`, protobuf3)** — `PersistenceService` зеркально повторяет текущий `Store` 1:1:
`Lookup`, `EvictStale`, `SaveLocator`, `BumpUsed`, `AppendAudit`, `SaveGolden`, `GetGolden`,
`RecordStep`(→quarantined bool), `IsQuarantined`, `ClearQuarantine`, `AuditRows`(для calibrate).
Стабы генерируются для Go + Python в CI; хэш `.proto` проверяется против зарегистрированных стабов (расхождение = ошибка сборки).

**Go store-gateway (`internal/store/`)** — владеет SQLite (WAL) + миграциями схемы + single-writer;
реализует `PersistenceService`. Жизненный цикл: **agentctl запускает его как дочерний процесс** через Unix-domain socket
(local/CI), передавая адрес в brain через `STORE_ADDR`. Опция TCP для K3s позже.

**Brain (`brain/store.py`)** — переписан как тонкий gRPC-клиент, сохраняющий ТОЧНЫЕ текущие сигнатуры методов (drop-in). Прямой доступ `calibrate.py` к `store.db` заменён RPC `AuditRows`.

**Toolchain (VERIFY при реализации):** `protoc`/`buf`; Go `google.golang.org/grpc` + `google.golang.org/protobuf`;
Python `grpcio` + `grpcio-tools`; предпочтителен чистый Go SQLite-драйвер (`modernc.org/sqlite`, cgo-free).

**Гейт M2b-1:** при работающем store-gateway explore + baseline + replay + calibrate ведут себя идентично
(те же exit codes / heal / golden); `grep -r sqlite3 brain/` не возвращает ничего (brain не держит дескриптор БД);
Go unit-тесты для gateway; Python offline-набор работает с in-proc fake store, реализующим тот же интерфейс (так что тесты trust-layer/heal остаются свободными от browser+grpc).

## M2b-2 — MCP-SDK transport
**pw-executor** — переписать `server.ts` на `@modelcontextprotocol/sdk` (`McpServer` + `StdioServerTransport`),
регистрируя 7 инструментов (navigate, snapshot, click, probe, interactives, screenshotHash, traceStop) со схемами входных данных; поведение то же, stdout зарезервирован для протокола.
**Brain (`brain/executor.py`)** — заменить написанный вручную клиент MCP stdio-клиентом (`mcp`), обёрнутым
за существующим `Executor.call(method, **params)`, так что `graph`/`healing`/`replay` не изменяются. Сохранить
fallback на JSON-RPC клиент с feature-flag (снижение риска GAP-VERIFY-002).
**Toolchain (VERIFY):** `@modelcontextprotocol/sdk` (npm), `mcp` (pypi) — подтвердить версии + API
`McpServer.registerTool` / `ClientSession.call_tool` перед кодированием (anti-hallucination).
**Гейт M2b-2:** `tools/list` возвращает 7 инструментов; live-гейты M0–M3 всё ещё проходят через MCP transport.

## Тестируемость (честно)
Кросс-процессные gRPC + MCP-stdio не могут быть полностью проверены offline здесь → **live-гейты передаются
пользователю** (как для M0–M3). Offline-покрытие сохраняется через: in-proc fake `PersistenceService` для
Python-набора, Go unit-тесты для gateway и contract-тесты схемы инструментов MCP.

## ADR'ы
- **ADR-015 (M2b-1):** store-gateway = Go gRPC-сервис, запускаемый agentctl через UDS; `brain/store.py`
  становится тонким gRPC-клиентом, сохраняющим интерфейс методов (drop-in). Восстанавливает ADR-007.
- **ADR-016 (M2b-2):** pw-executor мигрирует на MCP SDK; brain обёртывает MCP-клиент за существующим
  интерфейсом `ex.call`; JSON-RPC сохраняется как задокументированный fallback.

## Вне scope
Наблюдаемость M4b (Go report-service, OTel→Tempo, Prometheus HTTP) · M5 (visual heal PoC, K3s/ArgoCD).
