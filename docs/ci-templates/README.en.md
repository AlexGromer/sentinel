# Sentinel — CI templates

> 🌐 [Русский](README.md) · **English**

> Ready-made templates for **your** CI. These are **not** our `.github/workflows/` — they are
> examples you copy into your own repository to run Sentinel UI tests on every commit.

## What this is

Sentinel is a CLI with **structured exit codes** (`0/1/2/3`), so it slots into any CI as an ordinary
build step: run → exit code → verdict. This folder ships two templates:

- [`Jenkinsfile`](Jenkinsfile) — a declarative pipeline (Jenkins).
- [`.gitlab-ci.yml`](.gitlab-ci.yml) — GitLab CI (Docker-in-Docker).

Both do the same thing: build the image, **replay a frozen `plan.json`** against the live app, and
map the exit code onto a build verdict.

## Prerequisites

- Docker (Docker-in-Docker for GitLab).
- A checkout of the Sentinel repo alongside (or a published image — after M11.1).
- A committed, frozen plan at `config/plan.json` (see [Freezing a plan](#freezing-a-plan)).
- Optional `ANTHROPIC_API_KEY` (a CI secret) — enables LLM self-healing; without a key it runs the
  deterministic heuristic planner + L1–L6 heal (fully offline).

The run command (identical for both templates):

```sh
docker compose run --rm sentinel run --target "$TARGET" --replay --plan /config/plan.json --ci \
  --aut-version "$COMMIT_SHA"
```

`--ci` forbids bypassing the plan_hash hard-abort: a tampered/stale plan fails closed (exit 3).
`/config/plan.json` comes from the mounted `./config` folder (see `docker-compose.yml`).

## Exit codes → CI verdict

| exit | Meaning | CI verdict |
|------|---------|------------|
| `0` | pass — the UI behaves as the frozen plan expects | ✅ success |
| `1` | step-fail — an assertion failed / the described flow is gone | ❌ failure ("the test found a problem") |
| `2` | golden/visual regression — accessibility-baseline drift | ❌ failure (the UI regressed) |
| `3` | plan-integrity / config error (plan_hash mismatch, bad config) | ⚠ warning/unstable — **needs a human** (re-baseline or fix config) |

Source of truth: `cmd/agentctl/main.go:10` · `docs/M3_CONTRACT.md` · `docs/TESTING.md`.

## Jenkins

Copy [`Jenkinsfile`](Jenkinsfile) to your repo root. Set `TARGET` (and optionally `ANTHROPIC_API_KEY`
via Jenkins credentials). Mapping: `0`→PASS, `1`/`2`→`error` (FAILURE), `3`→`currentBuild.result =
'UNSTABLE'`. The `runs/**` artifacts are archived in `post { always }`.

## GitLab CI

Copy [`.gitlab-ci.yml`](.gitlab-ci.yml). Set `TARGET` in `variables` and `ANTHROPIC_API_KEY` as a
masked variable. The job exits with Sentinel's **own** code; `allow_failure.exit_codes: [3]` makes
exit 3 a yellow warning while `1`/`2` stay a red failure. Artifacts: `runs/`.

## Freezing a plan

1. Author the scenario: via the [chat-front](../chat/) (describe the test in words) **or**
   `agentctl run --target <URL> --describe "…"` / `--goal "…"` → you get `runs/<id>/scenario.json`
   (+ `plan.json`).
2. Freeze the golden baseline: `agentctl baseline update --plan runs/<id>/plan.json` — the **only**
   golden-mutation path.
3. Commit `plan.json` to `config/plan.json`. CI then replays it on every commit.

## Offline / no key

Without `ANTHROPIC_API_KEY` Sentinel does not fail: the planner is the deterministic heuristic and
heal is L1–L6 (no LLM re-grounding). It heals complex drift more slowly, but the run is fully offline
and reproducible — handy for air-gapped CI. For `file://` fixtures no network is needed at all.
