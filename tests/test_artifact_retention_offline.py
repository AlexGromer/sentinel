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


# ---- driving the REAL _run_explore, offline (ADR-131) --------------------------------------------
# A fake executor and four pages are enough to make the graph walk, and `break_at` makes it fall over
# on a chosen call. That is what turns the checkpoint's lifetime from a claim about source into a
# claim about behaviour: the file either exists after the call or it does not.

_PAGES = {"http://t/": [("b1", "Кнопка 1"), ("b2", "Кнопка 2")], "http://t/a": [("b3", "Кнопка 3")],
          "http://t/b": [("b4", "Кнопка 4")], "http://t/c": []}
_LINKS = {"http://t/": ["http://t/a", "http://t/b", "http://t/c"], "http://t/a": ["http://t/"],
          "http://t/b": ["http://t/"], "http://t/c": ["http://t/"]}


class _FakeEx:
    """The executor surface the crawl graph uses. `break_at` breaks the snapshot on a chosen call;
    `exc` decides WHICH way the run dies, and that distinction is the whole point of the pair below:
    a `RuntimeError` is caught by the salvage handler, a `KeyboardInterrupt` is not."""

    def __init__(self, break_at=None, exc=RuntimeError):
        self.url, self.snaps, self.break_at, self.exc = "http://t/", 0, break_at, exc

    def call(self, m, **p):
        if m == "browser.navigate":
            self.url = p["url"]
            return {"url": self.url, "status": 200, "timing": None}
        if m == "browser.currentUrl":
            return {"url": self.url, "title": ""}
        if m == "browser.snapshot":
            self.snaps += 1
            if self.break_at is not None and self.snaps >= self.break_at:
                raise self.exc("browser.snapshot: FAKE — broken on purpose at call %d" % self.snaps)
            return {"ariaSnapshot": "- document", "nodeCount": 1}
        if m == "browser.interactives":
            return {"elements": [{"role": "button", "name": n, "testid": None, "text": n, "id": i,
                                  "locator": {"testid": i}, "alternatives": [], "disabled": False,
                                  "visible": True} for i, n in _PAGES.get(self.url, [])]}
        if m == "browser.links":
            return {"links": [{"href": h, "text": h} for h in _LINKS.get(self.url, [])]}
        if m == "browser.click":
            return {"clicked": True, "url": self.url}
        if m == "browser.perceptionAudit":
            return {"ratio": 1.0, "total": 1, "addressable": 1}
        return {}


def _drive_explore(break_at, exc):
    """Run the REAL `_run_explore` to its end and report (rc, raised, artifact dir)."""
    import contextlib
    import io
    os.environ.setdefault("SENTINEL_LIVE_FRAMES", "0")
    for stray in ("ORCH_ADDR", "GOAL", "DESCRIBE"):
        os.environ.pop(stray, None)
    from brain import budget
    from brain.__main__ import _run_explore
    out = pathlib.Path(tempfile.mkdtemp(prefix="retention-explore-"))
    budget.reset(plan_limit=10 ** 9, heal_limit=10 ** 9)
    rc, raised = None, None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _run_explore(_FakeEx(break_at=break_at, exc=exc), "ret", out, "http://t/", 0.85, 60)
    except BaseException as e:                                # noqa: BLE001 — the point is to catch ALL
        raised = e
    return rc, raised, out


def _assert_no_checkpoint(out: pathlib.Path, when: str) -> None:
    for name in ("checkpoint.db", "checkpoint.db-wal", "checkpoint.db-shm"):
        assert not (out / name).exists(), (
            f"{name} survived {when} — the largest file a run produces is left behind on exactly the "
            "runs that produce it")


def test_the_checkpoint_is_discarded_even_when_the_run_crashes():
    """BEHAVIOURAL, on the real `_run_explore` — the crash is caught, the work is salvaged, and the
    checkpoint still goes.

    ⚠ REWRITTEN TWICE, and both reasons are worth keeping.

    (1) The first version read 900 characters after `with _checkpointer(` and looked for the word
    `finally:`. W7 added a nested `try/except` around `app.invoke`, the real `finally` slid past the
    900th character, and the assertion went red over a property that had not changed at all.

    (2) The second version walked the syntax tree — but asked whether ANY `Try` in the file discards
    from its `finally`, which is not the same question. MEASURED: moving the discard out of
    `_run_explore`'s `finally` onto the happy path AND adding one to `_run_chat`'s unrelated `finally`
    left this gate green at `2 call site(s), 1 of them in a finally`, while a crashed crawl kept its
    checkpoint. A file-wide existential is satisfied by the wrong occurrence.

    So the question is asked of the run itself. This case covers the caught crash; the sibling below
    covers the one no `except Exception` sees. Together they also pin the salvage contract — exit
    code 5 and a partial artefact — which nothing else asserts end to end (the completeness gate
    drives the graph and calls `_salvage_explore` by hand, so the WIRING inside `_run_explore` is
    invisible to it)."""
    rc, raised, out = _drive_explore(break_at=3, exc=RuntimeError)
    assert raised is None, f"a caught crash escaped _run_explore: {raised!r}"
    # ⚠ THE LITERAL, not the constant. The first version of this line compared `rc` against
    # `EXIT_TOOL_FAILURE_SALVAGED` — and a mutation setting that constant to 0 walked straight
    # through, green, while agentctl handed the hub a clean pass over a run our own tool broke on
    # step 46. A check written in terms of the thing it is checking asserts nothing. The number is
    # a CONTRACT: agentctl, control-api and the hub all read it, so it is pinned here and
    # cross-checked against the catalogue that publishes it to them.
    assert rc == 5, (
        f"a salvaged crash returned {rc}, not 5: the exit code is how the catalogue tells "
        "`fault: tool` WITH a result from `fault: tool` with nothing")
    from brain.__main__ import EXIT_TOOL_FAILURE_SALVAGED
    assert EXIT_TOOL_FAILURE_SALVAGED == 5, (
        f"the constant drifted to {EXIT_TOOL_FAILURE_SALVAGED}; every consumer of this number reads "
        "the catalogue, not this module")
    cat = json.loads((REPO / "brain" / "events.json").read_text(encoding="utf-8"))["exit_codes"]
    assert cat["5"]["fault"] == "tool", (
        f"exit 5 is catalogued as fault {cat['5']['fault']!r}: a salvaged run would blame the tested "
        "application for our own breakage — the substitution ADR-087 forbade")
    plan = out / "plan.json"
    assert plan.exists(), "the crash took the work with it — no plan.json was salvaged"
    c = json.loads(plan.read_text()).get("completeness") or {}
    assert c.get("complete") is False and c.get("reason") == "aborted", (
        f"the salvaged plan does not declare itself aborted: {c}")
    # The lost quality of the run must be IN THE FILE the person is left with. `explore.crashed` is
    # spoken by the caller before salvage, so the real path carries it and a hand-driven salvage
    # cannot — which is exactly why this assertion lives on this side of the fence.
    assert "explore.crashed" in (json.loads(plan.read_text()).get("degradations") or []), (
        "the salvaged plan does not name the degradation that produced it: "
        f"{json.loads(plan.read_text()).get('degradations')!r}")
    _assert_no_checkpoint(out, "a salvaged crash")
    print(f"     salvaged crash: rc={rc}, plan.json kept, degradations named, checkpoint gone")


def test_a_crash_that_salvaged_nothing_does_not_promise_a_result():
    """Exit 5 means "the tool broke, and what it found was SAVED". If nothing was saved, saying 5
    sends a person looking for an artefact that is not there.

    The salvage path can produce nothing for reasons that have nothing to do with the crawl: the
    checkpoint state may be unreadable, or the write itself may fail on a full disk. `salvaged` used
    to be set unconditionally — the handler ran, therefore the run was declared salvaged — so an empty
    run directory still exited 5. The decision is now made on what `_salvage_explore` reports having
    written, and the honest code for "we broke and there is nothing" is the one that already existed:
    4.

    Driven by making salvage report nothing, because that is exactly the branch: the CALLER's choice
    between two catalogue promises is the property under test, not how salvage fails."""
    import brain.__main__ as M
    real = M._salvage_explore
    M._salvage_explore = lambda *a, **k: {}
    try:
        rc, raised, out = _drive_explore(break_at=3, exc=RuntimeError)
    finally:
        M._salvage_explore = real
    assert raised is None, f"the crash escaped instead of being classified: {raised!r}"
    assert rc == 4, (
        f"a crash that salvaged nothing returned {rc}, not 4 — exit 5 promises an artefact, and the "
        "run directory has none")
    assert not (out / "plan.json").exists(), (
        "the fixture is wrong: salvage was stubbed out, so no plan.json should exist and the "
        "assertion above would be proving something else")
    _assert_no_checkpoint(out, "a crash with nothing to salvage")
    print("     nothing salvaged: rc=4, no artefact promised")


def test_the_checkpoint_is_discarded_when_the_crash_is_not_an_exception():
    """The counter-case, and the one that needs the `finally` rather than the handler.

    `except Exception` does not see a `KeyboardInterrupt` — a person pressing Ctrl-C, or the
    orchestrator signalling a run to stop. The salvage handler is skipped entirely, `_run_explore`
    raises out, and the ONLY thing standing between that and a 9 MB file left on disk is the
    `finally`. MEASURED: with the discard moved to the happy path this case fails and the caught-crash
    case above still passes — which is why both are here and neither is redundant."""
    rc, raised, out = _drive_explore(break_at=3, exc=KeyboardInterrupt)
    assert isinstance(raised, KeyboardInterrupt), (
        f"an interrupt did not propagate out of _run_explore (rc={rc}, raised={raised!r}); if it is "
        "being swallowed, a stopped run now reports something other than being stopped")
    _assert_no_checkpoint(out, "an interrupt")
    print("     interrupt: propagated, checkpoint gone")


def test_the_discard_guards_the_explore_run_itself():
    """STRUCTURAL, and deliberately kept beside the behavioural pair — with a different job.

    The pair proves the discard fires on the two exits a test can drive. This proves it is
    UNCONDITIONAL: the `with _checkpointer(...)` of `_run_explore` sits in the BODY of a `try` whose
    `finally` discards, so an exit path nobody has thought to drive — an early `return` added later,
    a third exception type — is covered by construction rather than by having been tested.

    It is bound to that specific `with`, not to the file: `_run_chat` opens a checkpointer too, over
    the SHARED conversation store, which must NOT be discarded. A file-wide question cannot tell those
    two apart, and the measurement above is what that costs.

    ⚠ ITS NON-REDUNDANCY IS MEASURED, not assumed — this codebase treats two checks over one fact as
    a cost until someone names the defect each one catches. Four mutations were run against the pair
    above and this one:

      * discard moved to the happy path                       -> behavioural (interrupt) kills it
      * discard moved to `_run_chat`'s unrelated `finally`     -> behavioural (interrupt) kills it
      * salvage moved after the discard, into the `finally`    -> behavioural (no plan.json) kills it
      * `with _checkpointer(...)` moved OUT of the `try`,      -> ONLY THIS ONE kills it
        the `finally` kept inside it

    The last is a real leak and invisible to behaviour: the driven paths still discard, but anything
    raised by `_checkpointer.__enter__` or `build_graph` — after the sqlite file exists — now skips
    the discard entirely, and no fake executor can provoke that. That is the class this gate owns."""
    import ast

    tree = ast.parse((REPO / "brain" / "__main__.py").read_text())

    def calls_discard(nodes) -> bool:
        return any(isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_discard_checkpoint"
                   for n in nodes for sub in ast.walk(n))

    fn = next((f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef) and f.name == "_run_explore"), None)
    assert fn is not None, "_run_explore is gone; this gate is pointed at a function that no longer exists"
    opens = [w for w in ast.walk(fn) if isinstance(w, ast.With)
             and any(isinstance(it.context_expr, ast.Call)
                     and getattr(it.context_expr.func, "id", "") == "_checkpointer" for it in w.items)]
    assert len(opens) == 1, (
        f"_run_explore opens {len(opens)} checkpointers; this gate assumes exactly one and would "
        "otherwise be guarding whichever it happened to find first")
    guarded = [t for t in ast.walk(fn) if isinstance(t, ast.Try) and calls_discard(t.finalbody)
               and any(opens[0] is sub for stmt in t.body for sub in ast.walk(stmt))]
    assert guarded, (
        "the `with _checkpointer(...)` of _run_explore is not inside a `try` whose `finally` calls "
        "_discard_checkpoint. A discard on the happy path — or one in some OTHER function's `finally` "
        "— leaves the checkpoint behind on exactly the runs that produce the biggest ones.")
    # And salvage must run BEFORE the discard, i.e. inside the guarded body: `_salvage_explore` reads
    # the state through `app.get_state`, and a discard that got there first would take the copy with it.
    body_calls = {getattr(sub.func, "id", "") for stmt in guarded[0].body for sub in ast.walk(stmt)
                  if isinstance(sub, ast.Call)}
    assert "_salvage_explore" in body_calls, (
        "salvage no longer runs inside the guarded block; state is read from the graph, so it has to "
        "happen before the checkpoint is discarded")
    total = sum(1 for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_discard_checkpoint")
    print(f"     _discard_checkpoint: {total} call site(s); _run_explore's is inside a guarding `finally`")


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
