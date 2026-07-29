"""Offline gate: the container build context must actually satisfy every go:embed pattern.

Run:  .venv/bin/python tests/test_container_embed_context_offline.py

The Dockerfile's go-build stage does NOT copy the repository. It names files one by one:

    COPY docs/embed.go docs/index.html docs/prices.json docs/backend-presets.json ... docs/
    COPY brain/embed.go brain/events.json brain/

on purpose — `COPY docs/ docs/` would invalidate the layer and rebuild every Go binary on every
prose edit. The cost of that choice is a hand-kept list, and a hand-kept list drifts. Adding a file
to a //go:embed directive without adding it here compiles fine locally (the whole tree is present)
and fails only inside the image, where the tree is a curated subset.

And there are TWO hand-kept lists, not one. `.dockerignore` excludes `docs/**` with a list of `!`
re-includes, so a file can be named correctly by COPY and still be absent — Docker then fails at
the COPY step with `"/docs/x.json": not found`, which looks nothing like the compiler error a
missing COPY produces. Both layers are modelled here, because neither is a proxy for the other:
the capabilities.json break needed a fix in BOTH files, and fixing only the Dockerfile still left
the real image build failing.

That has now happened twice — 2026-07-23 for the webui embed, 2026-07-29 for capabilities.json —
and both times the only thing guarding it was a comment asking a human to keep the files in sync
(there are three such comments across the Dockerfile and .dockerignore). A comment is not a gate.
The second failure was written by someone who had just read one.

This gate does NOT assert the shape of either file — a claim like "capabilities.json appears in the
Dockerfile" is a surrogate: it passes for a COPY in the wrong stage, a typo'd destination, or a
pattern in a different embed directive it never checks. Instead it REPRODUCES the failure:

  1. parse the go-build stage of the real Dockerfile — its COPY sources/destinations and the
     ./cmd/... targets its RUN line actually builds;
  2. materialise exactly that context into a temp dir — nothing else from the repo is present;
  3. compile those targets there, for real.

So it fails when, and only when, the image build would fail — in a fraction of a second, in the
suite every change already runs, rather than minutes later in the airgap job. Extending a go:embed
pattern and forgetting the Dockerfile breaks this test with the compiler's own message.

The parser is deliberately strict: a COPY form it does not model (a wildcard, --from=, a rename)
is a hard failure, not a skip. A gate that silently stops covering something is the vacuous pass
this project keeps meeting in its own tests.

Known boundary, stated rather than hidden: the .dockerignore matcher covers the forms this file
uses (`dir`, `dir/**`, `!re-include`, `**/name`, `*`, `?`) and treats `**/x` as requiring a leading
directory, where BuildKit also matches a top-level `x`. That divergence cannot affect this repo's
rules, and the model was cross-checked against a real `docker build` in both directions — passing
when the image builds, failing when it does not — rather than trusted on reading.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = "go-build"


def _go_build_stage(dockerfile):
    """The lines of `FROM ... AS go-build`, up to the next FROM."""
    lines = dockerfile.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*FROM\s+.*\bAS\s+" + re.escape(STAGE) + r"\s*$", ln, re.I):
            start = i + 1
            break
    assert start is not None, (
        f"no `FROM ... AS {STAGE}` stage in the Dockerfile — this gate is checking a stage that no "
        "longer exists and would otherwise pass over nothing"
    )
    out = []
    for ln in lines[start:]:
        if re.match(r"^\s*FROM\s", ln, re.I):
            break
        out.append(ln)
    return out


def _join_continuations(lines):
    """Fold `\\`-continued instructions into single logical lines."""
    joined, buf = [], ""
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        if buf.strip():
            joined.append(buf.strip())
        buf = ""
    if buf.strip():
        joined.append(buf.strip())
    return joined


def _copies(stage_lines):
    """Every COPY in the stage, as (sources, destination)."""
    out = []
    for ln in stage_lines:
        if not re.match(r"^COPY\s", ln, re.I):
            continue
        assert "--from=" not in ln, (
            f"COPY --from is not modelled by this gate: {ln!r}. Teach the parser rather than "
            "letting the context silently diverge from the real build."
        )
        parts = ln.split()[1:]
        assert not any(p.startswith("--") for p in parts), f"unmodelled COPY flag in {ln!r}"
        assert len(parts) >= 2, f"malformed COPY: {ln!r}"
        srcs, dst = parts[:-1], parts[-1]
        for s in srcs:
            assert "*" not in s and "?" not in s, (
                f"wildcard COPY source {s!r} is not modelled by this gate — teach the parser"
            )
        out.append((srcs, dst))
    assert out, f"no COPY instructions found in the {STAGE} stage — the parser is not matching"
    return out


def _build_targets(stage_lines):
    """The ./cmd/... packages the stage's RUN line actually compiles."""
    targets = []
    for ln in stage_lines:
        if re.match(r"^RUN\s", ln, re.I):
            targets += re.findall(r"go\s+build\s+.*?(\./cmd/[\w./-]+)", ln)
    assert targets, (
        f"no `go build ./cmd/...` found in the {STAGE} stage — this gate would compile nothing "
        "and report success"
    )
    return targets


def _dockerignore_rules():
    """(regex, negated) for every .dockerignore pattern, in file order.

    Docker resolves a path by the LAST pattern that matches it, so order is load-bearing and the
    list must stay a list. `**` spans separators, `*`/`?` do not — Go's filepath.Match extended,
    which is what BuildKit applies.
    """
    path = os.path.join(REPO, ".dockerignore")
    if not os.path.exists(path):
        return []
    rules = []
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:].strip() if neg else line
        pat = pat.strip("/")
        out, i = "", 0
        while i < len(pat):
            c = pat[i]
            if pat.startswith("**", i):
                out += ".*"
                i += 2
            elif c == "*":
                out += "[^/]*"
                i += 1
            elif c == "?":
                out += "[^/]"
                i += 1
            else:
                out += re.escape(c)
                i += 1
        rules.append((re.compile("^" + out + "$"), neg))
    return rules


def _dockerignored(rel, rules):
    """Would BuildKit keep `rel` out of the build context?

    A pattern that matches a DIRECTORY excludes everything under it, so each ancestor is tested too
    — that is what `docs/**` plus `!docs/index.html` relies on to work at all.
    """
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    excluded = False
    for rx, neg in rules:
        if any(rx.match(c) for c in candidates):
            excluded = not neg
    return excluded


def _uncopied_witness(copies):
    """A repo file the COPY set does NOT bring in.

    Proof that the reconstruction is a strict subset — without it the gate could be compiling the
    repository itself and reporting success over a context that never existed. Computed rather than
    named: hardcoding a witness (say README.md) turns a legitimate `COPY README.md ./` into a false
    failure, which is how the first draft of this gate behaved.
    """
    prefixes = [s.rstrip("/") for srcs, _dst in copies for s in srcs]
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__")]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), REPO)
            if not any(rel == p or rel.startswith(p + os.sep) for p in prefixes):
                return rel
    raise AssertionError(
        "every file in the repo is copied into the go-build stage, so 'the context is a subset' no "
        "longer holds — this gate would be compiling the repository, not the image context"
    )


def _materialise(copies, ctx, rules):
    """Reproduce the COPY instructions into ctx, through .dockerignore. Nothing else gets in."""
    copied = 0
    for srcs, raw_dst in copies:
        # An ABSOLUTE destination (`/app/brain/`, `/bin/` — ordinary in a Dockerfile) must be rooted
        # inside the reconstruction. os.path.join drops everything before an absolute component, so
        # without this the materialiser writes to the HOST filesystem and then compiles a context
        # that is missing the file. Found by a mutation, not by reading.
        dst = raw_dst.lstrip("/") or "."
        for src in srcs:
            rel = src.rstrip("/")
            real = os.path.join(REPO, rel)
            assert os.path.exists(real), f"COPY source does not exist in the repo: {src}"
            if os.path.isdir(real):
                # `COPY a/ b/` places the CONTENTS of a into b — minus whatever .dockerignore keeps
                # out of the context in the first place.
                target = os.path.join(ctx, dst.rstrip("/"))
                for root, dirs, files in os.walk(real):
                    sub = os.path.relpath(root, real)
                    dirs[:] = [
                        d for d in dirs
                        if not _dockerignored(os.path.join(rel, sub, d) if sub != "." else os.path.join(rel, d), rules)
                    ]
                    for f in files:
                        frel = os.path.join(rel, sub, f) if sub != "." else os.path.join(rel, f)
                        if _dockerignored(frel, rules):
                            continue
                        out = os.path.join(target, sub, f) if sub != "." else os.path.join(target, f)
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        shutil.copy2(os.path.join(root, f), out)
                        copied += 1
            else:
                # Docker does not silently skip this one: a named COPY source that .dockerignore
                # keeps out of the context fails the build outright, with "not found" — a message
                # that looks nothing like the compiler error a missing COPY produces, which is why
                # both layers have to be modelled and neither is a proxy for the other.
                assert not _dockerignored(rel, rules), (
                    f"COPY names {src}, but .dockerignore excludes it from the build context, so "
                    f"the image build fails with '\"/{rel}\": not found'. Add a `!{rel}` re-include."
                )
                target = os.path.join(ctx, dst, os.path.basename(real)) if dst.endswith("/") \
                    else os.path.join(ctx, dst)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(real, target)
                copied += 1
    return copied


def main():
    dockerfile = open(os.path.join(REPO, "Dockerfile"), encoding="utf-8").read()
    stage = _join_continuations(_go_build_stage(dockerfile))
    copies = _copies(stage)
    targets = _build_targets(stage)

    # A missing toolchain must fail rather than skip: an un-run gate reads exactly like a passing
    # one, which is the whole failure mode this file exists to close.
    assert shutil.which("go"), (
        "the Go toolchain is not on PATH, so this gate cannot compile the reconstructed context. "
        "Refusing to report success over a check that did not run."
    )

    rules = _dockerignore_rules()
    assert rules, ".dockerignore parsed to zero rules — the context model would ignore exclusions"

    ctx = tempfile.mkdtemp(prefix="sentinel-buildctx-")
    try:
        n = _materialise(copies, ctx, rules)

        # The reconstruction must be a strict SUBSET of the repo, or the gate is testing the repo
        # rather than the image context — measuring a copy instead of the thing.
        witness = _uncopied_witness(copies)
        assert not os.path.exists(os.path.join(ctx, witness)), (
            f"the reconstructed context contains {witness}, which no COPY instruction brings in — "
            "the materialiser is leaking the repository into the context"
        )

        env = dict(os.environ, CGO_ENABLED="0")
        for tgt in targets:
            proc = subprocess.run(
                ["go", "build", "-o", os.devnull, tgt],
                cwd=ctx, env=env, capture_output=True, text=True,
            )
            assert proc.returncode == 0, (
                f"`go build {tgt}` FAILS in the container build context, so the image cannot be "
                f"built — a //go:embed pattern names a file the Dockerfile's COPY lines do not "
                f"bring in (or a source file is missing from the stage).\n{proc.stderr.strip()}"
            )
    finally:
        shutil.rmtree(ctx, ignore_errors=True)

    print(f"container embed context: OK ({len(copies)} COPY instructions -> {n} files through "
          f"{len(rules)} .dockerignore rules; {len(targets)} binaries compile against the "
          f"reconstructed context: {' '.join(targets)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
