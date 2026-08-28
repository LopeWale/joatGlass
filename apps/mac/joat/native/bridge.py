"""Bounded, typed Unix-socket client for the Swift EventKit bridge."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRIDGE_VERSION = 1
MAXIMUM_FRAME_BYTES = 1_048_576


class NativeBridgeProtocolError(RuntimeError):
    """The peer violated the versioned framing or response contract."""


class NativeBridgeUnavailable(RuntimeError):
    """The native peer is unavailable; a sent write may be ambiguous."""

    def __init__(self, message: str, *, possible_external_commit: bool = False) -> None:
        super().__init__(message)
        self.possible_external_commit = possible_external_commit


class NativeBridgeError(RuntimeError):
    """A typed native error with an explicit external-commit phase."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        commit_phase: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.commit_phase = commit_phase


@dataclass(frozen=True)
class AuthorizationSnapshot:
    calendar: str
    reminders: str


@dataclass(frozen=True)
class NativeBridgeResponse:
    store_instance_id: str
    store_generation: int
    authorization: AuthorizationSnapshot
    commit_phase: str
    result: Any


class NativeBridgeClient:
    """One serialized connection; Python remains the only SQLite writer."""

    def __init__(self, path: Path, *, timeout_s: float = 10.0) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            info = self._path.lstat()
        except OSError as exc:
            raise NativeBridgeUnavailable("native bridge socket is missing") from exc
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise NativeBridgeUnavailable("native bridge path is not an app-owned socket")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise NativeBridgeUnavailable("native bridge socket is not owner-only")
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(self._path)
        except OSError as exc:
            raise NativeBridgeUnavailable("native bridge connection failed") from exc

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def __aenter__(self) -> NativeBridgeClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object | None,
    ) -> None:
        await self.close()

    async def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        payload_digest: str | None = None,
        timeout_s: float | None = None,
    ) -> NativeBridgeResponse:
        if not method:
            raise ValueError("native bridge method is required")
        write_bound = attempt_id is not None or payload_digest is not None
        if (attempt_id is None) != (payload_digest is None):
            raise ValueError("attempt ID and payload digest must be supplied together")
        if payload_digest is not None:
            try:
                raw_digest = bytes.fromhex(payload_digest)
            except ValueError as exc:
                raise ValueError("payload digest must be hexadecimal SHA-256") from exc
            if len(raw_digest) != 32:
                raise ValueError("payload digest must be hexadecimal SHA-256")

        request_id = str(uuid.uuid4())
        effective_timeout = timeout_s if timeout_s is not None else self._timeout_s
        request = {
            "version": BRIDGE_VERSION,
            "request_id": request_id,
            "method": method,
            "deadline_unix_ms": int((time.time() + effective_timeout) * 1_000),
            "attempt_id": attempt_id,
            "payload_digest": payload_digest,
            "body": body or {},
        }
        payload = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAXIMUM_FRAME_BYTES:
            raise NativeBridgeProtocolError("native bridge request exceeds frame limit")

        async with self._lock:
            await self.connect()
            assert self._reader is not None and self._writer is not None
            sent = False
            try:
                async with asyncio.timeout(effective_timeout):
                    self._writer.write(struct.pack(">I", len(payload)) + payload)
                    await self._writer.drain()
                    sent = True
                    header = await self._reader.readexactly(4)
                    size = struct.unpack(">I", header)[0]
                    if size < 1 or size > MAXIMUM_FRAME_BYTES:
                        raise NativeBridgeProtocolError(
                            "native bridge response frame is invalid"
                        )
                    raw = await self._reader.readexactly(size)
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                await self.close()
                raise NativeBridgeUnavailable(
                    "native bridge disconnected before a receipt",
                    possible_external_commit=write_bound and sent,
                ) from exc
            except TimeoutError as exc:
                await self.close()
                raise NativeBridgeUnavailable(
                    "native bridge response timed out",
                    possible_external_commit=write_bound and sent,
                ) from exc

        try:
            response = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NativeBridgeProtocolError("native bridge returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise NativeBridgeProtocolError("native bridge response must be an object")
        if response.get("version") != BRIDGE_VERSION:
            raise NativeBridgeProtocolError("native bridge response version mismatch")
        if response.get("request_id") != request_id:
            raise NativeBridgeProtocolError("native bridge response request ID mismatch")
        authorization = response.get("authorization")
        if not isinstance(authorization, dict):
            raise NativeBridgeProtocolError("native bridge authorization snapshot is missing")
        commit_phase = response.get("commit_phase")
        if not isinstance(commit_phase, str):
            raise NativeBridgeProtocolError("native bridge commit phase is missing")
        error = response.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise NativeBridgeProtocolError("native bridge error is malformed")
            raise NativeBridgeError(
                str(error.get("code") or "native_error"),
                str(error.get("message") or "native operation failed"),
                retryable=error.get("retryable") is True,
                commit_phase=commit_phase,
            )
        instance = response.get("store_instance_id")
        generation = response.get("store_generation")
        if not isinstance(instance, str) or not isinstance(generation, int):
            raise NativeBridgeProtocolError("native bridge store identity is missing")
        return NativeBridgeResponse(
            store_instance_id=instance,
            store_generation=generation,
            authorization=AuthorizationSnapshot(
                calendar=str(authorization.get("calendar") or "unknown"),
                reminders=str(authorization.get("reminders") or "unknown"),
            ),
            commit_phase=commit_phase,
            result=response.get("result"),
        )
