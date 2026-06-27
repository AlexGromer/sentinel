---
title: Sentinel — docs hub & calculators
---

# Sentinel

**Автономный self-healing агент для UI-тестирования.** Sentinel сам исследует веб-приложение,
решает что тестировать, замораживает детерминированный воспроизводимый план и восстанавливает
сломанные локаторы при дрейфе DOM — генерируя артефакты для инженеров (отчёты, трассировки,
экспортированные Playwright-спеки, regression baselines).

> Эта страница — хаб GitHub Pages: интерактивные калькуляторы (ниже) обслуживаются прямо отсюда,
> а документация рендерится в репозитории. *This is the GitHub Pages hub: the interactive
> calculators below are served from here; the prose docs render in the repository.*

## 🧮 Интерактивные калькуляторы / Interactive calculators

Полностью клиентские (vanilla JS, без сервера и сети) — работают и из GitHub Pages, и локально
по `file://` (air-gapped). Авторитетные формулы и таблицы — в
[`docs/LOCAL_MODELS.md`](https://github.com/AlexGromer/sentinel/blob/main/docs/LOCAL_MODELS.md) §5.

| Калькулятор | Назначение |
|-------------|------------|
| [VRAM / hardware](calculators/vram.html) | `VRAM ≈ params·bytes(quant) + KV-cache + overhead` → влезает ли модель в N ГБ; по VRAM GPU — какие классы моделей подходят |
| [Token-cost per test phase](calculators/token-cost.html) | токены по фазам (explore-plan · goal/describe-scenario · heal-text · heal-vision; replay = 0) → cloud-$ или local-время, с бюджетами PLAN 50k / HEAL 20k |
| [Model selector](calculators/model-selector.html) | масштаб AUT × роль (plan/heal/vision) × железо → рекомендованная модель + quant + runtime |

## 📚 Документация / Documentation

**Старт / Start here**
- [README](https://github.com/AlexGromer/sentinel/blob/main/README.md) ([EN](https://github.com/AlexGromer/sentinel/blob/main/README.en.md)) — обзор + быстрый старт
- [docs/TESTING.md](https://github.com/AlexGromer/sentinel/blob/main/docs/TESTING.md) — offline-гейты, локальные модели, live-прогон, zero-level docker-compose
- [docs/DEVELOPMENT.md](https://github.com/AlexGromer/sentinel/blob/main/docs/DEVELOPMENT.md) — сборка по компонентам, milestone-гейты, рецепты расширения

**Модели и железо / Models & hardware**
- [docs/LOCAL_MODELS.md](https://github.com/AlexGromer/sentinel/blob/main/docs/LOCAL_MODELS.md) — VRAM-методика, token-cost-методика, каталог моделей и runtime/роутеров (verified)

**Безопасность и поставка / Security & distribution**
- [docs/THREAT_MODEL.md](https://github.com/AlexGromer/sentinel/blob/main/docs/THREAT_MODEL.md) — STRIDE-lite по границам доверия
- [SECURITY.md](https://github.com/AlexGromer/sentinel/blob/main/SECURITY.md) — политика раскрытия уязвимостей
- [docs/DISTRIBUTION.md](https://github.com/AlexGromer/sentinel/blob/main/docs/DISTRIBUTION.md) — эпик дистрибуции и онбординга (Release · compose · Helm/Flux · setup-WebUI · air-gapped)

**Архитектура и механика / Architecture & mechanics**
- [ARCHITECTURE.md](https://github.com/AlexGromer/sentinel/blob/main/ARCHITECTURE.md) ([EN](https://github.com/AlexGromer/sentinel/blob/main/ARCHITECTURE.en.md)) — контекст, компоненты, ADR (журнал решений)
- [docs/STATE_MACHINE.md](https://github.com/AlexGromer/sentinel/blob/main/docs/STATE_MACHINE.md) · [SELF_HEALING.md](https://github.com/AlexGromer/sentinel/blob/main/docs/SELF_HEALING.md) · [DETERMINISM.md](https://github.com/AlexGromer/sentinel/blob/main/docs/DETERMINISM.md) · [OBSERVABILITY.md](https://github.com/AlexGromer/sentinel/blob/main/docs/OBSERVABILITY.md) · [OUTPUTS.md](https://github.com/AlexGromer/sentinel/blob/main/docs/OUTPUTS.md)
- [docs/ROADMAP.md](https://github.com/AlexGromer/sentinel/blob/main/docs/ROADMAP.md) · milestone-контракты `docs/M*_CONTRACT.md`

---

*Sentinel — Apache-2.0. Все калькуляторы и документация генерируются из проверенных фактов кода;
неподтверждённые специфики моделей помечены «verify before use» (knowledge cutoff Jan-2026).*
