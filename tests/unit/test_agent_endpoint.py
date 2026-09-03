"""The Even AI door: bearer token, OpenAI shape, lens-sized plain text.

These tests speak raw HTTP over a real loopback socket, because the peer is
the Even app's Dart client and the parser is ours — a fake request object
would prove nothing about the bytes on the wire.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from joat.core.interfaces import ReplyEngine
from joat.services.agent_endpoint import (
    EMPTY_REPLY,
    AgentEndpoint,
    agent_token,
    last_user_text,
    lens_text,
)


class ScriptedEngine(ReplyEngine):
    def __init__(self, chunks: list[str], delay_s: float = 0.0) -> None:
        self.chunks = chunks
        self.delay_s = delay_s
        self.asked: list[str] = []

    async def reply(self, text: str) -> str | None:
        return "".join(self.chunks) or None

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.asked.append(text)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        for chunk in self.chunks:
            yield chunk


async def http(
    port: int, method: str, path: str, body: Any = None,
    headers: dict[str, str] | None = None, raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else b""
    )
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost",
             f"Content-Length: {len(payload)}"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, response_body = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.split(b"\r\n")
    status = int(status_line.split(b" ")[1])
    response_headers: dict[str, str] = {}
    for line in header_lines:
        name, _, value = line.decode().partition(":")
        response_headers[name.strip().lower()] = value.strip()
    return status, response_headers, response_body


AUTH = {"Authorization": "Bearer secret"}


def ask(text: str, **extra: Any) -> dict[str, Any]:
    return {"model": "openclaw", "messages": [{"role": "user", "content": text}], **extra}


@pytest.fixture
async def door() -> AsyncIterator[tuple[AgentEndpoint, ScriptedEngine]]:
    engine = ScriptedEngine(["Hello", " world."])
    endpoint = AgentEndpoint(engine, "secret", port=0, model_label=lambda: "claude-opus-5")
    await endpoint.start()
    try:
        yield endpoint, engine
    finally:
        await endpoint.stop()


@pytest.mark.asyncio
async def test_a_missing_or_wrong_token_is_refused(door: tuple[AgentEndpoint, ScriptedEngine]) -> None:
    endpoint, engine = door
    status, _, body = await http(endpoint.port, "POST", "/v1/chat/completions", ask("hi"))
    assert status == 401 and json.loads(body)["error"]["type"] == "authentication_error"
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions", ask("hi"),
                              {"Authorization": "Bearer wrong"})
    assert status == 401
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions", ask("hi"),
                              {"Authorization": "Basic secret"})
    assert status == 401
    assert engine.asked == [], "an unauthenticated request never reaches the mind"


@pytest.mark.asyncio
async def test_a_chat_completion_comes_back_in_the_openai_shape(
    door: tuple[AgentEndpoint, ScriptedEngine],
) -> None:
    endpoint, engine = door
    status, headers, body = await http(
        endpoint.port, "POST", "/v1/chat/completions", ask("tell me a joke"), AUTH,
    )
    assert status == 200 and headers["content-type"].startswith("application/json")
    payload = json.loads(body)
    assert payload["object"] == "chat.completion" and payload["model"] == "claude-opus-5"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hello world."}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["id"].startswith("chatcmpl-")
    assert engine.asked == ["tell me a joke"]


@pytest.mark.asyncio
async def test_the_root_path_and_a_mounted_prefix_both_reach_the_mind(
    door: tuple[AgentEndpoint, ScriptedEngine],
) -> None:
    endpoint, engine = door
    for path in ("/", "/agent/v1/chat/completions", "/agent"):
        status, _, _ = await http(endpoint.port, "POST", path, ask("hi"), AUTH)
        assert status == 200, path
    assert len(engine.asked) == 3


@pytest.mark.asyncio
async def test_the_newest_user_turn_is_what_gets_asked(
    door: tuple[AgentEndpoint, ScriptedEngine],
) -> None:
    endpoint, engine = door
    body = {"messages": [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "what time"},
                                     {"type": "text", "text": "is it"}]},
    ]}
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions", body, AUTH)
    assert status == 200 and engine.asked == ["what time is it"]


@pytest.mark.asyncio
async def test_a_request_with_no_user_turn_or_bad_json_is_a_400(
    door: tuple[AgentEndpoint, ScriptedEngine],
) -> None:
    endpoint, _ = door
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions",
                              {"messages": [{"role": "system", "content": "x"}]}, AUTH)
    assert status == 400
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions",
                              headers=AUTH, raw_body=b"{not json")
    assert status == 400
    status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions",
                              headers=AUTH, raw_body=b"[]")
    assert status == 400


@pytest.mark.asyncio
async def test_streaming_requests_get_server_sent_events(
    door: tuple[AgentEndpoint, ScriptedEngine],
) -> None:
    endpoint, _ = door
    status, headers, body = await http(
        endpoint.port, "POST", "/v1/chat/completions", ask("hi", stream=True), AUTH,
    )
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    events = [line for line in body.decode().split("\n\n") if line.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    first = json.loads(events[0][len("data: "):])
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["content"] == "Hello world."


@pytest.mark.asyncio
async def test_health_and_unknown_paths(door: tuple[AgentEndpoint, ScriptedEngine]) -> None:
    endpoint, _ = door
    status, _, body = await http(endpoint.port, "GET", "/agent/health")
    assert status == 200 and json.loads(body) == {"ok": True, "model": "claude-opus-5"}
    status, _, _ = await http(endpoint.port, "GET", "/nothing/here")
    assert status == 404
    status, _, _ = await http(endpoint.port, "GET", "/v1/chat/completions", headers=AUTH)
    assert status == 405


@pytest.mark.asyncio
async def test_an_oversized_body_is_rejected_before_it_is_read() -> None:
    endpoint = AgentEndpoint(ScriptedEngine(["x"]), "secret", port=0, max_body=64)
    await endpoint.start()
    try:
        status, _, _ = await http(endpoint.port, "POST", "/v1/chat/completions",
                                  ask("a" * 200), AUTH)
        assert status == 413
    finally:
        await endpoint.stop()


@pytest.mark.asyncio
async def test_a_silent_mind_still_gets_words_on_the_lens_and_markdown_is_stripped() -> None:
    engine = ScriptedEngine([])
    endpoint = AgentEndpoint(engine, "secret", port=0)
    await endpoint.start()
    try:
        _, _, body = await http(endpoint.port, "POST", "/", ask("hi"), AUTH)
        assert json.loads(body)["choices"][0]["message"]["content"] == EMPTY_REPLY
        engine.chunks = ["**Bold** and `code`\n\n- a list item"]
        _, _, body = await http(endpoint.port, "POST", "/", ask("hi"), AUTH)
        assert json.loads(body)["choices"][0]["message"]["content"] == "Bold and code a list item"
    finally:
        await endpoint.stop()


@pytest.mark.asyncio
async def test_a_mind_that_takes_too_long_is_a_gateway_timeout() -> None:
    endpoint = AgentEndpoint(ScriptedEngine(["late"], delay_s=0.5), "secret", port=0,
                             request_timeout_s=0.05)
    await endpoint.start()
    try:
        status, _, body = await http(endpoint.port, "POST", "/", ask("hi"), AUTH)
        assert status == 504 and json.loads(body)["error"]["type"] == "timeout"
    finally:
        await endpoint.stop()


def test_lens_text_strips_markup_and_cuts_at_a_sentence() -> None:
    assert lens_text("# Title\n\n1. one\n2. two\n[link](http://x) *em* _it_") == "Title one two link em it"
    long = "First sentence here. " * 40
    cut = lens_text(long, max_chars=100)
    assert len(cut) <= 100 and cut.endswith("here.")
    words = "word " * 50
    cut = lens_text(words, max_chars=40)
    assert len(cut) <= 41 and cut.endswith("…")
    assert lens_text("```py\nprint(1)\n```\nplain") == "plain"
    assert lens_text("snake_case_name stays") == "snake_case_name stays"


def test_last_user_text_handles_strings_parts_and_absence() -> None:
    assert last_user_text({"messages": [{"role": "user", "content": " hi "}]}) == "hi"
    assert last_user_text({"messages": [{"role": "user", "content": ""}]}) is None
    assert last_user_text({"messages": [{"role": "assistant", "content": "x"}]}) is None
    assert last_user_text({"messages": "nope"}) is None
    assert last_user_text({}) is None
    assert last_user_text({"messages": [{"role": "user", "content": 5}]}) is None


def test_the_token_comes_from_the_environment_or_a_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "agent-token"
    monkeypatch.delenv("JOAT_AGENT_TOKEN", raising=False)
    created = agent_token(path)
    assert len(created) >= 24 and path.read_text().strip() == created
    assert (path.stat().st_mode & 0o777) == 0o600
    assert agent_token(path) == created, "the file is read back, never regenerated"
    monkeypatch.setenv("JOAT_AGENT_TOKEN", "from-env")
    assert agent_token(path) == "from-env"


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(ValueError):
        AgentEndpoint(ScriptedEngine([]), "")
