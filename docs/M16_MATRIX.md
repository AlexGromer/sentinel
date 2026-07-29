# Матрица CLI↔UI — измерение разрыва, 2026-07-29

Источник для `docs/M16_CONTRACT.md`. Каждая строка — **пользовательская возможность**,
сформулированная как цель человека, а не как деталь реализации. Снято инвентаризацией
диспетчера `cmd/agentctl/main.go:905-933` (плюс вызов самого бинаря), маршрутов
`cmd/control-api/main.go:1855-1903`, разметки и обработчиков `docs/index.html` и живого
`GET /v1/config-schema`.

## Вердикты

| вердикт | значение | шт |
|---|---|---|
| `ui-missing` | в UI недостижимо | 22 |
| `ui-only` | существует только в UI — под принципом §2.1 это тоже разрыв (нет в CLI и/или HTTP) | 15 |
| `parity` | есть на всех поверхностях, равная полнота | 5 |
| `ui-partial` | в UI есть, но выражает меньше, чем CLI/HTTP | 5 |
| `dead-in-ui` | элемент управления есть, обработчик не работает | 1 |
| `first-launch-exempt` | bootstrap, выведен из-под принципа | 1 |

## Разрыв по поверхностям

| поверхность | возможность отсутствует | из 49 |
|---|---|---|
| CLI (`agentctl`) | 20 | 41 % |
| HTTP (control-api) | 18 | 37 % |
| UI (хаб) | 23 + 5 частично | 47 % / 57 % |
| **есть на всех трёх** | **5** | **10 %** |

## Прогон

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Jump to the Live view and auto-connect to a run selected from the Runs list ('Watch') | `dead-in-ui` | none | none dedicated (would reuse GET /v1/stream) | 👁 Watch button, Library › Runs, data-watch (docs/index.html:2853, handler 2860) |
| Configure run-scoped token budgets, session-auth reuse, and a RunConfig scenario/file for a run | `ui-missing` | agentctl run --run-config <yaml> / --scenario (main.go:480-481); RunConfig carries plan_budget/heal_budget/tot | runRequest struct has NO budget/auth/scenario fields at all — only Target,Mode,Goal,Describe,Planner,CoverageT | Run view has inputs for all of these (b-planbud/b-healbud/b-totbud/b-ss/b-sss/b-loginplan/b-pwnotrace, docs/in |
| Toggle CI-safety override flags for a run (--ci, --force-replay, --aut-version) | `ui-missing` | agentctl run --ci --force-replay --aut-version (main.go:487-489) | none — runRequest struct carries none of these three (main.go:1061-1062) | none found — grepped 'aut-version','aut_version','AUT_VERSION','b-ci','force-replay','force_replay','FORCE_REP |
| Replay a previously recorded plan (frozen regression re-run) | `ui-partial` | agentctl run --replay --plan <path> (any plan.json on disk, main.go:484-485,508-511) | POST /v1/runs mode:'replay', from_run:<run_id> (main.go:558-604) — only replays a run already known to control | 🔁 Re-run (id=ch-rerun / b-rerun, docs/index.html:913/837) → bSubmit/chRunFlow with mode:'replay', from_run:bLa |
| ↳ **не выразимо в UI:** Arbitrary/external --plan file path. UI/HTTP can only replay a run the server already tracks by run_id (from_run); there is no way to upload or reference a plan.json that did not originate from a run  | | | | |
| Re-baseline a run's golden assertions | `ui-partial` | agentctl baseline update --plan <path> [--target --artifact-dir] (main.go:561-584) | POST /v1/runs mode:'baseline', from_run:<id> only (main.go:558-604) | 📌 Baseline (b-baseline/ch-baseline, docs/index.html:838/914) → mode:'baseline', from_run:bLastRunId/chLastRunI |
| ↳ **не выразимо в UI:** --target override (baseline against a URL different from the plan's own target_url) and an arbitrary external --plan path; HTTP/UI only accept from_run of a server-tracked run. | | | | |
| Toggle heal-llm / heal-visual healing for a single run | `ui-partial` | agentctl run --heal-llm (main.go:486, per-run flag); HEAL_VISUAL exact-allowlisted through to the brain subpro | POST /v1/runs llm object only carries backend/base_url/model_planner/model_heal/vision (bLLM(), docs/index.htm | Setup wizard renders 'heal_llm'/'heal_visual' as GLOBAL persisted config fields (docs/setup/index.html:464,474 |
| ↳ **не выразимо в UI:** No per-run override for heal_llm/heal_visual in the Run view or in the POST /v1/runs body — only a persisted global default reachable via the Setup Wizard's PUT /v1/config, which affects every subsequ | | | | |
| Cancel a currently running execution | `ui-partial` | none — no cancel/kill subcommand among the 12 (main.go:905-933) | POST /v1/runs/{id}/cancel — works for ANY run id, signals the process tree directly (cancel.go:33) | ■ Stop (id=ch-stop, docs/index.html:912, handler 2516→chStopRun 2497) posts to /v1/runs/{chRunningId}/cancel — |
| ↳ **не выразимо в UI:** No stop/cancel control exists anywhere for a run started from the Run/Build view (grepped 'b-stop','bStopRun','b-cancel','bCancel' across docs/index.html — zero hits). Only chat-mode runs are cancella | | | | |
| Attach to a live run's real-time AG-UI event timeline and take over manual control (HITL) | `ui-only` | none — no agentctl subcommand attaches to a live run | GET /v1/stream (WS, run_id query) + control frames {type:'control',action:'takeover'\|'return'} (ws.go:199) | Live view 🔌 Connect / 🖐 Take over / ↩ Return (docs/index.html:929-930, handlers 3002,2945-2950) |
| Launch an explore/goal/describe run against a target URL | `parity` | agentctl run --target ... [--mode --goal --describe --planner --coverage-target --max-steps] (cmd/agentctl/mai | POST /v1/runs, runRequest{Target,Mode,Goal,Describe,Planner,CoverageTarget,MaxSteps,LLM} (main.go:1056,1061-10 | Run view ▶ Run (id=b-run, docs/index.html:833, click handler 2287-2298) posts target/mode/planner/goal/describ |
| Choose planner strategy and override LLM backend/model/vision for a run | `parity` | agentctl run --planner (main.go:477); LLM_BACKEND/LLM_MODEL/LLM_VISION set via shell env for that invocation ( | planner field in runRequest + llm{backend,base_url,model_planner,model_heal,vision} object (main.go:1061-1062; | #b-planner select (docs/index.html:732-733, wired 2291) + bLLM() builds {backend,base_url,model_planner,model_ |
| Continue a multi-turn chat conversation via a stable conversation id | `parity` | agentctl run --conversation-id (main.go:490, used with --mode chat, not validated as required) | conversation_id field in chatRequest/runRequest (main.go:1061-1062,1677-1678) | Chat view auto-manages chConvId/chTurnN (docs/index.html:2477,2565-2572) + ▶ Resume (data-resume, 2898, handle |

## Авторинг и библиотека

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Schedule a promoted test for recurring/automatic execution | `ui-missing` | none | POST /v1/tests/promote accepts a `schedule` field, but it is stored and NEVER executed — no scheduler exists a | none — grepped 'schedule' across docs/index.html: zero matches; tPromoteReq (docs/index.html:2792-2796) only e |
| Promote a saved scenario into a durable, named test | `ui-only` | none — no agentctl subcommand touches tests/scenarios (12-command switch, main.go:905-933) | POST /v1/tests/promote {scenario_id,name} (main.go:1482,1487-1488) | ⇪ Promote (data-promote, docs/index.html:2756, handler 2764→tPromote 2802→tPromoteReq 2792) |
| Rename a saved test | `ui-only` | none | no dedicated rename RPC — implemented client-side as promote-under-new-name then delete-old (comment docs/inde | ✎ Rename (data-rename-test, docs/index.html:2777, handler 2786→tRenameTest 2807) → POST /v1/tests/promote then |
| Delete a saved scenario or test | `ui-only` | none | DELETE /v1/scenarios/{id} (main.go:1419), DELETE /v1/tests/{id} (main.go:1462) | 🗑 delete scenario (2757→tDeleteScenario), 🗑 delete test (2778→tDeleteTest 2820) |
| Browse/list saved scenarios and tests | `ui-only` | none | GET /v1/scenarios (main.go:1387), GET /v1/tests (main.go:1430) | Library view, ⟳ Refresh (lib-refresh, 946→tLoadLibrary 2790) |
| Browse/list saved chat conversations | `ui-only` | none | GET /v1/chats (main.go:1508) | Library › Chats, ⟳ Refresh (chats-refresh, 966→tLoadChats 2910) |
| Delete a saved chat conversation | `ui-only` | none | DELETE /v1/chats/{id} (main.go:1540) | 🗑 delete chat (data-del-chat, 2899→tDeleteChat 2916→2918) |

## Импорт и экспорт

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Transpile an existing test suite pulled from a git repository (clone + ref) | `ui-missing` | agentctl import --from-git <url> --ref <branch/tag> (main.go:755-756) | none — import_handler.go only accepts a files[] upload, no from-git/ref parameter | none — grepped 'from-git','from_git','--ref' across docs/index.html: zero matches (only the file-upload Import |
| Ground an import against a live application via a nested explore (--verify/--target) | `ui-missing` | agentctl import --verify --target <url> (main.go:758-759,779-782) — runs a nested explore and grounds the tran | none — import_handler.go's importRequest has no verify/target fields | none found |
| Ground/import using a supplied explore-map JSON instead of running a fresh explore (--map) | `ui-missing` | agentctl import --map <site-map.json> (main.go:757, mutually exclusive with --verify) | none | none found |
| Export a frozen plan to a runnable Playwright .spec.ts file | `ui-missing` | agentctl export-spec --plan <path> [-o <out>] (main.go:600-617) | none — grepped for /v1/export-spec, ExportSpec: no route/handler anywhere | none — grepped 'export-spec','exportSpec' across docs/index.html: zero matches |
| Land exported spec files into a git repository/worktree, optionally push (with branch/subdir/me | `ui-missing` | agentctl export-git --to-git <url> --spec <file>... [--branch --subdir --message --push] (main.go:663-738) | none — grepped for export-git/ExportGit/export_git: no route/handler | none — grepped 'export-git','to-git' across docs/index.html: zero matches |
| Transpile an existing local test-file suite (Playwright/Cypress) into Sentinel plans | `parity` | agentctl import --from <dir> (main.go:754,762) | POST /v1/import {files[].name, files[].content} (import_handler.go:38,43-44) | Import button (id=imp-go, docs/index.html:863, handler 3995-4014) uploads *.spec.ts/.spec.js/.cy.ts/.cy.js as  |

## Версионирование

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| List a test's revision history | `ui-missing` | agentctl revisions list --test <id> (main.go:625-652) | GET /v1/tests/{id}/revisions (revisions_handler.go:106) | none — grepped 'revisions' across docs/index.html: zero matches |
| Show one revision's content | `ui-missing` | agentctl revisions show --test <id> --rev <rev> (main.go:632-641) | GET /v1/tests/{id}/revisions/show (revisions_handler.go:112) | none found |
| Diff two revisions of a test | `ui-missing` | agentctl revisions diff --test <id> --rev <a> --rev-b <b> (main.go:633-634) | GET /v1/tests/{id}/revisions/diff (revisions_handler.go:109) | none found |
| Roll a test back to an earlier revision | `ui-missing` | agentctl revisions rollback --test <id> --rev <target> (main.go:636-641) | POST /v1/tests/{id}/revisions/rollback?to=<rev> — `to` is REQUIRED with no default (revisions_handler.go:63-69 | none found |

## Результаты

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Regenerate a report (html/json/junit/metrics.prom) on demand for an existing run directory | `ui-missing` | agentctl report --run <dir> (main.go:834-847) | none dedicated — `agentctl report` is only auto-invoked internally by spawnRun's post-run pipeline (main.go:72 | none — the UI only fetches already-generated report.json/report.html/junit.xml via GET /v1/runs/{id}/artifact; |
| Browse/download a run's artifact files (plan.json, report.html, trace.zip, etc.) | `ui-partial` | direct filesystem access to the run directory — any file, no restriction (implicit, not an agentctl subcommand | GET /v1/runs/{id}/artifact?name=<whitelisted> — fixed whitelist of 13 names (main.go:1278, list given in HTTP  | bShowArtifacts / per-run artifact viewer + download=1 query (main.go:1278 consumed via docs/index.html per giv |
| ↳ **не выразимо в UI:** Any file in the run directory NOT in the fixed 13-name whitelist (e.g. raw stdout capture or ad-hoc debug files a specific brain build might drop) is invisible to HTTP/UI and reachable only via direct | | | | |
| Browse historical run verdicts/results across all runs | `ui-only` | none — no query/list command; report.json/junit.xml exist only per-run-directory on disk | GET /v1/results (main.go:1802, limit/offset) | Results view, ⟳ Refresh (results-refresh, 978→tLoadResults 2675) |
| Inspect one historical result record | `ui-only` | none | GET /v1/results/{id} (main.go:1817) | reachable from the Results view list (tLoadResults render, per given inventory) |
| View metric trend charts over time | `ui-only` | none | GET /v1/trends?metric=...&window=50 (main.go:1834) | Metrics subpanel, ⟳ Refresh (metrics-refresh, 987→tLoadMetrics 2716, loops per metric) |
| Stream, filter, and search structured diagnostic logs for a run | `ui-only` | none — no agentctl subcommand; runs/control-<id>/logs/run.jsonl is a raw file readable by any text tool outsid | GET /v1/runs/{id}/logs (lvl/cat/mod/code/src/step/q/after/limit params, logs_api.go:36) | Logs view — run selector, level/src/cat/mod filters, regex/case/sort toggles, search box, ★ Save / ✕ Clear (do |

## Конфигурация

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Manage local user accounts / per-user login (OSS-core-scoped identity) | `ui-missing` | none | none — no user/tenant/owner column in any table (internal/store/server.go schema, per lead's given fact), no i | none — Settings view only holds a single shared bearer-token input (#capitok), not a login/user system |
| OIDC/SSO/RBAC (multi-user identity and role-based access) | `ui-missing` | none | none | none |
| Discover available RunConfig/LLM/retention knobs (schema introspection) | `ui-only` | none — no `agentctl config` or schema-dump command; knobs are discovered by reading brain/runconfig.py or docs | GET /v1/config-schema, no auth (main.go:306) | Setup wizard dynamically renders a form from this schema (docs/setup/index.html, e.g. heal_llm at line 464, au |
| Read the persisted global config document | `ui-only` | none | GET /v1/config, bearer (readyz.go:248-249) | Setup wizard loads current values into its generated form |
| Write/update the persisted global config (LLM backend/model, retention policy, heal thresholds, | `ui-only` | none — CLI never persists a global config; all its knobs are per-invocation env vars or a per-run YAML file | PUT /v1/config, bearer, validated by configguard.Validate (readyz.go:293-326) | Setup wizard save action (schema-driven form, docs/setup/index.html) |
| Set retention policy for traces/runs/logs (keep-count and TTL hours) | `parity` | SENTINEL_TRACE_KEEP/_TTL_HOURS, SENTINEL_RUN_KEEP/_TTL_HOURS, SENTINEL_LOG_KEEP/_TTL_HOURS — env vars read on  | same six fields exposed via GET/PUT /v1/config schema (cmd/control-api/main.go:363-407) | Setup wizard fields 'trace_keep'/'trace_ttl_hours'/'run_keep'/'run_ttl_hours'/'log_keep'/'log_ttl_hours' |

## Операции

| возможность | вердикт | CLI | HTTP | UI |
|---|---|---|---|---|
| Clear the locator quarantine table (retry previously-blacklisted locators) | `ui-missing` | agentctl locators clear-quarantine (main.go:586-597) — only accepted spelling, no flags | none — grepped '/v1/locators','locators','handleLocators': no hits anywhere in mux() | none found |
| Recompute heal-precision calibration from healing_audit | `ui-missing` | agentctl calibrate (main.go:881-888, ignores any trailing args) | none — grepped 'calibrate'/'Calibrate': no route/handler | none found |
| Redact secrets from an existing trace.zip on demand | `ui-missing` | agentctl redact-trace --trace <path> (main.go:860-878) | none dedicated — ADR-098 redaction is applied automatically server-side to trace.zip artifacts produced by a r | none found |
| Purge/delete rows from named store tables (compliance-grade data deletion) | `ui-missing` | agentctl purge-store --tables <list> [--older-than --vacuum] --yes (purge.go:28-113) | none — grepped 'purge-store'/'PurgeStore'/'purge_store': no route/handler | none found |
| Sweep/delete whole run directories marked as already downloaded | `ui-missing` | agentctl sweep-downloaded [--yes\|--dry-run] (sweep_downloaded.go:49-88) | none — grepped 'sweep-downloaded'/'SweepDownloaded': no route (note: an unrelated automatic internal mechanism | none found |
| Control the host-env passthrough allowlist into the brain process (security-relevant) | `ui-missing` | SENTINEL_ENV_ALLOWLIST=0 (disable), SENTINEL_ENV_ALLOW=<names> (extra names) — env-only (main.go:341,361) | none | none found |
| Select the brain's Python interpreter / attach to a remote browser via CDP / force a headed bro | `ui-missing` | BRAIN_PYTHON (main.go:396), PW_CDP_ENDPOINT (pw-executor/src/launch.ts:23), PW_HEADLESS=0 / PW_HEADED=1 (launc | none | none — grepped 'PW_CDP_ENDPOINT','cdp','PW_HEADLESS','PW_HEADED' across docs/setup/index.html and cmd/control- |
| Check system health/readiness (dependency probes: store, LLM, config) | `ui-only` | none — agentctl itself has no healthcheck/doctor subcommand | GET /healthz (main.go:295), GET /readyz (readyz.go:227, detail gated behind bearer) | Settings 'Проверить/Check' (cap-check, 645→1686-1700) + Live rail LLM probe polling /readyz (2314-2350) |
| Bootstrap/deploy control-api itself (listen address, token source, CORS origins, store-gateway/ | `first-launch-exempt` | CONTROL_API_ADDR/TOKEN/TOKEN_FILE/AUTOTOKEN/PRINT_TOKEN/CORS_ORIGINS/STORE_ADDR/STORE_TOKEN/ORCH_ADDR/UI_DIR/S | none (control-api takes no CLI flags either, per lead's given fact — configured purely by env) | none — cannot exist: the UI is itself served BY control-api, so none of these can be set through a UI that doe |

## Проверка на опровержение

Оба опасных класса утверждений проверены отдельными проходами, каждый с установкой
«утверждение неверно, пока не доказано обратное»:

- **«отсутствует в UI»** (22 строки) — ни одна не опровергнута; ложное «отсутствует» стоило бы
  повторной постройки уже существующего;
- **«присутствует в UI»** — одна поправка: политика удержания оказалась `ui-partial`, а не
  `parity` (офлайновый снимок схемы мастера не несёт `run_keep`, `run_ttl_hours`, `trace_always`).
