"""Offline gate: a run's residue has a lifetime, and artifacts never enter the database.

Run:  .venv/bin/python tests/test_artifact_retention_offline.py

ADR-099. The backlog item proposed a NEW retention axis. Measurement found two things that changed
the shape of the work:

  * a `retention` family already existed — five settings plus `sweepTraces`/`sweepLogs` with their own
    tests. A second, competing policy over the same files would be the drift this codebase keeps
    removing, so the run directory joined the family instead of getting its own vocabulary;
  * the actual leak was somewhere nobody was looking. On a dev box `runs/` held 606 MB, of which
    **570 MB was `checkpoint.db`** — 284 files, up to 9 MB each, pruned by nothing because the two
    sweepers own traces and logs and no sweeper owned the directory. Traces were 0 MB; logs 4 MB.

What this pins:
  * the checkpoint is deleted when the run ends, including when it CRASHES;
  * it is deleted for a reason that survives reading — it is unresumable by construction, which is
    exactly why multi-turn chat keeps a separate shared store;
  * directory retention is OFF by default: the evidence a person came back for is not disk hygiene;
  * artifacts never enter the database, which was true and unasserted.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_the_checkpoint_is_deleted_when_the_run_ends():
    """Driven through the REAL helper against a real file, then again against a crash.

    `finally`, not a happy-path call: a failed run is exactly when nobody goes looking for stray
    files, and it is also when the checkpoint is largest."""
    from brain.__main__ import _discard_checkpoint
    d = pathlib.Path(tempfile.mkdtemp())
    ckpt = d / "checkpoint.db"
    ckpt.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
    # SQLite leaves these beside the database; deleting the .db alone would leave the pair.
    (d / "checkpoint.db-wal").write_bytes(b"wal")
    (d / "checkpoint.db-shm").write_bytes(b"shm")
    keep = d / "plan.json"
    keep.write_text("{}")

    _discard_checkpoint(str(ckpt))

    for gone in (ckpt, d / "checkpoint.db-wal", d / "checkpoint.db-shm"):
        assert not gone.exists(), f"{gone.name} survived"
    assert keep.exists(), "the sweep took the evidence with it; only the checkpoint is residue"
    # Idempotent: a second teardown (a retry, a double-close) must not raise.
    _discard_checkpoint(str(ckpt))


def test_the_checkpoint_is_discarded_even_when_the_run_crashes():
    """Asserted on the source, because provoking a graph crash needs a browser and an LLM.

    Anchored on the `finally` that owns the call: a happy-path `_discard_checkpoint()` after the
    `with` would leave the largest files behind on exactly the runs that produce them."""
    src = (REPO / "brain" / "__main__.py").read_text()
    i = src.index("with _checkpointer(ckpt) as saver:")
    window = src[i:i + 900]
    assert "finally:" in window, f"the checkpointer block has no finally:\n{window[:400]}"
    fin = window[window.index("finally:"):]
    assert "_discard_checkpoint(" in fin.split("\n\n")[0], (
        f"the discard is not in the finally, so a crashed run keeps its checkpoint:\n{fin[:200]}")


def test_the_reason_it_is_safe_to_delete_is_written_down():
    """A deletion whose justification lives only in a commit message is a deletion someone will undo.

    The checkpoint is unresumable BY CONSTRUCTION — the thread is keyed by a run_id unique to the run
    — and the proof that this is not an accident is that multi-turn chat keeps its own shared store
    rather than reusing it. Both facts are asserted so the pair cannot drift apart."""
    src = (REPO / "brain" / "__main__.py").read_text()
    i = src.index("def _discard_checkpoint(")
    doc = src[i:i + 1400]
    assert "run_id" in doc and "resum" in doc.lower(), (
        "the helper does not say why deletion is safe")
    # ...and the shared store it contrasts with must still exist, or the contrast is stale.
    assert "_conversations_store_path" in src, \
        "the shared conversation store is gone; the justification for deleting the per-run one changed"


def test_directory_retention_is_off_by_default():
    """The evidence a person came back for is not disk hygiene.

    Both knobs default to 0 in the schema AND in the code that reads them; the pairing matters,
    because a default that disagrees with itself is how a setting quietly does nothing — or quietly
    does something."""
    go = (REPO / "cmd" / "agentctl" / "main.go").read_text()
    for env, want in (("SENTINEL_RUN_KEEP", "0"), ("SENTINEL_RUN_TTL_HOURS", "0")):
        m = re.search(re.escape(f'envInt("{env}", ') + r"(-?\d+)\)", go)
        assert m, f"{env} is not read by agentctl"
        assert m.group(1) == want, f"{env} defaults to {m.group(1)} in code, want {want}"

    api = (REPO / "cmd" / "control-api" / "main.go").read_text()
    for key in ("run_keep", "run_ttl_hours"):
        i = api.index(f'"{key}": map[string]any{{')
        entry = api[i:api.index("},", i)]
        assert '"default": 0' in entry, f"{key} does not default to 0 in the schema:\n{entry}"
        assert '"group": "retention"' in entry, (
            f"{key} is not in the existing retention group — a second vocabulary for one decision")
        assert '"hint"' in entry, f"{key} has no bilingual hint, so the wizard renders it blank"


def test_the_newest_run_is_never_swept():
    """A person who has just run something and finds nothing there learns that the tool eats its own
    output. Asserted on the loop, because the Go tests prove the behaviour and this proves the
    behaviour was intended rather than emergent."""
    go = (REPO / "cmd" / "agentctl" / "main.go").read_text()
    i = go.index("func sweepRuns(")
    body = go[i:go.index("\n}", go.index("for i, d := range dirs", i))]
    assert "if i == 0 {" in body and "continue" in body, (
        f"sweepRuns has no guard for the newest run:\n{body[-400:]}")


def test_artifacts_never_enter_the_database():
    """True, and until now unasserted — which is how it would have stopped being true.

    The store is the one place a leak outlives the disk cleanup: files can be swept, rows are copied,
    backed up and shipped. Checked against the schema rather than the code, because the schema is what
    a future feature would have to change first."""
    proto = (REPO / "proto" / "persistence.proto").read_text()
    # CONTENT is what must never be stored, not the word. `screenshot_hash` is legitimate and is the
    # reason goldens are cheap: a hash is not the picture. (My first version of this check forbade the
    # word `screenshot` outright and failed against correct code — a reminder that "must not contain"
    # is only as good as what it names.)
    for field in re.findall(r"^\s*(?:repeated\s+)?(\w+)\s+(\w+)\s*=\s*\d+;", proto, re.M):
        typ, name = field
        if typ != "bytes":
            continue
        assert False, (
            f"the persistence schema has a `bytes` field ({name}): artifacts are kept on disk on "
            "purpose, where retention can reach them, and a row cannot be swept by a file sweeper")
    for word in ("artifact", "blob"):
        assert word not in proto.lower(), (
            f"the persistence schema mentions {word!r}: artifacts belong on disk, not in rows")
    # The one place a picture IS referenced is by hash, and that is what makes goldens storable.
    assert "screenshot_hash" in proto, (
        "goldens stopped being stored as hashes; if a picture entered the schema, retention can no "
        "longer reach it")


def test_the_trace_is_downloadable_and_served_as_binary():
    """ADR-099 opens it. Until now the post-mortem of a failed run needed shell access to the server —
    unavailable to exactly the person the product is for.

    Opened KNOWING what is inside: ADR-098 redacts the text, and the SCREENSHOTS are untouched by
    decision. The comment beside the whitelist entry has to say so, because a reviewer who does not
    know that will assume redaction covered everything."""
    api = (REPO / "cmd" / "control-api" / "main.go").read_text()
    i = api.index("var artifactWhitelist")
    wl = api[i:api.index("\n}", i)]
    # ⚠ The presence of the ENTRY is checked by a Go test that asks the server for the file
    # (`TestTraceIsDownloadableAsBinary`). A substring check here was walked straight through by a
    # mutation: commenting the line out leaves `// "trace.zip": true,`, which still contains the
    # substring. What is asserted here is the part a behavioural test cannot see — the WARNING beside
    # it.
    assert not any(l.strip().startswith("//") and '"trace.zip"' in l for l in wl.splitlines()), \
        "the trace.zip entry is commented out"
    assert "SCREENSHOT" in wl.upper(), (
        "the whitelist does not warn that screenshots are NOT redacted; a reader will assume ADR-098 "
        "covered the whole archive")
    # Binary, and an attachment: a zip served as JSON arrives corrupted through anything that assumes
    # text, and the browser must never be invited to open it.
    j = api.index("func (s *server) handleRunArtifact")
    h = api[j:api.index("\n// readArtifact", j)]
    assert 'strings.HasSuffix(name, ".zip")' in h, "the handler has no binary branch"
    zipbranch = h[h.index('strings.HasSuffix(name, ".zip")'):]
    assert "application/zip" in zipbranch[:400], "the zip is not served with a zip content type"
    assert "attachment" in zipbranch[:400], "the zip is not forced to download"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  ", fn.__name__)
    print(f"OK — {len(fns)} artifact-retention tests passed")
