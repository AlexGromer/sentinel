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

from .eventlog import log

# Control verbs returned by report()/poll().
CONTINUE = "continue"
ABORT = "abort"
TAKEOVER = "takeover"


class _Noop:
    """Used when no orchestrator is configured: the run is never aborted and never paused."""

    # ADR-108c: `wired` says whether there is anybody on the other end. The map gate reads it because a
    # gate with no one to answer it is not a safeguard, it is a hang — a headless run (CI, cron, the
    # air-gapped bundle) has no operator, and waiting for one would stop the product working at all.
    wired = False

    def report(self, run_id, node, prompt_tokens, completion_tokens, status="running") -> str:
        return CONTINUE

    def poll(self, run_id, node="checkpoint") -> str:
        return CONTINUE

    def map_decision(self, run_id) -> str:
        return ""

    def close(self) -> None:
        pass


class _GrpcRunControl:
    wired = True   # ADR-108c: there is an orchestrator, so a person can be asked (see _Noop.wired)

    # Transport failures are COUNTED, not just logged. Every call here swallows its error on purpose
    # (telemetry must never break a run), and that deliberate quiet is exactly what let a dead channel
    # look like a healthy one for as long as it did. A caller that needs an ANSWER — the map gate — has
    # to be able to tell "nobody answered" from "nobody could answer": the two look identical from the
    # outside and have different remedies.
    transport_errors = 0

    def __init__(self, addr: str) -> None:
        import grpc
        from .pb import runcontrol_pb2 as pb, runcontrol_pb2_grpc as pbg
        from .grpcaddr import target
        self._pb = pb
        # ORCH_ADDR is a BARE socket path (cmd/orchestrator passes `sock` verbatim), and gRPC reads a
        # bare path as a DNS name. So this channel never connected — and because every call here
        # swallows its error by design ("telemetry must never break the run"), the whole control
        # channel degraded to "continue" in silence: budget reconciliation, the abort signal, and
        # operator takeover/return alike. Found by RUNNING the map gate and watching 137 resolver
        # errors scroll past while the gate waited for an answer that had already been given.
        self._ch = grpc.insecure_channel(target(addr))
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
                log("system.orchestrator_abort_signal", reason=c.reason)
            elif verb == TAKEOVER:
                log("system.orchestrator_takeover_signal", reason=c.reason)
            return verb
        except Exception as e:  # telemetry must never break the run
            self.transport_errors += 1
            log("system.runcontrol_report_error", error=e)
            return CONTINUE

    def poll(self, run_id, node="checkpoint") -> str:
        """M9.8 F4 (ADR-054): a 0-token heartbeat — surfaces a pending takeover/abort at the superstep
        boundary without spending budget (the orchestrator adds 0 to the run's spend)."""
        return self.report(run_id, node, 0, 0, status="running")

    def map_decision(self, run_id) -> str:
        """ADR-108c: the operator's answer to the map gate — "" (not answered yet), "approve", "reject".

        Another 0-token heartbeat, on its own node name so the wait is legible in the orchestrator's log
        as waiting-for-a-person rather than as ordinary progress. A transport error reads as "" — not
        answered — because the alternative is to invent an answer nobody gave, and the caller already
        bounds the wait with a timeout.
        """
        try:
            c = self._stub.ReportEvent(self._pb.RunEvent(
                run_id=run_id, node="map_gate", prompt_tokens=0, completion_tokens=0, status="running"))
            return getattr(c, "map_decision", "") or ""
        except Exception as e:
            self.transport_errors += 1
            log("system.runcontrol_report_error", error=e)
            return ""

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
        log("system.runcontrol_unavailable", error=e)
        return _Noop()
