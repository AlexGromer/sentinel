#!/usr/bin/env python3
"""HEALTH-005 PR-C — the browser service writes the service journal, in the shape everything else reads.

Run:  .venv/bin/python tests/test_browser_journal_offline.py

WHAT WAS MISSING. The browser service is part of the default stack and was absent from the deployment's
own record entirely: it logged with `console.error`, i.e. the container's stdio — not a file, not
catalogued, not filterable, not in the UI, and destroyed by `docker compose down`.

WHY THIS RUNS THE SHIPPED FUNCTION INSTEAD OF READING THE SOURCE. The catalogue gate vouches for
TypeScript emitters by scanning their source for code literals, which is enough to say "this code is
not a phantom" and nothing more. The property that actually broke in PR-B is different: the hub
renders a record in the reader's language by matching the catalogue's ENGLISH TEMPLATE against the
message the service really sent, so a message that drifts from its template silently falls back to raw
English — six Go codes were doing exactly that with every gate green. Checking that here by grepping
the .ts would be a check on a spelling. So this compiles the module and CALLS it, then reads what
landed on disk.

⚠ The template matcher below is a second implementation of the one in
cmd/control-api/svcjournal_wording_test.go. They cover different languages' output and the rule is
eight lines; the alternative is leaving one of the two sides unchecked.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = os.path.join(REPO, "pw-executor")
DIST = os.path.join(PW, "dist", "svcjournal.js")
SRC = os.path.join(PW, "src", "svcjournal.ts")

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def template_re(tpl: str) -> "re.Pattern[str]":
    """The matcher the hub effectively applies: literals literal, {field} anything, ANCHORED.

    Unanchored it would accept a prefix, which is exactly the drift this exists to catch.
    """
    out, rest = ["^"], tpl
    while rest:
        i = rest.find("{")
        if i < 0:
            out.append(re.escape(rest))
            break
        j = rest.find("}", i)
        if j < 0:
            out.append(re.escape(rest))
            break
        out.append(re.escape(rest[:i]))
        out.append("(.*?)")
        rest = rest[j + 1:]
    out.append("$")
    return re.compile("".join(out))


def ensure_built() -> bool:
    """Rebuild when dist is missing or older than the source.

    A gate that boots a BUILT artifact must rebuild it, or it measures whatever was lying around —
    a rule this project bought with a mutation that "survived" because the gate started a stale
    binary. Absent node is reported LOUDLY rather than skipped quietly: a check that silently skips
    is indistinguishable from one that passes, which is how two mutations survived in PR-B.
    """
    if subprocess.run(["node", "--version"], capture_output=True, timeout=30).returncode != 0:
        print("  SKIP-LOUD: node is not on PATH, so the browser journal is UNCHECKED here "
              "(not 'checked and fine')")
        return False
    fresh = os.path.exists(DIST) and os.path.getmtime(DIST) >= os.path.getmtime(SRC)
    if fresh:
        return True
    r = subprocess.run(["npm", "run", "build"], cwd=PW, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(DIST):
        fail("dist/svcjournal.js is stale and `npm run build` failed: " + (r.stderr or "")[-300:])
        return False
    return True


def main() -> int:
    catalogue = json.loads(open(os.path.join(REPO, "brain", "events.json"), encoding="utf-8").read())
    if not ensure_built():
        return 1 if failures else 0

    with tempfile.TemporaryDirectory() as state:
        # Drive the REAL module: build the sentences with the exported builders and write them with
        # the exported writer, exactly as cdp-service.ts does.
        script = (
            "const j = require(%r);"
            "j.journal('service.started','info',"
            "  j.startedMsg('v9.9.9', j.supervisor(), 4242, ' — CDP 0.0.0.0:9223, live 0.0.0.0:9224'));"
            "j.journal('service.stopped','info', j.stoppedMsg('signal SIGTERM'));"
            "for (let i=0;i<200;i++) j.journal('service.started','info', j.startedMsg('v','manual',1,''));"
            % DIST
        )
        env = dict(os.environ, SENTINEL_STATE_DIR=state)
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, env=env, timeout=120)
        if r.returncode != 0:
            fail(f"the writer failed to run: {r.stderr[-400:]}")
            print_failures()
            return 1

        path = os.path.join(state, "logs", "service.jsonl")
        if not os.path.exists(path):
            fail(f"nothing was written to {path} — the browser service's records go nowhere")
            print_failures()
            return 1

        # Permissions must match what the Go writer creates, or the file's mode would depend on which
        # service happened to create it first.
        mode = os.stat(path).st_mode & 0o777
        if mode != 0o640:
            fail(f"the journal is mode {oct(mode)}; the Go writer creates 0640 and the two must agree")
        dmode = os.stat(os.path.dirname(path)).st_mode & 0o777
        if dmode != 0o750:
            fail(f"the logs directory is mode {oct(dmode)}, want 0750")

        # It must NOT rotate: two rotators racing on one file discard each other's generations.
        if os.path.exists(path + ".1"):
            fail("the browser writer rotated the journal — rotation belongs to exactly one writer, "
                 "and a second one renaming the file discards the generation the first just made")

        records = [json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
        if len(records) != 202:
            fail(f"{len(records)} records on disk, want 202 — lines are being lost or merged")

        first = records[0]
        for field in ("seq", "ts", "lvl", "cat", "code", "msg", "svc"):
            if field not in first:
                fail(f"the record has no {field!r}: {first} — it is the same wire format as svclog.Record "
                     f"or the reader cannot parse it")
        if first.get("svc") != "browser":
            fail(f"svc is {first.get('svc')!r}; the whole point of one file for every service is that "
                 f"`svc` says which one wrote a line")
        if first.get("cat") != "service":
            fail(f"cat is {first.get('cat')!r}, want 'service' — the reader filters on it")
        if first.get("seq") != 1:
            fail(f"seq starts at {first.get('seq')} — the run-log reader drops records at or below the "
                 f"`after` cursor, so a zero would be invisible on that path")

        # THE PROPERTY THAT BROKE IN PR-B, now checked on the Node side: message vs catalogue template.
        for rec in records[:2]:
            entry = catalogue["events"].get(rec["code"])
            if entry is None:
                fail(f"{rec['code']} is emitted by the browser service and is not catalogued")
                continue
            if not template_re(entry["en"]).match(rec["msg"]):
                fail(f"{rec['code']}: the browser service's message and its catalogue template "
                     f"disagree, so the hub cannot render this row in Russian and will silently show "
                     f"the English\n      message:  {rec['msg']!r}\n      template: {entry['en']!r}")

    # It must not throw when the journal cannot be written — a service that refuses to start over its
    # own log file turns a logging problem into an outage.
    #
    # The unwritable path is a directory this process owns and then makes read-only, NOT something
    # under /proc: measured, mkdir under /proc does not fail, it HANGS, and a gate that hangs is worse
    # than one that fails. Both halves are asserted — the process survived AND the write really did
    # fail — because "survived" alone is also what a run that quietly wrote somewhere else looks like.
    with tempfile.TemporaryDirectory() as ro_parent:
        ro = os.path.join(ro_parent, "read-only")
        os.mkdir(ro, 0o500)
        script = "const j = require(%r); j.journal('service.started','info','x'); console.log('survived');" % DIST
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, SENTINEL_STATE_DIR=os.path.join(ro, "state")))
        if r.returncode != 0 or "survived" not in r.stdout:
            fail("an unwritable journal path took the process down: "
                 f"rc={r.returncode} stderr={r.stderr[-200:]}")
        if "journal write failed" not in r.stderr:
            fail("the write did not actually fail, so this check proved nothing — it must exercise "
                 f"the failure branch, not merely finish. stderr={r.stderr[-200:]!r}")

    if failures:
        print_failures()
        return 1
    print("browser journal: OK (202 records in one file, 0640/0750, svc=browser, seq from 1, "
          "messages match their catalogue templates, unwritable path survived)")
    return 0


def print_failures() -> None:
    print(f"FAIL — {len(failures)} problem(s):")
    for f in failures:
        print("  - " + f)


if __name__ == "__main__":
    sys.exit(main())
