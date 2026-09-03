"""The mind on Claude: streams, remembers, and hands tool calls to the belt.

Nothing here touches the network. The client is a scripted double whose
shape is the SDK's streaming surface (``messages.stream`` as an async
context manager that iterates events and answers ``get_final_message``), so
the loop is exercised the way the real SDK drives it.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from joat.agents.claude_reply_engine import (
    EXHAUSTED_NOTICE,
    FAILED_NOTICE,
    PERSONA,
    REFUSAL_NOTICE,
    UNAVAILABLE_NOTICE,
    ClaudeReplyEngine,
    load_api_key,
)
from joat.agents.grounding import Grounding, static_fact
from joat.agents.tools import Tool, ToolBelt
from joat.core.interfaces import ReplyEngine


def text_event(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text),
    )


def final(stop: str, *blocks: Any) -> Any:
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


def text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def tool_block(id_: str, name: str, input_: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


class FakeStream:
    def __init__(self, events: list[Any], result: Any) -> None:
        self._events = events
        self._result = result
        self._it = iter(())

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> FakeStream:
        self._it = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def get_final_message(self) -> Any:
        return self._result


class FakeMessages:
    def __init__(self, turns: list[tuple[list[Any], Any]]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def stream(self, **request: Any) -> FakeStream:
        self.requests.append(request)
        if not self.turns:
            raise RuntimeError("no scripted turn left")
        events, result = self.turns.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeStream(events, result)


class FakeClient:
    def __init__(self, turns: list[tuple[list[Any], Any]]) -> None:
        self.messages = FakeMessages(turns)
        self.retrieved: list[str] = []
        self.models = SimpleNamespace(retrieve=self._retrieve)

    async def _retrieve(self, model: str) -> Any:
        self.retrieved.append(model)
        return SimpleNamespace(id=model)

    def with_options(self, **_: Any) -> FakeClient:
        return self


class LocalEngine(ReplyEngine):
    """Stands in for the resident MLX fallback."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.asked: list[str] = []

    async def reply(self, text: str) -> str | None:
        return "".join(self.chunks) or None

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.asked.append(text)
        for chunk in self.chunks:
            yield chunk


def make_engine(
    turns: list[tuple[list[Any], Any]], **kwargs: Any,
) -> tuple[ClaudeReplyEngine, FakeClient]:
    client = FakeClient(turns)
    return ClaudeReplyEngine(client=client, **kwargs), client


async def drain(engine: ReplyEngine, text: str) -> list[str]:
    return [chunk async for chunk in engine.stream(text)]


JOKE = "Why did the scarecrow win an award? Because he was outstanding in his field."


@pytest.mark.asyncio
async def test_a_plain_answer_streams_a_short_first_chunk_then_the_rest() -> None:
    engine, _ = make_engine([
        ([text_event("Why did the "), text_event("scarecrow win an award? "),
          text_event("Because he was outstanding in his field.")],
         final("end_turn", text_block(JOKE))),
    ])
    chunks = await drain(engine, "tell me a joke")
    assert "".join(chunks) == JOKE
    assert len(chunks[0]) == 8, "the first chunk lands early so the HUD moves"
    assert all(len(c) <= 64 for c in chunks)
    assert await engine.reply("") is None


@pytest.mark.asyncio
async def test_the_previous_exchange_is_sent_on_the_next_turn() -> None:
    engine, client = make_engine([
        ([text_event(JOKE)], final("end_turn", text_block(JOKE))),
        ([text_event("Another one.")], final("end_turn", text_block("Another one."))),
    ])
    await drain(engine, "tell me a joke")
    await drain(engine, "another")
    sent = client.messages.requests[1]["messages"]
    assert sent == [
        {"role": "user", "content": "tell me a joke"},
        {"role": "assistant", "content": JOKE},
        {"role": "user", "content": "another"},
    ]


@pytest.mark.asyncio
async def test_working_memory_expires_and_is_bounded() -> None:
    engine, client = make_engine([
        ([text_event("ok")], final("end_turn", text_block("ok"))),
    ])
    stale = time.monotonic() - engine.HISTORY_TTL_S - 1
    engine._history.append((stale, "old question", "old answer"))
    for i in range(engine.HISTORY_EXCHANGES + 3):
        engine.remember(f"q{i}", f"a{i}")
    await drain(engine, "now")
    sent = client.messages.requests[0]["messages"]
    assert {"role": "user", "content": "old question"} not in sent
    assert len(sent) == engine.HISTORY_EXCHANGES * 2 + 1
    assert sent[0] == {"role": "user", "content": "q3"}


@pytest.mark.asyncio
async def test_a_tool_call_runs_through_the_belt_and_the_answer_follows() -> None:
    seen: list[dict[str, Any]] = []

    async def weather(args: dict[str, Any]) -> str:
        seen.append(args)
        return "72F and sunny"

    belt = ToolBelt([Tool(
        "get_weather", "Current weather.",
        {"type": "object", "properties": {"location": {"type": "string"}}}, weather,
    )])
    engine, client = make_engine([
        ([], final("tool_use", text_block("Checking."),
                   tool_block("tu_1", "get_weather", {"location": "Norman"}))),
        ([text_event("It is 72F and sunny.")],
         final("end_turn", text_block("It is 72F and sunny."))),
    ], tools=belt)

    chunks = await drain(engine, "JOAT what's the weather")

    assert "".join(chunks) == "It is 72F and sunny."
    assert seen == [{"location": "Norman"}]
    second = client.messages.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1] == {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "tu_1", "content": "72F and sunny",
    }]}
    assert client.messages.requests[0]["tools"][0]["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_as_an_error_result_not_a_crash() -> None:
    async def broken(args: dict[str, Any]) -> str:
        raise RuntimeError("provider down")

    belt = ToolBelt([Tool("get_weather", "", {"type": "object"}, broken)])
    engine, client = make_engine([
        ([], final("tool_use", tool_block("tu_9", "get_weather", {}))),
        ([text_event("I couldn't reach the weather source.")],
         final("end_turn", text_block("I couldn't reach the weather source."))),
    ], tools=belt)

    chunks = await drain(engine, "weather?")

    result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and result["content"].startswith("error:")
    assert "".join(chunks) == "I couldn't reach the weather source."


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_an_error_result_without_a_belt() -> None:
    engine, client = make_engine([
        ([], final("tool_use", tool_block("tu_1", "nope", {}))),
        ([text_event("done")], final("end_turn", text_block("done"))),
    ])
    await drain(engine, "x")
    result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


@pytest.mark.asyncio
async def test_tool_rounds_are_capped_so_a_looping_model_cannot_spin_forever() -> None:
    async def probe(args: dict[str, Any]) -> str:
        return "again"

    belt = ToolBelt([Tool("probe", "", {"type": "object"}, probe)])
    turns: list[tuple[list[Any], Any]] = [
        ([], final("tool_use", tool_block(f"tu_{i}", "probe", {}))) for i in range(12)
    ]
    engine, client = make_engine(turns, tools=belt)
    chunks = await drain(engine, "loop")
    assert chunks == [EXHAUSTED_NOTICE]
    assert len(client.messages.requests) == 7  # the first call plus six tool rounds


@pytest.mark.asyncio
async def test_a_refusal_with_no_text_yields_a_notice() -> None:
    engine, _ = make_engine([([], final("refusal"))])
    assert await drain(engine, "x") == [REFUSAL_NOTICE]


@pytest.mark.asyncio
async def test_an_api_failure_hands_the_turn_to_the_fallback_engine() -> None:
    local = LocalEngine(["local ", "answer"])
    engine, _ = make_engine([([], RuntimeError("boom"))], fallback=local)
    assert await drain(engine, "hi") == ["local ", "answer"]
    assert local.asked == ["hi"]


@pytest.mark.asyncio
async def test_an_api_failure_without_a_fallback_says_so() -> None:
    engine, _ = make_engine([([], RuntimeError("boom"))])
    assert await drain(engine, "hi") == [FAILED_NOTICE]


@pytest.mark.asyncio
async def test_a_failure_after_text_started_keeps_what_was_said() -> None:
    class Dying(FakeStream):
        async def get_final_message(self) -> Any:
            raise RuntimeError("cut off")

    engine, client = make_engine([])

    def stream(**request: Any) -> FakeStream:
        client.messages.requests.append(request)
        return Dying([text_event("Half an ")], None)

    client.messages.stream = stream  # type: ignore[method-assign]
    chunks = await drain(engine, "hi")
    assert "".join(chunks) == "Half an "


@pytest.mark.asyncio
async def test_missing_credentials_yield_the_unavailable_notice_or_the_fallback() -> None:
    engine = ClaudeReplyEngine(client=None)
    engine._client_failed = True
    assert await drain(engine, "hi") == [UNAVAILABLE_NOTICE]

    local = LocalEngine(["offline reply"])
    engine = ClaudeReplyEngine(client=None, fallback=local)
    engine._client_failed = True
    assert await drain(engine, "hi") == ["offline reply"]


@pytest.mark.asyncio
async def test_the_request_carries_persona_cache_marker_facts_and_effort() -> None:
    grounding = Grounding(sources=[static_fact("The wearer's name is Sam")],
                          unknowns=["their calendar"])
    engine, client = make_engine([
        ([text_event("hi")], final("end_turn", text_block("hi"))),
    ], grounding=grounding)
    await drain(engine, "hello")
    request = client.messages.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["output_config"] == {"effort": "low"}
    system = request["system"]
    assert system[0]["text"] == PERSONA
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "The wearer's name is Sam" in system[1]["text"]
    assert "their calendar" in system[1]["text"]
    assert "tools" not in request


@pytest.mark.asyncio
async def test_the_model_is_read_per_turn_and_haiku_gets_no_effort_knob() -> None:
    chosen: list[str | None] = ["claude-haiku-4-5"]
    engine, client = make_engine([
        ([text_event("a")], final("end_turn", text_block("a"))),
        ([text_event("b")], final("end_turn", text_block("b"))),
    ], model=lambda: chosen[0])
    await drain(engine, "one")
    chosen[0] = None
    await drain(engine, "two")
    first, second = client.messages.requests
    assert first["model"] == "claude-haiku-4-5" and "output_config" not in first
    assert second["model"] == "claude-opus-5" and second["output_config"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_complete_is_a_bare_task_call_without_persona_or_tools() -> None:
    belt = ToolBelt([Tool("probe", "", {"type": "object"}, LocalEngine([]).reply)])  # type: ignore[arg-type]
    engine, client = make_engine([
        ([], final("end_turn", text_block("Three bullet points."))),
    ], tools=belt)
    result = await engine.complete(system="Summarise.", text="a long transcript")
    assert result == "Three bullet points."
    request = client.messages.requests[0]
    assert request["system"] == "Summarise." and "tools" not in request
    assert await engine.complete(system="", text="x") is None


@pytest.mark.asyncio
async def test_warm_probes_the_model_and_reports_ready() -> None:
    ready: list[bool] = []
    engine, client = make_engine([], on_ready=lambda: ready.append(True))
    assert engine.ready is False
    assert await engine.warm() is True
    assert client.retrieved == ["claude-opus-5"] and ready == [True] and engine.ready


def test_the_belt_exposes_anthropic_tool_definitions() -> None:
    async def run(args: dict[str, Any]) -> str:
        return ""

    belt = ToolBelt([
        Tool("get_weather", "Weather now.", {"type": "object", "properties": {}}, run),
        Tool("bare", "", {"properties": {"q": {"type": "string"}}}, run),
    ])
    defs = belt.anthropic_tools()
    assert defs[0] == {
        "name": "get_weather", "description": "Weather now.",
        "input_schema": {"type": "object", "properties": {}},
    }
    assert defs[1]["input_schema"]["type"] == "object"
    assert defs[1]["description"] == "bare"


def test_the_api_key_comes_from_the_environment_or_the_support_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = tmp_path / "anthropic-api-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert load_api_key(key_file) is None
    key_file.write_text("sk-ant-from-file\n")
    assert load_api_key(key_file) == "sk-ant-from-file"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert load_api_key(key_file) == "sk-ant-from-env"


def test_chunk_sizes_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ClaudeReplyEngine(chunk_chars=0)
    with pytest.raises(ValueError):
        ClaudeReplyEngine(max_tokens=0)


class NoCredentialsMessages:
    """The real SDK's behaviour without a key, observed live 2026-09-03: the
    client constructs, and every request raises a bare TypeError."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, **request: Any) -> FakeStream:
        self.calls += 1
        raise TypeError(
            "Could not resolve authentication method. Expected one of api_key, "
            "auth_token, or credentials to be set."
        )


@pytest.mark.asyncio
async def test_missing_credentials_are_a_configuration_state_not_a_retry_loop() -> None:
    client = FakeClient([])
    messages = NoCredentialsMessages()
    client.messages = messages  # type: ignore[assignment]
    local = LocalEngine(["offline reply"])
    engine = ClaudeReplyEngine(client=client, fallback=local)

    assert await drain(engine, "one") == ["offline reply"]
    assert await drain(engine, "two") == ["offline reply"]
    assert messages.calls == 1, "after the first refusal the client is not dialled again"
    assert engine.ready is False


@pytest.mark.asyncio
async def test_a_key_written_after_boot_is_picked_up_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    constructed: list[str | None] = []

    class RecordingClient:
        def __init__(self, api_key: str | None = None) -> None:
            constructed.append(api_key)
            self.messages = FakeMessages([
                ([text_event("hello")], final("end_turn", text_block("hello"))),
            ])

    monkeypatch.setattr(anthropic, "AsyncAnthropic", RecordingClient)
    keys: list[str | None] = [None]
    monkeypatch.setattr(
        "joat.agents.claude_reply_engine.load_api_key", lambda *a, **k: keys[0],
    )
    engine = ClaudeReplyEngine(client=None)
    engine._client_failed = True
    engine._key_tried = None

    assert await drain(engine, "hi") == [UNAVAILABLE_NOTICE]
    assert constructed == [], "no key, nothing changed: the client is not rebuilt"
    keys[0] = "sk-ant-new"
    assert await drain(engine, "hi") == ["hello"]
    assert constructed == ["sk-ant-new"]
