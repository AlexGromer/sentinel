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
 && CGO_ENABLED=0 go build -ldflags "-s -w -X main.version=${VERSION}" -o /out/control-api ./cmd/control-api \
 && CGO_ENABLED=0 go build -ldflags "-s -w -X main.version=${VERSION}" -o /out/orchestrator ./cmd/orchestrator

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
# ⚠ ADR-126 добавил сюда `orchestrator` — ЧЕТВЁРТЫЙ бинарь. До этого образ вёз три из четырёх: пакет
# `.deb` ставил все четыре (`scripts/build-deb.sh`), а образ — нет, поэтому в контейнерной поставке
# оркестратора не было физически, и `CONTROL_API_ORCH_ADDR` было некуда указывать даже при желании.
COPY --from=go-build /out/agentctl /out/store-gateway /out/control-api /out/orchestrator /app/bin/
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
# LIVE-VNC (W3, ADR-127): the `vnc` compose profile runs a HEADED Chromium on a virtual X display and
# exports it over RFB. Both tools live in THIS image rather than in a second one, and the number is
# why — measured 2026-08-17 by building both shapes:
#
#   runtime as it was      1513 MB
#   runtime + a vnc stage  1524 MB      →  +10 MB, 0.66%
#
# The estimate that had argued for a separate image (+246 MB, "xvfb alone is +225") was taken against
# a BARE base, where it is right: on `node:24-bookworm-slim` the same two packages cost +235 MB. It is
# wrong for OUR runtime, because `playwright install --with-deps chromium` above already installs the
# X stack — the same reason the comment there keeps the full Chromium "for headed mode and the LIVE
# arc". What is actually added here is `x11vnc` and its 17 dependencies.
#
# So the second image would have bought 10 MB at the price of a second GHCR tag, a second cosign
# signature, a second SBOM, a branch in release.yml, and a docker-compose.ghcr.yml that only works if
# that second image was published — five surfaces that execute only on a tag. That is the class of
# surface this project found three times in one session (the orchestrator, the ghcr form, the Windows
# cross-build), bought for two thirds of one percent.
#
# ⚠ `xvfb` IS NAMED EXPLICITLY even though it is already present transitively. Depending on
# `--with-deps` to keep providing it would make the whole profile hostage to a Playwright release that
# trims its dependency list — and the failure would arrive as "Xvfb: not found" inside a container
# nobody rebuilt on purpose. Naming it costs nothing (apt sees it installed) and states the need.
#
# NOT installed, each with a reason rather than by omission:
#   x11vnc's alternatives — NOT tigervnc/`Xvnc`, which is an X server AND a VNC server in one: that
#                would put a second X server in the image with no way to point it at the display
#                Chromium already holds. x11vnc exports an EXISTING display, which is what we have.
#   websockify — the hub reaches the screen through control-api's own relay (ADR-127), which speaks
#                WebSocket to the browser and raw TCP to x11vnc. A websockify container would be a
#                service nobody exercises between releases. `[LIVE-VNC-OWN-BRIDGE]` is closed early.
#   x11-utils  — `xdpyinfo` would be a nicer readiness probe than waiting for the X socket, and it
#                drags the X client stack in for one binary. The socket is the same fact, for free.
#   xauth      — the vnc container has exactly ONE X client, its own, in its own namespace. A
#                MIT-MAGIC-COOKIE between two processes that already share a pid namespace protects
#                nothing and adds a file to get wrong.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc \
 && rm -rf /var/lib/apt/lists/*
# The entrypoint the `browser-vnc` service overrides to. It is NOT this image's ENTRYPOINT: the image
# stays `agentctl`, and one compose service points at this script instead — so the default deployment
# is byte-identical in behaviour to what it was before this line existed.
COPY scripts/vnc-entrypoint.sh /app/bin/vnc-entrypoint.sh
RUN chmod 0755 /app/bin/vnc-entrypoint.sh
ENV PYTHONPATH=/app BRAIN_PYTHON=/app/.venv/bin/python
ENTRYPOINT ["/app/bin/agentctl"]
CMD ["--help"]
