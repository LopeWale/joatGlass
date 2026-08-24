"""End-to-end transport tests against a real WebSocket server.

Not mocked: these start HubGatewayTransport on a loopback port and drive it
with a websockets client speaking the exact JSON the Hub plugin emits. A unit
test with a fake socket would not have caught the connection-replacement or
confirm-future paths, which are the parts most likely to break in the field.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from jarvis.ble.hub_gateway import HubGatewayTransport
from jarvis.core.protocol import (
    Device,
    EventSource,
    EventType,
    HudText,
    Imu,
    InputEvent,
)

HOST = "127.0.0.1"


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll until predicate() is truthy. Beats arbitrary sleeps.

    asyncio.wait_for rather than asyncio.timeout so the suite also runs on
    Python 3.10 sandboxes/CI, even though the project targets 3.12.
    """

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


@pytest.fixture
async def transport(unused_tcp_port: int):
    t = HubGatewayTransport(host=HOST, port=unused_tcp_port)
    await t.start()
    try:
        yield t, f"ws://{HOST}:{unused_tcp_port}"
    finally:
        await t.stop()


class TestConnection:
    async def test_hello_is_recorded(self, transport) -> None:
        t, url = transport
        async with websockets.connect(url) as ws:
            await ws.send(
                json.dumps({"kind": "hello", "sn": "SN1", "model": "g2", "launchSource": "appMenu"})
            )
            await _wait_for(lambda: t.hello is not None)
        assert t.hello.sn == "SN1"

    async def test_connected_flag_tracks_the_socket(self, transport) -> None:
        t, url = transport
        assert not t.connected
        async with websockets.connect(url):
            await _wait_for(lambda: t.connected)
        await _wait_for(lambda: not t.connected)

    async def test_newest_connection_wins(self, transport) -> None:
        """Reconnect-after-sleep is common; two live plugins are not."""
        t, url = transport
        first = await websockets.connect(url)
        await _wait_for(lambda: t.connected)
        async with websockets.connect(url) as second:
            await _wait_for(lambda: t._conn is not None and t._conn is not None)
            await second.send(json.dumps({"kind": "device", "connect": "connected"}))
            await _wait_for(lambda: t.device is not None)
        with pytest.raises(websockets.ConnectionClosed):
            await first.recv()


class TestUplink:
    async def test_audio_arrives_as_packed_pcm(self, transport) -> None:
        t, url = transport
        got: list[tuple[bytes, int]] = []
        t.on_audio(lambda pcm, seq: got.append((pcm, seq)))
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"kind": "audio", "source": "glasses", "seq": 0, "pcm": [1, -1]}))
            await _wait_for(lambda: bool(got))
        assert got[0] == (b"\x01\x00\xff\xff", 0)

    async def test_audio_gap_is_detected(self, transport, caplog) -> None:
        t, url = transport
        got: list[int] = []
        t.on_audio(lambda pcm, seq: got.append(seq))
        async with websockets.connect(url) as ws:
            for seq in (0, 5):  # 1-4 dropped by MacLink's bounded queue
                await ws.send(json.dumps({"kind": "audio", "source": "glasses", "seq": seq, "pcm": []}))
            await _wait_for(lambda: len(got) == 2)
        assert "audio gap" in caplog.text

    async def test_input_reaches_subscribers(self, transport) -> None:
        t, url = transport
        got: list[InputEvent] = []
        t.on_input(got.append)
        async with websockets.connect(url) as ws:
            await ws.send(
                json.dumps(
                    {
                        "kind": "input",
                        "event": {
                            "eventType": EventType.CLICK,
                            "eventSource": EventSource.GLASSES_RIGHT,
                        },
                    }
                )
            )
            await _wait_for(lambda: bool(got))
        assert got[0].is_right_temple_click

    async def test_device_reaches_subscribers(self, transport) -> None:
        t, url = transport
        got: list[Device] = []
        t.on_device(got.append)
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"kind": "device", "connect": "connected", "battery": 80}))
            await _wait_for(lambda: bool(got))
        assert got[0].battery == 80

    async def test_imu_reaches_subscribers(self, transport) -> None:
        t, url = transport
        got: list[Imu] = []
        t.on_imu(got.append)
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"kind": "imu", "x": 1, "y": 2, "z": 3, "at": 9}))
            await _wait_for(lambda: bool(got))
        assert got[0] == Imu(x=1.0, y=2.0, z=3.0, at=9)

    async def test_garbage_does_not_kill_the_link(self, transport) -> None:
        """Deploy skew must degrade, not disconnect."""
        t, url = transport
        got: list[Device] = []
        t.on_device(got.append)
        async with websockets.connect(url) as ws:
            await ws.send("not json at all")
            await ws.send(json.dumps({"kind": "from_the_future"}))
            await ws.send(json.dumps({"kind": "device", "connect": "connected"}))
            await _wait_for(lambda: bool(got))
        assert got[0].connect == "connected"


class TestDownlink:
    async def test_send_uses_camel_case_wire_format(self, transport) -> None:
        t, url = transport
        async with websockets.connect(url) as ws:
            await _wait_for(lambda: t.connected)
            await t.send(HudText(text="hello"))
            assert json.loads(await ws.recv()) == {
                "kind": "hud.text",
                "containerId": 2,
                "text": "hello",
            }

    async def test_send_with_no_plugin_is_dropped_not_raised(self, transport) -> None:
        t, _ = transport
        await t.send(HudText(text="into the void"))  # must not raise


class TestConfirm:
    async def test_accepted_confirm_resolves_true(self, transport) -> None:
        t, url = transport
        async with websockets.connect(url) as ws:
            await _wait_for(lambda: t.connected)
            task = asyncio.create_task(t.request_confirm("act-1", "Delete?"))
            request = json.loads(await ws.recv())
            assert request["kind"] == "confirm.request"
            await ws.send(json.dumps({"kind": "confirm", "actionId": "act-1", "accepted": True}))
            assert await task is True

    async def test_declined_confirm_resolves_false(self, transport) -> None:
        t, url = transport
        async with websockets.connect(url) as ws:
            await _wait_for(lambda: t.connected)
            task = asyncio.create_task(t.request_confirm("act-2", "Delete?"))
            await ws.recv()
            await ws.send(json.dumps({"kind": "confirm", "actionId": "act-2", "accepted": False}))
            assert await task is False

    async def test_timeout_fails_closed(self, transport) -> None:
        """Silence is not consent — the safety gate must default to declined."""
        t, url = transport
        async with websockets.connect(url) as ws:
            await _wait_for(lambda: t.connected)
            task = asyncio.create_task(t.request_confirm("act-3", "Delete?", timeout_s=0.1))
            await ws.recv()
            assert await task is False

    async def test_answer_for_a_different_action_does_not_resolve(self, transport) -> None:
        """A stale answer must not satisfy a live gate."""
        t, url = transport
        async with websockets.connect(url) as ws:
            await _wait_for(lambda: t.connected)
            task = asyncio.create_task(t.request_confirm("act-4", "Delete?", timeout_s=0.3))
            await ws.recv()
            await ws.send(json.dumps({"kind": "confirm", "actionId": "stale", "accepted": True}))
            assert await task is False


class TestReviewRegressions:
    async def test_malformed_pcm_sample_does_not_kill_the_link(self, transport) -> None:
        """A null inside the pcm array must be dropped like any other garbage,
        not escape pack_pcm as a TypeError and crash the session handler."""
        t, url = transport
        got: list[int] = []
        t.on_audio(lambda pcm, seq: got.append(seq))
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"kind": "audio", "source": "glasses", "seq": 0, "pcm": [1, None]}))
            await ws.send(json.dumps({"kind": "audio", "source": "glasses", "seq": 1, "pcm": [2]}))
            await _wait_for(lambda: bool(got))
        assert got == [1]  # bad batch dropped, link alive, next batch delivered

    async def test_confirm_with_no_plugin_fails_closed_immediately(self, transport) -> None:
        """No wearer to ask → declined now, not after stalling a full timeout."""
        t, _ = transport
        loop = asyncio.get_event_loop()
        started = loop.time()
        assert await t.request_confirm("act-void", "Delete?", timeout_s=8.0) is False
        assert loop.time() - started < 1.0
