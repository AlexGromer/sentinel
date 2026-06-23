"""Sentinel M0 brain — minimal "perceive" node.

Spawns pw-executor (via PW_EXECUTOR_CMD), drives it over newline-delimited
JSON-RPC 2.0 / stdio, prints the accessibility tree, and ensures trace.zip in
ARTIFACT_DIR. At M1 this becomes a single node inside a LangGraph StateGraph.
Pure stdlib (no deps) for M0 — proves the wire, not the intelligence.
"""
import json
import os
import pathlib
import shlex
import subprocess
import sys


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


class Executor:
    """Newline JSON-RPC client over the pw-executor subprocess stdio."""

    def __init__(self, cmd: str) -> None:
        self.proc = subprocess.Popen(
            shlex.split(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,  # executor logs flow up to agentctl
            text=True,
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, **params: object) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"executor closed during '{method}'")
        resp = json.loads(line)
        if resp.get("error"):
            raise RuntimeError(f"{method}: {resp['error']['message']}")
        return resp.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    target = os.environ.get("TARGET_URL")
    run_id = os.environ.get("RUN_ID", "local")
    artifact_dir = os.environ.get("ARTIFACT_DIR", f"./runs/{run_id}")
    pw_cmd = os.environ.get("PW_EXECUTOR_CMD")
    if not target:
        log("FATAL: TARGET_URL not set")
        return 2
    if not pw_cmd:
        log("FATAL: PW_EXECUTOR_CMD not set")
        return 2

    out = pathlib.Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace_path = str((out / "trace.zip").resolve())
    log(f"run_id={run_id} target={target} artifacts={out}")

    ex = Executor(pw_cmd)
    try:
        log("executor:", ex.call("initialize"))
        nav = ex.call("browser.navigate", url=target)
        log("navigated:", nav)
        snap = ex.call("browser.snapshot")
        aria = str(snap.get("ariaSnapshot", ""))
        (out / "snapshot.aria.yaml").write_text(aria)
        print("=" * 60)
        print(f"ACCESSIBILITY TREE — {nav.get('title')} ({nav.get('url')})")
        print(f"nodes: {snap.get('nodeCount')}")
        print("=" * 60)
        print(aria)
        log("trace saved:", ex.call("browser.traceStop", path=trace_path))
        ex.call("shutdown")
    finally:
        ex.close()

    p = pathlib.Path(trace_path)
    ok = p.exists() and p.stat().st_size > 0
    log(f"trace.zip present={ok} size={p.stat().st_size if p.exists() else 0} path={trace_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
