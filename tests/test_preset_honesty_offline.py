"""Offline gate: a runtime preset tells the truth about whether it has a service behind it (ADR-091).

Run:  .venv/bin/python tests/test_preset_honesty_offline.py

The wizard offers nine runtime presets. Exactly TWO — ollama and litellm — have a real service in this
repo's docker-compose; the other seven are placeholders whose address the user must replace. That was
stated only inside a `_note` field in the JSON, which nobody opens, so seven of the nine dropdown
entries silently meant "you have more work to do".

This is the LiteLLM defect one level down: the capability exists, the way in does not. The gate keeps
the hints tied to what compose ACTUALLY defines, because that file changes and the hints will not
follow it on their own.
"""
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = pathlib.Path(__file__).resolve().parent.parent
PRESETS = REPO / "docs" / "backend-presets.json"
COMPOSE = REPO / "docker-compose.yml"

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def compose_services() -> set[str]:
    """Top-level service names, read as text: pyyaml is not a dependency of this suite and the shape
    here is a flat two-space-indented mapping under `services:`."""
    src = COMPOSE.read_text()
    body = src[src.index("\nservices:"):]
    return {m.group(1) for m in re.finditer(r"^  ([a-z][a-z0-9-]*):", body, re.M)}


def test_every_preset_carries_a_bilingual_hint() -> None:
    presets = json.loads(PRESETS.read_text())["presets"]
    if len(presets) < 5:
        fail("fewer than 5 presets — this gate is looking at the wrong file")
        return
    for name, p in presets.items():
        hint = p.get("hint")
        if not isinstance(hint, dict) or not hint.get("ru") or not hint.get("en"):
            fail(f"preset {name!r} has no bilingual hint — the user cannot tell what it needs")


def test_a_preset_naming_a_compose_service_matches_reality() -> None:
    """A hint that says "start it with --profile X" must refer to a service that exists.

    The direction that matters: a hint promising a service we do not ship sends the user to run a
    command that fails, which is worse than saying nothing.
    """
    presets = json.loads(PRESETS.read_text())["presets"]
    services = compose_services()
    if not services:
        fail("no services parsed from docker-compose.yml — every check below would be vacuous")
        return
    for name, p in presets.items():
        hint = (p.get("hint") or {}).get("en", "")
        for m in re.finditer(r"--profile ([a-z][a-z0-9-]*)", hint):
            svc = m.group(1)
            if svc not in services:
                fail(f"preset {name!r} tells the user to run `--profile {svc}`, but docker-compose.yml "
                     f"defines no such service (has: {', '.join(sorted(services))})")


def test_a_preset_without_a_service_says_so() -> None:
    """The other direction, and the one that was actually broken: a preset whose host is not a service
    we ship must be marked as a placeholder, or the user copies an address that resolves to nothing."""
    presets = json.loads(PRESETS.read_text())["presets"]
    services = compose_services()
    marked = 0
    for name, p in presets.items():
        base = p.get("base_url") or ""
        if not base:
            continue                       # anthropic/openai: no host of ours to promise
        host = re.sub(r"^https?://", "", base).split(":")[0].split("/")[0]
        hint = (p.get("hint") or {})
        text = (hint.get("ru", "") + " " + hint.get("en", ""))
        is_ours = host in services
        says_placeholder = ("ЗАГОТОВКА" in text) or ("PLACEHOLDER" in text)
        if is_ours and says_placeholder:
            fail(f"preset {name!r} points at our own service {host!r} but calls itself a placeholder")
        if not is_ours and not says_placeholder:
            fail(f"preset {name!r} points at {host!r}, which is not a service this repo defines, and "
                 f"does not say so — the user copies an address that resolves to nothing")
        if says_placeholder:
            marked += 1
    if marked == 0:
        fail("no preset is marked as a placeholder, yet most have no service — the check is not "
             "seeing the hints at all")


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} problem(s):")
        for m in FAILURES:
            print("  -", m)
        return 1
    svc = sorted(compose_services())
    print(f"preset honesty OK — {len(fns)} checks; compose defines {len(svc)} services ({', '.join(svc)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
