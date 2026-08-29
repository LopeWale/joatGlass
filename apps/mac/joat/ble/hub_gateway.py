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
import hashlib
import json
import logging
import secrets
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import ip_address
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from ..core.interfaces import (
    AudioCallback,
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    DeviceCallback,
    GlassesTransport,
    InputCallback,
)
from ..core.log_redaction import (
    SecretRedactionFilter as _RedactToken,
    install_websocket_secret_redaction as _install_redaction,
)
from ..core.confirmation import ConfirmationResult
from ..core.pairing import PairingStore
from ..core.protocol import (
    Audio,
    Auth,
    AuthAccepted,
    Confirm,
    ConfirmRequest,
    Device,
    DownlinkMessage,
    G2Diagnostic,
    G2DiagnosticControl,
    Hello,
    Imu,
    Input,
    Location,
    LocationProbeControl,
    NavigationAction,
    NavigationOrigin,
    NavigationOriginFailure,
    NavigationRerouteRequest,
    NavigationRouteAck,
    NavigationState,
    NavigationViewAck,
    PairingAck,
    PairingSet,
    Policy,
    ProtocolError,
    CONFIRM_PROMPT_MAX_UTF16_UNITS,
    parse_uplink,
    utf16_units,
)
from ..services.g2_diagnostics import G2DiagnosticStore

log = logging.getLogger(__name__)


ImuCallback = Callable[[Imu], None]
ConfirmCallback = Callable[[Confirm], None]
HelloCallback = Callable[[Hello], None]
NavigationActionCallback = Callable[[NavigationAction], None]
NavigationRouteAckCallback = Callable[[NavigationRouteAck], None]
NavigationViewAckCallback = Callable[[NavigationViewAck], None]
NavigationStateCallback = Callable[[NavigationState], None]
NavigationRerouteCallback = Callable[[NavigationRerouteRequest], None]
NavigationOriginCallback = Callable[[NavigationOrigin], None]
NavigationOriginFailureCallback = Callable[[NavigationOriginFailure], None]


@dataclass(slots=True)
class _PendingConfirm:
    future: asyncio.Future[bool | None]
    session_id: str
    challenge_id: str
    payload_digest: str
    preview_digest: str
    pairing_identity_digest: bytes
    expires_at_s: float


class HubGatewayTransport(GlassesTransport, AudioFrameSource):
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
        auth_timeout_s: float = 4.0,
        g2_diagnostics: G2DiagnosticStore | None = None,
        location_probe: bool = False,
    ) -> None:
        if location_probe and g2_diagnostics is None:
            raise ValueError("location_probe requires g2_diagnostics receipt storage")
        self._host = host
        self._port = port
        self._pairing = pairing
        self._trust_loopback = trust_loopback
        self._auth_timeout_s = auth_timeout_s
        self._g2_diagnostics = g2_diagnostics
        self._location_probe = location_probe
        self._auth_lock = asyncio.Lock()
        self._refusals: dict[str, int] = {}
        self._lockout_warned = False
        self._server: object | None = None
        self._conn: ServerConnection | None = None

        self._audio_cbs: list[AudioCallback] = []
        self._audio_frame_cbs: list[AudioFrameCallback] = []
        self._input_cbs: list[InputCallback] = []
        self._device_cbs: list[DeviceCallback] = []
        self._imu_cbs: list[ImuCallback] = []
        self._confirm_cbs: list[ConfirmCallback] = []
        self._hello_cbs: list[HelloCallback] = []
        self._navigation_action_cbs: list[NavigationActionCallback] = []
        self._navigation_route_ack_cbs: list[NavigationRouteAckCallback] = []
        self._navigation_view_ack_cbs: list[NavigationViewAckCallback] = []
        self._navigation_state_cbs: list[NavigationStateCallback] = []
        self._navigation_reroute_cbs: list[NavigationRerouteCallback] = []
        self._navigation_origin_cbs: list[NavigationOriginCallback] = []
        self._navigation_origin_failure_cbs: list[NavigationOriginFailureCallback] = []
        self._disconnect_cbs: list[Callable[[], None]] = []

        self._pending_confirms: dict[str, _PendingConfirm] = {}
        self._session_id: str | None = None
        self._session_pairing_identity_digest: bytes | None = None
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
            if self._session_id is not None:
                self._invalidate_confirms(self._session_id)
            await self._conn.close()
            self._conn = None
            self._session_id = None
            self._session_pairing_identity_digest = None
        server = self._server
        if server is not None:
            server.close()  # type: ignore[attr-defined]
            await server.wait_closed()  # type: ignore[attr-defined]
            self._server = None
        if self._g2_diagnostics is not None:
            await self._g2_diagnostics.flush()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ── subscriptions ────────────────────────────────────────────────────────

    def on_audio(self, callback: AudioCallback) -> None:
        self._audio_cbs.append(callback)

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        """Subscribe to provenance-preserving frames before legacy consumers."""
        self._audio_frame_cbs.append(callback)

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
        actually been gone.
        """
        self._disconnect_cbs.append(callback)

    def on_navigation_action(self, callback: NavigationActionCallback) -> None:
        """Semantic HUD actions, already schema- and generation-bound."""
        self._navigation_action_cbs.append(callback)

    def on_navigation_route_ack(self, callback: NavigationRouteAckCallback) -> None:
        self._navigation_route_ack_cbs.append(callback)

    def on_navigation_view_ack(self, callback: NavigationViewAckCallback) -> None:
        self._navigation_view_ack_cbs.append(callback)

    def on_navigation_state(self, callback: NavigationStateCallback) -> None:
        self._navigation_state_cbs.append(callback)

    def on_navigation_reroute(self, callback: NavigationRerouteCallback) -> None:
        self._navigation_reroute_cbs.append(callback)

    def on_navigation_origin(self, callback: NavigationOriginCallback) -> None:
        self._navigation_origin_cbs.append(callback)

    def on_navigation_origin_failure(
        self, callback: NavigationOriginFailureCallback,
    ) -> None:
        self._navigation_origin_failure_cbs.append(callback)

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
        self,
        action_id: str,
        prompt: str,
        timeout_s: float = 8.0,
        *,
        payload_digest: str | None = None,
    ) -> bool:
        """Ask the wearer to approve an action; await their answer.

        Returns False on timeout. Fail-closed is the only safe default for a
        gate protecting consequential actions (RULES.md #6) — silence is not
        consent, and the wearer may simply not be looking at the HUD.
        """
        result = await self.request_confirmation(
            action_id,
            prompt,
            timeout_s,
            payload_digest=payload_digest,
        )
        return result.accepted if result is not None else False

    async def request_confirmation(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float = 8.0,
        *,
        payload_digest: str | None = None,
    ) -> ConfirmationResult | None:
        """Return durable evidence only for an explicit bound HUD decision."""

        if utf16_units(prompt) > CONFIRM_PROMPT_MAX_UTF16_UNITS:
            log.warning(
                "confirm %s declined — exact preview exceeds HUD capacity",
                action_id,
            )
            return None
        conn = self._conn
        session_id = self._session_id
        pairing_identity_digest = self._session_pairing_identity_digest
        if conn is None or session_id is None or pairing_identity_digest is None:
            # No plugin, no wearer to ask. The answer is already determined;
            # stalling the orchestrator for the full timeout helps nobody.
            log.info("confirm %s declined — no plugin connected", action_id)
            return None
        if action_id in self._pending_confirms:
            log.warning("confirm %s declined — action id already pending", action_id)
            return None
        if payload_digest is None:
            payload_digest = hashlib.sha256(
                b"joat-action-payload-v1\0" + prompt.encode("utf-8")
            ).hexdigest()
        try:
            payload_digest_bytes = bytes.fromhex(payload_digest)
        except ValueError:
            log.warning("confirm %s declined — payload digest is not hexadecimal", action_id)
            return None
        if len(payload_digest_bytes) != 32:
            log.warning("confirm %s declined — payload digest is not SHA-256", action_id)
            return None
        preview_digest = hashlib.sha256(
            b"joat-hud-confirm-v1\0" + prompt.encode("utf-8")
        ).hexdigest()
        challenge_id = secrets.token_urlsafe(32)
        issued_at_s = time.time()
        expires_at_s = issued_at_s + timeout_s
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool | None] = loop.create_future()
        pending = _PendingConfirm(
            future=future,
            session_id=session_id,
            challenge_id=challenge_id,
            payload_digest=payload_digest,
            preview_digest=preview_digest,
            pairing_identity_digest=pairing_identity_digest,
            expires_at_s=expires_at_s,
        )
        self._pending_confirms[action_id] = pending
        try:
            await conn.send(json.dumps(ConfirmRequest(
                action_id=action_id,
                prompt=prompt,
                challenge_id=challenge_id,
                session_id=session_id,
                payload_digest=payload_digest,
                preview_digest=preview_digest,
                expires_at_ms=int(expires_at_s * 1000),
            ).to_wire()))
            if self._conn is not conn or self._session_id != session_id:
                return None
            accepted = await asyncio.wait_for(future, timeout_s)
            if accepted is None:
                return None
            decided_at_s = time.time()
            authorization_digest = hashlib.sha256(
                b"joat-confirmation-authorization-v1\0"
                + action_id.encode("utf-8")
                + b"\0"
                + session_id.encode("utf-8")
                + b"\0"
                + challenge_id.encode("utf-8")
                + b"\0"
                + payload_digest.encode("ascii")
                + b"\0"
                + preview_digest.encode("ascii")
            ).digest()
            return ConfirmationResult(
                action_id=action_id,
                accepted=accepted,
                challenge_id=challenge_id,
                session_id=session_id,
                payload_digest=payload_digest_bytes,
                preview_digest=bytes.fromhex(preview_digest),
                pairing_identity_digest=pending.pairing_identity_digest,
                authorization_digest=authorization_digest,
                issued_at_us=int(issued_at_s * 1_000_000),
                expires_at_us=int(expires_at_s * 1_000_000),
                preview_completed_at_us=int(decided_at_s * 1_000_000),
                decided_at_us=int(decided_at_s * 1_000_000),
            )
        except websockets.ConnectionClosed:
            log.info("confirm %s declined — plugin disconnected during challenge", action_id)
            return None
        # asyncio.TimeoutError explicitly: only on 3.11+ is it an alias of the
        # builtin, and a miss here would leak the exception past the gate.
        except asyncio.TimeoutError:
            log.info("confirm %s timed out — treating as declined", action_id)
            return None
        finally:
            if self._pending_confirms.get(action_id) is pending:
                self._pending_confirms.pop(action_id, None)

    def _invalidate_confirms(self, session_id: str) -> None:
        for pending in tuple(self._pending_confirms.values()):
            if (
                pending.session_id == session_id
                and not pending.future.done()
            ):
                pending.future.set_result(None)

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

    def forget_pairing(self) -> bool:
        """Open an explicit, time-bounded local pairing window.

        The recovery for the OPPOSITE asymmetry from token adoption: the Mac
        is paired and the phone holds a different token (reinstall, cleared
        storage, a second phone). Nothing the phone does can fix that — it
        cannot authenticate to ask — so the authority has to be physical
        access to the Mac. Merely deleting or corrupting the token file is not
        enough; the native app must call this local-authority operation.
        """
        if self._pairing is None:
            return False
        self._pairing.begin_pairing()
        self._refusals.clear()
        self._lockout_warned = False
        log.info("pairing window opened locally for 120 seconds")
        return True

    def _authorize(self, conn: ServerConnection, request: Request) -> Response | None:
        """Reject closed pairing windows and credentials in request targets.

        A paired peer authenticates in the first application frame because a
        WebView WebSocket cannot attach a private header. This hook still
        rejects a missing/corrupt local credential unless the Mac explicitly
        opened a pairing window.
        """
        if "token" in parse_qs(urlsplit(request.path).query):
            log.warning("rejected credential in WebSocket URL")
            return conn.respond(
                HTTPStatus.BAD_REQUEST,
                "send credentials in the auth frame, never the URL\n",
            )
        if self._pairing is None or self._peer_is_exempt(conn):
            return None
        expected = self._pairing.token
        if expected is None:
            if self._pairing.window_open:
                return None
            log.warning("rejected pairing attempt — local pairing window is closed")
            return conn.respond(
                HTTPStatus.FORBIDDEN,
                "open Pair Glasses on the Mac before connecting\n",
            )
        return None

    def _record_refusal(self, conn: ServerConnection) -> None:
        """Record an app-auth failure without logging credential material."""
        # A wearer whose phone cannot authenticate sees only "reconnecting"
        # forever; the log said "rejected unpaired connection" and nothing
        # about what to DO. Count refusals per peer and, once it is clearly
        # a lockout rather than a stray probe, say the fix out loud — once.
        host = str(conn.remote_address[0]) if conn.remote_address else "?"
        self._refusals[host] = self._refusals.get(host, 0) + 1
        if self._refusals[host] >= 5 and not self._lockout_warned:
            self._lockout_warned = True
            log.error(
                "PAIRING LOCKOUT: %s has been refused %d times — its token does "
                "not match this Mac's. The wearer sees 'reconnecting' forever. "
                "Fix: use Pair / Replace Glasses in JOAT.app to open a local "
                "120-second pairing window.",
                host, self._refusals[host],
            )
        else:
            log.warning("rejected unpaired connection from %s", conn.remote_address)

    async def _receive_auth_message(self, conn: ServerConnection) -> Auth | PairingAck:
        raw = await asyncio.wait_for(conn.recv(), timeout=self._auth_timeout_s)
        try:
            payload = json.loads(raw)
            message = parse_uplink(payload)
        except (json.JSONDecodeError, ProtocolError, TypeError) as exc:
            raise ProtocolError(f"invalid authentication frame: {exc}") from exc
        if not isinstance(message, (Auth, PairingAck)):
            raise ProtocolError("first application frame must be auth")
        return message

    @staticmethod
    def _pairing_identity(token: str | None) -> bytes:
        material = token.encode("utf-8") if token is not None else b"local-loopback"
        return hashlib.sha256(b"joat-pairing-identity-v1\0" + material).digest()

    async def _authenticate(self, conn: ServerConnection) -> bytes | None:
        """Authenticate before the peer can replace the active HUD session."""
        if self._pairing is None:
            return self._pairing_identity(None)
        if self._peer_is_exempt(conn):
            await conn.send(json.dumps(AuthAccepted().to_wire()))
            return self._pairing_identity(None)

        async with self._auth_lock:
            try:
                first = await self._receive_auth_message(conn)
                if not isinstance(first, Auth):
                    raise ProtocolError("first application frame must be auth")

                expected = self._pairing.token
                if expected is not None:
                    supplied = first.token
                    if supplied is None or not secrets.compare_digest(
                        supplied.encode("utf-8"), expected.encode("utf-8")
                    ):
                        self._record_refusal(conn)
                        await conn.close(code=4401, reason="authentication failed")
                        return None
                    await conn.send(json.dumps(AuthAccepted().to_wire()))
                    return self._pairing_identity(expected)

                if not self._pairing.window_open:
                    await conn.close(code=4403, reason="pairing window closed")
                    return None

                if first.token is not None:
                    # Recovery adoption is safe only because a local user
                    # explicitly opened this bounded window.
                    self._pairing.confirm(first.token)
                    log.info("pairing: adopted a persisted peer token — window closed")
                    await conn.send(json.dumps(AuthAccepted().to_wire()))
                    return self._pairing_identity(first.token)

                candidate = PairingStore.mint()
                await conn.send(json.dumps(PairingSet(token=candidate).to_wire()))
                log.info("pairing: token offered to %s", conn.remote_address)
                ack = await self._receive_auth_message(conn)
                if (
                    not isinstance(ack, PairingAck)
                    or not self._pairing.window_open
                    or not secrets.compare_digest(
                        ack.token.encode("utf-8"), candidate.encode("utf-8")
                    )
                ):
                    await conn.close(code=4401, reason="pairing acknowledgement failed")
                    return None
                self._pairing.confirm(candidate)
                log.info("pairing: confirmed — remote peers now require auth")
                await conn.send(json.dumps(AuthAccepted().to_wire()))
                return self._pairing_identity(candidate)
            except (asyncio.TimeoutError, ProtocolError, websockets.ConnectionClosed):
                try:
                    await conn.close(code=4401, reason="authentication required")
                except websockets.ConnectionClosed:
                    pass
                return None

    # ── receiving ────────────────────────────────────────────────────────────

    async def _session(self, conn: ServerConnection) -> None:
        pairing_identity_digest = await self._authenticate(conn)
        if pairing_identity_digest is None:
            return
        session_id = secrets.token_urlsafe(18)
        if self._conn is not None:
            log.info("replacing existing plugin connection")
            if self._session_id is not None:
                self._invalidate_confirms(self._session_id)
            await self._conn.close()
        self._conn = conn
        self._session_id = session_id
        self._session_pairing_identity_digest = pairing_identity_digest
        self._last_seq = None
        host = str(conn.remote_address[0]) if conn.remote_address else "?"
        self._refusals.pop(host, None)
        self._lockout_warned = False
        log.info("plugin connected")
        try:
            if self._g2_diagnostics is not None:
                await conn.send(json.dumps(G2DiagnosticControl(enabled=True).to_wire()))
            if self._location_probe:
                await conn.send(json.dumps(LocationProbeControl(enabled=True).to_wire()))
            async for raw in conn:
                self._dispatch(raw, session_id)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._conn is conn:
                self._invalidate_confirms(session_id)
                self._conn = None
                self._session_id = None
                self._session_pairing_identity_digest = None
                for cb in self._disconnect_cbs:
                    cb()
            log.info("plugin disconnected")

    def _dispatch(self, raw: str | bytes, session_id: str) -> None:
        try:
            payload = json.loads(raw)
            message = parse_uplink(payload)
        except (json.JSONDecodeError, ProtocolError) as exc:
            # Deploy skew (newer plugin, older brain) must not kill the link.
            log.warning("dropping unparseable uplink: %s", exc)
            return

        match message:
            case Auth():
                # Replays on an established session are inert.
                return
            case Hello():
                self.hello = message
                log.info("device %s / %s", message.model, message.sn)
                for hello_cb in self._hello_cbs:
                    hello_cb(message)
            case Audio():
                self._handle_audio(message, session_id)
            case Imu():
                for imu_cb in self._imu_cbs:
                    imu_cb(message)
            case Input():
                if message.event.trace_id:
                    log.info(
                        "action trace=%s stage=input-received control=%s index=%s",
                        message.event.trace_id,
                        message.event.container_name or "unknown",
                        message.event.selected_index,
                    )
                for input_cb in self._input_cbs:
                    input_cb(message.event)
            case Device():
                self.device = message
                for device_cb in self._device_cbs:
                    device_cb(message)
            case Confirm():
                pending = self._pending_confirms.get(message.action_id)
                fields_match = (
                    pending is not None
                    and pending.session_id == session_id == self._session_id
                    and time.time() <= pending.expires_at_s
                    and message.challenge_id is not None
                    and secrets.compare_digest(
                        message.challenge_id.encode("utf-8"),
                        pending.challenge_id.encode("utf-8"),
                    )
                    and message.session_id is not None
                    and secrets.compare_digest(
                        message.session_id.encode("utf-8"),
                        pending.session_id.encode("utf-8"),
                    )
                    and message.payload_digest is not None
                    and secrets.compare_digest(
                        message.payload_digest.encode("utf-8"),
                        pending.payload_digest.encode("utf-8"),
                    )
                    and message.preview_digest is not None
                    and secrets.compare_digest(
                        message.preview_digest.encode("utf-8"),
                        pending.preview_digest.encode("utf-8"),
                    )
                )
                if pending is None or pending.future.done() or not fields_match:
                    log.warning(
                        "action trace=%s stage=confirmation-ignored reason=binding-mismatch",
                        message.action_id,
                    )
                    return
                log.info(
                    "action trace=%s stage=confirmation-result accepted=%s",
                    message.action_id,
                    message.accepted,
                )
                pending.future.set_result(message.accepted)
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
                # Pairing acks are consumed before a connection becomes active.
                return
            case G2Diagnostic():
                # The protocol parser is the redaction firewall. Never log
                # or persist arbitrary plugin fields, even when opted in.
                if self._g2_diagnostics is not None:
                    try:
                        self._g2_diagnostics.record(message.event)
                    except Exception as exc:
                        # Support capture is optional. A custom or degraded
                        # recorder must not drop a real G2 control session.
                        log.warning("dropping G2 diagnostic after %s", type(exc).__name__)
            case NavigationAction():
                for navigation_cb in self._navigation_action_cbs:
                    navigation_cb(message)
            case NavigationRouteAck():
                for navigation_ack_cb in self._navigation_route_ack_cbs:
                    navigation_ack_cb(message)
            case NavigationViewAck():
                for navigation_view_ack_cb in self._navigation_view_ack_cbs:
                    navigation_view_ack_cb(message)
            case NavigationState():
                for navigation_state_cb in self._navigation_state_cbs:
                    navigation_state_cb(message)
            case NavigationRerouteRequest():
                for navigation_reroute_cb in self._navigation_reroute_cbs:
                    navigation_reroute_cb(message)
            case NavigationOrigin():
                for navigation_origin_cb in self._navigation_origin_cbs:
                    navigation_origin_cb(message)
            case NavigationOriginFailure():
                for navigation_origin_failure_cb in self._navigation_origin_failure_cbs:
                    navigation_origin_failure_cb(message)

    def _handle_audio(self, message: Audio, session_id: str) -> None:
        if self._last_seq is not None and message.seq != self._last_seq + 1:
            # Gap = MacLink shed frames while the socket was down. Surfaced so
            # the STT layer can reset instead of splicing across the hole.
            log.warning("audio gap: seq %d → %d", self._last_seq, message.seq)
        self._last_seq = message.seq

        pcm = pack_pcm(message.pcm)
        frame = AudioFrame(
            pcm=pcm,
            connection_generation=session_id,
            seq=message.seq,
            source=message.source,
            speaker=message.speaker,
            received_at_us=time.time_ns() // 1_000,
        )
        # Capture subscribers run first. A durable note can therefore journal
        # the received frame before the conversational lane starts VAD/STT.
        for frame_cb in self._audio_frame_cbs:
            frame_cb(frame)
        for cb in self._audio_cbs:
            cb(pcm, message.seq)


def pack_pcm(samples: list[int]) -> bytes:
    """JSON sample integers → signed 16-bit LE bytes, the format STT wants.

    Values are clamped rather than allowed to raise: one malformed sample
    should not discard an entire 200 ms audio batch.
    """
    clamped = [max(-32768, min(32767, int(s))) for s in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)
