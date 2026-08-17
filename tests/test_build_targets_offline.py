"""Every place that builds this image NAMES the stage it wants — and every stage is built by someone.

WHY THIS EXISTS, measured 2026-08-17 while planning `[LIVE-VNC]`. `grep -n "target:"` over the
Dockerfile and every compose file returned ZERO. All three build sites — the CI `airgap` job, the
release workflow and `scripts/offline-verify.sh` — took the Dockerfile's LAST stage, which happened
to be `runtime`. Nothing said so anywhere; it was true by stage ORDER.

That is a defect waiting for its trigger, and `[LIVE-VNC]` is the trigger: it adds a second final
stage (`FROM runtime AS vnc`, +246 MB of X server) AFTER `runtime`. Without this gate every one of
the three sites would have silently started building and publishing THAT image — under the same tags,
through the same steps, past the same assertions, all of which are about `sentinel:local` and
`ghcr.io/.../sentinel:<tag>` by name rather than by content. The release would have handed everyone
a quarter of a gigabyte they did not ask for, immediately after ADR-124 removed 43% for exactly that
reason, and the first symptom would have been a bug report about image size.

TWO DIRECTIONS, because each catches what the other cannot:

  * a site that names no target, or names a stage that does not exist — the failure above;
  * a stage that nobody builds and nobody consumes — a stage written, never wired, and believed to
    ship. `[LIVE-VNC]`'s own second image is exactly that shape until release.yml builds it, so this
    direction is not hypothetical either: it is how the vnc image proves it is actually published.

⚠ DECLARED BOUNDARY, stated rather than left to be discovered. "A stage somebody consumes" is read
from `COPY --from=<stage>` alone. A stage consumed only through a mechanism this parser does not
model (`RUN --mount=from=`, a build-context alias) would be reported as unbuilt. That is a loud,
legible failure and a deliberate trade: the alternative is a parser that models all of BuildKit, and
a check nobody can read is worse than one whose edge is written down.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"

# Floors, in the shape the rest of the suite uses: a derived list removes "somebody forgot", and
# introduces "the parser stopped matching and every assertion over ∅ passed". These are the only
# things that catch that, so they are not optional — and they are edits, not counts that follow
# whatever the tree happens to say today.
MIN_STAGES = 3   # go-build · ts-build · runtime  (LIVE-VNC adds `vnc`; a floor only ever goes UP)
MIN_SITES = 3    # ci.yml (airgap) · release.yml (image) · scripts/offline-verify.sh


def stages() -> "dict[str, int]":
    """stage name -> the line it is declared on, from `FROM <image> AS <name>`.

    `AS` is matched case-insensitively because Dockerfile keywords are, and a repository that wrote
    `as` in lower case would otherwise derive an empty set and pass this file perfectly.
    """
    out: "dict[str, int]" = {}
    for i, line in enumerate(DOCKERFILE.read_text().splitlines(), start=1):
        m = re.match(r"(?i)^FROM\s+\S+(?:\s+--platform=\S+)?\s+AS\s+([A-Za-z0-9][\w.-]*)\s*$", line.strip())
        if m:
            out[m.group(1)] = i
    return out


def consumed() -> "set[str]":
    """Stage names that another stage copies from — i.e. intermediate by construction.

    An image reference (`COPY --from=ghcr.io/astral-sh/uv:0.11.8`) is not a stage of this file and is
    filtered by intersecting with the declared names at the call site.
    """
    text = DOCKERFILE.read_text()
    return {m.group(1) for m in re.finditer(r"(?im)^\s*COPY\s+--from=([A-Za-z0-9][\w.:/-]*)", text)}


def _workflow_sites(path: pathlib.Path) -> "list[tuple[str, str | None]]":
    """(step name, target or None) for every `docker/build-push-action` step in a workflow.

    Hand-rolled rather than PyYAML on purpose: the question is about a step's OWN `with:` block, and
    the answer has to be attributable to a step NAME so the failure message can say which one. A
    parsed document gives the same facts, but the walk to them is longer than the split below.

    A step begins at `      - ` (six spaces, the jobs.<id>.steps level) and runs to the next one.
    """
    text = path.read_text()
    starts = [m.start() for m in re.finditer(r"(?m)^      - ", text)]
    out: "list[tuple[str, str | None]]" = []
    for i, s in enumerate(starts):
        block = text[s: starts[i + 1] if i + 1 < len(starts) else len(text)]
        if "docker/build-push-action" not in block:
            continue
        nm = re.search(r"(?m)^\s+(?:- )?name:\s*(.+?)\s*$", block)
        tg = re.search(r"(?m)^\s+target:\s*(\S+)\s*$", block)
        out.append((nm.group(1) if nm else f"<unnamed step at char {s}>", tg.group(1) if tg else None))
    return out


def _script_sites(path: pathlib.Path) -> "list[tuple[str, str | None]]":
    """(command, target or None) for every `docker buildx build` / `docker build` in a shell script.

    Lines are joined on a trailing backslash first: `scripts/offline-verify.sh` splits its build over
    two lines, and a line-at-a-time scan would read the flag half of the command and miss the target.
    """
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    out: "list[tuple[str, str | None]]" = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not re.search(r"\bdocker\s+(buildx\s+)?build\b", stripped):
            continue
        tg = re.search(r"--target[= ](\S+)", stripped)
        out.append((stripped[:90], tg.group(1) if tg else None))
    return out


def build_sites() -> "list[tuple[str, str, str | None]]":
    """(file, what, target) for every place in the repository that builds this Dockerfile."""
    sites: "list[tuple[str, str, str | None]]" = []
    for wf in ("ci.yml", "release.yml"):
        p = REPO / ".github" / "workflows" / wf
        sites += [(wf, name, tgt) for name, tgt in _workflow_sites(p)]
    for sh in sorted((REPO / "scripts").glob("*.sh")):
        sites += [(f"scripts/{sh.name}", cmd, tgt) for cmd, tgt in _script_sites(sh)]
    return sites


def test_the_dockerfile_declares_the_stages_this_gate_reasons_about():
    st = stages()
    assert len(st) >= MIN_STAGES, (
        f"derived only {len(st)} stage(s) {sorted(st)} from {DOCKERFILE.name}, floor {MIN_STAGES}. "
        f"The `FROM … AS …` regex, not the Dockerfile, is what regressed — and with an empty set "
        f"every assertion below passes while checking nothing.")


def test_every_build_site_names_the_stage_it_wants():
    """Not "when there is more than one candidate" — ALWAYS.

    A conditional rule would be switched off today (there is one final stage) and would switch itself
    on, unannounced, in the same commit that makes it matter. Then the gate's first act would be to
    fail a PR about something else. Requiring the target unconditionally means this file has an
    opinion from the day it lands and every mutation below is red immediately.
    """
    st, sites = stages(), build_sites()
    assert len(sites) >= MIN_SITES, (
        f"derived only {len(sites)} build site(s) {[f'{f}: {w}' for f, w, _ in sites]}, floor "
        f"{MIN_SITES}. A walk that stopped finding build steps reports 'every site names its target' "
        f"in exactly the same words as a clean one.")

    for where, what, target in sites:
        assert target, (
            f"{where}: the build step '{what}' names no `target`, so it builds whatever stage the "
            f"Dockerfile happens to END with. That is a property of stage ORDER, not of this step — "
            f"append a stage and this site silently starts producing it, under the same tag, past "
            f"every assertion that names the image rather than reading it.")
        assert target in st, (
            f"{where}: the build step '{what}' names target '{target}', which {DOCKERFILE.name} "
            f"does not declare. Known stages: {sorted(st)}.")


def test_every_stage_is_either_built_by_someone_or_consumed_by_another():
    """The other direction: a stage nobody builds is a stage nobody tests.

    `go-build` and `ts-build` are consumed (`COPY --from=`), so they are exercised by every build of
    the stage that copies from them. A FINAL stage has no such witness — if no site names it, it is
    written, believed to ship, and never once produced.
    """
    st = stages()
    built = {t for _f, _w, t in build_sites() if t}
    intermediate = consumed() & set(st)
    orphans = sorted(set(st) - built - intermediate)
    assert not orphans, (
        f"stage(s) {orphans} are declared in {DOCKERFILE.name} and neither built by any site nor "
        f"copied from by another stage. Nothing produces them, so nothing tests them — and a stage "
        f"in the file reads to the next person as a thing that ships.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} build-target tests passed")
