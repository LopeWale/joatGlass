"""Real WebSocket proof for the opt-in G2 diagnostics receipt path."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.services.g2_diagnostics import G2DiagnosticStore


def test_location_probe_cannot_run_without_a_redacted_receipt_store() -> None:
    with pytest.raises(ValueError, match="requires g2_diagnostics"):
        HubGatewayTransport(location_probe=True)


@pytest.mark.asyncio
async def test_gateway_enables_and_persists_only_the_allowlisted_trace(
    tmp_path, unused_tcp_port: int,  # type: ignore[no-untyped-def]
) -> None:
    path = tmp_path / "g2-diagnostics.json"
    gateway = HubGatewayTransport(
        host="127.0.0.1",
        port=unused_tcp_port,
        pairing=PairingStore(path=tmp_path / "pairing.json"),
        g2_diagnostics=G2DiagnosticStore(path=path),
        location_probe=True,
    )
    await gateway.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
            assert json.loads(await socket.recv()) == {
                "kind": "g2.diagnostic.control", "enabled": True,
            }
            assert json.loads(await socket.recv()) == {
                "kind": "location.probe.control", "enabled": True,
            }
            await socket.send(json.dumps({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "page_transition",
                    "from": "overview",
                    "to": "list",
                    "reason": "show_list",
                    "outcome": "success",
                },
            }))
            await socket.send(json.dumps({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "location_probe_state",
                    "requested": "high",
                    "outcome": "success",
                },
            }))
            await socket.send(json.dumps({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "location_probe",
                    "sampleSequence": 1,
                    "profile": "high",
                    "lifecycle": "foreground",
                    "fields": 7,
                    "cadenceMs": 1004,
                    "accuracy": "lte10",
                    "timestampAge": "fresh",
                    "displacement": "5_20",
                    "headingDelta": "0_30",
                },
            }))
            await asyncio.wait_for(
                _wait_for(lambda: path.exists() and len(json.loads(path.read_text())["events"]) == 3),
                timeout=1,
            )
    finally:
        await gateway.stop()

    receipt = json.loads(path.read_text())
    assert receipt["events"][0].keys() == {
        "sequence", "recordedAtMs", "type", "from", "to", "reason", "outcome",
    }
    assert receipt["events"][0]["to"] == "list"
    assert receipt["events"][1].keys() == {
        "sequence", "recordedAtMs", "type", "requested", "outcome",
    }
    assert receipt["events"][1]["outcome"] == "success"
    assert receipt["events"][2].keys() == {
        "sequence", "recordedAtMs", "type", "sampleSequence", "profile", "lifecycle", "fields",
        "cadence", "accuracy", "timestampAge", "displacement", "headingDelta",
    }
    assert receipt["events"][2]["sequence"] == 3
    assert receipt["events"][2]["sampleSequence"] == 1
    assert receipt["events"][2]["cadence"] == "1_5s"
    assert "latitude" not in receipt["events"][2]


@pytest.mark.asyncio
async def test_failed_diagnostic_persistence_does_not_disconnect_the_plugin(
    tmp_path, unused_tcp_port: int,  # type: ignore[no-untyped-def]
) -> None:
    def broken_writer(_path, _events) -> None:  # type: ignore[no-untyped-def]
        raise OSError("disk unavailable")

    store = G2DiagnosticStore(
        path=tmp_path / "g2-diagnostics.json", writer=broken_writer,
    )
    gateway = HubGatewayTransport(
        host="127.0.0.1",
        port=unused_tcp_port,
        pairing=PairingStore(path=tmp_path / "pairing.json"),
        g2_diagnostics=store,
    )
    await gateway.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            await socket.recv()  # auth.accepted
            await socket.recv()  # g2.diagnostic.control
            await socket.send(json.dumps({
                "kind": "g2.diagnostic",
                "event": {"type": "gesture", "gesture": "tap", "delivery": "sys"},
            }))
            await store.flush()

            await socket.send(json.dumps({"kind": "device", "connect": "connected"}))
            await _wait_for(lambda: gateway.device is not None)
            assert gateway.connected
    finally:
        await gateway.stop()


async def _wait_for(predicate) -> None:  # type: ignore[no-untyped-def]
    while not predicate():
        await asyncio.sleep(0.01)
