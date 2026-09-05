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
    lens_text, seat_prompt,
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
    # The caller's earlier turns ride with the question (the audit's A8:
    # the door used to keep only the newest user turn); the system message
    # is the caller's instruction, not replayed speech.
    assert status == 200 and engine.asked == [seat_prompt(body, "what time is it")]
    asked = engine.asked[0]
    assert asked.rstrip().endswith("Wearer: what time is it")
    assert "Wearer: first" in asked and "JOAT: reply" in asked and "ignored" not in asked


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
    chunks = [json.loads(line[len("data: "):]) for line in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    # Streamed as produced (the audit's A8): one SSE event per mind chunk,
    # the role on the first, the words joining back to the whole answer.
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello world."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert len(chunks) >= 3


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


# ── the MCP route: the brain's tools for the CLI seats ───────────────────────

from joat.agents.tools import Tool, ToolBelt  # noqa: E402


async def rpc(port: int, message: Any, token: str | None = "secret") -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, _, body = await http(port, "POST", "/agent/mcp", message, headers)
    return status, (json.loads(body) if body else None)


@pytest.fixture
async def mcp_door() -> AsyncIterator[AgentEndpoint]:
    async def weather(args: dict[str, Any]) -> str:
        return f"72F in {args.get('location', 'here')}"

    async def broken(args: dict[str, Any]) -> str:
        raise RuntimeError("down")

    belt = ToolBelt([
        Tool("get_weather", "Current weather.",
             {"type": "object", "properties": {"location": {"type": "string"}}}, weather),
        Tool("broken", "", {"type": "object"}, broken),
    ])
    endpoint = AgentEndpoint(ScriptedEngine(["x"]), "secret", port=0, tools=belt)
    await endpoint.start()
    try:
        yield endpoint
    finally:
        await endpoint.stop()


@pytest.mark.asyncio
async def test_mcp_initialize_and_notifications(mcp_door: AgentEndpoint) -> None:
    status, reply = await rpc(mcp_door.port, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
    })
    assert status == 200
    assert reply["id"] == 1 and reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"] == {"tools": {}}
    status, reply = await rpc(mcp_door.port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and reply is None
    status, reply = await rpc(mcp_door.port, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert status == 200 and reply["result"] == {}


@pytest.mark.asyncio
async def test_mcp_lists_and_calls_the_belt(mcp_door: AgentEndpoint) -> None:
    _, reply = await rpc(mcp_door.port, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = [t["name"] for t in reply["result"]["tools"]]
    assert names == ["get_weather", "broken"]
    assert reply["result"]["tools"][0]["inputSchema"]["type"] == "object"
    _, reply = await rpc(mcp_door.port, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "get_weather", "arguments": {"location": "Norman"}},
    })
    assert reply["result"] == {"content": [{"type": "text", "text": "72F in Norman"}], "isError": False}
    _, reply = await rpc(mcp_door.port, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "broken"},
    })
    assert reply["result"]["isError"] is True and reply["result"]["content"][0]["text"].startswith("error:")
    _, reply = await rpc(mcp_door.port, {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"},
    })
    assert reply["result"]["isError"] is True


@pytest.mark.asyncio
async def test_mcp_batches_unknown_methods_and_auth(mcp_door: AgentEndpoint) -> None:
    status, reply = await rpc(mcp_door.port, [
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 8, "method": "resources/list"},
    ])
    assert status == 200 and [r["id"] for r in reply] == [7, 8]
    assert reply[1]["error"]["code"] == -32601
    status, _ = await rpc(mcp_door.port, {"jsonrpc": "2.0", "id": 9, "method": "ping"}, token=None)
    assert status == 401
    status, _, _ = await http(mcp_door.port, "GET", "/mcp", headers=AUTH)
    assert status == 405
    status, _, _ = await http(mcp_door.port, "DELETE", "/mcp", headers=AUTH)
    assert status == 200
