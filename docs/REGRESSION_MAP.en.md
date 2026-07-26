# Sentinel — Regression Map

> 🌐 [Русский](REGRESSION_MAP.md) (основная версия) · **English**

> **What this is.** An inventory of which degradations Sentinel detects, by what mechanism, and where
> it reports them — together with an explicit list of what it does **not** detect.
>
> **Why in this shape.** The document is deliberately written like `docs/OUTPUTS.md`, where anything
> unimplemented is marked `as-built: not written as a file`. A map with no "no" rows stops being a
> planning instrument and becomes a leaflet. Every row below is checkable: facts about our code carry
> a file path, normative references carry a date and a URL, and data about how interfaces break
> carries a citation.
>
> **Revision date:** 2026-07-26 · **Code state:** ADR-070

---

## Contents

1. [Three subjects of regression](#1-three-subjects-of-regression)
2. [Normative frame](#2-normative-frame)
3. [What is known about how UI tests break](#3-what-is-known-about-how-ui-tests-break)
4. [Matrix: tool regressions](#4-tool-regressions)
5. [Matrix: test regressions](#5-test-regressions)
6. [Matrix: application regressions](#6-application-regressions)
7. [Structurally out of reach today](#7-structurally-out-of-reach-today)
8. [Coverage against the 25010 quality axes](#8-coverage-against-the-25010-quality-axes)
9. [Open questions](#9-open-questions)

---

## 1. Three subjects of regression

A regression here is a degradation relative to a previously observed state. UI testing constantly
conflates three different subjects, and **that conflation produces the most expensive class of
error**:

| subject | what regresses | who is at fault | what the reader expects |
|---|---|---|---|
| **the tool** | Sentinel itself performs worse: the planner degraded, exploration looped, the store went away | us | "fix your tool" |
| **the test** | the artefact we produced stopped matching reality: a locator is dead, the plan was tampered with, the flow changed | the test | "update the test" |
| **the application** | the system under test got worse | the application | "here is a bug, go fix it" |

Conflating the three is the source of two concrete defects the product carried for months:

- **`exit 0` under a drifted interface.** Self-healing changes the test's binding, the test passes,
  the verdict is green. An *application* regression (the interface changed) was silently absorbed as
  a *test* repair. Backlog item `[PROD-HEAL-VERDICT]`.
- **`PASSED` while the application threw exceptions.** `app.*` events reach the log and nowhere
  else. An *application* regression reaches neither the verdict nor the Results domain. Backlog item
  `[PROD-VERDICT-APP]`.

Hence the rule for reading this map: **a row is only useful together with the "where reported"
column.** Detecting and not saying is the same as not detecting.

---

## 2. Normative frame

Three national standards supply the terms and the axes the matrix is laid out on. Dates of entry
into force are given, because citing a ГОСТ without its year is a classic source of error.

| standard | subject | how we use it |
|---|---|---|
| [**ГОСТ Р 56920-2016 / ISO/IEC/IEEE 29119-1:2013**](https://protect.gost.ru/) — "Systems and software engineering. Software testing. Part 1. Concepts and definitions"; in force **2017-06-01** | testing terminology | the source of meaning for "test", "run", "defect", "coverage" — so this map does not invent its own vocabulary |
| [**ГОСТ Р ИСО/МЭК 25010-2015**](http://docs.cntd.ru/document/1200121069) (SQuaRE) — "Quality models for systems and software products"; approved **2015-05-29** | the product quality model: **8 characteristics** — functional suitability · reliability · performance efficiency · usability · security · compatibility · maintainability · portability | **the matrix axes**. Coverage summary in §8 |
| [**ГОСТ Р 52872-2019**](https://base.garant.ru/73664694/) — "Internet resources… and other user interfaces. Accessibility requirements for people with disabilities…"; in force **2019-08-29**, built on **WCAG 2.1** | interface accessibility | the reference set of accessibility criteria. Today we use the accessibility tree as a **hash**, not as a criterion — see §7 |

**The important consequence of 25010 for us.** The standard distinguishes *functional completeness*,
*functional correctness* and *functional appropriateness* inside one characteristic. Sentinel today
checks mostly **correctness** (the step did what was expected) and barely touches **completeness**
(are all functions covered) — because the coverage metric counts buttons only (§5,
`[PROD-CRAWL]`).

---

## 3. What is known about how UI tests break

Not opinion but published measurement. This is what a detection system should be aimed at.

| source | what was measured | consequence for us |
|---|---|---|
| **Hammoudi, Rothermel, Tonella** — "Why do Record/Replay Tests of Web Applications Break?", [ICST 2016](https://www.researchgate.net/publication/305525535_Why_do_RecordReplay_Tests_of_Web_Applications_Break) | **453 versions** of web applications, **1065 breakages** identified; a taxonomy built | **the vast majority of breakages are locators, caused by changes in page structure**. Our self-healing (`brain/healing.py`, the L1–L6 rotation) hits exactly the most frequent class — a demonstrably correct bet |
| **Systematic Literature Review** — "Test Breakage Prevention and Repair Techniques", [arXiv:1909.10750](https://arxiv.org/pdf/1909.10750) | a survey of prevention and repair techniques | repairing a locator is a studied problem; **reporting on the repair** is far less developed, and that is our niche (`[PROD-HEAL-VERDICT]`) |
| **Luo, Hariri, Eloussi, Marinov** — "An Empirical Analysis of Flaky Tests", [FSE 2014](https://mir.cs.illinois.edu/lamyaa/publications/fse14.pdf) | **201 commits** fixing flakiness across **51** open-source projects | the top category is **Async Wait** (waiting on an external operation). Playwright's auto-waiting covers part of it for us, but **nothing measures it**: there is no flake rate (§5) |
| "An Empirical Analysis of **UI-based** Flaky Tests", [ICSE 2021](https://weihang-wang.github.io/papers/UIFlaky-icse21.pdf) | flakiness specific to UI tests | confirms UI flakiness is its own class with its own causes, not a subset of the general one |
| "On the Brittleness of Legacy Web UI Testing: A Pragmatic Perspective", [ISSTA 2025](https://dl.acm.org/doi/10.1145/3713081.3731742) | root causes of brittleness from a pragmatic standpoint | brittleness is created not only by the test but by **application design** unsuited to testing — something we cannot fix from our side, but can **report** |

---

## 4. Tool regressions

Degradations in Sentinel itself, as observed on someone else's environment. Regressions of our source
code in CI are out of scope here — that is `docs/TESTING.md`.

| what breaks | detected? | mechanism | exit | where reported | GAP |
|---|---|---|---|---|---|
| the brain did not start / crashed | **yes** | control-api records the abnormal exit (`cmd/control-api/main.go:478`) | `-1` | verdict + `logs/run.jsonl` | — |
| the run was signal-killed or cancelled | **yes** | ADR-069: `state=canceled` is separated from `failed`, because a killed process exits with −1 indistinguishably from a crash (`main.go:506-512`) | `-1` | verdict (state beside the code) | — |
| the LLM is unreachable → the planner silently falls back to heuristic | **partly** | 16 codes carry `degrades: true` in `brain/events.json`, each with a `{ru,en}_verdict` hint | unchanged | **logs only**; never reaches the verdict | `[PROD-VERDICT-APP]` (same mechanism) |
| pw-executor did not come up | **yes** | the `browser.launched` event is absent | `-1` | logs | — |
| exploration loops on an element that will not act | **yes** (ADR-070) | a per-element retry budget in `brain/graph.py`; events `plan.element_blacklisted`, `plan.unactionable_elements` | unchanged | logs + the reason it stopped | — |
| the token budget is exhausted | **yes** | `brain/runcontrol.py` → `plan.orchestrator_abort` | unchanged | logs + token metrics | — |
| the store is not running | **yes** (ADR-069) | five list endpoints carry `store:false` + a `store_reason` that names the remedy | — | a banner in the UI beside the data | — |
| degradation caused by a local model (14B/7B structure worse than a cloud one) | **no** | — | — | — | `RISK-002`, `GAP-VERIFY-*` |

**The section's key weakness:** "silent degradation" is detected but stays in the logs. A run on the
heuristic instead of the LLM is externally indistinguishable from a run on the LLM.

---

## 5. Test regressions

Degradations of the artefact Sentinel produced.

| what breaks | detected? | mechanism | exit | where reported | GAP |
|---|---|---|---|---|---|
| the plan was tampered with or corrupted | **yes** | `plan_hash` — SHA-256 over every field of every step (`brain/state.py::canonical_plan_hash`); checked **before** execution | **3** | verdict; **nothing runs** | — |
| a golden was forged | **yes** | HMAC-SHA256 over the golden's integrity-bearing fields (`brain/store.py:52`), byte-identical to the Go gateway | **3** | verdict | — |
| a locator stopped resolving | **yes** | `brain/healing.py`: cache keyed on the page hash → rotation of `alternatives[]` (testid 0.95 → role+name 0.90 → label 0.88 → text 0.80) → textual LLM re-ground → visual re-ground (set-of-marks) | unchanged | `heal-report.json`, the `healed` counter | — |
| **healing bound to a different element** | **no** | `_llm_reground`/`_visual_reground` pick a new selector from the current page; there is no "is this the same element" check | unchanged | — | `[PROD-HEAL-VERDICT]` |
| **the flow changed, not the locator** | **no** | healing finds something "close enough" and passes | unchanged | — | `[PROD-HEAL-VERDICT]` |
| **rollback to a previous test version** | **no** | `plan_hash` is an integrity fingerprint, not a version; `TestRecord` is a single row with no revisions; `baseline` overwrites goldens | — | — | `[PROD-VERSIONING]` |
| a systematically failing step fails the whole run | **yes** | quarantine (`store.record_step`, ADR-013/M3) — suppresses the step's contribution to `exit 1` but **not** to `exit 2`: a real application change must not hide behind a flaky-locator quarantine | — | `heal-report.json` | — |
| **flake rate** ("failed 1 in 10") | **no** | quarantine is a binary fact; there is no statistic over history | — | — | `[PROD-FLAKE-RATE]` |
| **Async Wait as a class** (top-1 per FSE'14) | **partly** | Playwright's auto-waiting removes some of it; **nothing measures it** | — | — | `[PROD-FLAKE-RATE]` |
| the test's coverage of application functionality | **structurally wrong** | coverage counts **buttons only** (`brain/graph.py`: `buttons = [e for e in elements if e["role"] == "button"]`); links, fields, selects and checkboxes never enter the denominator. Worse: `interactive_seen` accumulates across pages, so **going deeper grows the denominator and lowers coverage** → the convergence condition `coverage>=0.85 AND frontier empty` is practically unreachable on a multi-page site | — | `coverage` in the metrics (the number misleads) | `[PROD-CRAWL]` |

---

## 6. Application regressions

The thing the product exists for.

| what breaks | detected? | mechanism | exit | where reported | GAP |
|---|---|---|---|---|---|
| a step did not execute | **yes** | `brain/replay.py` | **1** | verdict + step breakdown | — |
| the page's accessibility tree changed | **yes, authoritatively** | `_a11y_hash` (`brain/replay.py:41`) — a hash of the ARIA snapshot; compared on **first** landing on a page, symmetrically in `baseline` and `replay`, so a later click cannot shift the golden | **2** | verdict + `regressions[]` | — |
| the page screenshot changed | **yes, advisory** | a screenshot hash; by default it does **not** affect the exit code — cross-process render instability would otherwise produce false failures | unchanged (default) | `regressions[]` | `RISK-009` |
| the application threw a JS exception | **yes, but not into the verdict** | `app.js_error`, emitted by `pw-executor/src/server.ts:79` | unchanged | **the Logs view only** | `[PROD-VERDICT-APP]` |
| a console error/warning | same | `app.console_error` / `app.console_warn`, `server.ts:82-83` | unchanged | same | `[PROD-VERDICT-APP]` |
| an application request failed | same | `app.request_failed`, `server.ts:87` | unchanged | same | `[PROD-VERDICT-APP]` |
| the application answered 4xx/5xx | same | `app.http_error`, `server.ts:93` | unchanged | same | `[PROD-VERDICT-APP]` |
| the application opened a dialog | same | `app.dialog`, `server.ts:96` | unchanged | same | `[PROD-VERDICT-APP]` |
| **the interface changed but the test healed** | **not as a signal** | healing worked; the verdict is green | `0` | the `healed` counter | `[PROD-HEAL-VERDICT]` |
| **performance degraded** | **no** | only `duration_ms` for the whole run is measured | — | a metric | `[PROD-PERF]` |
| **accessibility degraded** (52872 / WCAG 2.1 criteria) | **no** | the accessibility tree is used as a hash, not as a criterion: "changed" ≠ "became inaccessible" | — | — | `[PROD-A11Y]` |
| application data (DB/API) does not match expectation | **no, and not planned without an ADR** | `docs/DISTRIBUTION.md`: the black-box guarantee — Sentinel has no direct DB access | — | — | deferred; requires a new ADR |

---

## 7. Structurally out of reach today

Not "we did not do it" but "the current architecture does not reach it".

**Perception blind spots** (`GAP-RISK-005`). `browser.interactives` works from a fixed CSS selector
over `button, a[href], input, select, textarea, [role=button], [role=tab]`. Out of reach: **shadow
DOM**, the contents of **canvas**, **cross-origin iframes**, custom web components with no ARIA role.
An element perception cannot see enters neither the map, nor coverage, nor any check — and its
absence is signalled by nothing.

**SPA states.** The frontier grows from `browser.links` of the same origin. A route change without an
`<a href>` is invisible. There is no state deduplication: two URLs rendering the same view count
twice, one URL with different states counts once. There is no return to a prior state between
exploration branches.

**Idempotency of retries.** A step retried during healing is not idempotent: on a step that writes to
a database or a queue, a retry causes a double write. Today this is harmless only because we never
touch the backend directly.

**Value volatility.** Goldens are stable for the shape of a page, but values arriving from a DB/API
drift. Without a "structural versus volatile" split, extending checks to data would produce false
`exit 2`.

**Byte-stability of screenshots** (`RISK-009`) is unproven across separate browser processes — which
is why visual regression remains advisory.

**Cross-browser.** ADR-036: Chromium-only by design. `connectOverCDP` exists for Chromium only, and
**golden hashes differ per rendering engine** — a Chromium golden would give `exit 2` in Firefox for
no reason. Firefox/WebKit need goldens **per engine**, not a flag (`GAP-OPS-001`,
`[PROD-ENGINES]`).

---

## 8. Coverage against the 25010 quality axes

| characteristic | covered? | by what |
|---|---|---|
| **functional suitability** | correctness — **yes**; completeness — **weakly** | steps/assertions give correctness; coverage as a measure of completeness is structurally wrong (§5) |
| **reliability** | **partly** | quarantine + the retry budget; no flake rate |
| **performance efficiency** | **no** | only `duration_ms` per run |
| **usability** | **no** | not measured |
| **security** | **out of core scope** | a separate module M10, commercial (ADR-056) |
| **compatibility** | **partly** | one engine (ADR-036), no cross-browser |
| **maintainability** (of the test, not the application) | **partly** | healing exists; versioning and rollback do not |
| **portability** | **not applicable** | a property of the application; we do not assess it |

Bottom line: of eight axes, **one and a half** are confidently covered. That is not a verdict — it is
a map of where the product can grow, and at the same time the answer to "what can we already
guarantee".

---

## 9. Open questions

Each leads to a `BACKLOG.md` item:

- `[PROD-VERDICT-APP]` — carry `app.*` through to the verdict and the Results domain; decide whether
  a non-zero count affects the exit code (default: no, visibility only).
- `[PROD-HEAL-VERDICT]` — UI drift as a first-class outcome: what, where, how; separate re-bind (the
  same element by another key) from re-ground (a new selector) — they are different news.
- `[PROD-CRAWL]` — measure exploration on a real 50+ page SPA, then redefine coverage.
- `[PROD-VERSIONING]` — plan revisions, diff, rollback, golden history.
- `[PROD-FLAKE-RATE]` — flake rate from run history.
- `[PROD-PERF]`, `[PROD-A11Y]` — new check axes (performance, accessibility as a 52872/WCAG
  criterion).
- `[PROD-ENGINES]` — goldens per engine + a cross-browser mode.

**How to maintain this document.** Every new detection mechanism adds a row to §4–6 with a file path;
every new "we do not catch this" adds a row with a GAP reference. The document loses its value the
moment rows saying "no" stop appearing in it.
