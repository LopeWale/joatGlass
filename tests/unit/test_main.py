"""Pure startup-policy tests for the app-owned JOAT engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.__main__ import gateway_network_policy


def test_private_tailnet_gateway_binds_only_loopback_and_keeps_pairing() -> None:
    policy = gateway_network_policy(
        requested_host="0.0.0.0",
        private_tailnet_gateway=True,
    )

    assert policy.host == "127.0.0.1"
    assert policy.trust_loopback is False


def test_direct_development_gateway_preserves_requested_listener() -> None:
    policy = gateway_network_policy(
        requested_host="::",
        private_tailnet_gateway=False,
    )

    assert policy.host == "::"
    assert policy.trust_loopback is True


def test_private_tailnet_gateway_rejects_an_ambiguous_explicit_listener() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        gateway_network_policy(
            requested_host="192.168.12.200",
            private_tailnet_gateway=True,
        )


def test_known_home_mini_serve_origin_is_declared_without_raw_gateway_tls() -> None:
    manifest = json.loads(
        (Path(__file__).parents[2] / "apps/hub-plugin/app.json").read_text()
    )
    whitelist = manifest["permissions"][2]["whitelist"]

    assert "wss://admins-mac-mini.tail3a3363.ts.net" in whitelist
    assert all(not entry.startswith("wss://") or ":8787" not in entry for entry in whitelist)
