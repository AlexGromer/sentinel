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
# Deps mirror brain/pyproject.toml. `openai` (OpenAI-compat backend, ADR-019: local models /
# routers) and `pyyaml` (RunConfig YAML, ADR-027/028) are REQUIRED at runtime — without them
# local-model runs and `--run-config` break inside the container.
RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir \
      langgraph langgraph-checkpoint-sqlite langgraph-checkpoint-postgres anthropic openai pyyaml \
      grpcio grpcio-tools mcp \
      opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc prometheus-client
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
