#!/usr/bin/env python3
"""
check_bilingual.py — Bilingual docs-parity gate for the Sentinel repo.

Rules
-----
1. Every primary *.md (not ending .en.md) that is NOT in SINGLE_LANGUAGE must
   have a sibling <stem>.en.md in the same directory.
2. Every *.en.md must have a sibling <stem>.md (no orphan English files).
3. WARN-only (never fail): when the heading count differs between a paired
   primary and its .en.md counterpart, print a WARN line but do not exit
   non-zero.

Exit 0 + one-line "OK" summary when no violations are found.
Exit 1 + grep-able "ERROR" lines when violations are found.

Usage
-----
    python3 scripts/check_bilingual.py [repo-root]

repo-root defaults to the directory two levels above this script
(i.e. the repository root when the script lives in scripts/).
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# SINGLE_LANGUAGE allowlist
# Paths are POSIX-format, relative to the repository root.
# Every entry must include a one-line reason.
# ---------------------------------------------------------------------------
SINGLE_LANGUAGE: set[str] = {
    # Internal working / tooling files — updated programmatically or by the
    # project management process; a bilingual copy would immediately diverge.
    "BACKLOG.md",               # sprint backlog; tooling-managed, not end-user docs
    "FILEMAP.md",               # auto-updated file-map index; internal working file
    # Community-health files — GitHub ecosystem convention is English-only for
    # these standard files; translating them would break platform integrations.
    "CODE_OF_CONDUCT.md",       # standard GitHub community health file
    "CONTRIBUTING.md",          # contributor guide; community health file
    "SECURITY.md",              # security policy; community health file
    # GitHub platform templates — PR / issue form strings rendered by GitHub UI;
    # the platform does not support per-locale template variants.
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    # Test fixtures — README embedded inside testdata/; documents the HTML
    # fixture structure for developers only, not end-user documentation.
    "testdata/fixtures/README.md",
}

# Directories to skip entirely during the walk. Includes gitignored runtime/scratch dirs
# (state/, .claude/, runs/, .venv/, __pycache__) so the gate only sees source-tracked docs —
# matching what CI checks out (a fresh tree has none of these).
SKIP_DIRS: frozenset[str] = frozenset(
    # "frontend" / "extension" are dev-only scaffolds (ADR-044 / M9.8) — single-language dev-tool READMEs,
    # not part of the bilingual product docs (like node_modules).
    {"node_modules", "dist", ".git", "bin", "state", ".claude", "runs", ".venv", "__pycache__", "memory", "frontend", "extension", ".next"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_headings(path: Path) -> int:
    """Return the number of ATX-heading lines (# ... through ###### ...) in *path*."""
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.match(r"^#{1,6}\s", line):
                    count += 1
    except OSError:
        pass
    return count


def _collect_md(root: Path) -> tuple[set[Path], set[Path]]:
    """Walk *root* and return (primary_md, en_md) path sets."""
    primary: set[Path] = set()
    english: set[Path] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk does not descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            full = Path(dirpath) / fname
            if fname.endswith(".en.md"):
                english.add(full)
            else:
                primary.add(full)

    return primary, english


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        repo_root = Path(argv[1]).resolve()
    else:
        # Default: two levels up from this file (scripts/check_bilingual.py -> repo root).
        repo_root = Path(__file__).resolve().parent.parent

    if not repo_root.is_dir():
        print(f"ERROR repo root not found: {repo_root}", file=sys.stderr)
        return 1

    primary_md, en_md = _collect_md(repo_root)
    all_md: set[Path] = primary_md | en_md

    errors: list[str] = []
    warnings: list[str] = []
    verified_pairs: int = 0

    # ------------------------------------------------------------------
    # Rule 1 — every primary *.md not in SINGLE_LANGUAGE needs a .en.md.
    # ------------------------------------------------------------------
    for p in sorted(primary_md):
        rel = p.relative_to(repo_root).as_posix()
        if rel in SINGLE_LANGUAGE:
            continue

        en_sibling = p.with_name(p.stem + ".en.md")
        if en_sibling not in all_md:
            errors.append(
                f"MISSING_EN    {rel}"
                f"  ->  expected {en_sibling.relative_to(repo_root).as_posix()}"
            )
        else:
            verified_pairs += 1
            # WARN-only: heading drift between the pair.
            pc = _count_headings(p)
            ec = _count_headings(en_sibling)
            if pc != ec:
                warnings.append(
                    f"HEADING_DRIFT  {rel} ({pc} headings)"
                    f"  vs  {en_sibling.relative_to(repo_root).as_posix()} ({ec} headings)"
                )

    # ------------------------------------------------------------------
    # Rule 2 — every *.en.md must have a primary sibling (no orphans).
    # ------------------------------------------------------------------
    for p in sorted(en_md):
        rel = p.relative_to(repo_root).as_posix()
        # Strip ".en.md" (6 chars) and re-append ".md".
        primary_name = p.name[: -len(".en.md")] + ".md"
        primary_sibling = p.parent / primary_name
        if primary_sibling not in all_md:
            errors.append(
                f"ORPHAN_EN     {rel}"
                f"  ->  missing {primary_sibling.relative_to(repo_root).as_posix()}"
            )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    for w in warnings:
        print(f"WARN  {w}")

    if errors:
        for e in errors:
            print(f"ERROR {e}")
        print(
            f"\nFAIL: {len(errors)} bilingual-parity error(s)."
            f"  pairs_ok={verified_pairs}  allowlisted={len(SINGLE_LANGUAGE)}"
            f"  warnings={len(warnings)}"
        )
        return 1

    print(
        f"OK: bilingual parity verified."
        f"  pairs={verified_pairs}  allowlisted={len(SINGLE_LANGUAGE)}"
        f"  warnings={len(warnings)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
