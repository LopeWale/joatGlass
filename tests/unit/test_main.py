"""Pure startup-policy tests for the app-owned JOAT engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.__main__ import build_argument_parser, gateway_network_policy


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


def test_g2_diagnostics_are_explicitly_opt_in() -> None:
    parser = build_argument_parser()

    assert parser.parse_args([]).g2_diagnostics is False
    assert parser.parse_args(["--g2-diagnostics"]).g2_diagnostics is True


def test_the_engine_never_arms_the_g2_trace_on_its_own() -> None:
    """"Record G2 diagnostics" off must mean off. The engine used to arm the
    trace a second after every connection, so the glasses streamed every
    event and g2-diagnostics.json was written with the toggle off. The only
    store is the one built at startup from `--g2-diagnostics` (or the
    location probe, which needs it) and handed to the gateway."""
    import inspect

    import joat.__main__ as engine

    source = inspect.getsource(engine.main)
    assert "set_g2_diagnostics(" not in source
    assert source.count("G2DiagnosticStore(") == 1
    assert "if args.g2_diagnostics or args.location_probe" in source


def test_voice_latency_receipt_is_explicitly_opt_in() -> None:
    parser = build_argument_parser()

    assert parser.parse_args([]).voice_latency_diagnostics is False
    assert parser.parse_args(["--voice-latency-diagnostics"]).voice_latency_diagnostics is True


def test_checked_in_manifest_is_generic_and_never_declares_raw_gateway_tls() -> None:
    # Personal Mac origins come from apps/hub-plugin/.env at pack time; the
    # shared manifest carries only the simulator's loopback gateway.
    manifest = json.loads(
        (Path(__file__).parents[2] / "apps/hub-plugin/app.json").read_text()
    )
    whitelist = manifest["permissions"][2]["whitelist"]

    assert manifest["package_id"] == "com.example.joat"
    assert whitelist and all(
        entry in {"ws://127.0.0.1:8787", "ws://localhost:8787"} for entry in whitelist
    )
    assert all(not entry.startswith("wss://") or ":8787" not in entry for entry in whitelist)
