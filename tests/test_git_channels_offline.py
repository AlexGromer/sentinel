"""Offline gate: git as an import source and an export target (PROD-IMPORT/EXPORT, PR-6, OSS).

Run:  .venv/bin/python tests/test_git_channels_offline.py

Everything here runs against a LOCAL BARE REPOSITORY in a temp directory — no network, ever. That is
not a testing convenience: a local path must work with no network for the air-gapped install to be
whole (the file-based revision store was justified by exactly that promise), so CI and an air-gapped
operator exercise the same code path. If this gate ever needs the internet, the property it is
protecting has already been lost.

The line this implements is protocol vs service. Reaching a repository over plain git is plumbing —
one more way to get at files, with the filesystem channel doing the real work — so it is OSS.
Managed integration (stored credentials, PR automation over the forge APIs, webhooks, conflict
policy, multi-repo routing) is a service and lives elsewhere.

Three behaviours are pinned, and each was a real defect first:

  1. a clone that CHECKS OUT NOTHING is named as such. A repository whose HEAD points at a branch that
     does not exist — a bare repo initialised as `master` and pushed to as `main`, ordinary in the
     wild — clones successfully to an empty tree. Downstream that surfaced as "no *.spec.ts found",
     sending the reader hunting for missing tests instead of a dangling HEAD.
  2. an export into a TEMPORARY CLONE without --push is REFUSED. The first version committed there and
     printed "committed <sha>" — a commit that ceased to exist when the command returned. It reported
     success, twice in a row, for doing nothing.
  3. a re-export of an unchanged spec is a NO-OP, not an empty commit.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

AGENTCTL = os.path.join(REPO, "bin", "agentctl")
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(cwd, *args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=GIT_ENV,
                       capture_output=True, text=True, timeout=120)
    if check:
        assert r.returncode == 0, (args, r.stderr[-400:])
    return r


def ctl(*args, timeout=300):
    return subprocess.run([AGENTCTL] + list(args), cwd=REPO, env=GIT_ENV,
                          capture_output=True, text=True, timeout=timeout)


def _seed_bare(root, files, head="main"):
    """A local bare repo holding `files`, with HEAD pointing where we say."""
    bare = os.path.join(root, "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", bare], check=True, env=GIT_ENV)
    work = os.path.join(root, "seed")
    os.makedirs(work)
    git(work, "init", "-q", ".")
    for rel, body in files.items():
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(body)
    git(work, "add", "-A")
    git(work, "-c", "commit.gpgsign=false", "commit", "-qm", "seed")
    git(work, "push", "-q", bare, "HEAD:refs/heads/main")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/" + head)
    return bare, work


def main():
    assert os.path.exists(AGENTCTL), (
        "bin/agentctl is not built, so this gate cannot run — refusing to report success over a "
        "check that did not happen (CI builds it before the suite)"
    )
    spec = open(os.path.join(REPO, "testdata", "import", "playwright-login.spec.ts"),
                encoding="utf-8").read()
    cy = open(os.path.join(REPO, "testdata", "import-dialects", "cypress-legacy-layout",
                           "checkout.spec.ts"), encoding="utf-8").read()

    root = tempfile.mkdtemp(prefix="sentinel-gitgate-")
    try:
        bare, _ = _seed_bare(root, {"e2e/login.spec.ts": spec, "e2e/checkout.spec.ts": cy})

        # 1 — IMPORT from a local bare repo, with no network at all.
        out = os.path.join(root, "imp")
        r = ctl("import", "--from-git", bare, "--artifact-dir", out)
        assert r.returncode == 0, (r.returncode, r.stderr[-500:])
        rep = json.loads(open(os.path.join(out, "import-report.json"), encoding="utf-8").read())
        assert rep["engines"] == ["cypress", "playwright"], rep["engines"]
        assert rep["totals"]["tests"] == 4, rep["totals"]
        assert rep["totals"]["skipped"] == 0, rep["totals"]
        print("PASS import --from-git reads a local bare repo with no network (2 engines, 4 tests)")

        # 2 — exactly one source. Both or neither is ambiguous, and picking one silently is how a
        #     caller ends up importing something they did not name.
        r = ctl("import", "--from", root, "--from-git", bare)
        assert r.returncode == 2 and "exactly one" in r.stderr, (r.returncode, r.stderr[-300:])
        r = ctl("import")
        assert r.returncode == 2, r.returncode
        print("PASS import demands exactly one of --from / --from-git")

        # 3 — a clone that checks out NOTHING says why. Before this it reported "no *.spec.ts found",
        #     which points at the wrong problem entirely.
        dangling = os.path.join(root, "dang")
        os.makedirs(dangling)
        bare2, _ = _seed_bare(dangling, {"e2e/a.spec.ts": spec}, head="nope")
        r = ctl("import", "--from-git", bare2, "--artifact-dir", os.path.join(root, "imp2"))
        assert r.returncode != 0, "a repo that checked out nothing was treated as a successful import"
        assert "checked out nothing" in r.stderr and "HEAD" in r.stderr, r.stderr[-300:]
        print("PASS a dangling HEAD is named, not reported as a suite with no tests")

        # 4 — EXPORT into a bare/remote target WITHOUT --push is refused, because the commit would
        #     live only in a temp clone and be discarded. It used to print "committed <sha>" for it.
        sp = os.path.join(root, "exported.spec.ts")
        open(sp, "w", encoding="utf-8").write(spec)
        r = ctl("export-git", "--spec", sp, "--to-git", bare, "--branch", "x")
        assert r.returncode == 2, (r.returncode, r.stdout[-200:], r.stderr[-300:])
        assert "discarded" in r.stderr, r.stderr[-300:]
        print("PASS exporting to a remote without --push is refused, not silently discarded")

        # 5 — a local WORKING TREE is written IN PLACE, and the commit survives the command.
        work = os.path.join(root, "checkout")
        git(root, "clone", "-q", bare, work)
        r = ctl("export-git", "--spec", sp, "--to-git", work, "--subdir", "e2e")
        assert r.returncode == 0, (r.returncode, r.stderr[-400:])
        assert "committed" in r.stdout, r.stdout
        tree = git(work, "ls-tree", "-r", "--name-only", "HEAD").stdout
        assert "e2e/exported.spec.ts" in tree, tree
        print("PASS a local checkout is written in place and the commit survives")

        # 6 — re-exporting an unchanged spec is a NO-OP. An empty commit per run would turn a
        #     scheduled export into history noise.
        before = git(work, "rev-list", "--count", "HEAD").stdout.strip()
        r = ctl("export-git", "--spec", sp, "--to-git", work, "--subdir", "e2e")
        assert r.returncode == 0 and "no change" in r.stdout, (r.returncode, r.stdout)
        after = git(work, "rev-list", "--count", "HEAD").stdout.strip()
        assert before == after, ("an empty commit was made", before, after)
        print("PASS re-exporting an unchanged spec changes nothing")

        # 7 — with --push the REMOTE actually receives it. Reporting a push that did not happen is
        #     the same class of lie as the discarded commit above.
        r = ctl("export-git", "--spec", sp, "--to-git", bare, "--branch", "sentinel/pushed",
                "--subdir", "e2e", "--push")
        assert r.returncode == 0 and "pushed" in r.stdout, (r.returncode, r.stdout, r.stderr[-300:])
        refs = git(bare, "show-ref").stdout
        assert "refs/heads/sentinel/pushed" in refs, refs
        landed = git(bare, "ls-tree", "-r", "--name-only", "sentinel/pushed").stdout
        assert "e2e/exported.spec.ts" in landed, landed
        print("PASS --push lands the spec on the remote branch it names")

        print("ALL PASS (7)")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
