"""Sentinel brain — AG-UI event emission (M14, ADR-055; docs/M14_CONTRACT.md §2/§4).

Prints one typed AG-UI envelope per event as a `@@AGUI <json>` stdout line. This is the in-band
transport §2 describes: control-API's WS `/v1/stream` line-reader (main.go, W2) recognizes the
`@@AGUI ` prefix and forwards `data` as a typed server->client event; every other stdout line
passes through as a `log` event. No new transport, no network, no LLM — pure/offline, importable
with zero side effects beyond the print() inside `emit`.

Envelope (frozen, §2): {"type": <event>, "run_id": <id>, "seq": <int>, "ts": <iso8601>, "data": {...}}
"""
import json
import sys
from datetime import datetime, timezone

PREFIX = "@@AGUI "  # the exact prefix the control-API consumer scans stdout lines for (§2)

_seq = 0  # module-level monotonic sequence counter, per-process. NOT threaded through RunState:
          # emission is stdout-only observability and must never affect plan_hash/exit codes/artifacts
          # (see brain/graph.py callers), so there is no need to persist/resume it across checkpoints.


def emit(event_type: str, run_id: str, **data) -> None:
    """Print one `@@AGUI` envelope line for `event_type` with the given `run_id` and `data` fields."""
    global _seq
    _seq += 1
    envelope = {"type": event_type, "run_id": run_id, "seq": _seq,
                "ts": datetime.now(timezone.utc).isoformat(), "data": data}
    print(PREFIX + json.dumps(envelope, separators=(",", ":")), file=sys.stdout, flush=True)
