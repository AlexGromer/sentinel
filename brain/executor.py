"""Sentinel brain — JSON-RPC client over the pw-executor subprocess (stdio).

M1 transport stays the M0 newline-delimited JSON-RPC 2.0 (MCP-SDK migration = M2).
"""
import json
import shlex
import subprocess
import sys


def log(*a: object) -> None:
    print("[brain]", *a, file=sys.stderr, flush=True)


class Executor:
    def __init__(self, cmd: str) -> None:
        self.proc = subprocess.Popen(
            shlex.split(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
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
