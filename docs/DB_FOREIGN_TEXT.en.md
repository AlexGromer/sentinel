# Foreign text in the database — inventory and cleanup

> 🌐 [Русский](DB_FOREIGN_TEXT.md) (primary) · **English**

ADR-100. The exhaustive per-column classification lives in
[`db-foreign-text.json`](db-foreign-text.json); it is machine-readable and checked by a gate in each
language. This page carries the reasoning without which the list cannot be applied.

## Why this document exists

Sentinel drives **somebody else's website**. Everything it sees there — labels, headings, customer
names, order numbers — and everything the operator types — passwords, tokens, goal phrases — is
**foreign text**.

ADR-098 stripped it out of the trace and ADR-099 gave a run directory a lifetime. Both act **at write
time**, and write-time redaction is powerless over what was written **before it existed**. Foreign
text accumulated in SQLite for as long as there was no policy at all. This document answers where;
`agentctl purge-store` answers what to do about it.

## The distinction that carries everything: inherent vs incidental

It decides feasibility, not phrasing.

**INHERENT** — the text is here **because the feature needs it**.

```
healed_locators.value = {"role":"button","name":"Confirm payment"}
```

The button's label is what finds the button. Remove it and you are left with `{"role":"button"}`, i.e.
"any button on the page", and healing breaks. **Redaction is impossible**: it would protect nothing
and destroy the feature.

**INCIDENTAL** — the text arrived in passing; the column does not need it. Freely redactable.

**The headline finding: nearly all foreign text in this database is inherent.** The heal domain has no
incidental leaks at all — they were looked for specifically. Cleanup here therefore means **deleting
rows on an explicit command**, not blanking fields. For inherent text only three things are available:
a lifetime, restricted access, and an honest statement that this database holds another site's text by
design.

## Where exactly

There are **three** files, not one, carrying two schemas.

| File | Contents | Written by |
|---|---|---|
| `state/locators.db` | 4 heal/trust tables | `brain/` directly (`LocalStore`) or via the gateway |
| `state/control-store.db` | the 7 M13 domains | the Go gateway only |
| `runs/<id>/checkpoint.db` | the full `RunState` | deleted in `finally` (ADR-099) |

⚠ `brain/store.py::_SCHEMA` declares **only the four heal tables**. `scenarios`, `results`, `metrics`
and `config` exist solely in Go — looking for them on the Python path is wasted effort.

### What carries foreign text

Inherent: `healed_locators.value` and `.page_path` · `healing_audit.original`, `.healed`, `.page_path` ·
`golden_snapshots.page_key` · `runs.target` · `chats.last_goal`, `.last_target` · `scenarios.name`,
`.target`, `.steps_json` · `results.steps_json`, `.regressions_json` · `metrics.labels_json`.

Incidental: `scenarios.tags` · `tests.name` · `config.value_json` · inside JSON,
`results.steps_json[].error` and `[].assert.actual`.

### What was verified clean

This half matters just as much: a column wrongly declared clean **never enters a cleanup at all**.

- `semantic_id` in both tables — `sha1(f"{path}|{role}|{name}")[:12]`, measured live as `07ece0d2c1a9`.
- `step_failures` entirely — across 45 live rows: `step_key` is a hash, `last5` is `[1,1]`.
- `golden_snapshots` apart from `page_key` — sha256 and an HMAC.
- `runs.error` — only `os/exec` errors (`cmd/control-api/main.go:623,655`). ⚠ That cleanliness rests
  on `lineWriter.Write` always returning `(len(p), nil)`, which is a property of the implementation
  rather than a guarded invariant, and no test pins it — `[SEC-RUNS-ERROR-UNGUARDED]`.
- `tests.schedule` — a cron expression; nothing in the codebase builds this value from page content.
- `chats.summary` — **not** a model-written summary but a deterministic string bounded at 80
  characters of the first turn (`brain/graph.py:213`), and today **unreachable**: `ChatProjector`
  requires `STORE_ADDR`, which `agentctl` never sets for `--mode chat`. Fix that wiring and the column
  immediately starts holding operator phrasing verbatim (`[SEC-CHATS-WIRING-GAP]`).

### The one leak that is NOT inherent

`scenarios.steps_json[].value` / `.text` stores **the typed value — a password or a card number — in
the clear**.

The sharpest statement of the gap: `scripts/collect-live-run.sh:255` **already blanks** that value
when a scenario leaves in a support bundle. In SQLite it sits as written. One policy, two outcomes,
because the policy lives in a bash script instead of on the write path.

The root cause is not in the database: `_SCHEMA_STEPS` and `_SCHEMA_DRAFT`
(`brain/planner.py:66-79`) have `value` and **no `secretRef`**, whereas the recorder path carries it
through (`record_bridge.py:108`) and `pw-executor` resolves it from the environment
(`server.ts:528-536`). The mechanism exists and works — it is simply unavailable on the dominant
authoring path. Closing it is `[SEC-SCENARIO-SECRETREF]`.

## The cleanup procedure

```
agentctl purge-store --tables healing_audit,runs --yes [--older-than 720h] [--vacuum]
```

**Invoked explicitly, never automatically.** The command appears in no sweep. The reason is stronger
than symmetry with file retention: a swept trace is reproducible by running again, healing history is
not, and an automatic purge would make "the tool tidied up" indistinguishable from "evidence was
erased".

**Two policies, both legitimate, neither the default.** The deployer chooses:

| | Rows | Bytes | Recoverability |
|---|---|---|---|
| without `--vacuum` | gone from queries | **remain** in freed pages | preserved |
| `--vacuum` | gone | gone | **destroyed** |

That the bytes remain is not theory: `modernc.org/sqlite` v1.53.0 reports `secure_delete=0`, so a
deleted row stays greppable in the file. Measured, not assumed, and a gate holds the claim.

⚠ `VACUUM` **does not clear `-wal` by itself** — the command checkpoints before and after, or "the
bytes are gone" would be a lie. It also rewrites the whole file and needs free space of about the
database size; a skip for lack of space is stated out loud rather than swallowed.

**The report is counts, never content** (the `redact-trace` rule: a tool that printed what it found
would be a second copy of the leak). The command always names **the capability being given up**:
purging `healing_audit` blinds `agentctl calibrate`, purging `healed_locators` resets the heal cache,
purging `scenarios`/`tests` destroys authored assets that nothing regenerates.

`config` is **not purgeable**: it is live configuration, not accumulated history. Its foreign text is
a reason to stop putting a target and a goal into config, not a reason to let a cleanup tool delete
the service's settings.

## What this document does not close

- **Inherent text will not go away.** As long as the product drives someone else's site, locators will
  contain that site's words. The protection here is mode `0600`
  (`internal/store/server.go:137`), `state/` in `.gitignore`, and the fact that the support bundle
  collects only `runs/<id>/` and never touches the database.
- **Whether a strict no-erase mode is needed** (forbidding any deletion for the sake of evidence
  preservation) is **not measured**. It is named as a question, not introduced as a requirement.
