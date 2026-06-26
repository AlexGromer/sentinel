"""Sentinel — OpenTelemetry tracing for the brain (M4b, ADR-018).

OTLP export only when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise a no-op tracer (zero overhead).
Robust to OpenTelemetry not being installed. Span attributes carry prompt_HASH, NEVER prompt/page content.
"""
import contextlib
import hashlib
import os

_tracer = None


def prompt_hash(text: str) -> str:
    """A privacy-safe fingerprint of a prompt (for span attributes — never the content itself)."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def setup_tracing(service: str = "sentinel-brain") -> None:
    """Configure an OTLP tracer iff OTEL_EXPORTER_OTLP_ENDPOINT is set; else stay no-op."""
    global _tracer
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("sentinel.brain")
    except Exception:  # otel missing / misconfigured -> stay no-op
        _tracer = None


@contextlib.contextmanager
def span(name: str, **attrs):
    """Start a span (no-op if tracing isn't configured). Attributes must be metadata only — never content."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception:
                    pass
        yield sp


def set_llm_tokens(sp, result) -> None:
    """Record normalized LLM token counts on a span (counts only — never content).

    `result` is an `llm.LLMResult` (prompt_tokens / completion_tokens). Tolerant of a None span
    and of objects missing the fields (records 0)."""
    if sp is None:
        return
    try:
        sp.set_attribute("llm.prompt_tokens", int(getattr(result, "prompt_tokens", 0) or 0))
        sp.set_attribute("llm.completion_tokens", int(getattr(result, "completion_tokens", 0) or 0))
    except Exception:
        pass


def inject_context(carrier: dict) -> dict:
    """Inject the current W3C trace context (`traceparent`) into a carrier dict so a downstream
    process (pw-executor / store-gateway) can continue the trace (M8, ADR-021). No-op when tracing
    isn't configured — the carrier is returned unchanged."""
    if _tracer is None:
        return carrier
    try:
        from opentelemetry.propagate import inject
        inject(carrier)
    except Exception:
        pass
    return carrier


def extract_context(carrier: dict):
    """Extract a W3C trace context from a carrier dict; returns an OpenTelemetry Context (use as the
    parent of a child span) or None when tracing isn't configured / nothing is present."""
    if _tracer is None:
        return None
    try:
        from opentelemetry.propagate import extract
        return extract(carrier or {})
    except Exception:
        return None
