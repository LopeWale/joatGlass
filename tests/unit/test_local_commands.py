"""Local child-pipe acknowledgements, identity fences, and owner lifetime."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

import pytest

from joat.services import local_commands as commands
from joat.services.local_commands import Capability, CommandResult, LocalCommandDispatcher


READY = Capability(configured=True, available=True, disconnected=False)


class Harness:
    def __init__(self, owner=None, capability=READY):
        self.messages = []
        self.calls = 0
        self.parent = 1234
        self.capability = capability

        async def succeed():
            self.calls += 1
            return CommandResult("succeeded", "completed")

        self.dispatcher = LocalCommandDispatcher(
            epoch=str(uuid4()), parent_pid=self.parent, engine_pid=4321,
            owners={action: owner or succeed for action in commands.ACTIONS},
            projection=lambda: {action: self.capability for action in commands.ACTIONS},
            emit=self.messages.append, current_parent=lambda: self.parent,
        )

    def request(self, action="news.refresh", **changes):
        return {**self.dispatcher.identity, "kind": "request", "action": action,
                "request_id": str(uuid4()), "timeout_ms": 1000, **changes}

    async def settle(self):
        for _ in range(6):
            await asyncio.sleep(0)


@pytest.mark.parametrize("action", sorted(commands.ACTIONS))
async def test_success_requires_owner_result_and_clears_pending(action):
    h = Harness()
    request = h.request(action)
    h.dispatcher.receive(request)
    assert h.calls == 0
    assert h.messages[-1]["outcome"] == "pending"
    assert h.messages[-1]["capabilities"][action]["pending"] is True
    await h.settle()
    assert h.calls == 1
    assert [(m["outcome"], m["request_id"]) for m in h.messages] == [
        ("pending", request["request_id"]), ("succeeded", request["request_id"]),
    ]
    assert h.messages[-1]["capabilities"][action]["pending"] is False


@pytest.mark.parametrize("capability,reason", [
    (Capability(), "not_configured"),
    (Capability(configured=True, denied=True), "denied"),
    (Capability(configured=True), "busy"),
])
async def test_unavailable_command_is_refused_without_entering_owner(capability, reason):
    h = Harness(capability=capability)
    h.dispatcher.receive(h.request())
    await h.settle()
    assert h.calls == 0
    assert len(h.messages) == 1
    assert (h.messages[0]["outcome"], h.messages[0]["reason"]) == ("refused", reason)


async def test_owner_can_refuse_after_admission_and_errors_are_sanitized():
    async def refuse():
        return CommandResult("refused", "denied")

    async def fail():
        raise RuntimeError("secret-provider-token-and-private-url")

    for owner, expected in [(refuse, ("refused", "denied")), (fail, ("failed", "owner_failed"))]:
        h = Harness(owner)
        h.dispatcher.receive(h.request())
        await h.settle()
        assert (h.messages[-1]["outcome"], h.messages[-1]["reason"]) == expected
        assert "secret" not in json.dumps(h.messages)


async def test_timeout_cancels_owner_and_never_acknowledges_late_success():
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return CommandResult("succeeded", "completed")

    h = Harness(owner)
    h.dispatcher.receive(h.request(timeout_ms=100))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert h.messages[-1]["outcome"] == "timed_out"
    release.set()
    await h.settle()
    assert [m["outcome"] for m in h.messages] == ["pending", "timed_out"]


async def test_supersession_waits_for_cancellation_suppressing_owner_to_drain():
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def owner():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return CommandResult("succeeded", "completed")

    h = Harness(owner)
    first, replacement = h.request(), h.request()
    h.dispatcher.receive(first)
    await started.wait()
    h.dispatcher.receive(replacement)
    await cancelled.wait()
    await h.settle()
    assert calls == 1, "replacement must not overlap its retiring owner"
    release.set()
    await h.settle()
    await h.settle()
    assert calls == 2
    assert [(m["request_id"], m["outcome"]) for m in h.messages] == [
        (first["request_id"], "pending"), (first["request_id"], "superseded"),
        (replacement["request_id"], "pending"), (replacement["request_id"], "succeeded"),
    ]


@pytest.mark.parametrize("termination", ["cancel", "disconnect", "parent_changed"])
async def test_cancel_disconnect_and_parent_loss_suppress_late_ack(termination):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return CommandResult("succeeded", "completed")

    h = Harness(owner)
    request = h.request()
    h.dispatcher.receive(request)
    await started.wait()
    if termination == "cancel":
        h.dispatcher.receive({**request, "kind": "cancel"})
    elif termination == "disconnect":
        h.dispatcher.close()
    else:
        h.parent += 1
        h.dispatcher.receive(h.request())
    await cancelled.wait()
    release.set()
    await h.settle()
    assert [m["outcome"] for m in h.messages] == (
        ["pending", "cancelled"] if termination == "cancel" else ["pending"]
    )


@pytest.mark.parametrize("changed", [
    {"epoch": str(uuid4())}, {"engine_pid": 9999}, {"parent_pid": 9999}, {"v": True},
])
async def test_restart_or_foreign_identity_gets_no_state_or_ack(changed):
    h = Harness()
    h.dispatcher.receive(h.request(**changed))
    await h.settle()
    assert h.messages == []
    assert h.calls == 0


async def test_replay_during_pending_and_after_completion_never_reexecutes():
    h = Harness()
    request = h.request()
    h.dispatcher.receive(request)
    h.dispatcher.receive(request)
    await h.settle()
    h.dispatcher.receive(request)
    await h.settle()
    assert h.calls == 1
    assert [m["outcome"] for m in h.messages] == ["pending", "succeeded"]


async def test_replayed_id_with_invalid_timeout_cannot_retire_the_original():
    h = Harness()
    request = h.request()
    h.dispatcher.receive(request)
    h.dispatcher.receive({**request, "timeout_ms": False})
    await h.settle()
    assert h.calls == 1
    assert [m["outcome"] for m in h.messages] == ["pending", "succeeded"]


@pytest.mark.parametrize("invalid", [
    {"action": []}, {"action": {}}, {"action": "hub.uplink"},
    {"request_id": "not-a-uuid"}, {"request_id": None}, {"kind": "pairing"},
])
async def test_malformed_or_out_of_scope_request_is_ignored(invalid):
    h = Harness()
    h.dispatcher.receive(h.request(**invalid))
    await h.settle()
    assert h.messages == []
    assert h.calls == 0


@pytest.mark.parametrize("timeout", [None, True, 99, 30001, "1000"])
async def test_invalid_timeout_is_explicitly_refused(timeout):
    h = Harness()
    h.dispatcher.receive(h.request(timeout_ms=timeout))
    await h.settle()
    assert h.calls == 0
    assert h.messages[-1]["reason"] == "invalid_request"


async def test_replay_budget_is_bounded(monkeypatch):
    monkeypatch.setattr(commands, "MAX_REQUESTS", 1)
    h = Harness()
    h.dispatcher.receive(h.request())
    await h.settle()
    h.dispatcher.receive(h.request())
    assert h.messages[-1]["reason"] == "busy"
    assert h.calls == 1


def test_capability_projection_contains_only_known_actions_and_boolean_fields():
    h = Harness(capability=Capability(True, True, True, True, False, True))
    h.dispatcher.publish("hello")
    projection = h.messages[0]["capabilities"]
    assert set(projection) == commands.ACTIONS
    for item in projection.values():
        assert set(item) == {"configured", "available", "active", "denied", "disconnected", "pending"}
        assert all(type(value) is bool for value in item.values())
        assert item["pending"] is False


CHILD = '''
import asyncio
from joat.services.local_commands import ChildCommandPipe, Capability, CommandResult, ACTIONS
async def main():
    pipe = ChildCommandPipe.from_environment()
    print("ordinary output stays off command protocol", flush=True)
    async def owner():
        return CommandResult("succeeded", "completed")
    await pipe.run({action: owner for action in ACTIONS},
                   lambda: {action: Capability(configured=True, available=True) for action in ACTIONS})
asyncio.run(main())
'''


async def start_child(parent_pid=None, program=CHILD):
    env = {**os.environ, "JOAT_COMMAND_EPOCH": str(uuid4()),
           "JOAT_COMMAND_PARENT_PID": str(parent_pid or os.getpid()),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "apps" / "mac")}
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", program, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_real_child_pipe_has_identity_acknowledgements_and_clean_eof():
    child = await start_child()
    try:
        hello = json.loads(await asyncio.wait_for(child.stdout.readline(), 2))
        assert hello["kind"] == "hello"
        assert hello["engine_pid"] == child.pid
        assert hello["parent_pid"] == os.getpid()
        identity = {key: hello[key] for key in ("v", "epoch", "engine_pid", "parent_pid")}
        request = {**identity, "kind": "request", "request_id": str(uuid4()),
                   "action": "news.refresh", "timeout_ms": 1000}
        child.stdin.write(json.dumps(request).encode() + b"\n")
        await child.stdin.drain()
        replies = [json.loads(await asyncio.wait_for(child.stdout.readline(), 2)) for _ in range(2)]
        assert [reply["outcome"] for reply in replies] == ["pending", "succeeded"]
        assert all(reply["request_id"] == request["request_id"] for reply in replies)
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 2)
        assert child.returncode == 0
        assert b"ordinary output" in await child.stderr.read()
        assert await child.stdout.read() == b""
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_restarted_child_ignores_request_from_previous_process_epoch():
    old_child = await start_child()
    try:
        old_hello = json.loads(await asyncio.wait_for(old_child.stdout.readline(), 2))
        old_child.stdin.close()
        await asyncio.wait_for(old_child.wait(), 2)
    finally:
        if old_child.returncode is None:
            old_child.kill()
            await old_child.wait()
    child = await start_child()
    try:
        hello = json.loads(await asyncio.wait_for(child.stdout.readline(), 2))
        assert hello["epoch"] != old_hello["epoch"]
        assert hello["engine_pid"] != old_hello["engine_pid"]
        requests = []
        for source in (old_hello, hello):
            identity = {key: source[key] for key in ("v", "epoch", "engine_pid", "parent_pid")}
            requests.append({**identity, "kind": "request", "request_id": str(uuid4()),
                             "action": "service.retry", "timeout_ms": 1000})
        for request in requests:
            child.stdin.write(json.dumps(request).encode() + b"\n")
        await child.stdin.drain()
        replies = [json.loads(await asyncio.wait_for(child.stdout.readline(), 2)) for _ in range(2)]
        assert [reply["outcome"] for reply in replies] == ["pending", "succeeded"]
        assert all(reply["request_id"] == requests[-1]["request_id"] for reply in replies)
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 2)
        assert child.returncode == 0
        assert await child.stdout.read() == b""
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.parametrize("frame", [b"{broken json}\n", b"x" * (commands.MAX_FRAME + 1) + b"\n", b"\xff\n"])
async def test_real_child_pipe_closes_on_malformed_or_oversized_frame(frame):
    child = await start_child()
    try:
        assert json.loads(await asyncio.wait_for(child.stdout.readline(), 2))["kind"] == "hello"
        child.stdin.write(frame)
        await child.stdin.drain()
        await asyncio.wait_for(child.wait(), 2)
        assert child.returncode == 0
        assert await child.stdout.read() == b""
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_real_child_refuses_forged_parent_before_publishing_state():
    child = await start_child(parent_pid=os.getpid() + 100000)
    stdout, stderr = await asyncio.wait_for(child.communicate(), 2)
    assert child.returncode != 0
    assert stdout == b""
    assert b"command parent identity mismatch" in stderr


async def test_real_child_eof_cancels_pending_owner_without_late_ack():
    program = CHILD.replace(
        'return CommandResult("succeeded", "completed")',
        'print("owner started", flush=True)\n        await asyncio.sleep(60)\n'
        '        return CommandResult("succeeded", "completed")',
    )
    child = await start_child(program=program)
    try:
        hello = json.loads(await asyncio.wait_for(child.stdout.readline(), 2))
        identity = {key: hello[key] for key in ("v", "epoch", "engine_pid", "parent_pid")}
        child.stdin.write(json.dumps({**identity, "kind": "request", "request_id": str(uuid4()),
                                      "action": "service.retry", "timeout_ms": 30000}).encode() + b"\n")
        await child.stdin.drain()
        assert json.loads(await asyncio.wait_for(child.stdout.readline(), 2))["outcome"] == "pending"
        assert b"ordinary output" in await asyncio.wait_for(child.stderr.readline(), 2)
        assert b"owner started" in await asyncio.wait_for(child.stderr.readline(), 2)
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 2)
        assert child.returncode == 0
        assert await child.stdout.read() == b""
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_descendant_stdin_is_eof_and_cannot_steal_shell_commands():
    program = CHILD.replace(
        'print("ordinary output stays off command protocol", flush=True)',
        'import subprocess, sys\n'
        '    result = subprocess.run([sys.executable, "-c", '
        '"import sys; print(repr(sys.stdin.buffer.read()))"], timeout=1, capture_output=True)\n'
        '    assert result.returncode == 0 and result.stdout.strip() == b"b\'\'"',
    )
    child = await start_child(program=program)
    try:
        hello = json.loads(await asyncio.wait_for(child.stdout.readline(), 2))
        identity = {key: hello[key] for key in ("v", "epoch", "engine_pid", "parent_pid")}
        child.stdin.write(json.dumps({**identity, "kind": "request", "request_id": str(uuid4()),
                                      "action": "news.refresh", "timeout_ms": 1000}).encode() + b"\n")
        await child.stdin.drain()
        replies = [json.loads(await asyncio.wait_for(child.stdout.readline(), 2)) for _ in range(2)]
        assert [reply["outcome"] for reply in replies] == ["pending", "succeeded"]
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 2)
        assert child.returncode == 0
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_news_cancelled_fetch_never_commits_and_background_refresh_serializes(tmp_path):
    from joat.services.news import Headline, NewsService

    config = tmp_path / "news.json"
    config.write_text(json.dumps({"categories": [], "customFeeds": ["https://example.invalid/rss"]}))
    loop = asyncio.get_running_loop()
    started = [asyncio.Event() for _ in range(3)]
    finished = [asyncio.Event() for _ in range(3)]
    released = [threading.Event() for _ in range(3)]
    calls = []

    def fetch(url, category):
        index = len(calls)
        calls.append(index)
        loop.call_soon_threadsafe(started[index].set)
        assert released[index].wait(3), "test must release every background fetch"
        loop.call_soon_threadsafe(finished[index].set)
        return [Headline(title=f"pass {index}", link=f"https://example.invalid/{index}",
                         source="fixture", category=category, published_at=100,
                         guid=str(index))]

    service = NewsService(config_path=config, fetch_feed=fetch, clock=lambda: 100)
    tasks = []
    try:
        first = asyncio.create_task(service.refresh_changed())
        tasks.append(first)
        await asyncio.wait_for(started[0].wait(), 1)
        first.cancel()
        for _ in range(4):
            await asyncio.sleep(0)
        replacement = asyncio.create_task(service.refresh_changed())
        tasks.append(replacement)
        background = asyncio.create_task(service.refresh_changed())
        tasks.append(background)
        # Repeated cancellation must not break the thread-drain fence.
        first.cancel()
        for _ in range(4):
            await asyncio.sleep(0)
        assert calls == [0], "replacement and background must wait for retiring fetch threads"
        assert not first.done()
        assert service.headlines == ()
        released[0].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 1)
        await asyncio.wait_for(finished[0].wait(), 1)
        await asyncio.wait_for(started[1].wait(), 1)
        for _ in range(4):
            await asyncio.sleep(0)
        assert calls == [0, 1], "background pass must wait for current refresh"
        assert service.headlines == (), "cancelled fetch must never commit its result"
        released[1].set()
        assert await asyncio.wait_for(replacement, 1) is True
        assert [item.guid for item in service.headlines] == ["1"]
        await asyncio.wait_for(started[2].wait(), 1)
        released[2].set()
        assert await asyncio.wait_for(background, 1) is True
        for _ in range(4):
            await asyncio.sleep(0)
        assert [item.guid for item in service.headlines] == ["2"]
    finally:
        for gate in released:
            gate.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_service_configuration_survives_unavailable_credential_owner():
    from joat.services.local_commands import service_retry_capability
    capability = service_retry_capability(
        configured=True, owner_available=False, state="unavailable", busy=False,
    )
    assert capability.configured
    assert not capability.available
    assert not capability.active
    assert capability.disconnected
    assert not capability.denied


@pytest.mark.parametrize("state", ["approval_required", "blocked", "unavailable"])
def test_service_retry_remains_available_for_existing_unready_owner(state):
    from joat.services.local_commands import service_retry_capability
    capability = service_retry_capability(
        configured=True, owner_available=True, state=state, busy=False,
    )
    assert capability.configured and capability.available
    assert capability.disconnected and not capability.active
    assert capability.denied == (state in {"approval_required", "blocked"})
