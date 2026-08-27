"""End-to-end transport tests against a real WebSocket server.

Not mocked: these start HubGatewayTransport on a loopback port and drive it
with a websockets client speaking the exact JSON the Hub plugin emits. A unit
test with a fake socket would not have caught the connection-replacement or
confirm-future paths, which are the parts most likely to break in the field.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import websockets

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.core.protocol import (
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


@pytest.fixture
async def paired_transport(unused_tcp_port: int, tmp_path):
    """Gateway with pairing REQUIRED even from loopback, so the token paths
    are exercisable from a test that can only connect via 127.0.0.1."""
    store = PairingStore(tmp_path / "pairing.json")
    t = HubGatewayTransport(
        host=HOST, port=unused_tcp_port, pairing=store, trust_loopback=False
    )
    await t.start()
    try:
        yield t, f"ws://{HOST}:{unused_tcp_port}", store
    finally:
        await t.stop()


async def _pair(url: str) -> str:
    """Complete a full pairing handshake and return the confirmed token."""
    async with websockets.connect(url) as ws:
        frame = json.loads(await ws.recv())
        token = frame["token"]
        await ws.send(json.dumps({"kind": "pairing", "token": token}))
        await asyncio.sleep(0.05)  # let the ack land before the socket closes
    return str(token)


class TestPairing:
    """Transport auth (RULES.md #5): TOFU token, enforced pre-handshake."""

    async def test_first_client_is_offered_a_token(self, paired_transport) -> None:
        t, url, store = paired_transport
        async with websockets.connect(url) as ws:
            frame = json.loads(await ws.recv())
        assert frame["kind"] == "pairing.set"
        assert frame["token"]

    async def test_offer_is_not_persisted_until_acked(self, paired_transport) -> None:
        """The strand-the-wearer bug: a lost pairing frame must NOT burn the
        token, or the phone that never got it is 401'd forever."""
        t, url, store = paired_transport
        async with websockets.connect(url) as ws:
            await ws.recv()  # offer received, deliberately not acked
        assert store.token is None
        # The window is still open: a retry pairs cleanly.
        token = await _pair(url)
        assert store.token == token

    async def test_ack_persists_and_closes_the_window(self, paired_transport) -> None:
        t, url, store = paired_transport
        token = await _pair(url)
        assert store.token == token
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await websockets.connect(url)
        assert exc.value.response.status_code == 401

    async def test_mismatched_ack_is_ignored(self, paired_transport) -> None:
        """A forged ack must not pair a token the real phone never stored."""
        t, url, store = paired_transport
        async with websockets.connect(url) as ws:
            await ws.recv()
            await ws.send(json.dumps({"kind": "pairing", "token": "forged-token"}))
            await asyncio.sleep(0.05)
        assert store.token is None

    async def test_wrong_token_is_rejected(self, paired_transport) -> None:
        t, url, store = paired_transport
        await _pair(url)
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await websockets.connect(f"{url}/?token=not-the-token")
        assert exc.value.response.status_code == 401

    async def test_non_ascii_token_is_rejected_cleanly(self, paired_transport) -> None:
        """compare_digest raises TypeError on non-ASCII str — which would
        surface as a 500 + ERROR traceback any LAN peer could trigger."""
        t, url, store = paired_transport
        await _pair(url)
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await websockets.connect(f"{url}/?token=%C3%A9")
        assert exc.value.response.status_code == 401

    async def test_paired_client_reconnects_with_token(self, paired_transport) -> None:
        t, url, store = paired_transport
        token = await _pair(url)
        async with websockets.connect(f"{url}/?token={token}") as ws:
            await ws.send(
                json.dumps({"kind": "hello", "sn": "SN2", "model": "g2", "launchSource": "x"})
            )
            await _wait_for(lambda: t.hello is not None)
        assert t.hello.sn == "SN2"

    async def test_already_paired_store_never_reissues(self, unused_tcp_port, tmp_path) -> None:
        """A restart must not hand the token to the next comer."""
        store = PairingStore(tmp_path / "pairing.json")
        first = PairingStore.mint()
        store.confirm(first)
        t = HubGatewayTransport(
            host=HOST, port=unused_tcp_port, pairing=store, trust_loopback=False
        )
        await t.start()
        try:
            with pytest.raises(websockets.exceptions.InvalidStatus):
                await websockets.connect(f"ws://{HOST}:{unused_tcp_port}")
            assert store.token == first
        finally:
            await t.stop()

    async def test_loopback_is_trusted_by_default(self, unused_tcp_port, tmp_path) -> None:
        """Simulator + speak_test connect from this Mac with no token, even
        when a phone is already paired."""
        store = PairingStore(tmp_path / "pairing.json")
        store.confirm(PairingStore.mint())
        t = HubGatewayTransport(host=HOST, port=unused_tcp_port, pairing=store)
        await t.start()
        try:
            async with websockets.connect(f"ws://{HOST}:{unused_tcp_port}"):
                await _wait_for(lambda: t.connected)
        finally:
            await t.stop()

    async def test_transport_survives_a_peer_that_drops_mid_pairing(
        self, paired_transport
    ) -> None:
        """The stale-_conn bug: an exception during the TOFU offer skipped
        the cleanup finally, leaving connected=True and every later send —
        including the fail-closed confirm gate — raising instead of failing
        closed."""
        t, url, store = paired_transport
        ws = await websockets.connect(url)
        await ws.close()
        await _wait_for(lambda: not t.connected)
        # Fail-closed, not an exception, once the peer is gone.
        assert await t.request_confirm("a1", "ok?", timeout_s=0.1) is False


class TestBindFamilies:
    """iOS resolves .local via Bonjour and may pick IPv6; an IPv4-only
    listener then refuses the connection the phone actually attempts."""

    async def test_wildcard_host_accepts_both_families(self, unused_tcp_port) -> None:
        t = HubGatewayTransport(host="0.0.0.0", port=unused_tcp_port)
        await t.start()
        try:
            for host in ("127.0.0.1", "[::1]"):
                async with websockets.connect(f"ws://{host}:{unused_tcp_port}"):
                    await _wait_for(lambda: t.connected)
        finally:
            await t.stop()

    async def test_explicit_host_is_still_honored(self, unused_tcp_port) -> None:
        # An operator pinning a single interface must keep getting exactly it.
        t = HubGatewayTransport(host="127.0.0.1", port=unused_tcp_port)
        await t.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}"):
                await _wait_for(lambda: t.connected)
        finally:
            await t.stop()


class TestSendResilience:
    async def test_send_survives_a_peer_vanishing_mid_write(self, transport) -> None:
        """A phone can drop between the connected check and the write.
        That is a dropped frame, not an exception for the caller."""
        t, url = transport
        ws = await websockets.connect(url)
        await _wait_for(lambda: t.connected)
        await ws.close()
        # No _wait_for: the point is racing the close, exactly as the field
        # storm did. This must not raise.
        await t.send(HudText(text="hello"))

    async def test_confirm_gate_stays_fail_closed_when_the_peer_drops(
        self, transport
    ) -> None:
        t, url = transport
        ws = await websockets.connect(url)
        await _wait_for(lambda: t.connected)
        await ws.close()
        assert await t.request_confirm("a1", "ok?", timeout_s=0.2) is False


class TestRedactionRegistration:
    """The filter must be tested WHERE IT IS REGISTERED, not in isolation.

    The first version attached _RedactToken to the parent logger
    'websockets' while the request line is emitted by the child
    'websockets.server' — and Python propagates records to ancestor
    HANDLERS, never through ancestor FILTERS. The unit tests called
    filter() directly, so they were green while the registration was inert
    and the pairing token reached brain.log in cleartext (audit finding,
    reproduced live). This test exercises the real hierarchy end to end.
    """

    async def test_child_logger_token_is_redacted_at_the_handler(
        self, unused_tcp_port, tmp_path
    ) -> None:
        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            t = HubGatewayTransport(
                host=HOST, port=unused_tcp_port,
                pairing=PairingStore(tmp_path / "pairing.json"),
            )
            await t.start()  # registration happens here
            try:
                # The exact emitter and shape websockets 17 uses for the
                # request line — the path the token actually rides.
                logging.getLogger("websockets.server").debug(
                    "< %s %s %s", "GET", "/glasses?token=SEKRIT-VALUE", "HTTP/1.1")
            finally:
                await t.stop()
            out = buf.getvalue()
            assert "SEKRIT-VALUE" not in out, "token reached the handler in cleartext"
            assert "token=<redacted>" in out
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
            for f in list(root.filters):
                root.removeFilter(f)

    async def test_registration_is_idempotent(self, unused_tcp_port, tmp_path) -> None:
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            for port_bump in (0, 1):
                t = HubGatewayTransport(
                    host=HOST, port=unused_tcp_port + port_bump,
                    pairing=PairingStore(tmp_path / f"p{port_bump}.json"),
                )
                await t.start()
                await t.stop()
            from joat.ble.hub_gateway import _RedactToken
            assert sum(isinstance(f, _RedactToken) for f in handler.filters) == 1
        finally:
            root.removeHandler(handler)

    async def test_real_pairing_handshake_never_emits_the_token(
        self, unused_tcp_port, tmp_path
    ) -> None:
        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        server_log = logging.getLogger("websockets.server")
        client_log = logging.getLogger("websockets.client")

        old_root_level = root.level
        old_server_level = server_log.level
        old_client_level = client_log.level
        old_server_propagate = server_log.propagate
        old_client_propagate = client_log.propagate

        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        server_log.setLevel(logging.DEBUG)
        client_log.setLevel(logging.DEBUG)
        server_log.propagate = True
        client_log.propagate = True
        try:
            store = PairingStore(tmp_path / "pairing.json")
            t = HubGatewayTransport(
                host=HOST,
                port=unused_tcp_port,
                pairing=store,
                trust_loopback=False,
            )
            await t.start()
            try:
                async with websockets.connect(
                    f"ws://{HOST}:{unused_tcp_port}/glasses"
                ) as ws:
                    offer = json.loads(await ws.recv())
                    assert offer["kind"] == "pairing.set"
                    token = str(offer["token"])
                    await ws.send(json.dumps({"kind": "pairing", "token": token}))
                    await _wait_for(lambda: store.token == token)

                async with websockets.connect(
                    f"ws://{HOST}:{unused_tcp_port}/glasses?token={token}"
                ):
                    pass

                with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
                    await websockets.connect(f"ws://{HOST}:{unused_tcp_port}/glasses")
            finally:
                await t.stop()

            assert exc.value.response.status_code == 401
            out = buf.getvalue()
            assert token not in out, "pairing token reached the emitted logs"
            assert token[:12] not in out, "pairing token prefix reached the emitted logs"
            assert "token=<redacted>" in out
            assert '"token": "<redacted>"' in out
        finally:
            root.removeHandler(handler)
            root.setLevel(old_root_level)
            server_log.setLevel(old_server_level)
            client_log.setLevel(old_client_level)
            server_log.propagate = old_server_propagate
            client_log.propagate = old_client_propagate


class TestPairingRecovery:
    """The absorbing states both audit passes confirmed, closed.

    An unpaired Mac facing a token-PRESENTING peer is the recovery case
    (lost ack, or pairing.json deleted on a working install). Offering a
    fresh token there strands the wearer — the plugin correctly ignores
    offers that do not match what it holds — so the Mac adopts instead.
    """

    async def test_unpaired_mac_adopts_a_presented_token(self, paired_transport) -> None:
        t, url, store = paired_transport
        async with websockets.connect(f"{url}/?token=HELD-BY-PHONE") as ws:
            await ws.send(json.dumps(
                {"kind": "hello", "sn": "S", "model": "g2", "launchSource": "x"}))
            await _wait_for(lambda: store.token is not None)
        assert store.token == "HELD-BY-PHONE"
        # Window closed: a bare LAN peer is refused again.
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await websockets.connect(url)
        assert exc.value.response.status_code == 401

    async def test_adopted_peer_gets_no_offer(self, paired_transport) -> None:
        t, url, store = paired_transport
        async with websockets.connect(f"{url}/?token=HELD-BY-PHONE") as ws:
            await _wait_for(lambda: store.token is not None)
            await ws.send(json.dumps({"kind": "policy", "resume": "auto"}))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.3)  # no pairing.set arrives

    async def test_each_unpaired_connection_is_offered_a_fresh_token(
        self, paired_transport
    ) -> None:
        """A prober that connects during the window must not learn the token
        a later, real phone will ack."""
        t, url, store = paired_transport
        async with websockets.connect(url) as ws:
            first = json.loads(await ws.recv())["token"]  # prober sees this
        async with websockets.connect(url) as ws:
            second = json.loads(await ws.recv())["token"]
            assert second != first
            # The prober's stale token cannot complete the pairing…
            await ws.send(json.dumps({"kind": "pairing", "token": first}))
            await asyncio.sleep(0.2)
            assert store.token is None
            # …the current offer can.
            await ws.send(json.dumps({"kind": "pairing", "token": second}))
            await _wait_for(lambda: store.token == second)

    async def test_runbook_recovery_delete_file_then_reconnect(
        self, unused_tcp_port, tmp_path
    ) -> None:
        """The exact operator procedure that used to convert a paired install
        into a permanently open one (adversary-found trigger, pass 2)."""
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm("PHONE-TOKEN")
        t = HubGatewayTransport(host=HOST, port=unused_tcp_port,
                                pairing=store, trust_loopback=False)
        await t.start()
        try:
            path.unlink()  # the runbook step, engine still running
            async with websockets.connect(
                f"ws://{HOST}:{unused_tcp_port}/?token=PHONE-TOKEN"
            ) as ws:
                await _wait_for(lambda: store.token == "PHONE-TOKEN")
            # Paired again, no restart, no storage wipe; strangers refused.
            with pytest.raises(websockets.exceptions.InvalidStatus):
                await websockets.connect(f"ws://{HOST}:{unused_tcp_port}")
        finally:
            await t.stop()


class TestLockoutRecovery:
    """The mirror of token adoption: the MAC is paired and the phone holds a
    different token (reinstall, cleared storage, a second phone). The phone
    cannot authenticate to ask for help, so the wearer sees 'reconnecting'
    forever — observed on hardware. Authority to fix it is physical access
    to the Mac."""

    async def test_paired_mac_refuses_a_phone_with_the_wrong_token(
        self, paired_transport
    ) -> None:
        t, url, store = paired_transport
        store.confirm("MAC-SIDE-TOKEN")
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await websockets.connect(f"{url}/?token=PHONE-SIDE-DIFFERENT")
        assert exc.value.response.status_code == 401

    async def test_forget_pairing_reopens_the_window(self, paired_transport) -> None:
        t, url, store = paired_transport
        store.confirm("MAC-SIDE-TOKEN")
        assert t.forget_pairing() is True
        # The phone's own token is now adopted on its next dial — no wipe.
        async with websockets.connect(f"{url}/?token=PHONE-SIDE-DIFFERENT") as ws:
            await ws.send(json.dumps(
                {"kind": "hello", "sn": "S", "model": "g2", "launchSource": "x"}))
            await _wait_for(lambda: store.token == "PHONE-SIDE-DIFFERENT")
        # And strangers are refused again immediately.
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await websockets.connect(url)

    async def test_repeated_refusals_log_an_actionable_lockout_warning(
        self, paired_transport, caplog
    ) -> None:
        t, url, store = paired_transport
        store.confirm("MAC-SIDE-TOKEN")
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                with pytest.raises(websockets.exceptions.InvalidStatus):
                    await websockets.connect(f"{url}/?token=WRONG")
        assert "PAIRING LOCKOUT" in caplog.text
        assert "delete" in caplog.text  # tells the operator what to DO

    async def test_forget_on_an_unpaired_store_is_a_no_op(self, paired_transport) -> None:
        t, url, store = paired_transport
        assert store.token is None
        assert t.forget_pairing() is False
