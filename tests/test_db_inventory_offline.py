"""Offline gate: the Python schema is classified too (ADR-100).

Run:  .venv/bin/python tests/test_db_inventory_offline.py

`docs/db-foreign-text.json` is the inventory of every column that can hold FOREIGN TEXT — the words
of the site under test, and the operator's own typed phrases. Its Go twin
(internal/store/purge_inventory_test.go) checks it against the schema the Go gateway creates.

This is the other half, and it is not a duplicate: `brain/store.py::_SCHEMA` is a SECOND, separately
maintained DDL. It declares only the four heal/trust tables — `scenarios`/`results`/`metrics`/`config`
exist solely in the Go gateway — so a column added on the Python side is invisible to the Go gate.
The two halves together cover both writers; either alone leaves a schema unchecked.

Why it opens a real store rather than reading the source: an assertion about the SHAPE OF SOURCE CODE
is a surrogate for the thing it claims to check. This runs LocalStore's real constructor and asks the
database that actually results.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.store import LocalStore                        # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALID = {"inherent", "incidental", "none"}


def main() -> int:
    with open(os.path.join(REPO, "docs", "db-foreign-text.json"), encoding="utf-8") as fh:
        inv = json.load(fh)["tables"]
    assert inv, "inventory parsed to zero tables — every assertion below would be vacuous"

    with tempfile.TemporaryDirectory() as d:
        store = LocalStore(os.path.join(d, "locators.db"))
        rows = store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()
        tables = [r[0] for r in rows]
        assert tables, "LocalStore created no tables — this gate would pass vacuously"

        for tbl in tables:
            assert tbl in inv, (
                f"table {tbl!r} exists in brain/store.py::_SCHEMA but is not classified in "
                f"docs/db-foreign-text.json")
            cols = [r[1] for r in store.db.execute(f"PRAGMA table_info({tbl})").fetchall()]
            assert cols, f"table {tbl} reported zero columns — the per-column checks would be vacuous"
            classified = inv[tbl]["columns"]
            for c in cols:
                assert c in classified, (
                    f"{tbl}.{c} exists in the Python store but is not classified in "
                    f"docs/db-foreign-text.json")
                foreign = classified[c]["foreign"]
                assert foreign in VALID, f"{tbl}.{c}: foreign={foreign!r}, want one of {sorted(VALID)}"
                if foreign != "none":
                    assert classified[c].get("note"), (
                        f"{tbl}.{c} is classified {foreign!r} with no note — say what lands there")
            for c in classified:
                assert c in cols, (
                    f"docs/db-foreign-text.json classifies {tbl}.{c}, which the Python store "
                    f"does not create")

        # The premise this file rests on, asserted rather than assumed: the Python DDL really is the
        # smaller of the two. If it ever grows the M13 domains, "the Go gate covers those" stops being
        # true and this comment stops being a description.
        assert set(tables) == {"healed_locators", "healing_audit", "golden_snapshots",
                               "step_failures"}, (
            f"brain/store.py::_SCHEMA now declares {sorted(tables)} — the split of responsibility "
            f"between this gate and the Go one was stated for the four heal tables only")

    print(f"db-inventory: OK ({len(tables)} Python tables classified; "
          f"{len(inv)} tables in the inventory overall)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
