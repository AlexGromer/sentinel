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
| `l6-newtab.html` | L6 — Multi-tab | M9.4: browser multi-page tracking (A6) + in-app tab perception (A5) | `target=_blank` link + `window.open()` button (new browser tabs → `browser.tabs`/`browser.switchTab`); a `role=tablist/tab/tabpanel` widget (`[role=tab]` now surfaced by `browser.interactives`) |
| `l7-appfaults.html` | L7 — A misbehaving app | ADR-067: the application's OWN faults (console errors, a failing fetch, an uncaught throw) reach the log as `app.*` and must NOT be blamed on the tool | 8 identical `console.error` lines (the log view's collapsing case), a 404 fetch the page swallows, handlers that throw |
| `l8-blindspots.html` | L8 — What we cannot see | ADR-093: every remaining perception blind spot, so the boundary is MEASURED rather than asserted. `browser.perceptionAudit` must report `ratio < 1.0` here and name each zone | §1 `[onclick]`/`[tabindex]`/`contenteditable`/clickable ARIA roles (→ `unseen.outside_selector` = 5) · §2 a button drawn on `<canvas>` (→ `opaque.canvas`) · §3 a **closed** shadow root (→ `opaque.shadow_roots_closed`; the open case is NOT a blind spot — see `l5.html`) · §4 a virtualised list (counted by nothing, deliberately) · §5 controls off screen by each of the two mechanisms — a collapsed box and `visibility:hidden`, whose box stays full size |
| `l9-roles.html` | L9 — One control per ARIA role | ADR-094: the role model. `browser.interactives` must report the role the ACCESSIBILITY TREE has, not the tag name — the two differ for most of this page | §1 an explicit `role` attribute overriding the tag (`<button role=tab>`, `<a role=button>`, `<a role=tab>`, `<div role=button>`) · §2 `<input>` as eight roles chosen by `type` (textbox / searchbox / checkbox / radio / spinbutton / slider / button) · §3 tags whose implicit role is not their name (`a[href]`→link, `select`→combobox, `select[multiple]`→**listbox**) · §3b where the NAME comes from with no label — a placeholder-only input (the placeholder IS the accessible name) and an unlabelled `<select>` (which has NO name; its options are not a label). Both branches had SURVIVING mutations until this section existed · §4 an element with NO role (`input[type=hidden]`, carrying a testid on purpose so it is dropped for the reason the test names) |
| `l10-frames.html` (+ `l10-inner.html`) | L10 — Frames | ADR-095: a control behind a frame boundary. `$$eval`/`locator`/`getByRole` do NOT cross it; `frameLocator` does. Before this fixture no fixture had an `<iframe>` at all, so `unseen.iframe` read 0 on every page the tool had seen | §1 a NAMED frame that also carries an `id` (so the name>id preference is pinned, not merely exercised) · §2 `iframe#terms` · §3 an anonymous frame (positional `iframe >> nth=N`) · §4 a frame inside a frame — perceived to depth 1 only, the rest counted in `opaque.frames_nested`. `l10-inner.html` is a separate FILE because a `srcdoc` nested in a `srcdoc` parses empty |

## Demo Credentials

| Fixture | Username | Password | Notes |
|---|---|---|---|
| L2 (`l2.html`) | `demo` | `demo` | Reveals `#panel-logged-in` on success |
| L4 (`l4.html`) | `admin` | `secret` | Sets `sessionStorage.l4_user`, redirects to `l4-dashboard.html` |

## File List

Not written here. The Level Map above already names every fixture, and a second hand-kept copy shows
only what is superfluous — never what is missing, because an absent row has nothing to look at
(`docs/DEVELOPMENT.md` §0, principle 5). Ask the tree instead:

```bash
ls testdata/fixtures/*.html
```

The invariant: every file that command prints must be **named in a Level Map row** — not necessarily
a row of its own, since `l4.html` / `l4-dashboard.html` / `l4-billing.html` share one row and
`l10-frames.html` / `l10-inner.html` share another. A fixture the command prints and the Level Map
never names is the defect this section used to hide. Nothing here is counted on purpose: a written
count goes stale faster than it gets corrected.

One fact the Level Map does not carry, kept from the list this section replaced: `l10-inner.html` is
the OUTER frame of the nested pair — `l10-frames.html` loads it with `<iframe id="outer">`, and the
nesting the fixture is about lives inside it.

## Notes for Scenario Authors

- All navigation uses relative hrefs — valid under `file://` without a server.
- L4 step sequencing uses `sessionStorage`; a fresh page load of `l4-dashboard.html` without going through `l4.html` first will show `admin` as the fallback username (graceful degradation, not a hard gate).
- L5 shadow DOM: the `<x-color-picker>` shadow root has `mode: 'open'`, so pierce-locators work. The `color-applied` custom event uses `composed: true` to cross the boundary.
- L5 dynamic content: `#dynamic-slot` starts with `aria-busy="true"` and class `dynamic-placeholder`; 600ms after the "Dynamic content" tab is clicked it is replaced with `class="dynamic-content"` and child elements `#dyn-item-1/2/3`.
