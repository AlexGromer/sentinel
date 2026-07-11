#!/usr/bin/env bash
# Sentinel — live-run artifact collector (M9-LIVE preparation; docs/M9_LIVE_PLAN.md §C).
#
#   scripts/collect-live-run.sh <run_id> [--with-trace] [--outdir DIR]
#
# Bundles ONE run's artifacts into <outdir>/live-<id>.tar.gz (default outdir: live-results/) so a run
# performed on the test machine can be carried to the machine where it gets analysed. Transfer the
# tarball by USB/scp — NOT via git: .gitignore's bare `*.tar.gz` silently swallows it, and a redaction
# miss committed to history cannot be undone without a history rewrite (gitleaks does not look inside
# a gzip, so it is NOT a safety net here).
#
# REDACTION IS ON BY DEFAULT and is applied to a STAGING COPY — runs/ is never modified. Two layers:
#   1. structural (JSON/JSONL): the literal `value`/`text` of every fill|type|select|press step that
#      carries no `secretRef` is blanked. LLM authoring cannot emit `secretRef` (brain/planner.py
#      _SCHEMA_STEPS), so "log in with password X" lands as PLAINTEXT in plan.json / scenario.json.
#   2. textual sweep over every collected file: Authorization/Bearer/Cookie headers, secret-ish
#      key=value pairs, and common credential shapes (sk-…, gh?_…, AKIA…, JWT, xox?-…).
# Hashes, ids and counters (plan_hash, golden sha256, step_id, token counts) are deliberately NOT
# touched — they carry no secrets and the analysis depends on them.
#
# NEVER collected, at any flag: checkpoint.db (opaque msgpack of the full RunState — raw goal/messages/
# site_map, not structurally redactable) and storage_state*.json (Playwright auth cookies + localStorage
# = session-hijack material). Unknown files are listed and left behind (fail-safe: unknown ⇒ not shipped).
#
# --with-trace opts trace.zip IN. It is shipped UNREDACTED — Playwright has no mask API and the trace
# captures live DOM (input.value) plus request bodies. Only do this for a run against a disposable
# dev stand, never against production data.
#
# Exit 0 = bundle written; 1 = failure. Missing artifacts warn but never fail: the repo has no
# "run finished" marker, and report.json/report.html/metrics.prom are written by a separate `report`
# subcommand, so a mid-flight run legitimately has plan.json and nothing else.
set -euo pipefail

REDACTED='[REDACTED]'

info() { printf '\033[1;34m==\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32mPASS\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

# Collection whitelist. SUPERSET of the browser-facing artifactWhitelist in cmd/control-api/main.go
# (~:777) — that list gates HTTP artifact reads, this one gates what leaves the machine. If one
# changes, check the other; they are allowed to differ, but not by accident.
COLLECT=(plan.json scenario.json reconcile-report.json heal-report.json baseline-report.json
         report.json report.html llm-transcript.jsonl metrics.prom)
# Recognised but deliberately not collected by default (so they do not show up as "unknown").
KNOWN_SKIP=(trace.zip checkpoint.db)

WITH_TRACE=""
OUTDIR=""
ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-trace) WITH_TRACE=1; shift ;;
    --outdir)     [ $# -ge 2 ] || fail "--outdir requires a directory argument"; OUTDIR="$2"; shift 2 ;;
    -h|--help)    usage 0 ;;
    -*)           fail "unknown flag: $1 (use --with-trace, --outdir DIR)" ;;
    *)            [ -z "$ID" ] || fail "unexpected extra argument: $1 (one run_id only)"; ID="$1"; shift ;;
  esac
done
[ -n "$ID" ] || usage 1

for t in tar python3; do command -v "$t" >/dev/null 2>&1 || fail "missing required tool: $t"; done
if   command -v sha256sum >/dev/null 2>&1; then SHACMD=(sha256sum)
elif command -v shasum    >/dev/null 2>&1; then SHACMD=(shasum -a 256)
else fail "sha256sum or shasum is required"; fi

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$0")")"

# realpath via python3 (already a hard dependency): macOS/BSD `readlink -f` is not portable.
realpath_() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

# --- resolve the run dir ---------------------------------------------------------------------------
# Charset-validate BEFORE building any path — mirrors validRunID (cmd/control-api/ws.go) and stops
# `../`, absolute paths and shell metacharacters at the door rather than after a join.
case "$ID" in
  *[!A-Za-z0-9_-]* | "") fail "invalid run_id '$ID' (allowed: letters, digits, '_' and '-')" ;;
esac
[ "${#ID}" -le 64 ] || fail "invalid run_id: longer than 64 characters"

RUNS="$ROOT/runs"
[ -d "$RUNS" ] || fail "no runs/ directory under $ROOT — nothing to collect"
cand=""
# control-API-spawned runs are always prefixed (cmd/control-api/main.go), agentctl's are not.
[ -d "$RUNS/$ID" ]         && cand="$RUNS/$ID"
[ -d "$RUNS/control-$ID" ] && { [ -z "$cand" ] || fail "ambiguous run_id '$ID': both runs/$ID and runs/control-$ID exist — pass the full directory name"; cand="$RUNS/control-$ID"; }
[ -n "$cand" ] || fail "run not found: neither runs/$ID nor runs/control-$ID exists"

RUNDIR="$(realpath_ "$cand")"
RUNS_REAL="$(realpath_ "$RUNS")"
case "$RUNDIR/" in
  "$RUNS_REAL"/*) ;;
  *) fail "refusing to collect '$cand': it resolves outside runs/ ($RUNDIR) — symlink escape" ;;
esac
{ [ -r "$RUNDIR" ] && [ -x "$RUNDIR" ]; } || \
  fail "run dir not readable: $RUNDIR — run dirs are chmod 0700 by design; collect as the user that ran agentctl"

info "collecting $(basename "$RUNDIR") (redaction ON; trace.zip: $([ -n "$WITH_TRACE" ] && echo INCLUDED || echo excluded))"

# --- stage ------------------------------------------------------------------------------------------
OUTDIR="${OUTDIR:-$ROOT/live-results}"
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
OUT="$OUTDIR/live-$ID.tar.gz"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE="$STAGE/live-$ID"
mkdir -p "$BUNDLE"

copied=0
for f in "${COLLECT[@]}"; do
  src="$RUNDIR/$f"
  if [ -L "$src" ]; then warn "$f is a symlink — skipped (never followed)"; continue; fi
  if [ -f "$src" ]; then cp -- "$src" "$BUNDLE/$f"; copied=$((copied + 1)); else warn "absent: $f"; fi
done
[ "$copied" -gt 0 ] || fail "no collectable artifact found in $RUNDIR (is this a run directory?)"

# LIVE_NOTES.md is cross-run and lives at runs/ level (docs/M9_LIVE_PLAN.md §C.2).
if [ -f "$RUNS/LIVE_NOTES.md" ] && [ ! -L "$RUNS/LIVE_NOTES.md" ]; then
  cp -- "$RUNS/LIVE_NOTES.md" "$BUNDLE/LIVE_NOTES.md"
else
  warn "runs/LIVE_NOTES.md absent — the bundle carries artifacts but no operator notes"
fi

# Everything else present in the run dir: named, but NOT shipped.
unknown=()
for src in "$RUNDIR"/* "$RUNDIR"/.*; do
  [ -e "$src" ] || continue
  b="$(basename -- "$src")"
  case "$b" in .|..) continue ;; esac
  skip=""
  for k in "${COLLECT[@]}" "${KNOWN_SKIP[@]}"; do [ "$b" = "$k" ] && { skip=1; break; }; done
  [ -n "$skip" ] && continue
  unknown+=("$b")
done
if [ "${#unknown[@]}" -gt 0 ]; then
  warn "not collected (unrecognised — fail-safe: unknown files never leave the machine): ${unknown[*]}"
  case "${unknown[*]}" in
    *state*) warn "  ^ a *state* file is present: Playwright storage-state holds auth cookies + localStorage. Never ship it." ;;
  esac
fi

# --- redact (staging copy only) ---------------------------------------------------------------------
info "redacting the staging copy (runs/ is not touched)"
python3 - "$BUNDLE" "$REDACTED" <<'PY'
"""Redact a staged Sentinel run bundle in place.

Layer 1 (structural, JSON/JSONL): blank the literal value/text of every fill|type|select|press step
that carries no secretRef — the LLM authoring schema cannot emit secretRef, so credentials typed into
a login form are persisted verbatim (brain/scenario.py).
Layer 2 (textual): sweep every file for auth headers, secret-ish key/value pairs and common credential
shapes. Applied to string leaves of JSON (so the document stays valid JSON) and to raw text otherwise.

Deliberately NOT redacted: hashes/ids/counters (plan_hash, golden sha256, step_id, *_tokens). They
carry no secret and the whole point of the bundle is to analyse them.
"""
import json
import os
import re
import sys

bundle, MASK = sys.argv[1], sys.argv[2]

TYPING = {"fill", "type", "select", "select_option", "press"}
SECRET_KEY = re.compile(
    r"^(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"auth|authorization|cookie|session|session[_-]?id|credential|credentials)$", re.I)

SWEEPS = [
    # auth headers (value → mask, header name kept so the shape stays readable). The value is consumed
    # to end-of-line, NOT \S+: `Authorization: Bearer <tok>` would otherwise mask only "Bearer" and
    # leave the token behind (caught by the CANARY-HDR canary).
    (re.compile(r"(?i)\b((?:proxy-)?authorization)\s*:\s*[^\r\n\"]+"), r"\1: " + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}"), "Bearer " + MASK),
    (re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n\"]+"), r"\1: " + MASK),
    # secret-ish key/value: `password: x`, `token=x`, and the keyword-adjacent prose an LLM writes
    # ("fill password hunter2"). \b keeps prompt_tokens / completion_tokens (word chars incl. '_') safe.
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
                r"credential|session[_-]?id)\b(\"?\s*[:=]\s*\"?|\s+)([^\s\"',;}&]{3,})"),
     lambda m: m.group(1) + m.group(2) + MASK),
    # credential shapes that stand alone with no keyword next to them
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{8,}\b"), MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}"), MASK),  # JWT
]

SAFE_KEY = re.compile(r"^(plan_hash|plan_id|step_id|id|sha256|golden.*|hash|.*_tokens|"
                      r"prompt_hash|model|planner|strategy|outcome|exit_code)$", re.I)


def sweep(s: str) -> str:
    for pat, rep in SWEEPS:
        s = pat.sub(rep, s)
    return s


def walk(node):
    if isinstance(node, dict):
        action = str(node.get("action_type") or node.get("action") or "").lower()
        typing_step = action in TYPING and not (node.get("secretRef") or node.get("secret_ref"))
        out = {}
        for k, v in node.items():
            if typing_step and k in ("value", "text") and isinstance(v, str) and v:
                out[k] = MASK
            elif isinstance(v, str) and SECRET_KEY.match(k) :
                out[k] = MASK
            elif isinstance(v, str) and not SAFE_KEY.match(k):
                out[k] = sweep(v)
            else:
                out[k] = walk(v)
        return out
    if isinstance(node, list):
        return [walk(x) for x in node]
    return node


def redact_json_text(text: str):
    """Return redacted text, or None when the payload is not the JSON we think it is."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return json.dumps(walk(doc), ensure_ascii=False, indent=2) + "\n"


for root, _dirs, files in os.walk(bundle):
    for name in sorted(files):
        path = os.path.join(root, name)
        if name == "trace.zip":          # binary, opt-in, unredactable — left byte-identical on purpose
            continue
        # surrogateescape: a non-UTF-8 byte survives the round-trip instead of aborting the redaction.
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            original = fh.read()

        if name.endswith(".jsonl"):
            lines = []
            for line in original.splitlines():
                if not line.strip():
                    lines.append(line)
                    continue
                try:
                    lines.append(json.dumps(walk(json.loads(line)), ensure_ascii=False))
                except (ValueError, RecursionError):
                    lines.append(sweep(line))       # partial/corrupt line: still swept, never passed raw
            redacted = "\n".join(lines) + "\n"
        elif name.endswith(".json"):
            redacted = redact_json_text(original)
            if redacted is None:                    # truncated JSON from a crashed run
                print("WARN  %s is not valid JSON — text-swept instead of structurally redacted"
                      % name, file=sys.stderr)
                redacted = sweep(original)
        else:
            redacted = sweep(original)              # .md .html .prom .txt and anything else textual

        if redacted != original:
            with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(redacted)
PY

# --- trace (opt-in, unredacted) ---------------------------------------------------------------------
if [ -n "$WITH_TRACE" ]; then
  if [ -f "$RUNDIR/trace.zip" ] && [ ! -L "$RUNDIR/trace.zip" ]; then
    cp -- "$RUNDIR/trace.zip" "$BUNDLE/trace.zip"
    warn "trace.zip INCLUDED and NOT redacted — it carries live DOM (input.value) and request bodies."
    warn "  Ship it only for a run against a disposable dev stand. Size: $(du -h "$BUNDLE/trace.zip" | cut -f1)"
  else
    warn "--with-trace given, but no trace.zip in the run dir (PW_NO_TRACE=1, or pruned by SENTINEL_TRACE_KEEP)"
  fi
fi

# --- README + manifest ------------------------------------------------------------------------------
cat > "$BUNDLE/README.txt" <<EOF
Sentinel live-run bundle — $(basename "$RUNDIR")
Produced by scripts/collect-live-run.sh (M9-LIVE; docs/M9_LIVE_PLAN.md §C).

THESE ARE REDACTED COPIES. They are NOT byte-faithful to runs/$(basename "$RUNDIR")/ and NOT
replay-safe: plan.json still carries its plan_hash field, but the redacted body no longer hashes to
it. Use this bundle for ANALYSIS (verdicts, heal strategies, confidence, timings, token/cost numbers),
never as a replay input.

Redacted: literal value/text of fill|type|select|press steps without a secretRef; Authorization /
Bearer / Cookie headers; secret-ish key=value pairs; api-key/JWT-shaped strings. Replaced with $REDACTED.
Untouched: hashes, ids, counters (plan_hash, golden sha256, step_id, token counts).

Not in this bundle, ever: checkpoint.db (opaque msgpack of the full RunState) and storage_state*.json
(auth cookies + localStorage).
trace.zip: $([ -f "$BUNDLE/trace.zip" ] && echo 'INCLUDED — UNREDACTED (live DOM + request bodies). Handle accordingly.' || echo 'excluded (default). Re-run with --with-trace if you accept the risk.')

Transfer by USB/scp. Do NOT commit this into git: a redaction miss in history is permanent, and
gitleaks does not scan inside a gzip.
EOF

# Build the file list BEFORE the manifest exists (no read-write race, and no word-splitting on names).
(
  cd "$BUNDLE"
  manifested=()
  while IFS= read -r f; do manifested+=("$f"); done < <(find . -type f | LC_ALL=C sort)
  "${SHACMD[@]}" "${manifested[@]}" > MANIFEST.sha256
)

tar -C "$STAGE" -czf "$OUT" "live-$ID"
pass "DONE: $OUT ($(du -h "$OUT" | cut -f1)) — transfer by USB/scp, not git"
