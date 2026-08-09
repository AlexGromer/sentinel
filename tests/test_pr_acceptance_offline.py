#!/usr/bin/env python3
"""QA-MATRIX-IN-REPO — the gate for docs/PR_ACCEPTANCE.md.

WHY THIS EXISTS. `docs/DEVELOPMENT.md` §0 principle 7 required "coverage in the check matrix" and
the check matrix was not in the repository: measured, the only occurrence of that phrase anywhere in
the tree was the principle itself. A normative reference into thin air.

WHAT IT REFUSES TO ACCEPT, and why each one is a measured failure rather than a hypothetical:

  1. A DOCUMENTED CHECK NO WORKFLOW RUNS. The document names jobs and steps of ci.yml; each must
     exist. That is one direction.
  2. A JOB THE DOCUMENT IS SILENT ABOUT. The other direction, and the important one — a hand-kept
     list shows what is superfluous (a row for a deleted job goes red) but NOT what is missing,
     because absence has no representation to look at. So the job sets must be EQUAL. This is
     `docs/DEVELOPMENT.md` §0 principle 5 applied to the document itself.
  3. A MANUAL ROW WITH NO RECORDED REASON. Shape copied from componentsWithoutProbe
     (cmd/control-api/readyz.go): an exemption with no stated reason is an omission that learned to
     pass a test.
  4. NO FLOORS. A parser that stops matching yields an empty set, and every assertion over an empty
     set passes perfectly. Both halves carry a floor.
  5. A LITERAL LIST OF SUITE NAMES anywhere that reads as an instruction to execute today. Measured:
     `for t in m3 m4 m4b m5 b1 m7 m8` in CONTRIBUTING.md (7 names), a 20-name variant in
     docs/DEVELOPMENT.md, docs/TESTING.md and FILEMAP.md — while tests/ held 72 files. Someone who
     followed CONTRIBUTING literally ran 7 of 72.

Both the Russian and the English document are parsed by the SAME parser, over the same HTML markers,
so the mirror is CHECKED rather than merely present — which is more than the bilingual job asks for
(it only requires the sibling file to exist).

Offline: reads files, runs nothing, needs no network. stdlib only — the venv is not guaranteed to
carry PyYAML, and pulling a dependency in for a documentation gate would be its own decision.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
DOCS = [
    os.path.join(ROOT, "docs", "PR_ACCEPTANCE.md"),
    os.path.join(ROOT, "docs", "PR_ACCEPTANCE.en.md"),
]

# Floors. They are floors, not current counts: raising them on every added row would make them a
# maintenance tax rather than a tripwire. The numbers are set below what exists today and above what
# a broken parser would produce.
MIN_JOBS = 8
MIN_MACHINE_ROWS = 20
MIN_MANUAL_ROWS = 4

# Files where a literal suite list reads as "run this today". docs/M*_CONTRACT.md is deliberately
# absent — a milestone contract records what was run to accept THAT milestone, and rewriting it
# would retroactively alter the record. The exemption is stated in PR_ACCEPTANCE.md §4, where a
# reader can see it, rather than only here.
NO_LITERAL_LIST = [
    "CONTRIBUTING.md",
    os.path.join(".github", "PULL_REQUEST_TEMPLATE.md"),
    os.path.join("docs", "TESTING.md"),
    os.path.join("docs", "TESTING.en.md"),
    os.path.join("docs", "DEVELOPMENT.md"),
    os.path.join("docs", "DEVELOPMENT.en.md"),
    os.path.join("docs", "PR_ACCEPTANCE.md"),
    os.path.join("docs", "PR_ACCEPTANCE.en.md"),
    "FILEMAP.md",
]
# `for t in m3 m4 …` and any relative of it: a shell loop over bare milestone-ish suite names.
LITERAL_LIST = re.compile(r"for\s+\w+\s+in\s+m\d[\w ]*;")

errors = []


def fail(msg):
    errors.append(msg)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- what ci.yml actually declares ------------------------------------------------------------
def ci_jobs_and_steps(src):
    """Job ids and step names, straight out of the workflow.

    Regex rather than a YAML parse: the offline suites are stdlib-only by convention, and the two
    shapes needed here (a two-space job key under `jobs:`, a `- name:` step) are unambiguous in this
    file. The floors below are what catches the regex silently ceasing to match — without them a
    broken parser would make every assertion here pass over nothing.
    """
    after_jobs = src.split("\njobs:", 1)
    if len(after_jobs) != 2:
        fail("ci.yml has no `jobs:` block — this gate cannot have read the right file")
        return set(), set()
    jobs = set(re.findall(r"(?m)^  ([a-z0-9_-]+):\s*$", after_jobs[1]))
    steps = set(m.strip() for m in re.findall(r"(?m)^\s*- name: (.+?)\s*$", src))
    return jobs, steps


# --- the document -----------------------------------------------------------------------------
def section_rows(src, marker, path):
    """Table rows between <!-- pr-acceptance:NAME --> and its closing marker."""
    m = re.search(
        r"<!--\s*pr-acceptance:%s\s*-->(.*?)<!--\s*/pr-acceptance:%s\s*-->" % (marker, marker),
        src,
        re.S,
    )
    if not m:
        fail("%s: no <!-- pr-acceptance:%s --> block — the parser has nothing to read, and every "
             "assertion over it would pass by finding nothing" % (os.path.basename(path), marker))
        return []
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue  # the header separator
        rows.append(cells)
    return rows[1:] if rows else []  # drop the header row


def backticked(cell):
    """The single `code` span of a table cell, or the cell itself."""
    m = re.match(r"^`(.+)`$", cell.strip())
    return m.group(1) if m else cell.strip()


def main():
    if not os.path.exists(CI):
        print("FAIL: %s not found" % CI)
        return 1
    jobs, steps = ci_jobs_and_steps(read(CI))
    if len(jobs) < MIN_JOBS:
        fail("only %d jobs parsed out of ci.yml (floor %d) — the regex has stopped matching, so "
             "every check below would pass over nothing" % (len(jobs), MIN_JOBS))
    if len(steps) < MIN_MACHINE_ROWS:
        fail("only %d step names parsed out of ci.yml (floor %d) — same failure" % (len(steps), MIN_MACHINE_ROWS))

    for path in DOCS:
        name = os.path.basename(path)
        if not os.path.exists(path):
            fail("%s is missing — the Russian document and its English mirror are both normative" % name)
            continue
        src = read(path)

        machine = section_rows(src, "machine", path)
        manual = section_rows(src, "manual", path)

        if len(machine) < MIN_MACHINE_ROWS:
            fail("%s: %d machine rows (floor %d)" % (name, len(machine), MIN_MACHINE_ROWS))
        if len(manual) < MIN_MANUAL_ROWS:
            fail("%s: %d manual rows (floor %d)" % (name, len(manual), MIN_MANUAL_ROWS))

        # (1) every documented job and step exists in ci.yml
        named_jobs = set()
        for cells in machine:
            if len(cells) < 3:
                fail("%s: machine row has %d cells, want 3: %r" % (name, len(cells), cells))
                continue
            check, job, step = cells[0], backticked(cells[1]), backticked(cells[2])
            if not check.strip():
                fail("%s: a machine row names no check" % name)
            if job not in jobs:
                fail("%s: machine row names job %r, which ci.yml does not declare (%s)"
                     % (name, job, ", ".join(sorted(jobs))))
            else:
                named_jobs.add(job)
            if step not in steps:
                fail("%s: machine row names step %r, which no step in ci.yml is called. A documented "
                     "check that no workflow runs is indistinguishable from one that passes."
                     % (name, step))

        # (2) THE OTHER DIRECTION — a job nobody documented
        missing = jobs - named_jobs
        if missing:
            fail("%s: ci.yml runs job(s) %s that this document never mentions. A hand-kept list shows "
                 "what is superfluous and not what is absent; that is why this direction is checked "
                 "too (docs/DEVELOPMENT.md §0, principle 5)." % (name, ", ".join(sorted(missing))))

        # (3) every manual row carries a real recorded reason
        for cells in manual:
            if len(cells) < 3:
                fail("%s: manual row has %d cells, want 3: %r" % (name, len(cells), cells))
                continue
            what, how, why = cells[0], cells[1], cells[2]
            for label, cell in (("check", what), ("method", how), ("reason", why)):
                stripped = re.sub(r"[\s\-—–.*_`]", "", cell)
                if not stripped or stripped.upper() in ("TODO", "TBD", "N/A", "NA"):
                    fail("%s: manual row %r has an empty %s. The reason is the whole mechanism: a "
                         "skip with no recorded why is a skip that learned to pass."
                         % (name, what[:60], label))
            if len(why) < 80:
                fail("%s: manual row %r states its reason in %d characters. componentsWithoutProbe is "
                     "the shape being copied, and a one-word reason there would not have stopped "
                     "anything either." % (name, what[:60], len(why)))

    # (5) no literal suite list where it reads as an instruction
    for rel in NO_LITERAL_LIST:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            fail("%s does not exist, so this check passed over nothing" % rel)
            continue
        for n, line in enumerate(read(path).splitlines(), 1):
            if LITERAL_LIST.search(line):
                fail("%s:%d carries a literal list of suite names. tests/ holds %d files; a list is "
                     "how somebody ends up running seven of them: %s"
                     % (rel, n, len([f for f in os.listdir(os.path.join(ROOT, "tests"))
                                     if f.startswith("test_") and f.endswith("_offline.py")]),
                        line.strip()[:100]))

    # (6) the PR template actually carries the manual half, since that is where it is filled in.
    #
    # Both halves of this were tightened after mutation testing survived them:
    #   · a substring test for "PR_ACCEPTANCE.md" passed while the LINK pointed at another file —
    #     the prose still named it. So the link target is resolved against the filesystem.
    #   · counting checkboxes over the whole template passed while all four manual boxes were
    #     deleted, because the Docs checklist below supplied the count. So they are counted inside
    #     their own marker block, the same technique the document itself uses.
    tmpl_path = os.path.join(ROOT, ".github", "PULL_REQUEST_TEMPLATE.md")
    if os.path.exists(tmpl_path):
        tmpl = read(tmpl_path)
        targets = re.findall(r"\]\(([^)\s]+PR_ACCEPTANCE(?:\.en)?\.md)\)", tmpl)
        resolved = [t for t in targets
                    if os.path.exists(os.path.normpath(os.path.join(os.path.dirname(tmpl_path), t)))]
        if not resolved:
            fail("the PR template carries no LINK that resolves to docs/PR_ACCEPTANCE.md (found "
                 "targets: %s). Naming the file in prose is not pointing at it — the document would "
                 "be invisible to everyone who only ever opens a PR." % (targets or "none"))
        m = re.search(r"<!--\s*pr-acceptance:boxes\s*-->(.*?)<!--\s*/pr-acceptance:boxes\s*-->", tmpl, re.S)
        if not m:
            fail("the PR template has no <!-- pr-acceptance:boxes --> block — with no block to read, "
                 "a count over the whole file lets the Docs checklist stand in for the manual half")
        else:
            boxes = len(re.findall(r"(?m)^\s*- \[ \]", m.group(1)))
            if boxes < MIN_MANUAL_ROWS:
                fail("the PR template offers %d manual checkbox(es) inside its block; the manual half "
                     "is %d rows" % (boxes, MIN_MANUAL_ROWS))
    else:
        fail(".github/PULL_REQUEST_TEMPLATE.md is missing")

    if errors:
        print("FAIL: %d PR-acceptance error(s)" % len(errors))
        for e in errors:
            print("  - " + e)
        return 1

    jobs_n, steps_n = len(jobs), len(steps)
    print("pr-acceptance: OK (%d ci.yml jobs, all documented in both languages; %d step names "
          "resolved; manual half carries a recorded reason on every row; no literal suite list in "
          "%d instruction files)" % (jobs_n, steps_n, len(NO_LITERAL_LIST)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
