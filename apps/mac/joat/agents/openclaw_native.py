"""macOS-owned identity and token adapter for the OpenClaw Gateway client.

The Python runtime may ask the signed-in JOAT app to sign a challenge, but the
Ed25519 private key never crosses the owner-only native bridge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .openclaw_gateway import (
    ALLOWED_OPERATOR_SCOPES,
    DeviceTokenRecord,
    GatewayProfile,
)


DEVICE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class NativeBridgeResult(Protocol):
    @property
    def result(self) -> object: ...


class NativeBridgeAccess(Protocol):
    async def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
    ) -> NativeBridgeResult: ...


@dataclass(slots=True)
class NativeOpenClawCredentials:
    """Stable public identity plus narrow Keychain-backed secret operations."""

    _bridge: NativeBridgeAccess
    _profile_id: str
    device_id: str
    public_key: str

    @classmethod
    async def open(
        cls,
        *,
        bridge: NativeBridgeAccess,
        profile: GatewayProfile,
    ) -> NativeOpenClawCredentials:
        profile_id = hashlib.sha256(profile.url.encode("utf-8")).hexdigest()
        response = await bridge.call(
            "openclaw.identity.get",
            {"profile_id": profile_id},
        )
        result = _record(response.result)
        device_id = result.get("device_id") if result else None
        public_key = result.get("public_key") if result else None
        if (
            not isinstance(device_id, str)
            or not DEVICE_ID_PATTERN.fullmatch(device_id)
            or not isinstance(public_key, str)
            or not public_key
        ):
            raise RuntimeError("native bridge returned an invalid OpenClaw device identity")
        return cls(
            _bridge=bridge,
            _profile_id=profile_id,
            device_id=device_id,
            public_key=public_key,
        )

    async def sign(self, payload: str) -> str:
        response = await self._bridge.call(
            "openclaw.identity.sign",
            {"profile_id": self._profile_id, "payload": payload},
        )
        result = _record(response.result)
        signature = result.get("signature") if result else None
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("native bridge returned an invalid OpenClaw signature")
        return signature

    async def load(self, *, device_id: str, role: str) -> DeviceTokenRecord | None:
        self._validate_binding(device_id=device_id, role=role)
        response = await self._bridge.call(
            "openclaw.token.load",
            {
                "profile_id": self._profile_id,
                "device_id": device_id,
                "role": role,
            },
        )
        if response.result is None:
            return None
        result = _record(response.result)
        token = result.get("token") if result else None
        raw_scopes = result.get("scopes") if result else None
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(raw_scopes, list)
            or not all(isinstance(scope, str) for scope in raw_scopes)
        ):
            raise RuntimeError("native bridge returned an invalid OpenClaw pairing")
        scopes = cast(tuple[str, ...], tuple(raw_scopes))
        if not scopes or any(scope not in ALLOWED_OPERATOR_SCOPES for scope in scopes):
            raise RuntimeError("native bridge returned an invalid OpenClaw pairing")
        return DeviceTokenRecord(token=token, scopes=scopes)

    async def store(
        self,
        *,
        device_id: str,
        role: str,
        token: str,
        scopes: tuple[str, ...],
    ) -> None:
        self._validate_binding(device_id=device_id, role=role)
        if (
            not token
            or not scopes
            or len(set(scopes)) != len(scopes)
            or any(scope not in ALLOWED_OPERATOR_SCOPES for scope in scopes)
        ):
            raise ValueError("invalid OpenClaw device token record")
        response = await self._bridge.call(
            "openclaw.token.store",
            {
                "profile_id": self._profile_id,
                "device_id": device_id,
                "role": role,
                "token": token,
                "scopes": list(scopes),
            },
        )
        result = _record(response.result)
        if result is None or result.get("stored") is not True:
            raise RuntimeError("native bridge did not confirm OpenClaw token storage")

    def _validate_binding(self, *, device_id: str, role: str) -> None:
        if device_id != self.device_id or role != "operator":
            raise ValueError("OpenClaw credential binding does not match this device")


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)
