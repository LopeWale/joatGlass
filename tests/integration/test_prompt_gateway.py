"""Prompt provenance through the real, token-authenticated Hub gateway.

The isolated store never uses a live pairing token or physical glasses.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import AsyncIterator
import json
from pathlib import Path
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import ClientConnection

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.interfaces import AudioFrame
from joat.core.pairing import PairingStore
from joat.core.protocol import PromptAck, PromptPresent


@pytest.fixture
async def paired_gateway(
    unused_tcp_port: int, tmp_path: Path,
) -> AsyncIterator[tuple[HubGatewayTransport, str, str]]:
    store = PairingStore(tmp_path / "pairing.json")
    token = PairingStore.mint()
    store.confirm(token)
    gateway = HubGatewayTransport(
        host="127.0.0.1", port=unused_tcp_port, pairing=store, trust_loopback=False,
    )
    await gateway.start()
    try:
        yield gateway, f"ws://127.0.0.1:{unused_tcp_port}", token
    finally:
        await gateway.stop()


async def authenticate(socket: ClientConnection, token: str) -> None:
    await socket.send(json.dumps({"kind": "auth", "token": token}))
    assert json.loads(await asyncio.wait_for(socket.recv(), 1)) == {"kind": "auth.accepted"}


async def received_barrier(gateway: HubGatewayTransport, socket: ClientConnection) -> None:
    """A later ordered frame proves that the prior audio was dispatched."""
    received = asyncio.Event()
    gateway.on_device(lambda _device: received.set())
    await socket.send(json.dumps({"kind": "device", "connect": "connected", "mic": True}))
    await asyncio.wait_for(received.wait(), 1)


@pytest.mark.parametrize("source", ["glasses", "phone"])
async def test_authenticated_audio_preserves_epoch_without_inventing_a_new_one(
    paired_gateway: tuple[HubGatewayTransport, str, str], source: str,
) -> None:
    gateway, url, token = paired_gateway
    frames: list[AudioFrame] = []
    gateway.on_audio_frame(frames.append)
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        for seq, epoch in enumerate((None, "older-question", "current-question")):
            message = {"kind": "audio", "source": source, "seq": seq, "pcm": [4, -4]}
            if epoch is not None:
                message["audioEpoch"] = epoch
            await socket.send(json.dumps(message))
        await received_barrier(gateway, socket)

        assert [frame.audio_epoch for frame in frames] == [None, "older-question", "current-question"]
        assert [frame.source for frame in frames] == [source] * 3
        assert len({frame.connection_generation for frame in frames}) == 1
        assert all(frame.pcm == b"\x04\x00\xfc\xff" for frame in frames)
        assert not gateway.physical_g2_connected


@pytest.mark.parametrize("epoch", ["", True, 17, "contains a space", "a" * 129])
async def test_malformed_present_audio_epoch_is_rejected_before_audio_callbacks(
    paired_gateway: tuple[HubGatewayTransport, str, str], epoch: object,
) -> None:
    gateway, url, token = paired_gateway
    frames: list[AudioFrame] = []
    gateway.on_audio_frame(frames.append)
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        await socket.send(json.dumps({
            "kind": "audio", "source": "glasses", "seq": 0,
            "pcm": [4, -4], "audioEpoch": epoch,
        }))
        await received_barrier(gateway, socket)
        assert frames == []

        # A malformed new-contract frame must not kill ordinary legacy input.
        await socket.send(json.dumps({
            "kind": "audio", "source": "glasses", "seq": 1, "pcm": [8, -8],
        }))
        await received_barrier(gateway, socket)
        assert len(frames) == 1 and frames[0].audio_epoch is None


def question(gateway: HubGatewayTransport, prompt_id: str = "question-1") -> PromptPresent:
    generation = gateway.audio_connection_generation
    assert isinstance(generation, str) and generation
    return PromptPresent(
        prompt_id=prompt_id, question="What should I say?",
        presentation_binding="a" * 64, connection_generation=generation,
        source="glasses", expires_in_ms=45_000, owner_kind="messages",
        owner_id="draft-1", field="body",
    )


async def receive_kind(socket: ClientConnection, kind: str) -> dict[str, Any]:
    async with asyncio.timeout(1):
        while True:
            message = json.loads(await socket.recv())
            if message["kind"] == kind:
                return message


def ack(request: dict[str, Any], **changes: Any) -> str:
    return json.dumps({
        "kind": "prompt.ack", "promptId": request["promptId"],
        "presentationBinding": request["presentationBinding"],
        "connectionGeneration": request["connectionGeneration"],
        "source": request["source"], "success": True,
        "audioEpoch": request["promptId"],
    } | changes)


async def finish(task: asyncio.Task[bool]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)


async def test_prompt_ack_activates_synchronously_before_the_next_audio_callback(
    paired_gateway: tuple[HubGatewayTransport, str, str],
) -> None:
    gateway, url, token = paired_gateway
    order: list[str] = []
    receipts: list[PromptAck] = []
    gateway.on_audio_frame(lambda frame: order.append(f"audio:{frame.audio_epoch}"))

    def activate(receipt: PromptAck) -> bool:
        receipts.append(receipt)
        order.append("activate")
        return True

    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(question(gateway), on_presented=activate))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request))
            await socket.send(json.dumps({
                "kind": "audio", "source": "glasses", "seq": 0,
                "pcm": [4, -4], "audioEpoch": request["promptId"],
            }))
            await received_barrier(gateway, socket)
            assert await asyncio.wait_for(task, 1) is True
            assert order == ["activate", "audio:question-1"]
            assert receipts[0].connection_generation == gateway.audio_connection_generation
            assert isinstance(gateway.connection_generation, int)
            await socket.send(ack(request))
            await received_barrier(gateway, socket)
            assert len(receipts) == 1
        finally:
            await finish(task)


@pytest.mark.parametrize("changes", [
    {"connectionGeneration": "foreign-session"},
    {"presentationBinding": "b" * 64},
    {"source": "phone"},
    {"promptId": "old-question", "audioEpoch": "old-question"},
])
async def test_foreign_prompt_ack_cannot_arm_or_spend_the_current_question(
    paired_gateway: tuple[HubGatewayTransport, str, str], changes: dict[str, str],
) -> None:
    gateway, url, token = paired_gateway
    activated: list[PromptAck] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(
            question(gateway), on_presented=lambda receipt: activated.append(receipt) or True,
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request, **changes))
            await received_barrier(gateway, socket)
            assert not task.done() and activated == []
            await socket.send(ack(request))
            assert await asyncio.wait_for(task, 1) is True
            assert len(activated) == 1
        finally:
            await finish(task)


@pytest.mark.parametrize("failure", ["paint", "owner"])
async def test_failed_presentation_or_stale_owner_never_becomes_an_active_prompt(
    paired_gateway: tuple[HubGatewayTransport, str, str], failure: str,
) -> None:
    gateway, url, token = paired_gateway
    activated: list[PromptAck] = []
    revoked: list[bool] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(
            question(gateway), on_presented=lambda receipt: activated.append(receipt) or False,
            on_revoked=lambda: revoked.append(True),
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request, **({"success": False, "audioEpoch": None} if failure == "paint" else {})))
            assert await asyncio.wait_for(task, 1) is False
            assert len(activated) == (0 if failure == "paint" else 1)
            assert revoked == [True]
            await socket.send(ack(request))
            await received_barrier(gateway, socket)
            assert len(activated) == (0 if failure == "paint" else 1)
        finally:
            await finish(task)


async def test_hub_cancel_after_successful_ack_revokes_only_the_same_prompt(
    paired_gateway: tuple[HubGatewayTransport, str, str],
) -> None:
    gateway, url, token = paired_gateway
    revoked: list[bool] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(
            question(gateway), on_presented=lambda _receipt: True,
            on_revoked=lambda: revoked.append(True),
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request))
            assert await asyncio.wait_for(task, 1) is True
            for generation, prompt_id in (("wrong-session", request["promptId"]), (request["connectionGeneration"], "old")):
                await socket.send(json.dumps({"kind": "prompt.cancel", "connectionGeneration": generation, "promptId": prompt_id}))
            await received_barrier(gateway, socket)
            assert revoked == []
            cancel = json.dumps({"kind": "prompt.cancel", "connectionGeneration": request["connectionGeneration"], "promptId": request["promptId"]})
            await socket.send(cancel)
            await socket.send(cancel)
            await received_barrier(gateway, socket)
            assert revoked == [True]
        finally:
            await finish(task)


async def test_new_question_retires_old_waiter_without_old_cleanup_erasing_successor(
    paired_gateway: tuple[HubGatewayTransport, str, str],
) -> None:
    gateway, url, token = paired_gateway
    activated: list[str] = []
    revoked: list[str] = []
    tasks: list[asyncio.Task[bool]] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        try:
            for prompt_id in ("old", "new"):
                task = asyncio.create_task(gateway.request_prompt(
                    question(gateway, prompt_id),
                    on_presented=lambda receipt: activated.append(receipt.prompt_id) or True,
                    on_revoked=lambda prompt_id=prompt_id: revoked.append(prompt_id),
                ))
                tasks.append(task)
                request = await receive_kind(socket, "prompt.present")
                if prompt_id == "old":
                    old_request = request
            assert await asyncio.wait_for(tasks[0], 1) is False
            assert revoked == ["old"]
            await socket.send(ack(old_request))
            await received_barrier(gateway, socket)
            assert not tasks[1].done() and activated == []
            await socket.send(ack(request))
            assert await asyncio.wait_for(tasks[1], 1) is True
            assert activated == ["new"] and revoked == ["old"]
        finally:
            for task in tasks:
                await finish(task)


@pytest.mark.parametrize("ending", ["timeout", "cancel", "disconnect", "replacement", "stop"])
async def test_request_lifecycle_retires_question_and_late_ack_cannot_activate(
    paired_gateway: tuple[HubGatewayTransport, str, str], ending: str,
) -> None:
    gateway, url, token = paired_gateway
    activated: list[PromptAck] = []
    revoked: list[bool] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(
            question(gateway), on_presented=lambda receipt: activated.append(receipt) or True,
            on_revoked=lambda: revoked.append(True), timeout_s=0.02 if ending == "timeout" else 2,
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif ending == "disconnect":
                await socket.close()
            elif ending == "stop":
                await gateway.stop()
            elif ending == "replacement":
                async with websockets.connect(url) as new_socket:
                    await authenticate(new_socket, token)
                    await new_socket.send(ack(request))
                    await received_barrier(gateway, new_socket)
            if ending != "cancel":
                assert await asyncio.wait_for(task, 1) is False
            if ending in ("timeout", "cancel"):
                await socket.send(ack(request))
                await received_barrier(gateway, socket)
            assert revoked == [True] and activated == []
        finally:
            await finish(task)


@pytest.mark.parametrize("device_state", [{"mic": False}, {"wearing": False}])
async def test_mute_or_doff_revokes_a_presented_question_before_late_audio(
    paired_gateway: tuple[HubGatewayTransport, str, str], device_state: dict[str, bool],
) -> None:
    gateway, url, token = paired_gateway
    revoked: list[bool] = []
    order: list[str] = []
    gateway.on_audio_frame(lambda _frame: order.append("audio"))

    def revoke() -> None:
        revoked.append(True)
        order.append("revoked")

    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(
            question(gateway), on_presented=lambda _receipt: True, on_revoked=revoke,
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request))
            assert await asyncio.wait_for(task, 1) is True
            await socket.send(json.dumps({"kind": "device", "connect": "connected"} | device_state))
            await socket.send(json.dumps({"kind": "audio", "source": "glasses", "seq": 0,
                                          "pcm": [4, -4], "audioEpoch": request["promptId"]}))
            await received_barrier(gateway, socket)
            assert order == ["revoked", "audio"]
            assert revoked == [True]
        finally:
            await finish(task)


async def test_duplicate_presentation_cannot_replace_or_renew_an_accepted_question(
    paired_gateway: tuple[HubGatewayTransport, str, str],
) -> None:
    gateway, url, token = paired_gateway
    revoked = asyncio.Event()
    callbacks: list[str] = []
    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        original = replace(question(gateway), expires_in_ms=250)
        task = asyncio.create_task(gateway.request_prompt(
            original, on_presented=lambda _receipt: callbacks.append("first") or True,
            on_revoked=revoked.set,
        ))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request))
            assert await asyncio.wait_for(task, 1) is True
            assert await gateway.request_prompt(
                replace(original, expires_in_ms=45_000),
                on_presented=lambda _receipt: callbacks.append("duplicate") or True,
            ) is False
            await asyncio.wait_for(revoked.wait(), 1)
            assert callbacks == ["first"]
        finally:
            await finish(task)


async def test_throwing_activation_owner_is_not_logged_as_private_question_content(
    paired_gateway: tuple[HubGatewayTransport, str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, url, token = paired_gateway
    private_text = "Synthetic private message content"

    def activate(_receipt: PromptAck) -> bool:
        raise RuntimeError(private_text)

    async with websockets.connect(url) as socket:
        await authenticate(socket, token)
        task = asyncio.create_task(gateway.request_prompt(question(gateway), on_presented=activate))
        try:
            request = await receive_kind(socket, "prompt.present")
            await socket.send(ack(request))
            assert await asyncio.wait_for(task, 1) is False
            assert private_text not in caplog.text
            assert "RuntimeError" in caplog.text
        finally:
            await finish(task)
