"""Offline gates for ADR-108b — the turn where the model ANSWERS.

Run:  .venv/bin/python tests/test_conversation_offline.py

Before this, every path through the brain assumed the person had already decided what to test. A turn
that carried only words, on a conversation with no objective, was `fatal.chat_no_objective`, exit 3 —
the product whose centre is a chat could not be talked to. Not a missing feature so much as a missing
premise: the state where someone is still deciding did not exist.

What is asserted here is behaviour, not shape:
- a turn with words and no objective produces a REPLY and exits 0;
- with no model configured it still answers, deterministically, saying what it needs;
- talking does NOT pin an objective — the goal belongs to the conversation and is fixed once set
  (ADR-108a), so a passing remark must not become the thing this chat is forever about;
- the exchange is remembered, so the turn that finally states a goal arrives with its context;
- roles survive to a backend that understands them, instead of being flattened into a transcript.
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain import llm  # noqa: E402


class ChatCapableBackend:
    """A backend with complete_chat — i.e. one that understands roles."""
    name, model, supports_vision, supports_structured = "fake", "fake-chat", False, False

    def __init__(self, text="Ask me for a goal and a URL."):
        self.text, self.chat_calls, self.plain_calls = text, [], []

    def complete_chat(self, messages, *, max_tokens, temperature, system=None):
        self.chat_calls.append({"messages": [dict(m) for m in messages], "system": system})
        return llm.LLMResult(self.text, 5, 7, model=self.model)

    def complete(self, prompt, *, max_tokens, temperature):
        self.plain_calls.append(prompt)
        return llm.LLMResult(self.text, 5, 7, model=self.model)


class TextOnlyBackend:
    """A backend WITHOUT complete_chat — the MCP-sampling shape, text in and text out."""
    name, model, supports_vision, supports_structured = "fake", "fake-flat", False, False

    def __init__(self, text="flat"):
        self.text, self.prompts = text, []

    def complete(self, prompt, *, max_tokens, temperature):
        self.prompts.append(prompt)
        return llm.LLMResult(self.text, 3, 4, model=self.model)


def test_converse_keeps_roles_when_the_backend_understands_them():
    b = ChatCapableBackend()
    exchange = [{"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "what can you do?"}]
    r = llm.converse(b, exchange)
    assert r.text == b.text
    assert not b.plain_calls, "a role-aware backend must not be driven through the flattening path"
    assert len(b.chat_calls) == 1
    sent = b.chat_calls[0]
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"], sent["messages"]
    # The system prompt travels SEPARATELY (Anthropic takes it as a parameter, not a message).
    assert sent["system"] and "Sentinel" in sent["system"], sent["system"]


def test_converse_flattens_only_for_a_backend_that_cannot_take_roles():
    b = TextOnlyBackend()
    r = llm.converse(b, [{"role": "user", "content": "hello"},
                         {"role": "assistant", "content": "hi"},
                         {"role": "user", "content": "again"}])
    assert r.text == "flat"
    assert len(b.prompts) == 1
    p = b.prompts[0]
    # Speakers are LABELLED. A flattening that dropped who said what would hand the model a monologue
    # and call it a dialogue — which is exactly why this path is the fallback, not the implementation.
    assert "User: hello" in p and "Assistant: hi" in p and p.rstrip().endswith("Assistant:"), p


def _chat_env(tmp, db, message, conversation_id="conv-talk"):
    return {
        "RUN_ID": "t-conv", "RUN_MODE": "chat", "ARTIFACT_DIR": str(tmp), "TARGET_URL": "",
        "GOAL": "", "DESCRIBE": "", "MESSAGE": message, "CHECKPOINT_DSN": "",
        "SENTINEL_CONVERSATIONS_DB": db, "SENTINEL_CONVERSATION_ID": conversation_id,
        "PW_EXECUTOR_CMD": "false",   # a conversational turn must not need one; `false` proves it
    }


def _run_turn(message, db, conversation_id="conv-talk", backend=None):
    """Drive one chat turn through the real _run_chat, with the model swapped out."""
    from brain import __main__ as m
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved, orig = dict(os.environ), llm.make_backend
    os.environ.update(_chat_env(tmp, db, message, conversation_id))
    llm.make_backend = lambda role: (backend if role == "chat" else None)
    try:
        rc = m._run_chat("t-conv", tmp, conversation_id, "", 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        llm.make_backend = orig
    reply = None
    if (tmp / "reply.json").exists():
        reply = json.loads((tmp / "reply.json").read_text(encoding="utf-8"))
    return rc, reply, tmp


def test_a_turn_with_no_objective_gets_an_answer():
    db = str(pathlib.Path(tempfile.mkdtemp()) / "conv.db")
    b = ChatCapableBackend("I explore an app and freeze a replayable test.")
    rc, reply, _ = _run_turn("what do you do?", db, backend=b)
    assert rc == 0, f"a conversational turn must succeed, got exit {rc}"
    assert reply and reply["reply"] == b.text, reply
    assert reply["kind"] == "conversation", reply
    # Talking is not deciding: the objective belongs to the conversation and is pinned once, so a
    # remark must not become the thing this chat is forever about.
    assert reply["objective_pinned"] is False, reply
    assert len(b.chat_calls) == 1


def test_it_answers_even_with_no_model_configured():
    db = str(pathlib.Path(tempfile.mkdtemp()) / "conv.db")
    rc, reply, _ = _run_turn("hello?", db, backend=None)
    assert rc == 0, f"with no backend the turn must still answer, got exit {rc}"
    assert reply and reply["reply"], reply
    # The honest answer names what it needs, because without a backend there is nothing to converse
    # with — and "objective + target URL" is what the person has to supply either way.
    low = reply["reply"].lower()
    assert "objective" in low and "url" in low, reply["reply"]


def test_a_model_failure_is_answered_not_crashed():
    class Exploding:
        name, model, supports_vision, supports_structured = "fake", "boom", False, False

        def complete_chat(self, messages, **kw):
            raise RuntimeError("endpoint down")

        def complete(self, prompt, **kw):
            raise RuntimeError("endpoint down")

    db = str(pathlib.Path(tempfile.mkdtemp()) / "conv.db")
    rc, reply, _ = _run_turn("are you there?", db, backend=Exploding())
    assert rc == 0, "an unreachable model must not fail the turn"
    assert reply and "could not reach the model" in reply["reply"].lower(), reply


def test_the_exchange_is_remembered_across_turns():
    db = str(pathlib.Path(tempfile.mkdtemp()) / "conv.db")
    b1 = ChatCapableBackend("First answer.")
    rc1, _, _ = _run_turn("first question", db, backend=b1)
    assert rc1 == 0
    b2 = ChatCapableBackend("Second answer.")
    rc2, reply2, _ = _run_turn("second question", db, backend=b2)
    assert rc2 == 0, reply2
    sent = b2.chat_calls[0]["messages"]
    contents = [m["content"] for m in sent]
    # The turn that finally states an objective has to arrive with its context, so the previous
    # exchange — question AND answer — is what the model sees.
    assert "first question" in contents, contents
    assert "First answer." in contents, contents
    assert contents[-1] == "second question", contents


def test_talking_does_not_pin_the_objective():
    """The conversation stays open: a later turn may still set the goal, and it is honoured."""
    db = str(pathlib.Path(tempfile.mkdtemp()) / "conv.db")
    rc, _, _ = _run_turn("just chatting about login flows", db, backend=ChatCapableBackend())
    assert rc == 0
    # Now a turn that DOES carry an objective. It must not be refused as "the goal already changed" —
    # which is what would happen if the chat turn had pinned "just chatting…" as the objective.
    from brain import __main__ as m
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved = dict(os.environ)
    os.environ.update(_chat_env(tmp, db, ""))
    os.environ["GOAL"] = "log in as alice"
    os.environ["TARGET_URL"] = ""          # no target -> the AUTHORING path refuses on target, not goal
    try:
        rc2 = m._run_chat("t-goal", tmp, "conv-talk", "", 0.85, 40)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    # 2 = "no target for authoring" (fatal.chat_no_target). 3 would mean the objective was rejected,
    # i.e. something had been pinned by talking.
    assert rc2 == 2, f"a goal stated after small talk was refused (exit {rc2}) — talking pinned an objective"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"ALL PASS ({len(tests)})")
