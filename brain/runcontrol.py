"""Sentinel brain — RunControl client (M8, ADR-021).

Reports per-node-step token deltas to the Go orchestrator and honours its abort signal (the
orchestrator reconciles cumulative spend against the run budget; its SIGTERM is the
model-INDEPENDENT backstop). No-op when `ORCH_ADDR` is unset, so the standalone CLI path is
unchanged and the offline tests never touch gRPC.
"""
from __future__ import annotations

import os

from .executor import log


class _Noop:
    """Used when no orchestrator is configured: report() never aborts."""

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running") -> bool:
        return False

    def close(self) -> None:
        pass


class _GrpcRunControl:
    def __init__(self, addr: str) -> None:
        import grpc
        from .pb import runcontrol_pb2 as pb, runcontrol_pb2_grpc as pbg
        self._pb = pb
        self._ch = grpc.insecure_channel(addr)
        self._stub = pbg.RunControlStub(self._ch)

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running") -> bool:
        """Send a per-step token delta; returns True iff the orchestrator signals an abort."""
        try:
            c = self._stub.ReportEvent(self._pb.RunEvent(
                run_id=run_id, node=node, prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0), status=status))
            if c.abort:
                log(f"runcontrol: orchestrator signalled abort -> {c.reason}")
            return bool(c.abort)
        except Exception as e:  # telemetry must never break the run
            log("runcontrol report error (continuing):", e)
            return False

    def close(self) -> None:
        try:
            self._ch.close()
        except Exception:
            pass


def make_client():
    """`_GrpcRunControl` when ORCH_ADDR is set (orchestrator running), else a no-op."""
    addr = os.environ.get("ORCH_ADDR")
    if not addr:
        return _Noop()
    try:
        return _GrpcRunControl(addr)
    except Exception as e:
        log("runcontrol unavailable -> no-op:", e)
        return _Noop()
