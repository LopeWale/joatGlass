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
import errno
import json
import logging
import re
import secrets
import struct
from collections.abc import Callable
from http import HTTPStatus
from ipaddress import ip_address
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from ..core.interfaces import (
    AudioCallback,
    DeviceCallback,
    GlassesTransport,
    InputCallback,
)
from ..core.pairing import PairingStore
from ..core.protocol import (
    Audio,
    Confirm,
    ConfirmRequest,
    Device,
    DownlinkMessage,
    Hello,
    Imu,
    Input,
    Location,
    PairingAck,
    PairingSet,
    Policy,
    ProtocolError,
    parse_uplink,
)

log = logging.getLogger(__name__)


class _RedactToken(logging.Filter):
    """Strip ?token=... from websockets' request-line logging.

    The library logs the full request target at DEBUG, and our auth rides
    the query string — so `--verbose` (exactly what an operator enables to
    debug a pairing failure) would write the long-lived token into
    ~/Library/Logs/joat/brain.log, which the app surfaces in its UI and
    which outlives the on-wire exposure.
    """

    _PATTERN = re.compile(r"(token=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                self._PATTERN.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1<redacted>", record.msg)
        return True


def _install_redaction() -> None:
    """Attach _RedactToken where it actually RUNS: on handlers.

    The first version did logging.getLogger("websockets").addFilter(...) —
    and never redacted anything, because the token-bearing request line is
    emitted by the CHILD logger "websockets.server", and Python propagates
    records to ancestor HANDLERS, never through ancestor FILTERS. The filter
    was tested in isolation (vacuous — audit finding, verified live) while
    the registration was inert. Handler-level filters run for every record
    the handler processes, whichever logger emitted it.
    """
    targets = list(logging.getLogger().handlers)
    # Belt and braces: also the emitting loggers themselves, so a handler
    # attached directly to them later is covered too.
    for name in ("websockets.server", "websockets.client"):
        targets.append(logging.getLogger(name))  # type: ignore[arg-type]
    for target in targets:
        if not any(isinstance(f, _RedactToken) for f in target.filters):
            target.addFilter(_RedactToken())


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

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8787,
        pairing: PairingStore | None = None,
        trust_loopback: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._pairing = pairing
        self._trust_loopback = trust_loopback
        self._provisional: str | None = None
        self._server: object | None = None
        self._conn: ServerConnection | None = None

        self._audio_cbs: list[AudioCallback] = []
        self._input_cbs: list[InputCallback] = []
        self._device_cbs: list[DeviceCallback] = []
        self._imu_cbs: list[ImuCallback] = []
        self._confirm_cbs: list[ConfirmCallback] = []
        self._hello_cbs: list[HelloCallback] = []
        self._disconnect_cbs: list[Callable[[], None]] = []

        self._pending_confirms: dict[str, asyncio.Future[bool]] = {}
        self._last_seq: int | None = None
        self.device: Device | None = None
        self.hello: Hello | None = None
        self.location: Location | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # A just-killed predecessor can still hold the port for a moment
        # (the shell reaps it, but process death and socket release are not
        # simultaneous). Retry briefly instead of dying — an unretried bind
        # turned every app relaunch into a crash-loop.
        # A wildcard IPv4 host binds ONLY IPv4, but iOS resolves a .local
        # name via Bonjour to whichever family it prefers — commonly IPv6.
        # Passing None makes asyncio bind every interface in both families,
        # so the phone connects on whichever address it picked.
        bind_host = None if self._host in ("0.0.0.0", "::", "") else self._host

        last: OSError | None = None
        for attempt in range(10):
            try:
                self._server = await serve(
                    self._session, bind_host, self._port,
                    process_request=self._authorize,
                )
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                last = exc
                if attempt == 0:
                    log.info("port %d busy — waiting for the old engine to release it",
                             self._port)
                await asyncio.sleep(0.5)
        else:
            assert last is not None
            raise last
        if self._pairing is not None:
            _install_redaction()
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

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Fires when the LAST connection drops (not on newest-wins swaps).

        Without this the status pills latched whatever the hello set —
        'connected / streaming' forever, however long the glasses had
        actually been gone (audit finding 25).
        """
        self._disconnect_cbs.append(callback)

    # ── sending ──────────────────────────────────────────────────────────────

    async def send(self, message: DownlinkMessage) -> None:
        conn = self._conn
        if conn is None:
            log.warning("dropping %s — no plugin connected", type(message).__name__)
            return
        try:
            await conn.send(json.dumps(message.to_wire()))
        except websockets.ConnectionClosed:
            # The wearer's phone can drop between the None check and the
            # write (backgrounding, roaming, a reconnect replacing us). A
            # vanished peer is a dropped frame, not an error to raise into
            # the caller — request_confirm in particular must stay
            # fail-closed rather than explode inside the safety gate.
            log.info("dropping %s — plugin disconnected mid-send",
                     type(message).__name__)

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

    # ── pairing ──────────────────────────────────────────────────────────────

    def _peer_is_exempt(self, conn: ServerConnection) -> bool:
        """Loopback peers skip pairing: the simulator and test harnesses run
        on this Mac, and a local process could read the token file anyway —
        the boundary being enforced is the network, not the machine."""
        if not self._trust_loopback:
            return False
        addr = conn.remote_address
        try:
            return bool(ip_address(addr[0]).is_loopback)
        except (ValueError, IndexError, TypeError):
            return False

    @staticmethod
    def _presented_token(conn: ServerConnection) -> str:
        """The ?token= this peer dialed with, or '' when it dialed bare."""
        request = getattr(conn, "request", None)
        if request is None:
            return ""
        return str(parse_qs(urlsplit(request.path).query).get("token", [""])[-1])

    def _authorize(self, conn: ServerConnection, request: Request) -> Response | None:
        """Reject unpaired remote clients before the handshake completes.

        None = proceed. A 401 here is what the wearer sees as "socket never
        opens" on an unpaired phone — the runbook's re-pair procedure (delete
        the store file) is the fix.
        """
        if self._pairing is None or self._peer_is_exempt(conn):
            return None
        expected = self._pairing.token
        if expected is None:
            return None  # pairing window: first remote peer, token issued in _session
        supplied = parse_qs(urlsplit(request.path).query).get("token", [""])[-1]
        # Compare as BYTES: compare_digest raises TypeError on non-ASCII str,
        # which websockets turns into a 500 + ERROR traceback instead of our
        # 401 — letting any LAN peer spam the log and muddy the runbook's
        # "401 means unpaired" diagnostic.
        if secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return None
        log.warning("rejected unpaired connection from %s", conn.remote_address)
        return conn.respond(HTTPStatus.UNAUTHORIZED, "pairing token required\n")

    # ── receiving ────────────────────────────────────────────────────────────

    async def _session(self, conn: ServerConnection) -> None:
        if self._conn is not None:
            log.info("replacing existing plugin connection")
            await self._conn.close()
        self._conn = conn
        self._last_seq = None
        log.info("plugin connected")
        try:
            # TOFU offer INSIDE the try: minting touches the disk and the send
            # can hit a dropped socket, and an escape here would skip the
            # finally — leaving a stale _conn that reports connected while
            # every send (including the fail-closed confirm gate) raises.
            if (
                self._pairing is not None
                and self._pairing.token is None
                and not self._peer_is_exempt(conn)
            ):
                presented = self._presented_token(conn)
                if presented:
                    # ADOPT the token the peer already holds. An unpaired Mac
                    # facing a token-presenting peer is the RECOVERY case,
                    # both flavours the audit confirmed as permanent
                    # lockouts: the phone stored a token whose ack never
                    # arrived, or the operator deleted pairing.json on a
                    # working install. Offering a fresh token here strands
                    # them — the plugin (correctly) ignores offers that do
                    # not match what it holds. Adoption closes the window
                    # immediately with the token the wearer's phone already
                    # has. Trust model unchanged: first remote peer wins,
                    # exactly as TOFU always worked.
                    self._pairing.confirm(presented)
                    self._provisional = None
                    log.info("pairing: adopted token presented by %s — window closed",
                             conn.remote_address)
                else:
                    # Fresh mint PER CONNECTION, never cached per process: a
                    # prober that connects during the window must not learn
                    # the token a later, real phone will ack (audit finding).
                    self._provisional = PairingStore.mint()
                    await conn.send(json.dumps(PairingSet(token=self._provisional).to_wire()))
                    log.info("pairing: token offered to %s", conn.remote_address)
            async for raw in conn:
                self._dispatch(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._conn is conn:
                self._conn = None
                for cb in self._disconnect_cbs:
                    cb()
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
            case Location():
                first = self.location is None
                self.location = message
                if first:
                    log.info("wearer location known (±%sm)",
                             int(message.accuracy) if message.accuracy else "?")
            case PairingAck():
                # Only now is the token real: the phone has it on disk, so
                # locking out every other peer can no longer strand the wearer.
                if (
                    self._pairing is not None
                    and self._provisional is not None
                    and secrets.compare_digest(
                        message.token.encode("utf-8"), self._provisional.encode("utf-8")
                    )
                ):
                    self._pairing.confirm(self._provisional)
                    self._provisional = None
                    log.info("pairing: confirmed — remote peers now require the token")

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
