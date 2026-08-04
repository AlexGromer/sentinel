"""Offline gate: the generated Python stubs are IMPORTABLE, and carry the fields the .proto declares.

Run:  .venv/bin/python tests/test_pb_imports_offline.py

Why this exists. `protoc --python_out` emits a FLAT import for a sibling proto — `import persistence_pb2`
— which only resolves when the generating directory happens to be on sys.path. Inside a package it does
not, so `from brain.pb import store_pb2` raised ModuleNotFoundError.

Nothing caught it, and the consequence was invisible in the worst way: ChatProjector's constructor does
the import, `make_chat_projector()` therefore raised, and `_project_chat` is a best-effort call whose
exception only ever surfaced in a run's own log file. So the `chats` projection ADR-050 promises — the
browsable index behind GET /v1/chats — was never written by any deployment, and the endpoint answered
an honest empty list about a table nothing had ever filled.

It was found by RUNNING a chat turn and asking where the row went, not by reading. This gate is the
cheap version of that question: import the stubs, and check one field per message that the wire
actually has to carry. Both halves matter — an importable stub with a missing field fails just as
silently, because protobuf ignores unknown keyword arguments' absence rather than complaining.

After regenerating stubs, the flat imports must be rewritten to relative ones again:

    .venv/bin/python -m grpc_tools.protoc -Iproto --python_out=brain/pb --grpc_python_out=brain/pb \\
        proto/store.proto
    python3 - <<'EOF'
    import pathlib, re
    for p in pathlib.Path('brain/pb').glob('*.py'):
        s = p.read_text()
        s = re.sub(r'^import ([a-z_]+_pb2) as ([a-z_]+__pb2)$', r'from . import \\1 as \\2', s, flags=re.M)
        s = re.sub(r'^import ([a-z_]+_pb2)$', r'from . import \\1', s, flags=re.M)
        p.write_text(s)
    EOF
"""
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_stubs_import_as_a_package():
    """The import a caller actually writes — not a bare `import store_pb2` that only works from inside
    the directory the generator ran in."""
    from brain.pb import store_pb2, store_pb2_grpc  # noqa: F401
    from brain.pb import persistence_pb2  # noqa: F401


def test_no_flat_sibling_imports_remain():
    """A flat `import x_pb2` is the shape that broke this. Asserted over the FILES, so a regeneration
    that forgets the rewrite fails here instead of at the first chat turn — where the failure hid in a
    run log for months."""
    bad = []
    for p in sorted(pathlib.Path("brain/pb").glob("*.py")):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if re.match(r"^import [a-z_]+_pb2( as [a-z_]+__pb2)?$", line):
                bad.append(f"{p}:{i}: {line}")
    assert not bad, (
        "flat sibling imports found — these resolve only when brain/pb is on sys.path, so they raise "
        "ModuleNotFoundError inside the package:\n  " + "\n  ".join(bad)
    )


def test_the_projector_can_actually_be_constructed():
    """ChatProjector's __init__ does the import, which is why a broken stub showed up as a projector
    that could never exist rather than as an import error anyone would notice."""
    from brain.store import ChatProjector
    # A bogus address is fine: gRPC channels are lazy, so this exercises the import and the stub
    # wiring without needing a gateway. What it must NOT do is raise ModuleNotFoundError.
    ChatProjector("unix:/nonexistent-on-purpose.sock")


def test_messages_carry_the_fields_the_proto_declares():
    """One field per message that the wire has to carry. An importable stub missing a field fails just
    as quietly as an unimportable one."""
    from brain.pb import store_pb2 as pb

    expected = {
        "ChatProjection": ["conversation_id", "last_target", "turn_count", "last_goal", "summary", "owner"],
        "RunRecord": ["run_id", "state", "owner"],
        "Scenario": ["scenario_id", "plan_hash", "owner"],
        "TestRecord": ["test_id", "owner"],
        # fault_domain (HEALTH-004) rides next to `verdict` on purpose: the two answer different
        # questions (WHAT happened vs WHOSE problem it is), and a stub that carried only the first
        # would drop the second on the wire without a word — the failure mode this whole file is about.
        "ResultRecord": ["run_id", "verdict", "fault_domain", "owner"],
        "MetricPoint": ["run_id", "name", "value", "owner"],
        "User": ["user_id", "name", "pw_hash", "is_admin"],
    }
    for msg, fields in expected.items():
        desc = getattr(pb, msg).DESCRIPTOR
        have = {f.name for f in desc.fields}
        missing = [f for f in fields if f not in have]
        assert not missing, f"{msg} is missing {missing} (has: {sorted(have)})"


def test_upsert_chat_accepts_an_owner():
    """The signature the brain calls with. A keyword the stub does not know is a TypeError at the one
    moment nobody is watching — the end of a chat turn."""
    import inspect

    from brain.store import ChatProjector

    sig = inspect.signature(ChatProjector.upsert_chat)
    assert "owner" in sig.parameters, (
        "ChatProjector.upsert_chat takes no `owner` — the chats projection would land unowned, and "
        "'each person has their own chats' would be true of the schema and false of the data"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
