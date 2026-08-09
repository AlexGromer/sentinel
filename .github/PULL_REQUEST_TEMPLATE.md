<!-- Title: type(scope): summary — e.g. feat(m9): add browser.fill tool (ADR-022) -->

## What & why
<!-- One paragraph: what this changes and the problem it solves. Link the milestone/ADR/GAP. -->

Closes #
Milestone / ADR / GAP:

## How
<!-- Key implementation points; note any contract/architecture changes. -->

## Acceptance

The full list lives in [`docs/PR_ACCEPTANCE.md`](../docs/PR_ACCEPTANCE.md) and is not repeated here
— a fourth copy is a fourth thing to go stale. Its **machine half** is what CI runs; you do not tick
those. What follows is the **manual half**: four things no runner can do, each of which has found
defects that every green gate missed.

Do not tick a box you did not do. **Name what you saw** — a bare tick is unrefutable, and the point
of these four is that the next person can check your answer.

<!-- pr-acceptance:boxes -->
- [ ] **Looked at the `ui-smoke` screenshots** — which panel, and what was on it:
- [ ] **Live run against a real model** (not FakeBackend) — which model, and the outcome:
- [ ] **Docker in all three delivery forms** (`docker-compose.yml` / `.ghcr.yml` / `.offline.yml`) — which came up, and what was checked inside:
- [ ] **Mutations** — every new check must be able to fail. Which line, and did it survive:
<!-- /pr-acceptance:boxes -->
<!-- The markers are not decoration: tests/test_pr_acceptance_offline.py counts the boxes INSIDE
     them. Counting boxes in the whole file would let the Docs checklist below stand in for the
     manual half — measured by mutation, it did. -->


<!-- Not every PR adds a component; a one-document edit need not bring up three stacks. But a skip
     is DECLARED here, not assumed — docs/DEVELOPMENT.md §0 requires a recorded reason. -->
Skipped, and why:

## Docs
- [ ] Updated the relevant `docs/M*_CONTRACT.md` / `ARCHITECTURE.md` (ADR + change log) / `GAPS.md`
- [ ] Mirrored every `*.md` change into its `*.en.md` pair (banner intact on line 3)
- [ ] Updated `FILEMAP.md` for new/removed files
- [ ] Principle 7: a new component brought its probe, its logging, its `docs/capabilities.json`
      entry with **every** access path, and its coverage — or the reason it did not is written down

## Notes for reviewers
<!-- Anything that needs a live (user-run) gate: real browser, live OTLP, real provider, etc. -->
