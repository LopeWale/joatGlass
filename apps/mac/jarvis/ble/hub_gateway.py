"""GlassesTransport over a WebSocket to the Even Hub plugin.

The plugin is the client and connects out to us, because the Hub can only
reach domains listed in its app.json allowlist — it dials, we listen.

Audio arrives as JSON arrays of PCM sample integers, which is wasteful on the
wire but is what the plugin sends today; if throughput becomes a problem the
fix is base64 on both sides, and it changes only _handle_audio here and the
matching batch in AudioStreamer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from ..core.interfaces import (
    AudioCallback,
    DeviceCallback,
    GlassesTransport,
    InputCallback,
)
from ..core.protocol import (
    Audio,
    Confirm,
    ConfirmRequest,
    Device,
    DownlinkMessage,
    Hello,
    Imu,
    Input,
    Policy,
    ProtocolError,
    parse_uplink,
)

log = logging.getLogger(__name__)

ImuCallback = Callable[[Imu], None]
ConfirmCallback = Callable[[Confirm], None]
HelloCallback = Callable[[Hello], None]


class HubGatewayTransport(GlassesTransport):
    """Accepts one plugin connection at a time.

    One, not many: the G2 has a single wearer and a single HUD, so a second
    connection is a stale tab or a duplicate sideload, not a feature. The newest
    connection wins and the previous one is closed — reconnect-after-sleep is
    far more common than genuine concurrent use.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8787) -> None:
        self._host = host
        self._port = port
        self._server: object | None = None
        self._conn: ServerConnection | None = None

        self._audio_cbs: list[AudioCallback] = []
        self._input_cbs: list[InputCallback] = []
        self._device_cbs: list[DeviceCallback] = []
        self._imu_cbs: list[ImuCallback] = []
        self._confirm_cbs: list[ConfirmCallback] = []
        self._hello_cbs: list[HelloCallback] = []

        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self._last_seq: int | None = None
        self.device: Device | None = None
        self.hello: Hello | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await serve(self._session, self._host, self._port)
        log.info("hub gateway listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        server = self._server
        if server is not None:
            server.close()  # type: ignore[attr-defined]
            await server.wait_closed()  # type: ignore[attr-defined]
            self._server = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ── subscriptions ────────────────────────────────────────────────────────

    def on_audio(self, callback: AudioCallback) -> None:
        self._audio_cbs.append(callback)

    def on_input(self, callback: InputCallback) -> None:
        self._input_cbs.append(callback)

    def on_device(self, callback: DeviceCallback) -> None:
        self._device_cbs.append(callback)

    def on_imu(self, callback: ImuCallback) -> None:
        """Raw IMU samples — only flowing while trace mode is on."""
        self._imu_cbs.append(callback)

    def on_confirm(self, callback: ConfirmCallback) -> None:
        self._confirm_cbs.append(callback)

    def on_hello(self, callback: HelloCallback) -> None:
        """Fires when a plugin announces itself — the 'wearer is here' moment."""
        self._hello_cbs.append(callback)

    # ── sending ──────────────────────────────────────────────────────────────

    async def send(self, message: DownlinkMessage) -> None:
        conn = self._conn
        if conn is None:
            log.warning("dropping %s — no plugin connected", type(message).__name__)
            return
        await conn.send(json.dumps(message.to_wire()))

    async def request_confirm(
        self, action_id: str, prompt: str, timeout_s: float = 8.0
    ) -> bool:
        """Ask the wearer to approve an action; await their answer.

        Returns False on timeout. Fail-closed is the only safe default for a
        gate protecting consequential actions (RULES.md #6) — silence is not
        consent, and the wearer may simply not be looking at the HUD.
        """
        if self._conn is None:
            # No plugin, no wearer to ask. The answer is already determined;
            # stalling the orchestrator for the full timeout helps nobody.
            log.info("confirm %s declined — no plugin connected", action_id)
            return False
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending_confirms[action_id] = future
        try:
            await self.send(ConfirmRequest(action_id=action_id, prompt=prompt))
            return await asyncio.wait_for(future, timeout_s)
        # asyncio.TimeoutError explicitly: only on 3.11+ is it an alias of the
        # builtin, and a miss here would leak the exception past the gate.
        except asyncio.TimeoutError:
            log.info("confirm %s timed out — treating as declined", action_id)
            return False
        finally:
            self._pending_confirms.pop(action_id, None)

    # ── receiving ────────────────────────────────────────────────────────────

    async def _session(self, conn: ServerConnection) -> None:
        if self._conn is not None:
            log.info("replacing existing plugin connection")
            await self._conn.close()
        self._conn = conn
        self._last_seq = None
        log.info("plugin connected")
        try:
            async for raw in conn:
                self._dispatch(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._conn is conn:
                self._conn = None
            log.info("plugin disconnected")

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
            message = parse_uplink(payload)
        except (json.JSONDecodeError, ProtocolError) as exc:
            # Deploy skew (newer plugin, older brain) must not kill the link.
            log.warning("dropping unparseable uplink: %s", exc)
            return

        match message:
            case Hello():
                self.hello = message
                log.info("device %s / %s", message.model, message.sn)
                for hello_cb in self._hello_cbs:
                    hello_cb(message)
            case Audio():
                self._handle_audio(message)
            case Imu():
                for imu_cb in self._imu_cbs:
                    imu_cb(message)
            case Input():
                for input_cb in self._input_cbs:
                    input_cb(message.event)
            case Device():
                self.device = message
                for device_cb in self._device_cbs:
                    device_cb(message)
            case Confirm():
                future = self._pending_confirms.get(message.action_id)
                if future is not None and not future.done():
                    future.set_result(message.accepted)
                for confirm_cb in self._confirm_cbs:
                    confirm_cb(message)
            case Policy():
                log.info("wearer resume policy: %s", message.resume)

    def _handle_audio(self, message: Audio) -> None:
        if self._last_seq is not None and message.seq != self._last_seq + 1:
            # Gap = MacLink shed frames while the socket was down. Surfaced so
            # the STT layer can reset instead of splicing across the hole.
            log.warning("audio gap: seq %d → %d", self._last_seq, message.seq)
        self._last_seq = message.seq

        pcm = pack_pcm(message.pcm)
        for cb in self._audio_cbs:
            cb(pcm, message.seq)


def pack_pcm(samples: list[int]) -> bytes:
    """JSON sample integers → signed 16-bit LE bytes, the format STT wants.

    Values are clamped rather than allowed to raise: one malformed sample
    should not discard an entire 200 ms audio batch.
    """
    clamped = [max(-32768, min(32767, int(s))) for s in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)
