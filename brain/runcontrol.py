"""Sentinel brain — RunControl client (M8, ADR-021; M9.8 F4 takeover, ADR-054).

Reports per-node-step token deltas to the Go orchestrator and honours its control reply (the
orchestrator reconciles cumulative spend against the run budget; its SIGTERM is the
model-INDEPENDENT backstop). No-op when `ORCH_ADDR` is unset, so the standalone CLI path is
unchanged and the offline tests never touch gRPC.

The control reply is a VERB, not a bool: "continue" | "abort" | "takeover" (precedence abort >
takeover > continue). `report()` carries the real per-step token delta; `poll()` is a 0-token
heartbeat used at the superstep boundary to surface a pending operator takeover (M9.8 F4) without
spending budget. On takeover the brain interrupt()s + persists (brain/graph.py:checkpoint); on the
orchestrator's Return the verb drops back to "continue" and the brain resumes the same thread.
"""
from __future__ import annotations

import os

from .executor import log

# Control verbs returned by report()/poll().
CONTINUE = "continue"
ABORT = "abort"
TAKEOVER = "takeover"


class _Noop:
    """Used when no orchestrator is configured: the run is never aborted and never paused."""

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running") -> str:
        return CONTINUE

    def poll(self, run_id, node="checkpoint") -> str:
        return CONTINUE

    def close(self) -> None:
        pass


class _GrpcRunControl:
    def __init__(self, addr: str) -> None:
        import grpc
        from .pb import runcontrol_pb2 as pb, runcontrol_pb2_grpc as pbg
        self._pb = pb
        self._ch = grpc.insecure_channel(addr)
        self._stub = pbg.RunControlStub(self._ch)

    @staticmethod
    def _verb(c) -> str:
        """Map a Control reply to a verb. abort (hard stop) beats takeover (pause) beats continue."""
        if getattr(c, "abort", False):
            return ABORT
        if getattr(c, "takeover", False):
            return TAKEOVER
        return CONTINUE

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running") -> str:
        """Send a per-step token delta; returns the orchestrator's control verb (continue|abort|takeover)."""
        try:
            c = self._stub.ReportEvent(self._pb.RunEvent(
                run_id=run_id, node=node, prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0), status=status))
            verb = self._verb(c)
            if verb == ABORT:
                log(f"runcontrol: orchestrator signalled abort -> {c.reason}")
            elif verb == TAKEOVER:
                log(f"runcontrol: orchestrator signalled takeover -> {c.reason}")
            return verb
        except Exception as e:  # telemetry must never break the run
            log("runcontrol report error (continuing):", e)
            return CONTINUE

    def poll(self, run_id, node="checkpoint") -> str:
        """M9.8 F4 (ADR-054): a 0-token heartbeat — surfaces a pending takeover/abort at the superstep
        boundary without spending budget (the orchestrator adds 0 to the run's spend)."""
        return self.report(run_id, node, 0, 0, status="running")

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
