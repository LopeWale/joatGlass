"""Contracts for the macOS-owned OpenClaw credential boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pytest

from joat.agents.openclaw_gateway import DeviceTokenRecord, GatewayProfile
from joat.agents.openclaw_native import NativeOpenClawCredentials


@dataclass(frozen=True)
class FakeResponse:
    result: object


class FakeNativeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.device_token: str | None = None

    async def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
    ) -> FakeResponse:
        payload = body or {}
        self.calls.append((method, payload))
        if method == "openclaw.identity.get":
            return FakeResponse(
                {"device_id": "a" * 64, "public_key": "public-key-base64url"}
            )
        if method == "openclaw.identity.sign":
            assert payload["payload"] == "challenge-payload"
            return FakeResponse({"signature": "signature-base64url"})
        if method == "openclaw.token.load":
            if self.device_token is None:
                return FakeResponse(None)
            return FakeResponse(
                {"token": self.device_token, "scopes": ["operator.read"]}
            )
        if method == "openclaw.token.store":
            self.device_token = str(payload["token"])
            return FakeResponse({"stored": True})
        raise AssertionError(f"unexpected bridge method: {method}")


@pytest.mark.asyncio
async def test_native_credentials_keep_private_key_behind_bridge() -> None:
    bridge = FakeNativeBridge()
    profile = GatewayProfile("wss://admins-mac-mini.tail3a3363.ts.net")

    credentials = await NativeOpenClawCredentials.open(
        bridge=bridge,
        profile=profile,
    )
    signature = await credentials.sign("challenge-payload")

    profile_id = hashlib.sha256(profile.url.encode()).hexdigest()
    assert credentials.device_id == "a" * 64
    assert credentials.public_key == "public-key-base64url"
    assert signature == "signature-base64url"
    assert bridge.calls == [
        ("openclaw.identity.get", {"profile_id": profile_id}),
        (
            "openclaw.identity.sign",
            {"profile_id": profile_id, "payload": "challenge-payload"},
        ),
    ]


@pytest.mark.asyncio
async def test_native_credentials_round_trip_device_token_without_retaining_it() -> None:
    bridge = FakeNativeBridge()
    credentials = await NativeOpenClawCredentials.open(
        bridge=bridge,
        profile=GatewayProfile("wss://admins-mac-mini.tail3a3363.ts.net"),
    )

    assert await credentials.load(device_id="a" * 64, role="operator") is None
    await credentials.store(
        device_id="a" * 64,
        role="operator",
        token="paired-device-token",
        scopes=("operator.read",),
    )
    record = await credentials.load(device_id="a" * 64, role="operator")

    assert record == DeviceTokenRecord(
        token="paired-device-token", scopes=("operator.read",)
    )
    assert "paired-device-token" not in repr(record)
    assert "paired-device-token" not in repr(credentials)


@pytest.mark.asyncio
async def test_native_credentials_reject_malformed_identity() -> None:
    class MalformedBridge(FakeNativeBridge):
        async def call(
            self,
            method: str,
            body: dict[str, Any] | None = None,
        ) -> FakeResponse:
            if method == "openclaw.identity.get":
                return FakeResponse({"device_id": "short", "public_key": ""})
            return await super().call(method, body)

    with pytest.raises(RuntimeError, match="invalid OpenClaw device identity"):
        await NativeOpenClawCredentials.open(
            bridge=MalformedBridge(),
            profile=GatewayProfile("wss://admins-mac-mini.tail3a3363.ts.net"),
        )
