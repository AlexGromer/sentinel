<!-- Title: type(scope): summary — e.g. feat(m9): add browser.fill tool (ADR-022) -->

## What & why
<!-- One paragraph: what this changes and the problem it solves. Link the milestone/ADR/GAP. -->

Closes #
Milestone / ADR / GAP:

## How
<!-- Key implementation points; note any contract/architecture changes. -->

## Test gates (tick what applies)
- [ ] Go: `go build ./... && go vet ./... && go test ./internal/store/` green
- [ ] Python: offline suites green (`for t in m3 m4 m4b m5 b1 m7 m8; do .venv/bin/python tests/test_${t}_offline.py; done`)
- [ ] TS: `cd pw-executor && npx tsc --noEmit` green
- [ ] `gitleaks detect` clean
- [ ] New behaviour covered by an offline test (fake executor/backend — no network/browser)

## Docs
- [ ] Updated the relevant `docs/M*_CONTRACT.md` / `ARCHITECTURE.md` (ADR + change log) / `GAPS.md`
- [ ] Mirrored every `*.md` change into its `*.en.md` pair (banner intact on line 3)
- [ ] Updated `FILEMAP.md` for new/removed files

## Notes for reviewers
<!-- Anything that needs a live (user-run) gate: real browser, live OTLP, real provider, etc. -->
