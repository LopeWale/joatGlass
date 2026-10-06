"""The loopback pairing exemption is for this Mac's own clients, not for web
pages open in its browser (2026-10-06).

Any page can dial ws://127.0.0.1:8787 and its socket arrives from loopback,
so before this a foreign page skipped pairing like the simulator does — and
since the phone screen can sign subscription accounts in and out, it could
have finished a sign-in with an account of its own. A browser always names
the page in Origin; a foreign one now pairs like any network peer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore

TOKEN = "paired-token-0123456789"


async def _gateway(tmp_path: Path, port: int, *, paired: bool) -> HubGatewayTransport:
    store = PairingStore(path=tmp_path / "pairing.json")
    if paired:
        store.confirm(TOKEN)
    gateway = HubGatewayTransport(host="127.0.0.1", port=port, pairing=store)
    await gateway.start()
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:5190", "http://localhost:5190"])
async def test_this_macs_own_clients_skip_pairing(
    tmp_path: Path, unused_tcp_port: int, origin: str | None,
) -> None:
    gateway = await _gateway(tmp_path, unused_tcp_port, paired=True)
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{unused_tcp_port}", origin=origin,  # type: ignore[arg-type]
        ) as socket:
            assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_a_foreign_page_on_loopback_must_present_the_token(
    tmp_path: Path, unused_tcp_port: int,
) -> None:
    gateway = await _gateway(tmp_path, unused_tcp_port, paired=True)
    url = f"ws://127.0.0.1:{unused_tcp_port}"
    try:
        async with websockets.connect(url, origin="https://evil.example") as socket:  # type: ignore[arg-type]
            await socket.send(json.dumps({"kind": "auth", "token": None}))
            with pytest.raises(ConnectionClosed) as closed:
                await socket.recv()
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
        async with websockets.connect(url, origin="https://evil.example") as socket:  # type: ignore[arg-type]
            await socket.send(json.dumps({"kind": "auth", "token": TOKEN}))
            assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_a_foreign_page_cannot_pair_while_the_window_is_closed(
    tmp_path: Path, unused_tcp_port: int,
) -> None:
    gateway = await _gateway(tmp_path, unused_tcp_port, paired=False)
    try:
        with pytest.raises(InvalidStatus) as refused:
            async with websockets.connect(
                f"ws://127.0.0.1:{unused_tcp_port}", origin="https://evil.example",  # type: ignore[arg-type]
            ):
                pass
        assert refused.value.response.status_code == 403
    finally:
        await gateway.stop()
