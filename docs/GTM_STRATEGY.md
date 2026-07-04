# GTM & Monetization Strategy — Sentinel (ЧЕРНОВИК)

> **Статус:** proposed / draft · **Дата:** 2026-07-04 · **Автор:** @AlexGromer + co-architect.
> Не в `ARCHITECTURE.md §3` пока; при принятии proposed-ADR ниже → переносится в канон как **ADR-056**.
> При коммите: `.en`-зеркало ЛИБО добавить в `SINGLE_LANGUAGE`-allowlist (`scripts/check_bilingual.py`) как внутренний strategy-док.
> **Ничего здесь ещё не реализовано** — это карта решений на будущее (пост-M11). Всё смерженное (M0–M14) уже Apache-2.0.

---

## Proposed ADR-056 — Модель монетизации и поставки (draft)

| ADR | Дата | Решение | Статус | Контекст / отклонено |
|---|---|---|---|---|
| **ADR-056** | 2026-07-04 | **Open-core + offline-entitlements + topology-flexibility.** Core остаётся **Apache-2.0** (двигатель adoption/доверия). Монетизация = (a) **платные модули** под коммерческим EULA (отдельные артефакты, НЕ в Apache-repo), разблокируемые **Ed25519-подписанным offline-license-токеном** (verify локально, **no phone-home** — переиспользует golden-HMAC/GPG-дисциплину); (b) **managed-хостинг** (Element/EMS-стиль: выделенный single-tenant инстанс на нашей инфре); (c) **content-marketplace** (подписанные scenario/test-паки поверх M14-библиотеки); (d) **support/services**. Топология (self-host / hosted-shared / customer-infra / managed-dedicated) — переменная деплоя одной кодовой базы (уже заложено ADR-049). | **Proposed** | Air-gapped+Apache конфликтует с paywall → классический SaaS-license-server не годится; open-core+offline-token сохраняет суверенитет. **Отклонено:** phone-home-лицензирование (ломает air-gapped); закрыть core (убивает adoption/доверие); только-support (потолок выручки); multi-tenant-SaaS как единственная модель (ломает суверенитет для security-сегмента). |

---

## 1. Framework: **6 моделей = топологии × энтайтлмент-слои**

Твои 6 пунктов складываются в одну систему поверх ОДНОЙ кодовой базы:

- **Топологии (как развернуть):** #1 air-gapped-self-host · #2 hosted-shared (наш VPS-SaaS + free-tier) · #3 deploy-to-order (на инфре заказчика) · #4 **Element/EMS managed-dedicated** (выделенный инстанс per-customer на нашей инфре).
- **Энтайтлмент-слои (ценность поверх любой топологии):** #5 платные модули (код) · #6 библиотека scenarios/tests (контент).

Фундамент уже есть: **ADR-049** (профили=топология-не-фичи, оба air-gapped) · Helm/Flux (M11.3) · M14 scenarios/tests-домены.

---

## 2. ⚠ КОММЕРЧЕСКАЯ ГРАНИЦА — что НЕ отдавать в Apache

> **Правило-1:** Apache-2.0 **необратим** — что отдал, назад не заберёшь. Поэтому всё «commercial-reserve» строится **в ОТДЕЛЬНОМ приватном repo/артефакте с первого дня**, НИКОГДА не в Apache-repo. Граница проводится ДО постройки.
> **Правило-2 (важнее):** **open-core = ПОЛНЫЙ, полезный инструмент для одной команды self-host — не crippleware.** В коммерцию убираем только **enterprise-масштаб · мульти-команда/tenant · managed-хостинг · security-аудит · advanced-BI**, НЕ базовую функциональность. Иначе урезанное ядро убивает adoption (а adoption — двигатель всей воронки).

| Компонент | Плоскость | Почему |
|---|---|---|
| Core-пайплайн (agentctl · brain: graph/planner/heal/replay · pw-executor · store-gateway · trust-layer plan_hash/golden/quarantine) | **Apache** ✅ | двигатель доверия/adoption; уже смержено |
| Базовый control-API · vanilla co-pilot (M14) · CLI · provider-agnostic LLM · MCP-server · базовая observability · docker-compose standalone · L1–L6 фикстуры · docs | **Apache** ✅ | ядро опыта, уже смержено; adoption |
| **Entitlement-gate / license-VERIFIER** (Ed25519-verify + module-registry) | **Apache** (можно) | сам гейт бесполезен без premium-модулей; открытость гейта = ок |
| **M10 Security-модуль** (XSS/CSRF/IDOR/auth-bypass над explore-картой) | **🔒 COMMERCIAL** | высокая enterprise-ценность; уже framed как «authorization-gated separate» в BACKLOG — **НЕ строить в Apache-repo** |
| **Metrics-in-UI (M15)** — verdict/steps/heal/fail/regression/coverage/duration/cost + **базовые тренды** (pass/heal/flake-rate во времени) + native-charts | **Apache** ✅ | инструмент без health-обзора = crippleware; **open-core обязан быть ПОЛЕЗНЫМ** |
| **Enterprise-BI** — cross-project/org-rollups · cost-chargeback/attribution · ML-flake-scoring/anomaly-detection · long-retention/warehouse-export · team-analytics/SLA-reports | **🔒 COMMERCIAL** | enterprise-масштаб/мульти-команда, НЕ базовая функциональность |
| **Enterprise-auth** (Keycloak/OIDC/Vault/SSO/RBAC/multi-user) | **🔒 COMMERCIAL** | базовый bearer = Apache; enterprise SSO/RBAC = premium |
| **Provisioning/management-портал** (#4 EMS: спавн per-customer инстансов) | **🔒 COMMERCIAL** (никогда Apache) | это НАША хостинг-инфра + оркестрация = SaaS-control-plane |
| **License-issuing-сервер** (выдаёт подписанные токены по оплате) | **🔒 COMMERCIAL** (никогда Apache) | биллинг/лицензионный backend |
| **Multi-tenancy-слой** (#2 shared-SaaS) | **🔒 COMMERCIAL** | single-tenant = Apache; multi-tenant = hosting-фича |
| **Premium content-паки** (сертифицированные scenario/test-библиотеки, #6) | **🔒 COMMERCIAL** (контент) | механизм библиотеки (M14) = Apache; премиум-контент = marketplace |
| hw-autopilot (M-AUTOPILOT-LOCAL) · prompt-caching/dynamic-routing (M9.7) · premium-installer polish | **🟡 BORDERLINE** | базовое → Apache (adoption); «managed/enterprise»-полировка → commercial. Решить при постройке |
| SLA · support · priority-fixes · managed-upgrades | **🔒 COMMERCIAL** (services) | нет code-gating, чистые услуги |

**Что это значит на практике СЕЙЧАС:** M10/M15-advanced/enterprise-auth/EMS-portal/license-server/multi-tenancy/content-паки **не начинать в основном (Apache) repo**. Завести `sentinel-enterprise` (приватный) для premium-модулей + `sentinel-cloud` (приватный) для hosting/EMS/license-server. Core-repo (Apache) экспонирует стабильные **plugin/adapter-интерфейсы** (обобщение ADR-045), к которым premium цепляется — но сам premium-код туда не коммитится.

---

## 3. Тир-лестница (воронка)
```
Free/Demo (#2)     → ты хостишь shared · $0 на МАЛОЙ ЛОКАЛЬНОЙ модели (не free-API-ключах!) · вход/демо
   ↓
Self-host (#1+#5)  → заказчик сам (standalone/service) · open-core + offline-license на модули · или support
   ↓
Managed dedicated (#4) → ты хостишь выделенный инстанс per-customer · managed-hosting-подписка · KILLER-фича
   ↓
Enterprise (#3+#5+#6)  → на инфре заказчика · prof-services + SLA + модули + контент
```
Библиотека (#6) и модули (#5) продаются на любом тире. Модели **совмещаемы** — это точки одной воронки.

## 4. Фазы (поэтапно, пост-M11 — НЕ всё сразу)
1. **M-COMMERCIAL** (фундамент): Ed25519 offline-license + module-registry (обобщает ADR-045) + license-issuing-сервер + первый premium-модуль (кандидат: M10-security). Разблокирует #1+#5.
2. **Free-tier воронка (#2):** shared-инстанс + rate-limit + локальная малая модель.
3. **M-MANAGED / EMS-portal (#4):** provisioning-plane на стеке Alex'а (**Proxmox+K3s+ArgoCD+Crossplane+Tinkerbell = развёртывание по кнопке**) — твоя дифференцирующая фича.
4. **Content-marketplace (#6):** подписанные scenario-паки поверх M14-библиотеки.
5. **#3 Enterprise** — ручным proof-services параллельно с фазы 1 (не требует кода, требует тебя).

## 5. Механизм (air-gapped-friendly)
- **Оффлайн-license-токен:** `{customer, entitlements:[...], issued, expires, seats}`, Ed25519-подпись твоим приватным ключом → инструмент verify локально встроенным публичным ключом. Подписка = `expires`; продление = новый токен (файл `state/license.jwt`). **Единственный онлайн-компонент — license-issuing-сервер.**
- **Module-registry:** модуль объявляет `id`+`entitlement`; loader активирует только при наличии в лицензии. Premium = отдельные подписанные артефакты/контейнеры в bundle (ADR-053).
- **Деградация на expiry:** premium → read-only/warn; core (Apache) работает. Не бричить.

## 6. Честные caveats
- **Free-tier на free-API-ключах хрупок** → делать на **локальной малой модели** (ваш Ollama-стек, $0 инференс, контроль).
- **Multi-tenancy (#2) ломает суверенитет** → держать ОТДЕЛЬНЫМ tier, не путать с self-host/#4 (там изоляция сохранена).
- **#4 EMS = операц-нагрузка** (ты хостер → SLA/апгрейды/on-call) — оценивать как бизнес.
- **Оффлайн-лицензия мягко-принудительна** — гейт на open-core-границе можно пропатчить, НО premium-МОДУЛЬ не Apache → распространение пропатченного = нарушение EULA (юридический сдерживатель, не тех-замок). Так у всех (GitLab/Elastic/HashiCorp).
- **Не «отравляй» core** — entitlement-чек держи простым и на границе модуля, не вплетай в Apache-ядро.

---

## Next steps (когда дойдём)
- Принять ADR-056 → перенести в `ARCHITECTURE.md §3` (Proposed→Accepted) через PR.
- Завести приватные `sentinel-enterprise` + `sentinel-cloud` repo.
- Спроектировать plugin/adapter-интерфейс (обобщение ADR-045) в core как стабильный SPI.
- Вехи `M-COMMERCIAL` · `M-MANAGED` в BACKLOG/ROADMAP.
