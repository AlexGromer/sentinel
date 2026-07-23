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
# the go:embed patterns without adding the file here fails this build loudly, which is the intent.
COPY docs/embed.go docs/index.html docs/prices.json docs/backend-presets.json docs/
COPY docs/setup/ docs/setup/
COPY docs/chat/ docs/chat/
COPY docs/calculators/ docs/calculators/
RUN CGO_ENABLED=0 go build -o /out/agentctl ./cmd/agentctl \
 && CGO_ENABLED=0 go build -o /out/store-gateway ./cmd/store-gateway \
 && CGO_ENABLED=0 go build -o /out/control-api ./cmd/control-api

# --- stage 2: TypeScript pw-executor ----------------------------------------
FROM node:24-bookworm AS ts-build
WORKDIR /pw
COPY pw-executor/package.json pw-executor/package-lock.json ./
RUN npm ci
COPY pw-executor/tsconfig.json ./
COPY pw-executor/src/ src/
RUN npm run build

# --- stage 3: runtime (Playwright browsers + Python brain) ------------------
FROM mcr.microsoft.com/playwright:v1.61.1-noble AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
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
COPY brain/ /app/brain/
COPY testdata/ /app/testdata/
# Static web assets (setup-WebUI + calculators) for the `webui` compose profile — air-gapped, served
# locally via `python -m http.server` (no network). .dockerignore keeps this to the web subset only.
COPY docs/ /app/docs/
ENV PYTHONPATH=/app BRAIN_PYTHON=/app/.venv/bin/python
ENTRYPOINT ["/app/bin/agentctl"]
CMD ["--help"]
