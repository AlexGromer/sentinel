# Contributing to Sentinel

Thanks for your interest. Sentinel is a polyglot **monorepo** (Go control-plane · Python LangGraph
brain · TypeScript Playwright executor) shipped as one coordinated product. Please read this before
opening a PR.

## Ground rules

- **Docs-first.** Every milestone has a frozen contract in `docs/M*_CONTRACT.md` written *before* the
  code, plus an ADR row in `ARCHITECTURE.md §3`. Significant changes update `ARCHITECTURE.md`
  (component table / ADR / change log) and `GAPS.md`.
- **Bilingual docs.** Project docs are **Russian-primary** (`X.md`) with a paired **English copy**
  (`X.en.md`) carrying a `🌐` banner on line 3. Edit the `.md` first, then mirror into `.en.md`.
  (Community files — this file, `LICENSE`, `CODE_OF_CONDUCT.md`, `SECURITY.md` — are English-only.)
- **Update `FILEMAP.md`** when you add/remove/rename a file.
- **No secrets.** `gitleaks detect` must be clean before every commit. Never commit `.env`, keys,
  `.claude/`, `memory/`.

## Build & test gates (must pass before a PR)

```bash
# Go
go build ./... && go vet ./... && go test ./internal/store/
# Python (offline suites — no network, no browser)
for t in m3 m4 m4b m5 b1 m7 m8; do .venv/bin/python tests/test_${t}_offline.py; done
# TypeScript
cd pw-executor && npx tsc --noEmit
```

Full setup and per-component build live in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Commits — Conventional Commits

`type(scope): summary`, imperative mood, present tense. Types: `feat` `fix` `docs` `refactor`
`test` `chore` `ci` `build` `perf`. Scope is usually a milestone or component: `m9`, `brain`,
`pw-executor`, `orchestrator`. Example:

```
feat(m9): add browser.fill/type tools to pw-executor (ADR-022)
```

- Reference the ADR / GAP / milestone you implement.
- Commits are **GPG-signed** and carry a `Co-Authored-By` trailer for AI-assisted work.
- Keep one logical change per commit; keep the working tree green.

## Branching & PRs

- Branch from `main`: `feat/m9-fill-type`, `fix/...`, `docs/...`.
- Open a PR with the template; fill the checklist and link the issue.
- A PR needs: green CI (build → replay matrix), the test gates above, and an updated contract/ADR/GAP
  when behaviour or architecture changes.
- `main` is protected — merge via PR only.

## Issues

Use the templates (bug / feature). For capability requests, check `docs/M9_CONTRACT.md` and `GAPS.md`
first — much of the roadmap is already tracked there (`GAP-*`).

## Security

Do **not** open public issues for vulnerabilities — see [`SECURITY.md`](SECURITY.md).
