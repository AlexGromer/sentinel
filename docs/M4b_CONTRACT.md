# M4b Contract — "Observability" (frozen 2026-06-24)

> 🌐 **Русский** (основная версия) · [English](M4b_CONTRACT.en.md)

Цель: распределённая трассировка + push-метрики для запусков, теперь когда слой Go-сервисов (M2b) существует.

## Scope decision (ADR-018) — честно
**В M4b (offline-authorable, с gated export):**
- **OTel трассировка в brain** — span запуска + LLM-span'ы, несущие **prompt_HASH, никогда не содержимое prompt**;
  экспортирует в OTLP collector (→ Tempo) ЕСЛИ `OTEL_EXPORTER_OTLP_ENDPOINT` установлен, иначе **no-op** (нулевые накладные расходы).
- **Prometheus Pushgateway** для пакетных метрик (`PROM_PUSHGATEWAY`), так как агент — это **CronJob**.

**Отложено (с обоснованием, → GAP-OBS-001):**
- Всегда включённый HTTP endpoint `/metrics` для Go `report-service` — агент является **эфемерным пакетным заданием**, а не
  scraped-сервисом; Pushgateway / textfile для node_exporter подходят к этой модели. (HTML/JSON отчёт уже поставляется в M4.)
- OTel span'ы TS (`pw-executor`) + Go (`store-gateway`) — точки расширения (W3C context propagation в gRPC/MCP metadata).
- Жёсткий потолок бюджета на стороне Go — требует long-running Go orchestrator + отчётности о токенах brain→Go; эвристический путь по умолчанию не использует LLM, поэтому низкая ценность сейчас.

## OTel (`brain/otel.py`)
- `setup_tracing()` — если `OTEL_EXPORTER_OTLP_ENDPOINT` установлен, настроить `TracerProvider` +
  `OTLPSpanExporter` (gRPC) + `BatchSpanProcessor` + `Resource(service.name="sentinel-brain")`; иначе no-op.
  Устойчив к отсутствию OTel (try/except → no-op).
- Контекстный менеджер `span(name, **attrs)`; `prompt_hash(text)` = `sha256`.
- Span'ы: `sentinel.run` (run_id, mode, transport, store); `heal.llm` / `plan.llm` (model, **prompt_hash**,
  prompt_tokens, completion_tokens). **Атрибуты span'а НИКОГДА не несут prompt или содержимое страницы.** Sampling 100%.

## Prometheus Pushgateway (`brain/report.py`)
`push_metrics(report, gateway, job="sentinel", grouping)` строит `CollectorRegistry` с теми же
`sentinel_*` сериями и вызывает `push_to_gateway(...)`. Вызывается из режима `report` при установленном `PROM_PUSHGATEWAY`;
текстовый файл `metrics.prom` всё равно поставляется.

## Acceptance gate (Given/When/Then)
- **Не установлен:** `OTEL_EXPORTER_OTLP_ENDPOINT` / `PROM_PUSHGATEWAY` отсутствуют → span'ы no-op, нет push, тесты зелёные, нет новых режимов сбоя.
- **Установлен (пользователем, с collector/gateway):** трейсы запуска появляются в Tempo; метрики появляются в Pushgateway.
- **Offline-тест:** путь no-op `otel.setup_tracing()` + `span()` работают без collector; импорт `push_metrics` + no-op guard.

## Вне scope
Go report-service HTTP, TS/Go span'ы, потолок бюджета Go (GAP-OBS-001); SLO-дашборды.
