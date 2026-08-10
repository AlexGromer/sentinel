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
| `state/control-store.db` | the whole M13 store schema: the M13 domains plus `users` (ADR-109, local accounts); the per-column list lives in `db-foreign-text.json` | the Go gateway only |
| `runs/<id>/checkpoint.db` | the full `RunState` | deleted in `finally` (ADR-099) |

⚠ `brain/store.py::_SCHEMA` declares **only the four heal tables**. `scenarios`, `results`, `metrics`
and `config` exist solely in Go — looking for them on the Python path is wasted effort.

### What carries foreign text

The exhaustive classification lives in [`db-foreign-text.json`](db-foreign-text.json), and it lives
there ONLY: a gate in each language (`internal/store/purge_inventory_test.go`,
`tests/test_db_inventory_offline.py`) checks it against the schema a REAL store creates, so a column
added without a classification fails the build. A second copy of the list beside it would have no
gate — it would drift silently. Named here are only the entries that change a deployer's decision:

- **`healed_locators.value`** — the live cache read by every run, and it never expires: it holds the
  page's text longer than the healing history does.
- **`chats.last_goal`** and **`chats.summary`** — the operator's phrasing, verbatim. `summary` is not
  a model-written summary but the deterministic `_rolling_summary` string (`brain/graph.py`): the
  turn count plus the first 80 characters of the opening turn. It was unreachable until 2026-07-28
  (a chat run fell into the storeless branch); `runNeedsStore` (`cmd/agentctl/main.go`) now starts
  the store for `--mode chat`, SEC-CHATS-WIRING-GAP is closed, and the column fills on the dominant
  path — the hub spawns exactly `agentctl run --mode chat`. The source of truth is the checkpointer
  (`conversations.db`); this is a browsable index over it, purgeable but never redactable.
- **`metrics.labels_json`** — the target URL on EVERY metric point.
- **`scenarios.steps_json[].value`/`.text`** — the one leak that is NOT inherent; it has its own
  section below.

The rest is in the json; read it there, not here.

### What was verified clean

This half matters just as much: a column wrongly declared clean **never enters a cleanup at all**.

- `semantic_id` in both tables — `sha1(f"{path}|{role}|{name}")[:12]`, measured live as `07ece0d2c1a9`.
- `step_failures` entirely — across 45 live rows: `step_key` is a hash, `last5` is `[1,1]`.
- `golden_snapshots` apart from `page_key` — sha256 and an HMAC.
- `runs.error` — only `os/exec` errors: the two `rec.Error` assignment sites in
  `cmd/control-api/main.go` are a failed `cmd.Start` and a non-`ExitError` from `cmd.Wait`. That
  cleanliness rests on the invariant "`lineWriter.Write` always returns `(len(p), nil)`", and the
  invariant is **pinned by a test** — `cmd/control-api/linewriter_contract_test.go`: `os/exec`
  surfaces a writer error as `cmd.Wait()`'s error, which flows into `rec.Error` and then into the
  column, so the day `Write` returns an error the inventory's "`runs.error` is clean" would quietly
  become false. The test asserts the contract (no error on any input · no short write); both
  mutations are caught. There is deliberately NO redactor on the path into `rec.Error`: `redact` is a
  scanner for NAMED secrets and never sees arbitrary page text (ADR-081/098), so what protects here
  is the contract, not the redactor.
- `tests.schedule` — a cron expression; nothing in the codebase builds this value from page content.
- `users` entirely — carries no foreign text and is NOT subject to a purge: `user_id` is minted by
  control-api, `name` is a login the person chose for themselves (not text off a page), and `pw_hash`
  is an irreversible PBKDF2-HMAC-SHA256 (`internal/identity`), never a password. The distinction is
  essential: sweeping `users` is not sanitisation, it is account deletion. That is why the table is
  absent from `purgeable` (`internal/store/purge.go`) and `purge-store` refuses it by name.

### The one leak that is NOT inherent

`scenarios.steps_json[].value` / `.text` stores **the typed value — a password or a card number — in
the clear**.

The sharpest statement of the gap: `scripts/collect-live-run.sh:255` **already blanks** that value
when a scenario leaves in a support bundle. In SQLite it sits as written. One policy, two outcomes,
because the policy lives in a bash script instead of on the write path.

The root cause was not in the database, and it is closed (ADR-102): `_SCHEMA_STEPS` and
`_SCHEMA_DRAFT` (`brain/planner.py`) gained `secretRef` — a secret is entered by NAMING an
environment variable, never by its value; the prompts forbid inlining outright. The mechanism is end
to end and **fill-only**, because that is the product's existing contract: the recorder routes it
only in the fill branch (`brain/record_bridge.py::_attach_value`), `_verb_step` honours it only for
fill (`brain/scenario.py`), and the executor resolves it only for `browser.fill`
(`pw-executor/src/server.ts`, whose zod contract carries `secretRef` on exactly that verb). A
`secretRef` on any other verb is REJECTED into `unmatched` (`ground_scenario`/`reconcile`) rather
than silently dropped — a silent drop would read as "the secret is protected" while the field stayed
empty. ⚠ **The remainder is honest, which is why the column stays in the inventory:** the schema
offers a safe path but does not compel it — a goal whose own text spells out the password can still
lead to a literal in `value`; and `scenarios.steps_json` rows written BEFORE ADR-102 sit as they are,
cured by `agentctl purge-store`, not by redaction.

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
