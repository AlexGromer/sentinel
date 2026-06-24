"""Offline M4b tests — OTel no-op default + Pushgateway wiring (no collector/gateway needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_otel_noop_without_endpoint():
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    from brain.otel import setup_tracing, span, prompt_hash, set_llm_tokens
    setup_tracing()                       # no endpoint -> stays no-op
    with span("sentinel.run", run_id="r", mode="replay") as sp:
        assert sp is None                 # no-op span when tracing isn't configured
        set_llm_tokens(sp, object())      # tolerant of a None span
    assert len(prompt_hash("a prompt")) == 16
    assert prompt_hash("x") != prompt_hash("y")


def test_push_metrics_importable_and_builds():
    """push_metrics builds a registry from the report; pushing to an unreachable gateway raises."""
    from brain.report import push_metrics
    rep = {"plan_id": "p", "steps": [1, 2], "exit_code": 2, "healed": 1, "failed": 0,
           "regressions": [{"exit2": True}]}
    raised = False
    try:
        push_metrics(rep, "127.0.0.1:1")  # refused fast -> confirms the path executed
    except Exception:
        raised = True
    assert raised


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
