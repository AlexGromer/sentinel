# Sentinel container (M5-1). Multi-stage: Go binaries + TS pw-executor + Playwright runtime.
# VERIFY at build: playwright base image tag matches the pinned playwright npm version.
# syntax=docker/dockerfile:1

# --- stage 1: Go control-plane (agentctl + store-gateway) -------------------
FROM golang:1.26 AS go-build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ cmd/
COPY internal/ internal/
# ADR-064: control-api embeds the browser UI, so `package webui` (docs/embed.go) must be present at
# compile time. Only the embedded allowlist is copied — `COPY docs/ docs/` would invalidate this
# layer, and rebuild every Go binary, on every prose edit. KEEP IN SYNC WITH docs/embed.go: widening
# the go:embed patterns without adding the file here breaks this build. That sync is no longer left
# to a reader of this comment — tests/test_container_embed_context_offline.py reconstructs exactly
# the context these COPY lines produce and compiles against it, so the drift surfaces in the fast
# offline suite instead of minutes later in the airgap image build (which is how it was caught
# twice: 2026-07-23 for index.html, 2026-07-29 for capabilities.json).
COPY docs/embed.go docs/index.html docs/prices.json docs/backend-presets.json docs/capabilities.json docs/
COPY docs/setup/ docs/setup/
COPY docs/chat/ docs/chat/
COPY docs/calculators/ docs/calculators/
# M9-LIVE: control-api also embeds the event catalogue (`package eventcatalog`, brain/embed.go) to
# classify log lines and to serve the bilingual message list. Same shape as the webui embed above and
# the same reason for naming the files individually: `COPY brain/ brain/` would rebuild every Go
# binary on any Python edit. The 2026-07-23 airgap-CI failure was exactly this omission for webui.
COPY brain/embed.go brain/events.json brain/
# ADR-110: stamp the version INTO the image's binaries. Without this the release matrix stamped the
# tarballs (release.yml passes -ldflags) while the image did not, so `agentctl --version` answered
# "dev" and control-api's /healthz answered its hardcoded default on every published image — in the
# deployment we actually recommend. Whoever runs the container could not say which version they had,
# which is the first question any bug report has to answer.
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -ldflags "-s -w -X main.version=${VERSION}" -o /out/agentctl ./cmd/agentctl \
 && CGO_ENABLED=0 go build -ldflags "-s -w -X main.version=${VERSION}" -o /out/store-gateway ./cmd/store-gateway \
 && CGO_ENABLED=0 go build -ldflags "-s -w -X main.version=${VERSION}" -o /out/control-api ./cmd/control-api

# --- stage 2: TypeScript pw-executor ----------------------------------------
FROM node:24-bookworm AS ts-build
WORKDIR /pw
COPY pw-executor/package.json pw-executor/package-lock.json ./
RUN npm ci
COPY pw-executor/tsconfig.json ./
COPY pw-executor/src/ src/
RUN npm run build

# --- stage 3: runtime (Chromium + Python brain) -----------------------------
# ADR-124 (DIST-VARIANTS). Базой был `mcr.microsoft.com/playwright:v1.61.1-noble`, который везёт ВСЕ
# ТРИ движка Playwright, при том что ADR-036 фиксирует Chromium-only. Замер образа перед правкой:
#
#   всего 2.79 GB, из них /ms-playwright = 1.2 GB
#     chromium 379M · chromium_headless_shell 262M · ffmpeg 4.9M · firefox 293M · webkit 290M
#   два слоя базы: 2.03 GB и 330 MB (установка всех движков и их системных зависимостей)
#
# 583 MB движков не использовались никогда, плюс их зависимости внутри слоёв базы. После правки —
# 1.58 GB (−1.21 GB, −43%), браузеры 646 MB. Поведение проверено, а не предположено: прогон в новом
# образе даёт ТОТ ЖЕ `plan_hash edc74498ac7c5db0`, что и в старом, и сервис браузера (`cdp-service`)
# поднимается с `CDP_SERVICE_READY` и `/live/status` 200.
#
# ⚠ Полный `chromium` остаётся НАМЕРЕННО, хотя CI ставит только `chromium-headless-shell`: замерено
# `chromium.executablePath()` = `/ms-playwright/chromium-1228/…`, то есть именно его берёт
# `chromium.launch()` в `cdp-service`, и он же нужен headed-режиму (`PW_HEADED`) и дуге LIVE.
# Выбросить его значило бы сломать живой вид ради 379 MB.
FROM node:24-bookworm-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Brain deps come from the committed lockfile (brain/uv.lock), installed FROZEN by uv (#38) — a
# reproducible, pinned install instead of the old unpinned `pip install <names>`. uv is pinned by
# image tag and uses the system python3 (>=3.11); the venv lands at /app/.venv. Copy the manifest +
# lock first so the dependency layer caches across brain/ source changes. `package = false` in
# pyproject means only the locked deps install, not the brain itself.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_PREFERENCE=only-system UV_PYTHON=python3
COPY brain/pyproject.toml brain/uv.lock /app/brain/
RUN cd /app/brain && uv sync --frozen --no-dev
COPY --from=go-build /out/agentctl /out/store-gateway /out/control-api /app/bin/
COPY --from=ts-build /pw/dist /app/pw-executor/dist
COPY --from=ts-build /pw/node_modules /app/pw-executor/node_modules
# Браузеры ставятся ЯВНО и поимённо — это и есть исполнение ADR-036 (Chromium-only) в поставке, а не
# наследование чужого набора. Путь совпадает с тем, что задавал прежний базовый образ, поэтому всё,
# что на него ссылалось, продолжает работать. Гейт джобы `airgap` сверяет содержимое каталога с этим
# объявлением: движок, появившийся здесь незаявленным, красный.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN cd /app/pw-executor && npx playwright install --with-deps chromium chromium-headless-shell \
 && rm -rf /var/lib/apt/lists/* /root/.npm
COPY brain/ /app/brain/
COPY testdata/ /app/testdata/
# Static web assets (setup-WebUI + calculators) for the `webui` compose profile — air-gapped, served
# locally via `python -m http.server` (no network). .dockerignore keeps this to the web subset only.
COPY docs/ /app/docs/
ENV PYTHONPATH=/app BRAIN_PYTHON=/app/.venv/bin/python
ENTRYPOINT ["/app/bin/agentctl"]
CMD ["--help"]
