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
 && CGO_ENABLED=0 go build -o /out/store-gateway ./cmd/store-gateway

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
RUN python3 -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir \
      langgraph langgraph-checkpoint-sqlite langgraph-checkpoint-postgres anthropic grpcio grpcio-tools mcp
COPY --from=go-build /out/agentctl /out/store-gateway /app/bin/
COPY --from=ts-build /pw/dist /app/pw-executor/dist
COPY --from=ts-build /pw/node_modules /app/pw-executor/node_modules
COPY brain/ /app/brain/
COPY testdata/ /app/testdata/
ENV PYTHONPATH=/app BRAIN_PYTHON=/app/.venv/bin/python
ENTRYPOINT ["/app/bin/agentctl"]
CMD ["--help"]
