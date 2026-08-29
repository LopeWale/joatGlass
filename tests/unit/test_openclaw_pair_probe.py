"""Contracts for the bounded production-scope OpenClaw pairing probe."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType


def _probe_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "openclaw_pair_probe.py"
    spec = importlib.util.spec_from_file_location("openclaw_pair_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Bridge:
    async def __aenter__(self) -> _Bridge:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Credentials:
    device_id = "a" * 64


class _Connection:
    hello = {
        "server": {"version": "2026.8.0", "connId": "c1"},
        "auth": {"role": "operator", "scopes": ["operator.read", "operator.write"]},
    }
    advertised_methods = {"sessions.list", "sessions.create", "tools.effective"}

    async def close(self) -> None:
        return None


def test_probe_requests_the_exact_scopes_the_agent_terminal_requires(monkeypatch) -> None:
    probe = _probe_module()
    observed: dict[str, object] = {}

    async def open_credentials(*, bridge: object, profile: object) -> _Credentials:
        assert isinstance(bridge, _Bridge)
        observed["profile"] = profile
        return _Credentials()

    class Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        async def connect(self) -> _Connection:
            return _Connection()

    monkeypatch.setattr(probe, "NativeBridgeClient", lambda _path: _Bridge())
    monkeypatch.setattr(probe.NativeOpenClawCredentials, "open", open_credentials)
    monkeypatch.setattr(probe, "OpenClawGatewayClient", Client)

    result = asyncio.run(probe._probe(
        url="wss://admins-mac-mini.tail3a3363.ts.net",
        bridge_path=Path("/tmp/joat-native-bridge.sock"),
    ))

    assert observed["scopes"] == ("operator.read", "operator.write")
    assert result["requested_scopes"] == ["operator.read", "operator.write"]
    assert result["status"] == "connected_operator"


def test_probe_reports_a_production_scope_approval_request_without_a_session(monkeypatch) -> None:
    probe = _probe_module()
    observed: dict[str, object] = {}

    async def open_credentials(*, bridge: object, profile: object) -> _Credentials:
        assert isinstance(bridge, _Bridge)
        return _Credentials()

    class Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        async def connect(self) -> _Connection:
            raise probe.OpenClawPairingRequired(
                request_id="pairing-request-123",
                reason="not-paired",
            )

    monkeypatch.setattr(probe, "NativeBridgeClient", lambda _path: _Bridge())
    monkeypatch.setattr(probe.NativeOpenClawCredentials, "open", open_credentials)
    monkeypatch.setattr(probe, "OpenClawGatewayClient", Client)

    result = asyncio.run(probe._probe(
        url="wss://admins-mac-mini.tail3a3363.ts.net",
        bridge_path=Path("/tmp/joat-native-bridge.sock"),
    ))

    assert observed["scopes"] == ("operator.read", "operator.write")
    assert result == {
        "status": "approval_required",
        "gateway": "wss://admins-mac-mini.tail3a3363.ts.net",
        "device_id": "a" * 64,
        "request_id": "pairing-request-123",
        "reason": "not-paired",
        "requested_scopes": ["operator.read", "operator.write"],
    }
