#!/usr/bin/env python3
"""HEALTH-005 PR-C — every compose service rotates its logs and runs as the operator.

Run:  .venv/bin/python tests/test_compose_logging_offline.py

TWO MEASURED DEFECTS, both of which looked like nothing until somebody needed the thing they broke.

  1. No `logging:` key existed anywhere, in any of the three compose files. Each container therefore
     inherited the daemon's default driver — usually json-file with NO size limit — so a long-lived
     control-api grew its log until the disk did. And `docker compose down` destroys those logs, so
     the one place a container's own output lived was also the place it did not survive.

  2. The image declares no USER, so `docker run` gives uid 0. Everything a container CREATED under
     ./runs and ./state was root-owned on the host: uncleanable without sudo (renaming a directory
     writes to the directory itself, so owning the parent does not help), breaking `go test ./...` on
     a checkout, and — measured on a live stack in PR-B — making the service journal this milestone
     had just added UNREADABLE from the host, `-rw-r----- root root`.

WHY THIS ASSERTS ON THE RESOLVED MAPPING. PyYAML expands anchors and `<<:` merge keys, so what is
checked below is what docker will see, not the text a reader hopes means that. The distinction is not
academic here: `user:` reaches most services THROUGH the anchor, and a YAML merge key REPLACES a
mapping rather than deepening it — the exact mechanism that silently dropped PW_CDP_ENDPOINT from
control-api and cost a live debugging session. A text gate would have had to re-implement merge
semantics to see it; this one gets them for free.

Variable substitution is NOT resolved (that is docker's job at up-time), so `user` is asserted by
SHAPE — it must reference UID and GID with defaults — rather than by value.
"""
import os
import re
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.offline.yml"]

# Services built from OUR image must run as the operator. Third-party images must not be forced into
# a uid their own entrypoints were never written for, and they do not write into our bind mounts —
# ollama keeps its blobs in a named volume, litellm reads a read-only config.
#
# WHY THIS IS DERIVED AND NOT A TUPLE OF PREFIXES (2026-08-17, found while planning `[LIVE-VNC]`).
# It used to be
#     OURS = ("sentinel:local", "ghcr.io/alexgromer/sentinel")
# and a second image variant breaks it in the quietest possible way. `sentinel:vnc` does not start
# with `sentinel:local`, so a service running an image WE build, from a stage of OUR Dockerfile,
# mounting OUR ./state, would be classified as somebody else's — and this gate would then DEMAND it
# carry no `user:`. Obeying that makes it run as root and drop root-owned files on the host bind
# mount: the exact defect the `user:` half of this file exists to prevent, produced by the check that
# exists to prevent it. Worse, the SAME service in docker-compose.ghcr.yml DOES match the GHCR
# prefix, so the two files would be held to opposite rules while the parity gates call them equal.
#
# A prefix tuple answers "does this string look familiar", which is not the question being asked. The
# question is "is this image built from this repository", and that has a source IN the repository:
# the services carrying a `build:` key, plus the repository release.yml publishes to. Both are read
# below, so a third image variant is classified correctly by EXISTING rather than by somebody
# remembering to widen a tuple.
MIN_OURS_REPOS = 2   # `sentinel` (built here) and the GHCR repository release.yml pushes

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def repo_of(image: str) -> str:
    """The repository part of an image reference, with compose's ${VAR:-default} removed first.

    `${SENTINEL_VERSION:-latest}` carries a colon INSIDE the substitution, so a naive split on ':'
    returns a repository that does not exist. Stripping substitutions first is the difference between
    `ghcr.io/alexgromer/sentinel` and `ghcr.io/alexgromer/sentinel:${SENTINEL_VERSION`.
    """
    ref = re.sub(r"\$\{[^}]*\}", "", str(image))
    head, _, tail = ref.rpartition("/")   # a tag's colon is only ever after the last slash
    name = tail.partition(":")[0]
    return f"{head}/{name}" if head else name


def ours_repos() -> "set[str]":
    repos = set()
    with open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)          # PyYAML resolves `<<:`, so an anchored `build:` counts too
    for svc in (doc.get("services") or {}).values():
        if isinstance(svc, dict) and svc.get("build") and svc.get("image"):
            repos.add(repo_of(svc["image"]))
    with open(os.path.join(REPO, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
        m = re.search(r"(?m)^\s{2}IMAGE:\s*(\S+)\s*$", fh.read())
    if m:
        repos.add(repo_of(m.group(1)))
    return repos


OURS_REPOS = ours_repos()


def is_ours(svc: dict) -> bool:
    return repo_of(svc.get("image", "")) in OURS_REPOS


def main() -> int:
    total_services = 0
    total_ours = 0
    total_foreign = 0

    for name in FILES:
        path = os.path.join(REPO, name)
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)

        services = doc.get("services") or {}
        if not services:
            fail(f"{name}: parsed zero services — the parser, not the file, is what broke")
            continue

        # The rotation values must come from ONE place. Asserted as "every service's resolved logging
        # block is identical" rather than "the anchor exists": a hand-copied block that later drifted
        # would satisfy the second and is exactly what the anchor exists to prevent.
        seen_logging = {}

        for svc_name, svc in services.items():
            total_services += 1
            log = svc.get("logging")
            if not isinstance(log, dict):
                fail(f"{name}:{svc_name} has no `logging:` — it inherits the daemon default, which on "
                     f"most hosts is json-file with no size limit, and its output dies with the container")
                continue
            if log.get("driver") != "json-file":
                fail(f"{name}:{svc_name} logging driver is {log.get('driver')!r}; json-file is what "
                     f"`docker compose logs` and every host we support can read")
            opts = log.get("options") or {}
            if not opts.get("max-size"):
                fail(f"{name}:{svc_name} sets a logging driver but no max-size — a driver without a "
                     f"bound is the unbounded default with extra words")
            if not opts.get("max-file"):
                fail(f"{name}:{svc_name} has max-size but no max-file, so exactly one generation is "
                     f"kept and a rotation discards everything older than the last few minutes")
            seen_logging[svc_name] = yaml.safe_dump(log, sort_keys=True)

            if is_ours(svc):
                total_ours += 1
                user = str(svc.get("user", ""))
                if not user:
                    fail(f"{name}:{svc_name} runs our image with no `user:` — it will run as root and "
                         f"leave root-owned files in ./runs and ./state that need sudo to remove")
                elif "${UID" not in user or "${GID" not in user:
                    fail(f"{name}:{svc_name} user is {user!r} — it must reference ${{UID}}/${{GID}} so "
                         f"an operator whose uid is not 1000 can make it true")
                elif ":-" not in user:
                    fail(f"{name}:{svc_name} user is {user!r} with no default. UID/GID are SHELL "
                         f"variables and are usually absent from the environment, so this resolves to "
                         f"an empty uid and the stack fails to start rather than falling back")
            else:
                total_foreign += 1
                if svc.get("user"):
                    fail(f"{name}:{svc_name} is a third-party image ({svc.get('image')}) and is being "
                         f"forced to a uid its entrypoint was not written for; it writes to a named "
                         f"volume, not to our bind mounts, so there is nothing to fix by doing this")

        if len(set(seen_logging.values())) > 1:
            fail(f"{name}: services disagree about the rotation values, so at least one is a copy that "
                 f"drifted from the anchor: " +
                 "; ".join(f"{k}={v.strip()!r}" for k, v in sorted(seen_logging.items())))

    # FLOORS. Every assertion above is vacuously true over an empty set, and this gate walking nothing
    # is precisely how it would silently stop guarding after a parser change.
    if total_services < 15:
        fail(f"only {total_services} services parsed across {len(FILES)} files — the walk is not "
             f"finding them")
    if total_ours < 10:
        fail(f"only {total_ours} services were classified as running our image — the classifier is "
             f"broken, and the `user:` half of this gate asserted almost nothing")
    if total_foreign < 2:
        fail(f"only {total_foreign} third-party services found — the 'do not force a uid on someone "
             f"else's image' half of this gate asserted almost nothing")
    # A floor on the CLASSIFIER itself, not on what it classified. With an empty set of our
    # repositories every service is foreign: the `user:` half asserts nothing at all, and the
    # 'do not force a uid on someone else's image' half fails on every one of ours at once — a
    # confusing red rather than an informative one.
    if len(OURS_REPOS) < MIN_OURS_REPOS:
        fail(f"only {len(OURS_REPOS)} repository/ies derived as ours ({sorted(OURS_REPOS)}, floor "
             f"{MIN_OURS_REPOS}) — the derivation, not the stack, is what regressed. It reads the "
             f"services carrying `build:` in docker-compose.yml plus IMAGE: in release.yml")

    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"compose logging/user: OK ({total_services} services across {len(FILES)} files — "
          f"{total_ours} ours run as ${{UID}}:${{GID}}, {total_foreign} third-party left alone; "
          f"rotation identical everywhere)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
