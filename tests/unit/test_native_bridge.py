"""Typed Unix bridge framing and ambiguous-write failure semantics."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from joat.native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)


def response_for(request: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "version": 1,
        "request_id": request["request_id"],
        "store_instance_id": "store-1",
        "store_generation": 7,
        "authorization": {
            "calendar": "not_determined",
            "reminders": "denied",
        },
        "commit_phase": "read_only",
        "result": {"calendar_read": False},
        "error": None,
    }
    response.update(overrides)
    return response


async def start_server(
    path: Path,
    responder: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    chunked: bool = False,
) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            size = struct.unpack(">I", await reader.readexactly(4))[0]
            request = json.loads(await reader.readexactly(size))
            response = responder(request)
            if response is None:
                return
            payload = json.dumps(response, separators=(",", ":")).encode()
            frame = struct.pack(">I", len(payload)) + payload
            if chunked:
                for byte in frame:
                    writer.write(bytes([byte]))
                    await writer.drain()
            else:
                writer.write(frame)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=path)
    os.chmod(path, 0o600)
    return server


@pytest.fixture
def socket_path() -> Path:
    # Darwin's sockaddr_un path is only 104 bytes; pytest's nested tmp path
    # intentionally exceeds that on this host.
    path = Path("/tmp") / f"joat-native-test-{uuid.uuid4().hex}.sock"
    yield path
    path.unlink(missing_ok=True)


async def test_partial_frames_decode_without_assuming_packet_boundaries(socket_path: Path) -> None:
    path = socket_path
    server = await start_server(path, lambda request: response_for(request), chunked=True)
    try:
        async with NativeBridgeClient(path) as client:
            response = await client.call("capabilities")
        assert response.store_instance_id == "store-1"
        assert response.store_generation == 7
        assert response.result == {"calendar_read": False}
    finally:
        server.close()
        await server.wait_closed()


async def test_native_error_keeps_commit_phase_so_caller_never_blindly_retries(
    socket_path: Path,
) -> None:
    path = socket_path

    def fail(request: dict[str, Any]) -> dict[str, Any]:
        return response_for(
            request,
            commit_phase="committed_unverified",
            result=None,
            error={
                "code": "commit_readback_missing",
                "message": "save may have committed",
                "retryable": False,
            },
        )

    server = await start_server(path, fail)
    try:
        async with NativeBridgeClient(path) as client:
            with pytest.raises(NativeBridgeError) as raised:
                await client.call(
                    "calendar.create",
                    {"title": "Private title"},
                    attempt_id="attempt-1",
                    payload_digest="ab" * 32,
                )
        assert raised.value.code == "commit_readback_missing"
        assert raised.value.commit_phase == "committed_unverified"
        assert raised.value.retryable is False
    finally:
        server.close()
        await server.wait_closed()


async def test_disconnect_after_write_is_explicitly_ambiguous(socket_path: Path) -> None:
    path = socket_path
    server = await start_server(path, lambda _request: None)
    try:
        async with NativeBridgeClient(path) as client:
            with pytest.raises(NativeBridgeUnavailable) as raised:
                await client.call(
                    "reminders.create",
                    {"title": "Do it"},
                    attempt_id="attempt-2",
                    payload_digest="cd" * 32,
                )
        assert raised.value.possible_external_commit is True
    finally:
        server.close()
        await server.wait_closed()


async def test_response_version_and_request_identity_are_fail_closed(socket_path: Path) -> None:
    path = socket_path
    server = await start_server(
        path,
        lambda request: response_for(request, version=2, request_id="stale"),
    )
    try:
        async with NativeBridgeClient(path) as client:
            with pytest.raises(NativeBridgeProtocolError):
                await client.call("capabilities")
    finally:
        server.close()
        await server.wait_closed()


async def test_socket_must_be_owner_only(socket_path: Path) -> None:
    path = socket_path
    server = await start_server(path, lambda request: response_for(request))
    os.chmod(path, 0o666)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o666
        with pytest.raises(NativeBridgeUnavailable, match="owner-only"):
            await NativeBridgeClient(path).connect()
    finally:
        server.close()
        await server.wait_closed()
