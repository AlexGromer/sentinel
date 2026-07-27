"""Offline gate: the CI templates we ship actually consume what we produce (ADR-090).

Run:  .venv/bin/python tests/test_ci_templates_offline.py

`junit.xml` has existed since ADR-073 as "the machine contract every CI consumes", and both templates
in docs/ci-templates/ — the first thing a new user copies — consumed nothing. They archived `runs/`
and left the results invisible in the pipeline view. A contract nobody is wired to is a file.

Nothing gated these templates at all before this, so the wiring could quietly rot back out. The checks
are deliberately about BEHAVIOUR a user would notice (results appear; a passing run is not reddened by
a missing optional file), not about formatting.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = pathlib.Path(__file__).resolve().parent.parent
TPL = REPO / "docs" / "ci-templates"

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def test_gitlab_publishes_junit() -> None:
    f = TPL / ".gitlab-ci.yml"
    src = f.read_text()
    if "artifacts:" not in src:
        fail(f"{f.name}: no artifacts block — this check is looking at the wrong file")
        return
    if "reports:" not in src or "junit:" not in src:
        fail(f"{f.name}: archives runs/ but never declares `reports: junit:` — the results stay "
             f"invisible in the pipeline, which is the whole point of producing junit.xml")
    if "runs/*/junit.xml" not in src and "runs/**/junit.xml" not in src:
        fail(f"{f.name}: the junit path must glob run directories — one file per run")


def test_jenkins_publishes_junit() -> None:
    f = TPL / "Jenkinsfile"
    src = f.read_text()
    if "archiveArtifacts" not in src:
        fail(f"{f.name}: no archiveArtifacts — this check is looking at the wrong file")
        return
    # The junit STEP line, not the whole file: a comment explaining the option contains the option's
    # name, so `"allowEmptyResults" in src` is true even after the option is deleted. That exact trap
    # (a check satisfied by its own documentation) was caught twice elsewhere today.
    step = [ln for ln in src.splitlines()
            if ln.strip().startswith("junit ") or ln.strip().startswith("junit(")]
    if not step:
        fail(f"{f.name}: archives runs/** but never calls the `junit` step — Jenkins shows no tests")
        return
    if "allowEmptyResults" not in step[0]:
        # An explore run produces no junit.xml (it is a replay artifact). Without this, the template
        # reddens a perfectly good build over a file that was never meant to exist.
        fail(f"{f.name}: `junit` without allowEmptyResults would fail an explore run, which "
             f"legitimately produces no junit.xml")


def test_templates_run_the_report_that_produces_junit() -> None:
    """The wiring above is worthless if nothing generates the file.

    A template drives the CLI, and there `agentctl report` must be called explicitly — the chaining
    added in ADR-089 lives in control-api and covers UI-launched runs only. If neither template calls
    it, both publish nothing forever and the junit wiring above is decoration.
    """
    missing = []
    for name in (".gitlab-ci.yml", "Jenkinsfile"):
        src = (TPL / name).read_text()
        if "report" not in src or "--run" not in src:
            missing.append(name)
    if missing:
        fail("templates that publish junit.xml but never run `agentctl report --run` to create it: "
             + ", ".join(missing))


def main() -> int:
    if not TPL.exists():
        print(f"FAIL — {TPL} does not exist")
        return 1
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__) if not FAILURES else None
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} problem(s):")
        for m in FAILURES:
            print("  -", m)
        return 1
    print(f"CI templates OK — {len(fns)} checks: both templates publish junit.xml and generate it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
