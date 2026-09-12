"""HTTP/TLS contract checks use actual A2A Task status/parts envelopes.

The servers are controlled adversarial peers. They do not prove live-service
compatibility or rendered HUD behavior; those have separate acceptance runs.
"""
from __future__ import annotations

import asyncio
import ssl
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from joat.agents.a2a_terminal import A2AHttpTransport, A2ATerminalAgent, parse_result
from joat.core.a2a import A2AError, A2AErrorCode as Code, A2ARequestResult
from joat.services.agent_terminal import AgentTerminalRoster


@pytest.fixture(scope="session")
def tls_contexts(tmp_path_factory: pytest.TempPathFactory) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    path = tmp_path_factory.mktemp("a2a-tls")
    key, cert = path / "key.pem", path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


def task(state: str, *, identity: str = "task-1", context: str = "context-1", artifact_text: str = "answer",
         text: str | None = None, status_text: str | None = None) -> dict[str, Any]:
    if text is not None:
        artifact_text = text
    # Actual deployed response contract: status.state, NOT task.state.
    payload: dict[str, Any] = {"id": identity, "contextId": context, "status": {"state": state},
            "artifacts": [{"artifactId": "artifact-1", "parts": [{"kind": "text", "text": artifact_text}]}]}
    if status_text is not None:
        payload["status"]["message"] = {"role": "agent", "parts": [{"kind": "text", "text": status_text}]}
    return payload


class Peer:
    def __init__(self, server: TestServer, client_tls: ssl.SSLContext) -> None:
        self.server = server
        self.tls = client_tls
        self.calls: list[dict[str, Any]] = []
        self.reply: Any = lambda body: task("TASK_STATE_COMPLETED")
        self.reject = False
        self.poll_seen = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.redirect = False
        self.trap_calls = 0
        self.cancel_reply: Any = lambda body: task("TASK_STATE_CANCELED")

    def agent(self, **kwargs: Any) -> A2ATerminalAgent:
        async def token() -> str:
            return "test-peer-secret"
        return A2ATerminalAgent(str(self.server.make_url("/")), token, "Test agent",
                                transport=A2AHttpTransport(timeout_s=kwargs.pop("timeout_s", 2), ssl_context=self.tls),
                                poll_interval_s=.01, **kwargs)


@pytest.fixture
async def peer(tls_contexts: tuple[ssl.SSLContext, ssl.SSLContext]) -> AsyncIterator[Peer]:
    app = web.Application()
    server = TestServer(app, scheme="https")
    fixture = Peer(server, tls_contexts[1])

    async def handle(request: web.Request) -> web.Response:
        if request.path == "/trap":
            fixture.trap_calls += 1
            return web.json_response({})
        body = await request.json()
        assert body["jsonrpc"] == "2.0"
        fixture.calls.append(body)
        if fixture.reject or request.headers.get("Authorization") != "Bearer test-peer-secret":
            return web.Response(status=401)
        method = body["method"]
        if method == "tasks/get" and body["params"]["id"] != "task-1":
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32001, "message": "Task not found"}})
        if method == "message/send":
            message = body["params"]["message"]
            assert message["messageId"] and message["role"] == "user"
            assert message["parts"][0]["kind"] == "text"
            assert "contextId" not in body["params"] and "taskId" not in body["params"]
            if fixture.redirect:
                raise web.HTTPTemporaryRedirect(location="/trap")
        if method == "tasks/get":
            fixture.poll_seen.set()
        if method == "tasks/cancel":
            fixture.cancel_seen.set()
            response = fixture.cancel_reply(body)
        else:
            response = fixture.reply(body)
        if asyncio.iscoroutine(response):
            response = await response
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": response})

    app.router.add_post("/{tail:.*}", handle)
    await server.start_server(ssl=tls_contexts[0])
    try:
        yield fixture
    finally:
        await server.close()


async def collect(agent: A2ATerminalAgent, prompt: str = "Hello") -> str:
    return "".join([text async for text in agent.stream(prompt)])


@pytest.mark.parametrize("bad", [{"id": "x", "state": "TASK_STATE_COMPLETED"}, {},
                                 {"id": "x", "status": {}}, task("alien"),
                                 {"role": "user", "messageId": "m", "parts": []}])
def test_missing_or_wrong_state_never_means_completed(bad: dict[str, Any]) -> None:
    with pytest.raises(A2AError):
        parse_result(bad)


@pytest.mark.parametrize("endpoint", ["http://host/", "https://user:secret@host/", "https://host/?token=x", "https://host/#x", "https://host/a\nb"])
def test_unsafe_destinations_rejected(endpoint: str) -> None:
    async def token() -> str:
        return "test-token"
    with pytest.raises(A2AError):
        A2ATerminalAgent(endpoint, token, "Test")


async def test_actual_completed_shape_and_followup_context(peer: Peer) -> None:
    agent = peer.agent()
    assert await collect(agent) == "answer"
    assert await collect(agent, "Continue") == "answer"
    sends = [r for r in peer.calls if r["method"] == "message/send"]
    first, second = [r["params"]["message"] for r in sends]
    assert "contextId" not in first and "taskId" not in first
    assert second["contextId"] == "context-1" and "taskId" not in second
    assert first["messageId"] != second["messageId"]
    assert (await agent.history())[-1] == ("agent", "answer")


async def test_submitted_task_is_polled_not_marked_done(peer: Peer) -> None:
    peer.reply = lambda body: task("TASK_STATE_SUBMITTED") if body["method"] == "message/send" else task("TASK_STATE_COMPLETED", text="finished")
    agent = peer.agent()
    assert await collect(agent) == "finished"
    assert peer.poll_seen.is_set()
    assert sum(r["method"] == "message/send" for r in peer.calls) == 1


async def test_status_poll_snapshots_are_updates_only_not_answer_chunks(peer: Peer) -> None:
    state_order = ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"]
    next_state = iter(state_order)

    def followup(body: dict[str, Any]) -> dict[str, Any]:
        if body["method"] == "message/send":
            return task(next(next_state), text="submitted")
        return task(next(next_state), text="ignored")

    peer.reply = followup
    agent = peer.agent()
    updates: list[A2ARequestResult] = []

    async def on_update(result: A2ARequestResult) -> None:
        updates.append(result)

    outputs: list[str] = []

    async def collect_updates(generation: int) -> str:
        async for chunk in agent.stream_task_captured("status polling", context_generation=generation, on_update=on_update):
            outputs.append(chunk)
        return "".join(outputs)

    # stream_task_captured requires known generation, so initialize explicitly.
    await agent.initialize()
    generation = agent.capture_context_generation
    assert generation is not None
    output = await collect_updates(generation)
    assert output == "ignored"
    assert [result.state for result in updates] == ["submitted", "working", "completed"]


async def test_progress_updates_include_task_context_and_dedupe(peer: Peer) -> None:
    sequence: list[dict[str, Any]] = [
        task("TASK_STATE_SUBMITTED", text="queued"),
        task("TASK_STATE_WORKING", text="spin"),
        task("TASK_STATE_WORKING", text="spin"),
        task("TASK_STATE_COMPLETED", text="answer"),
    ]
    peer.reply = lambda body: sequence.pop(0) if body["method"] in {"message/send", "tasks/get"} else task("TASK_STATE_COMPLETED")
    agent = peer.agent()
    updates: list[tuple[str, str]] = []

    async def on_update(result: A2ARequestResult) -> None:
        updates.append((result.state, result.task_id or ""))

    async def collect_updates(generation: int) -> str:
        async for text in agent.stream_task_captured("Hello with progress", context_generation=generation, on_update=on_update):
            return text
        return ""

    await agent.initialize()
    output = await collect_updates(agent.capture_context_generation or 1)
    assert output == "answer"
    # submitted/working/completed progression captured once per distinct update.
    assert updates == [("submitted", "task-1"), ("working", "task-1"), ("completed", "task-1")]


async def test_input_required_prefers_status_text_over_artifacts(peer: Peer) -> None:
    peer.reply = lambda body: task(
        "TASK_STATE_INPUT_REQUIRED",
        artifact_text="artifact should be ignored",
        status_text="What city are you in?",
    )
    agent = peer.agent()
    with pytest.raises(A2AError) as exc:
        await collect(agent)
    assert exc.value.code == Code.INPUT_REQUIRED
    assert exc.value.text == "What city are you in?"


async def test_failed_task_does_not_publish_done(peer: Peer) -> None:
    peer.reply = lambda _: task("TASK_STATE_FAILED", text="private failure context")
    roster = AgentTerminalRoster([("test", peer.agent())])
    assert await roster.prepare_lane("test", is_current=lambda: True)
    await roster.send("Hello")
    lane = roster.lane("test")
    assert lane is not None and lane.task is not None
    await lane.task
    assert lane.last_error and not any("done." in row for row in lane.tail)
    assert "private failure context" not in lane.last_error


async def test_input_required_preserves_task_and_shows_question(peer: Peer) -> None:
    sends = 0
    def reply(body: dict[str, Any]) -> dict[str, Any]:
        nonlocal sends
        sends += 1
        if sends == 1:
            result = task("TASK_STATE_INPUT_REQUIRED", artifact_text="", status_text="Which city?")
            return result
        assert body["params"]["message"]["taskId"] == "task-1"
        assert body["params"]["message"]["contextId"] == "context-1"
        return task("TASK_STATE_COMPLETED", text="City received")
    peer.reply = reply
    agent = peer.agent()
    roster = AgentTerminalRoster([("test", agent)])
    assert await roster.prepare_lane("test", is_current=lambda: True)
    await roster.send("Weather")
    lane = roster.lane("test")
    assert lane is not None and lane.task is not None
    await lane.task
    assert "Which city?" in list(lane.tail)
    assert not any("done." in row for row in lane.tail)
    assert lane.state == "needs input"
    assert await collect(agent, "Chicago") == "City received"


async def test_cancel_running_task_requires_real_task_receipt(peer: Peer) -> None:
    peer.reply = lambda _: task("TASK_STATE_WORKING")
    agent = peer.agent()
    roster = AgentTerminalRoster([("test", agent)])
    await roster.prepare_lane("test", is_current=lambda: True)
    await roster.send("Work")
    await asyncio.wait_for(peer.poll_seen.wait(), 2)
    sentence = await roster.cancel()
    assert peer.cancel_seen.is_set()
    assert "confirmed by the agent" in sentence
    assert agent.cancellation_status == "confirmed"


async def test_cancel_boolean_ack_is_not_remote_stop(peer: Peer) -> None:
    peer.reply = lambda _: task("TASK_STATE_WORKING")
    peer.cancel_reply = lambda _: {"ok": True}
    agent = peer.agent()
    roster = AgentTerminalRoster([("test", agent)])
    await roster.prepare_lane("test", is_current=lambda: True)
    await roster.send("Work")
    await asyncio.wait_for(peer.poll_seen.wait(), 2)
    assert "unconfirmed" in await roster.cancel()
    assert agent.capture_context_generation is None


async def test_uncertain_send_cannot_be_replayed(peer: Peer) -> None:
    async def slow(_: object) -> dict[str, Any]:
        await asyncio.sleep(.1)
        return task("TASK_STATE_COMPLETED")
    peer.reply = slow
    agent = peer.agent(timeout_s=.03)
    await agent.initialize()
    with pytest.raises(A2AError) as caught:
        await collect(agent)
    assert caught.value.code == Code.SUBMISSION_UNCERTAIN
    with pytest.raises(A2AError):
        await collect(agent)
    assert sum(r["method"] == "message/send" for r in peer.calls) == 1


async def test_redirect_never_receives_token(peer: Peer) -> None:
    agent = peer.agent()
    await agent.initialize()
    peer.redirect = True
    with pytest.raises(A2AError):
        await collect(agent)
    assert peer.trap_calls == 0


async def test_unauthorized_and_missing_credentials_are_actionable(peer: Peer) -> None:
    peer.reject = True
    states: list[tuple[str, str]] = []
    agent = peer.agent(state_changed=lambda state, reason: states.append((state, reason)))
    with pytest.raises(A2AError) as caught:
        await agent.initialize()
    assert caught.value.code == Code.AUTHENTICATION_FAILED
    assert states[-1] == ("auth_required", "authentication_failed")


async def test_wrong_task_after_poll_blocks_further_send(peer: Peer) -> None:
    peer.reply = lambda body: task("TASK_STATE_WORKING") if body["method"] == "message/send" else task("TASK_STATE_COMPLETED", identity="someone-elses-task")
    agent = peer.agent()
    with pytest.raises(A2AError):
        await collect(agent)
    assert agent.capture_context_generation is None


async def test_stateless_agent_message_is_completed(peer: Peer) -> None:
    peer.reply = lambda _: {"kind": "message", "role": "agent", "messageId": "reply-1", "parts": [{"kind": "text", "text": "Direct reply"}]}
    assert await collect(peer.agent()) == "Direct reply"

async def test_follow_up_supplies_history_to_stateless_peer_and_reset_clears_it(peer: Peer) -> None:
    # This peer retains no per-context memory: a usable follow-up must carry
    # the earlier user detail and answer even when contextId is echoed.
    def reply(body: dict[str, Any]) -> dict[str, Any]:
        text = body['params']['message']['parts'][0]['text']
        if text == 'Remember cobalt lantern':
            return task('TASK_STATE_COMPLETED', text='Remembered')
        if text.endswith('Current user instruction:\nWhat phrase?'):
            assert 'Remember cobalt lantern' in text and 'Remembered' in text
            return task('TASK_STATE_COMPLETED', text='cobalt lantern')
        assert text == 'Fresh conversation'
        assert 'cobalt lantern' not in text
        return task('TASK_STATE_COMPLETED', text='Fresh')
    peer.reply = reply
    agent = peer.agent()
    assert await collect(agent, 'Remember cobalt lantern') == 'Remembered'
    assert await collect(agent, 'What phrase?') == 'cobalt lantern'
    await agent.reset_conversation()
    assert await collect(agent, 'Fresh conversation') == 'Fresh'

async def test_reopening_uncertain_lane_recovers_owned_task_without_resubmitting(peer: Peer) -> None:
    def first(body: dict[str, Any]) -> dict[str, Any]:
        if body['method'] == 'message/send':
            return task('TASK_STATE_SUBMITTED')
        raise web.HTTPServiceUnavailable()
    peer.reply = first
    agent = peer.agent()
    roster = AgentTerminalRoster([('test', agent)])
    assert await roster.prepare_lane('test', is_current=lambda: True)
    await roster.send('One instruction')
    lane = roster.lane('test')
    assert lane and lane.task
    await lane.task
    assert agent.needs_recovery
    peer.reply = lambda _: task('TASK_STATE_COMPLETED', text='Recovered answer')
    assert await roster.prepare_lane('test', is_current=lambda: True)
    assert not agent.needs_recovery
    assert 'Recovered answer' in roster.tail('test', 40)
    assert sum(call['method'] == 'message/send' for call in peer.calls) == 1


async def test_send_clears_stale_last_outcome_before_new_submission(peer: Peer) -> None:
    running = asyncio.Event()
    states: list[str] = []
    def state_changed(state: str, _reason: str) -> None:
        if state == "running":
            states.append(state)
            if len(states) == 1:
                running.set()

    agent = peer.agent(state_changed=state_changed)
    # Seed stale terminal outcome from prior turn.
    assert await collect(agent) == "answer"
    assert agent.last_outcome is not None
    running.clear()
    states.clear()

    # Keep response intentionally slow so the running state is observed before
    # the new task has any final output to publish.
    async def delayed_send(_: object) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return task("TASK_STATE_COMPLETED", text="second")

    peer.reply = delayed_send

    async def pending_submit() -> str:
        return await collect(agent, "New command")

    send_task = asyncio.create_task(pending_submit())
    await asyncio.wait_for(running.wait(), 1)
    assert states == ["running"]
    assert agent.last_outcome is None
    assert await asyncio.wait_for(send_task, 1) == "second"


async def test_recover_shows_input_required_question_from_status_text(peer: Peer) -> None:
    agent = peer.agent()
    assert await collect(agent) == "answer"
    peer.reply = lambda _: task("TASK_STATE_INPUT_REQUIRED", artifact_text="", status_text="Which city are you in?")
    result = await agent.recover()
    assert result == "Which city are you in?"
    assert ("agent", "Which city are you in?") in await agent.history()
