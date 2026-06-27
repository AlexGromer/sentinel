# Sentinel Test Fixtures — Graded Complexity

Static, self-contained HTML fixtures for Sentinel's explore + fill/login + goal/describe pipeline.
All files work as `file://` targets. No CDN or external network requests.

## Level Map

| File(s) | Level | What it tests | Key affordances |
|---|---|---|---|
| `l1.html` | L1 — Trivial | Explore: button discovery, click actions, anchor links, cross-fixture navigation | 4 buttons (primary/secondary/danger/success), 1 disabled button, in-page anchor links (`#section-*`), cross-fixture `<a>` links |
| `l2.html` | L2 — Login | M9.1 fill + login: correct/wrong credential paths, inline error, logged-in panel reveal | `#username`, `#password`, `#btn-login`, `#alert-error` (wrong creds), `#panel-logged-in` (correct creds), `#btn-logout` |
| `l3.html` | L3 — Validation | M9.1 negative/validation testing: per-field error messages, format/range/required/maxlength | `#f-email` (email format), `#f-number` (18–120), `#f-text` (required), `#f-bio` (max 80 chars + live counter), per-field `#err-*` divs |
| `l4.html` → `l4-dashboard.html` → `l4-billing.html` | L4 — Multi-page flow | M9.2 cross-page goal scenarios: 3-step nav, sessionStorage handoff, modal confirm | Step 1: login form → Step 2: sidebar dashboard with stats + CTA → Step 3: plan upgrade + invoice table + confirmation modal |
| `l5.html` | L5 — Tabs + Shadow DOM | RISK-005: ARIA tab keyboard nav, async content injection, shadow-DOM pierce locators | `role=tablist/tab/tabpanel`, dynamic slot (`#dynamic-slot`, injected 600ms after tab activation), `<x-color-picker>` custom element with `attachShadow({mode:'open'})` |

## Demo Credentials

| Fixture | Username | Password | Notes |
|---|---|---|---|
| L2 (`l2.html`) | `demo` | `demo` | Reveals `#panel-logged-in` on success |
| L4 (`l4.html`) | `admin` | `secret` | Sets `sessionStorage.l4_user`, redirects to `l4-dashboard.html` |

## File List

```
testdata/fixtures/
  l1.html               L1 trivial affordances
  l2.html               L2 login form (client-side auth)
  l3.html               L3 multi-field validation form
  l4.html               L4 step 1 — login
  l4-dashboard.html     L4 step 2 — dashboard
  l4-billing.html       L4 step 3 — billing + upgrade modal
  l5.html               L5 ARIA tabs + dynamic injection + shadow DOM
  README.md             this file
```

## Notes for Scenario Authors

- All navigation uses relative hrefs — valid under `file://` without a server.
- L4 step sequencing uses `sessionStorage`; a fresh page load of `l4-dashboard.html` without going through `l4.html` first will show `admin` as the fallback username (graceful degradation, not a hard gate).
- L5 shadow DOM: the `<x-color-picker>` shadow root has `mode: 'open'`, so pierce-locators work. The `color-applied` custom event uses `composed: true` to cross the boundary.
- L5 dynamic content: `#dynamic-slot` starts with `aria-busy="true"` and class `dynamic-placeholder`; 600ms after the "Dynamic content" tab is clicked it is replaced with `class="dynamic-content"` and child elements `#dyn-item-1/2/3`.
