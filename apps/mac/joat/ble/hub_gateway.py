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
import math
import secrets
import struct
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from http import HTTPStatus
from ipaddress import ip_address
from typing import Literal
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
from ..core.pairing import PairingAuthority, PairingStore
from ..core.protocol import (
    Audio,
    AudioSource,
    Auth,
    AuthAccepted,
    AudioControl,
    BrainstormAction,
    Confirm,
    ConfirmRequest,
    ControlAction,
    Device,
    DownlinkMessage,
    G2Diagnostic,
    G2DiagnosticControl,
    Hello,
    Imu,
    Input,
    LinkPing,
    LinkPong,
    Location,
    LocationRefresh,
    LocationProbeControl,
    NavigationAction,
    NavigationOrigin,
    NavigationOriginFailure,
    NavigationRerouteRequest,
    NavigationRouteAck,
    NavigationState,
    NavigationViewAck,
    PairingAck,
    Places,
    PairingSet,
    Policy,
    ResumePolicy,
    ProtocolError,
    PromptAck,
    PromptCancel,
    PromptPresent,
    ProductivityHudView,
    ProductivityViewAck,
    ReminderDueAck,
    ReminderDueComplete,
    SavedPlace,
    CONFIRM_PROMPT_MAX_UTF16_UNITS,
    MESSAGE_REVIEW_PROMPT_MAX_UTF16_UNITS,
    PLUGIN_HEARTBEAT_LOST_CLOSE_CODE,
    PLUGIN_REPLACED_CLOSE_CODE,
    PLUGIN_SLOT_OCCUPIED_CLOSE_CODE,
    parse_uplink,
    utf16_units,
)
from ..services.g2_diagnostics import G2DiagnosticStore
from .mic_meter import MicMeter

log = logging.getLogger(__name__)
REMINDER_DUE_ACK_REPROBE_SECONDS = 30.0
REMINDER_DUE_ACK_MIN_INTERVAL_SECONDS = 5.0
# The Hub plugin gives the Even SDK five seconds for ``getAppLocation``.
# The Mac must still leave time for that result to cross phone WebView ->
# WebSocket -> gateway; matching the SDK deadline races a valid late fix.
EVEN_LOCATION_ONE_SHOT_TIMEOUT_S = 5.0
DEFAULT_LOCATION_REFRESH_TIMEOUT_S = 7.0
# The Hub plugin drops the Mac link when one Mac->plugin message has not been
# handled within this long (``downlinkHandlerTimeoutMs`` in
# apps/hub-plugin/src/MacLink.ts). From the Mac that looks like an
# authenticated connection that lives ~15 s and closes with code 1005 (the
# socket closed with no close frame), over and over: 2,352 times on
# 2026-10-08/09 while a page-part image write hung on the glasses, and every
# open question (a spoken Messages reply, a Brainstorm topic) was cancelled by
# the reconnect. Nothing in brain.log named the pattern; this does.
PLUGIN_DOWNLINK_WATCHDOG_S = 15.0
# How far a connection's life may sit from the watchdog and still count.
PLUGIN_WATCHDOG_TOLERANCE_S = 3.0
# Consecutive watchdog-shaped drops before the warning. Two can be a phone
# backgrounding its WebView twice; three in a row is the loop.
PLUGIN_WATCHDOG_LOOP_THRESHOLD = 3
# Only the same glasses can take the link from the glasses (the wearer,
# 2026-10-13: "how is it possible for you to take over my glasses when you're
# testing"). Newest-wins exists so a phone can come back after its own socket
# died; it also let any display holding the key evict the phone. While the
# link is held by a display that named its glasses, a newer one must name the
# same glasses in its hello, within this long, or it waits as a standby.
TAKEOVER_HELLO_TIMEOUT_S = 3.0
# Frames a newcomer may send before its hello while that is decided.
TAKEOVER_BUFFER_FRAMES = 64
# What the plugin sends when the phone reports no glasses.
_NO_GLASSES_SERIALS = frozenset({"", "unknown"})
# JOAT's own heartbeat, for plugins that say they keep one (Hello.heartbeat).
# The websockets library's Ping is answered by the phone's network stack, so
# it cannot tell a frozen plugin page from a live one (RFC 6455 5.5.2-5.5.3).
HEARTBEAT_INTERVAL_S = 10.0
# Silence after which the plugin page is taken for frozen or gone and its
# link is closed, so it reconnects (and re-syncs) when it wakes.
HEARTBEAT_SILENCE_S = 45.0


ImuCallback = Callable[[Imu], None]
ConfirmCallback = Callable[[Confirm], None]
HelloCallback = Callable[[Hello], None]
NavigationActionCallback = Callable[[NavigationAction], None]
BrainstormActionCallback = Callable[[BrainstormAction], None]
ControlActionCallback = Callable[[ControlAction], None]
NavigationRouteAckCallback = Callable[[NavigationRouteAck], None]
NavigationViewAckCallback = Callable[[NavigationViewAck], None]
NavigationStateCallback = Callable[[NavigationState], None]
NavigationRerouteCallback = Callable[[NavigationRerouteRequest], None]
NavigationOriginCallback = Callable[[NavigationOrigin], None]
NavigationOriginFailureCallback = Callable[[NavigationOriginFailure], None]
ProductivityViewAckCallback = Callable[[ProductivityViewAck], None]
#: The wearer tapped a due Reminder page the current client had accepted;
#: the argument is the opaque occurrence id.
ReminderDueCompleteCallback = Callable[[str], None]
PlacesCallback = Callable[[tuple[SavedPlace, ...]], None]


@dataclass(slots=True)
class _PendingConfirm:
    future: asyncio.Future[bool | None]
    session_id: str
    challenge_id: str
    payload_digest: str
    preview_digest: str
    pairing_identity_digest: bytes
    expires_at_s: float


@dataclass(slots=True)
class _PendingLocationRefresh:
    future: asyncio.Future[Location | None]
    session_id: str


@dataclass(slots=True)
class _PendingReminderDueAck:
    future: asyncio.Future[bool]
    session_id: str
    generation: int
    attempt_id: str


@dataclass(slots=True)
class _PendingPrompt:
    request: PromptPresent
    future: asyncio.Future[bool]
    on_presented: Callable[[PromptAck], bool]
    on_revoked: Callable[[], None] | None
    acknowledgment_deadline: float
    expiry: asyncio.TimerHandle | None = None


class HubGatewayTransport(GlassesTransport, AudioFrameSource):
    """Accepts one plugin connection at a time.

    One, not many: the G2 has a single wearer and a single HUD, so a second
    connection is a stale tab or a duplicate sideload, not a feature. The newest
    connection wins and the previous one is closed — reconnect-after-sleep is
    far more common than genuine concurrent use — but only when it names the
    same glasses as the connection it replaces (see TAKEOVER_HELLO_TIMEOUT_S).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8787,
        pairing: PairingAuthority | None = None,
        trust_loopback: bool = True,
        auth_timeout_s: float = 4.0,
        g2_diagnostics: G2DiagnosticStore | None = None,
        location_probe: bool = False,
        takeover_hello_timeout_s: float = TAKEOVER_HELLO_TIMEOUT_S,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        heartbeat_silence_s: float = HEARTBEAT_SILENCE_S,
    ) -> None:
        if location_probe and g2_diagnostics is None:
            raise ValueError("location_probe requires g2_diagnostics receipt storage")
        self._host = host
        self._port = port
        self._pairing = pairing
        self._trust_loopback = trust_loopback
        self._auth_timeout_s = auth_timeout_s
        self._takeover_hello_timeout_s = takeover_hello_timeout_s
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_silence_s = heartbeat_silence_s
        self._last_uplink_at = time.monotonic()
        self._g2_diagnostics = g2_diagnostics
        self._g2_diagnostic_session_marker: tuple[int, int] | None = None
        self._location_probe = location_probe
        self._auth_lock = asyncio.Lock()
        self._refusals: dict[str, int] = {}
        self._lockout_warned = False
        self._server: object | None = None
        self._conn: ServerConnection | None = None
        # Downlinks sent while no plugin was connected, since the last one was.
        self._dropped_while_disconnected = 0
        # Authenticated connections in a row that ended the way the plugin's
        # downlink watchdog ends them (~15 s, no close frame), and whether
        # the current streak has been named in the log.
        self._watchdog_shaped_drops = 0
        self._watchdog_loop_warned = False

        self._audio_cbs: list[AudioCallback] = []
        self._audio_frame_cbs: list[AudioFrameCallback] = []
        self._input_cbs: list[InputCallback] = []
        # Redacted plugin trace events (tile uploads, transitions), for the
        # latency receipt's paint stage. Only flow while diagnostics are on.
        self._trace_cbs: list[Callable[[dict[str, str | int]], None]] = []
        self._device_cbs: list[DeviceCallback] = []
        self._imu_cbs: list[ImuCallback] = []
        self._confirm_cbs: list[ConfirmCallback] = []
        self._hello_cbs: list[HelloCallback] = []
        self._navigation_action_cbs: list[NavigationActionCallback] = []
        self._brainstorm_action_cbs: list[BrainstormActionCallback] = []
        self._control_action_cbs: list[ControlActionCallback] = []
        self._navigation_route_ack_cbs: list[NavigationRouteAckCallback] = []
        self._navigation_view_ack_cbs: list[NavigationViewAckCallback] = []
        self._navigation_state_cbs: list[NavigationStateCallback] = []
        self._navigation_reroute_cbs: list[NavigationRerouteCallback] = []
        self._navigation_origin_cbs: list[NavigationOriginCallback] = []
        self._navigation_origin_failure_cbs: list[NavigationOriginFailureCallback] = []
        self._productivity_view_ack_cbs: list[ProductivityViewAckCallback] = []
        self._reminder_due_complete_cbs: list[ReminderDueCompleteCallback] = []
        self._places_cbs: list[PlacesCallback] = []
        #: Due views the current client accepted: occurrence -> (attempt, generation).
        self._accepted_reminder_dues: dict[str, tuple[str, int]] = {}
        self._disconnect_cbs: list[Callable[[], None]] = []

        self._pending_confirms: dict[str, _PendingConfirm] = {}
        self._pending_location_refreshes: dict[str, _PendingLocationRefresh] = {}
        self._pending_reminder_due_acks: dict[str, _PendingReminderDueAck] = {}
        self._reminder_due_ack_unavailable_generation: int | None = None
        self._reminder_due_ack_reprobe_at_s: float | None = None
        self._reminder_due_ack_last_attempt_generation: int | None = None
        self._reminder_due_ack_last_attempt_at_s: float | None = None
        self._reminder_due_ack_inflight_generation: int | None = None
        self._session_id: str | None = None
        self._prompt_audio_source: AudioSource = "glasses"
        self._prompt: _PendingPrompt | None = None
        self._connection_generation = 0
        self._session_pairing_identity_digest: bytes | None = None
        self._standby_refused_generation: int | None = None
        # Content-free mic timing on the real glasses (mic_meter.py).
        self._mic_meter = MicMeter(link_rtt_s=self._link_rtt_s)
        # Unlike ordinary JOAT, a field-acceptance receipt must not allow a
        # loopback simulator to stand in for an actual paired wearer session.
        self._current_peer_is_loopback = False
        self._last_seq: int | None = None
        self.device: Device | None = None
        self.hello: Hello | None = None
        #: The wearer's choice for how the mic resumes after the glasses go
        #: back on (the plugin's Settings). "mac" means this process decides:
        #: __main__ answers a worn report with AudioControl(open=True).
        self.resume_policy: ResumePolicy | None = None
        self.location: Location | None = None
        self._location_cbs: list[Callable[[Location], None]] = []
        self._location_received_at_s: float | None = None

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
        self._prompt_audio_source = "glasses"
        self._retire_prompt()
        if self._conn is not None:
            if self._session_id is not None:
                self._invalidate_confirms(self._session_id)
                self._clear_reminder_due_acks(self._session_id)
            await self._conn.close()
            self._conn = None
            self._session_id = None
            self._session_pairing_identity_digest = None
            self._current_peer_is_loopback = False
            self.hello = None
        self._clear_location()
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

    @property
    def audio_connection_generation(self) -> str | None:
        """The admitted socket identity also carried by every AudioFrame."""
        return self._session_id if self._conn is not None else None

    @property
    def prompt_audio_source(self) -> AudioSource:
        """This connection's selected mic, never evidence of a PCM frame's source."""
        return self._prompt_audio_source

    @property
    def physical_g2_connected(self) -> bool:
        """Whether the current authenticated peer is a non-loopback G2.

        This is intentionally an acceptance-evidence predicate, rather than
        a generic capability flag: an official local simulator remains useful
        for development but must never satisfy a physical-run receipt.
        """

        return bool(
            self._conn is not None
            and not self._current_peer_is_loopback
            and self.hello is not None
            and self.hello.model == "g2"
        )

    @property
    def connection_generation(self) -> int:
        """Monotonic authenticated connection generation for stale-event gates."""

        return self._connection_generation

    @property
    def g2_diagnostics(self) -> G2DiagnosticStore | None:
        """Current redacted diagnostics owner, if the wearer enabled one."""

        return self._g2_diagnostics

    async def set_g2_diagnostics(self, store: G2DiagnosticStore | None) -> None:
        """Toggle the trusted-Mac diagnostic stream without restarting JOAT.

        The plugin may receive this control after it has already connected;
        that is deliberate.  A live acceptance run must not reset STT or the
        reply model merely to begin retaining its content-free receipt.
        """

        previous = self._g2_diagnostics
        self._g2_diagnostics = store
        self._record_g2_diagnostic_session()
        connection = self._conn
        if connection is not None:
            try:
                await connection.send(
                    json.dumps(G2DiagnosticControl(enabled=store is not None).to_wire())
                )
            except websockets.ConnectionClosed:
                # Link loss is handled by the regular connection lifecycle.
                # Keep the configured owner for a paired reconnect.
                pass
        if previous is not None and previous is not store:
            await previous.flush()

    def _record_g2_diagnostic_session(self) -> None:
        """Retain the trusted-Mac physical-vs-simulator fact once per session."""

        store = self._g2_diagnostics
        if store is None or self.hello is None:
            return
        marker = (id(store), self._connection_generation)
        if marker == self._g2_diagnostic_session_marker:
            return
        store.record_session(physical_g2=self.physical_g2_connected)
        self._g2_diagnostic_session_marker = marker

    # ── subscriptions ────────────────────────────────────────────────────────

    def on_audio(self, callback: AudioCallback) -> None:
        self._audio_cbs.append(callback)

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        """Subscribe to provenance-preserving frames before legacy consumers."""
        self._audio_frame_cbs.append(callback)

    def on_input(self, callback: InputCallback) -> None:
        self._input_cbs.append(callback)

    def on_trace(self, callback: Callable[[dict[str, str | int]], None]) -> None:
        """Observe the plugin's redacted diagnostic events (already parsed).

        The protocol parser is the redaction firewall; observers see only
        its fixed-vocabulary dicts, never raw plugin fields.
        """
        self._trace_cbs.append(callback)

    def on_device(self, callback: DeviceCallback) -> None:
        self._device_cbs.append(callback)

    def on_location(self, callback: Callable[[Location], None]) -> None:
        """Observe wearer positions the phone already sends.

        The dashboard's weather rides on this: the Even Hub SDK exposes no
        weather API, so the Mac resolves conditions for this position.
        """
        self._location_cbs.append(callback)

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

    def on_brainstorm_action(self, callback: BrainstormActionCallback) -> None:
        """Receive a bounded action from the currently painted G2 surface."""
        self._brainstorm_action_cbs.append(callback)

    def on_control_action(self, callback: ControlActionCallback) -> None:
        """Receive a request from the phone's JOAT control screen.

        Same authenticated socket as the lenses, so only the paired phone
        can switch an account or reset a Brainstorm.
        """
        self._control_action_cbs.append(callback)

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

    def on_places(self, callback: PlacesCallback) -> None:
        """Observe the wearer's saved places, whole, whenever the phone sends them."""
        self._places_cbs.append(callback)

    def on_reminder_due_complete(self, callback: ReminderDueCompleteCallback) -> None:
        """Observe a tap on a due Reminder page this client had accepted.

        The tap is admitted only with the exact occurrence and attempt UUIDs
        of an acknowledged due view under the current connection generation;
        anything else is dropped without a word. Completing the reminder is
        the subscriber's business and still runs the confirmed flow.
        """
        self._reminder_due_complete_cbs.append(callback)

    def on_productivity_view_ack(self, callback: ProductivityViewAckCallback) -> None:
        """Observe a content-free verifying-view acknowledgement.

        Callers must still distinguish this current-plugin receipt from native
        delivery, physical G2 pixels, and wearer attention.
        """
        self._productivity_view_ack_cbs.append(callback)

    # ── sending ──────────────────────────────────────────────────────────────

    def _link_rtt_s(self) -> float | None:
        conn = self._conn
        latency = getattr(conn, "latency", None) if conn is not None else None
        return latency if isinstance(latency, (int, float)) else None

    async def send(self, message: DownlinkMessage) -> None:
        conn = self._conn
        if isinstance(message, AudioControl) and conn is not None:
            self._mic_meter.on_control(message.open)
        if conn is None:
            # One line per disconnected spell, then a count when the glasses
            # return: logging every frame wrote ~1,500 identical warnings in
            # three days and buried everything else in brain.log.
            self._dropped_while_disconnected += 1
            if self._dropped_while_disconnected == 1:
                log.warning(
                    "dropping %s — no plugin connected (further drops are counted "
                    "until the glasses connect)",
                    type(message).__name__,
                )
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

    async def request_prompt(
        self,
        request: PromptPresent,
        *,
        on_presented: Callable[[PromptAck], bool],
        on_revoked: Callable[[], None] | None = None,
        timeout_s: float = 5.0,
    ) -> bool:
        """Present one question and activate its owner inside ack dispatch.

        The callback must synchronously establish the host ingress fence and
        arm its typed owner. Resolving a future alone would race following
        audio. A successful receipt remains owned until cancellation/expiry.
        """
        wire = request.to_wire()
        if (
            isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0
            or not callable(on_presented)
            or (on_revoked is not None and not callable(on_revoked))
        ):
            raise ValueError("invalid prompt publication callback or timeout")
        conn = self._conn
        if conn is None or request.connection_generation != self._session_id:
            return False
        previous = self._prompt
        if previous is not None and previous.request.prompt_id == request.prompt_id:
            # A duplicate may not extend either publication or answer time.
            return False
        self._retire_prompt()
        loop = asyncio.get_running_loop()
        now = loop.time()
        lifetime = request.expires_in_ms / 1_000
        deadline = now + min(timeout_s, lifetime)
        pending = _PendingPrompt(
            request=request, future=loop.create_future(),
            on_presented=on_presented, on_revoked=on_revoked,
            acknowledgment_deadline=deadline,
        )
        self._prompt = pending
        pending.expiry = loop.call_later(lifetime, self._retire_prompt, pending)
        succeeded = False
        try:
            async with asyncio.timeout_at(deadline):
                await conn.send(json.dumps(wire))
                if self._conn is not conn or self._session_id != request.connection_generation:
                    return False
                succeeded = await asyncio.shield(pending.future)
                return succeeded
        except (TimeoutError, websockets.ConnectionClosed):
            return False
        finally:
            if not succeeded:
                self._retire_prompt(pending)
                # State is already invalid. A bounded best-effort notification
                # removes a displayed question whose publication timed out.
                await self._send_prompt_cancel(pending)

    async def cancel_prompt(self, prompt_id: str | None = None) -> bool:
        pending = self._prompt
        if pending is None or (prompt_id is not None and pending.request.prompt_id != prompt_id):
            return False
        self._retire_prompt(pending)
        await self._send_prompt_cancel(pending)
        return True

    def _retire_prompt(self, pending: _PendingPrompt | None = None) -> None:
        current = self._prompt
        if current is None or (pending is not None and current is not pending):
            return
        self._prompt = None
        if current.expiry is not None:
            current.expiry.cancel()
        if not current.future.done():
            current.future.set_result(False)
        if current.on_revoked is not None:
            try:
                current.on_revoked()
            except Exception as exc:
                log.warning("prompt revocation callback failed error=%s", type(exc).__name__)

    async def _send_prompt_cancel(self, pending: _PendingPrompt) -> None:
        conn = self._conn
        request = pending.request
        if conn is None or request.connection_generation != self._session_id:
            return
        try:
            async with asyncio.timeout(0.25):
                await conn.send(json.dumps(PromptCancel(
                    request.prompt_id, request.connection_generation,
                ).to_wire()))
        except (TimeoutError, websockets.ConnectionClosed):
            pass

    def _accept_prompt_ack(self, message: PromptAck, session_id: str) -> None:
        pending = self._prompt
        if pending is None or pending.future.done():
            return
        request = pending.request
        if (
            session_id != message.connection_generation
            or request.connection_generation != session_id
            or request.prompt_id != message.prompt_id
            or request.presentation_binding != message.presentation_binding
            or request.source != message.source
        ):
            return
        if not message.success or asyncio.get_running_loop().time() >= pending.acknowledgment_deadline:
            self._retire_prompt(pending)
            return
        try:
            accepted = pending.on_presented(message) is True
        except Exception as exc:
            log.warning("prompt activation callback failed error=%s", type(exc).__name__)
            accepted = False
        # The callback may revoke or replace its own publication. That wins.
        if self._prompt is not pending:
            return
        if not accepted:
            self._retire_prompt(pending)
            return
        pending.future.set_result(True)

    async def request_reminder_due_ack(
        self,
        view: ProductivityHudView,
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        """Wait for the active plugin to accept one typed due Reminder view.

        The acknowledgement is bound to the current admitted Hub-client
        session, its generation, and a fresh attempt UUID. It establishes
        client acceptance, not an EventKit banner, physical G2 rasterization,
        or wearer attention. A timeout or reconnect is deliberately retryable.
        """

        if (
            view.domain != "reminders"
            or view.phase != "due"
            or view.reminder_due_id is None
            or view.reminder_due_generation is not None
        ):
            raise ValueError("reminder due acknowledgement requires an unbound due view")
        conn = self._conn
        session_id = self._session_id
        if conn is None or session_id is None:
            return False
        generation = self._connection_generation
        now_s = time.monotonic()
        # A rollout-skewed client can paint an older due screen but cannot
        # produce this newer receipt. One bounded timeout suppresses further
        # sends briefly, preventing every due occurrence from serially waiting
        # and repeatedly replacing its HUD. A later reprobe keeps a supported
        # client recoverable after a transient dropped receipt.
        if self._reminder_due_ack_unavailable_generation == generation:
            retry_at_s = self._reminder_due_ack_reprobe_at_s
            if retry_at_s is not None and now_s < retry_at_s:
                return False
            self._reminder_due_ack_unavailable_generation = None
            self._reminder_due_ack_reprobe_at_s = None
        # A due view is a foreground interruption. Serialize accepted/reprobe
        # attempts per client generation so a backlog cannot replace the HUD
        # with dozens of stale reminders in one monitor pass.
        if (
            self._reminder_due_ack_last_attempt_generation == generation
            and self._reminder_due_ack_last_attempt_at_s is not None
            and now_s - self._reminder_due_ack_last_attempt_at_s
            < REMINDER_DUE_ACK_MIN_INTERVAL_SECONDS
        ):
            return False
        if self._reminder_due_ack_inflight_generation == generation:
            return False
        reminder_due_id = view.reminder_due_id
        if reminder_due_id in self._pending_reminder_due_acks:
            return False
        attempt_id = str(uuid.uuid4())
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        pending = _PendingReminderDueAck(
            future=future,
            session_id=session_id,
            generation=generation,
            attempt_id=attempt_id,
        )
        self._pending_reminder_due_acks[reminder_due_id] = pending
        self._reminder_due_ack_last_attempt_generation = generation
        self._reminder_due_ack_last_attempt_at_s = now_s
        self._reminder_due_ack_inflight_generation = generation
        outbound = replace(
            view,
            reminder_due_attempt_id=attempt_id,
            reminder_due_generation=generation,
        )
        try:
            await conn.send(json.dumps(outbound.to_wire()))
            if self._conn is not conn or self._session_id != session_id:
                return False
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.01, min(timeout_s, 10.0)),
            )
        except asyncio.TimeoutError:
            if self._connection_generation == generation:
                self._reminder_due_ack_unavailable_generation = generation
                self._reminder_due_ack_reprobe_at_s = (
                    time.monotonic() + REMINDER_DUE_ACK_REPROBE_SECONDS
                )
            return False
        except websockets.ConnectionClosed:
            return False
        finally:
            current = self._pending_reminder_due_acks.get(reminder_due_id)
            if current is pending:
                self._pending_reminder_due_acks.pop(reminder_due_id, None)
                if not future.done():
                    future.cancel()
            if self._reminder_due_ack_inflight_generation == generation:
                self._reminder_due_ack_inflight_generation = None

    def _clear_reminder_due_acks(self, session_id: str | None = None) -> None:
        """Resolve in-flight due receipts false when their socket is gone."""

        # A due page the glass showed under a closed socket cannot be tapped
        # into completion under the next one.
        self._accepted_reminder_dues.clear()
        for reminder_due_id, pending in tuple(self._pending_reminder_due_acks.items()):
            if session_id is not None and pending.session_id != session_id:
                continue
            self._pending_reminder_due_acks.pop(reminder_due_id, None)
            if not pending.future.done():
                pending.future.set_result(False)

    def fresh_location(self, *, max_age_s: float = 120.0) -> Location | None:
        """Return only a connected, recently received, plausibly fresh phone fix."""
        received_at_s = self._location_received_at_s
        if self._conn is None or self.location is None or received_at_s is None:
            return None
        if time.monotonic() - received_at_s > max_age_s:
            return None
        # The SDK timestamp is optional.  When it is a plausible Unix
        # millisecond timestamp, reject a stale fix even if it was replayed to
        # this process moments ago.  Unknown timestamp formats fall back to
        # the authenticated delivery time rather than inventing a conversion.
        timestamp_ms = self.location.timestamp_ms
        now_ms = int(time.time() * 1000)
        earliest_ms = now_ms - int(max_age_s * 1000)
        if timestamp_ms is not None and 1_000_000_000_000 <= timestamp_ms < earliest_ms:
            return None
        return self.location

    async def request_location_refresh(
        self,
        *,
        timeout_s: float = DEFAULT_LOCATION_REFRESH_TIMEOUT_S,
    ) -> Location | None:
        """Ask the plugin for one high-accuracy fix bound to this socket session.

        ``timeout_s`` includes both the phone SDK call and relay delivery.  It
        is deliberately longer than the plugin's five-second SDK request.
        """
        conn = self._conn
        session_id = self._session_id
        if conn is None or session_id is None:
            return None
        request_id = f"location-{secrets.token_urlsafe(12)}"
        future: asyncio.Future[Location | None] = asyncio.get_running_loop().create_future()
        self._pending_location_refreshes[request_id] = _PendingLocationRefresh(
            future=future,
            session_id=session_id,
        )
        try:
            await self.send(LocationRefresh(request_id=request_id))
            return await asyncio.wait_for(asyncio.shield(future), timeout=max(0.01, timeout_s))
        except asyncio.TimeoutError:
            return None
        finally:
            pending = self._pending_location_refreshes.pop(request_id, None)
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    def _clear_location(self, session_id: str | None = None) -> None:
        """Drop cached coordinates and release refresh waiters for one session."""
        self.location = None
        self._location_received_at_s = None
        for request_id, pending in tuple(self._pending_location_refreshes.items()):
            if session_id is not None and pending.session_id != session_id:
                continue
            self._pending_location_refreshes.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result(None)

    async def request_confirm(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float = 8.0,
        *,
        payload_digest: str | None = None,
        diagnostic_kind: Literal["messages"] | None = None,
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
            diagnostic_kind=diagnostic_kind,
        )
        return result.accepted if result is not None else False

    async def request_confirmation(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float = 8.0,
        *,
        payload_digest: str | None = None,
        diagnostic_kind: Literal["messages"] | None = None,
        message_ui_action_id: str | None = None,
        purpose: Literal["terminal_input"] | None = None,
    ) -> ConfirmationResult | None:
        """Return durable evidence only for an explicit bound HUD decision."""

        # A Messages draft is drawn in JOAT's box, which holds more than the
        # native confirmation page; the box's own cap applies to it.
        prompt_cap = (
            MESSAGE_REVIEW_PROMPT_MAX_UTF16_UNITS if diagnostic_kind == "messages"
            else CONFIRM_PROMPT_MAX_UTF16_UNITS
        )
        if utf16_units(prompt) > prompt_cap:
            log.warning(
                "confirm %s declined — exact preview exceeds HUD capacity",
                action_id,
            )
            return None
        if diagnostic_kind not in (None, "messages"):
            log.warning("confirm %s declined — diagnostic kind is invalid", action_id)
            return None
        if purpose not in (None, "terminal_input"):
            log.warning("confirm %s declined — purpose is invalid", action_id)
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
                diagnostic_kind=diagnostic_kind,
                message_ui_action_id=message_ui_action_id,
                purpose=purpose,
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

    @staticmethod
    def _peer_is_loopback(conn: ServerConnection) -> bool:
        addr = conn.remote_address
        try:
            return bool(ip_address(addr[0]).is_loopback)
        except (ValueError, IndexError, TypeError):
            return False

    @staticmethod
    def _origin_is_local(conn: ServerConnection) -> bool:
        """No Origin (a native client: the harnesses, speak_test.py) or a page
        served from this Mac itself (the simulator loads the dev server)."""
        request = conn.request
        origin = request.headers.get("Origin") if request is not None else None
        if origin is None:
            return True
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.hostname is None:
            return False
        if parts.hostname == "localhost":
            return True
        try:
            return bool(ip_address(parts.hostname).is_loopback)
        except ValueError:
            return False

    def _peer_is_exempt(self, conn: ServerConnection) -> bool:
        """Loopback peers skip pairing: the simulator and test harnesses run
        on this Mac, and a local process could read the token file anyway —
        the boundary being enforced is the network, not the machine.

        A browser is the exception the network boundary misses: any web page
        open on this Mac can dial ws://127.0.0.1:8787 and its socket arrives
        from loopback. Since the phone screen can sign accounts in and out
        (2026-10-05), an exempt page could finish a sign-in with its own
        account and receive the wearer's turns. A browser always names the
        page in Origin, so a foreign Origin pairs like any other peer.
        """
        if not self._trust_loopback:
            return False
        return self._peer_is_loopback(conn) and self._origin_is_local(conn)

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
        try:
            self._pairing.begin_pairing()
        except PermissionError:
            # Under JOAT.app only its Pair Glasses opens a window.
            return False
        self._refusals.clear()
        self._lockout_warned = False
        log.info("pairing window opened locally for 120 seconds")
        return True

    @staticmethod
    def _is_standby_path(path: str) -> bool:
        return "1" in parse_qs(urlsplit(path).query).get("standby", [])

    def _standby_refused(self, path: str, conn: ServerConnection | None = None) -> bool:
        """A standby dial is a display another one replaced, asking whether
        the glasses are free. It may take them only when no display holds
        them: newest-wins would otherwise let it kick the live one, and the
        two would trade the glasses back and forth."""
        if not self._is_standby_path(path):
            return False
        current = self._conn
        if current is None or current is conn:
            return False
        if self._standby_refused_generation != self._connection_generation:
            self._standby_refused_generation = self._connection_generation
            log.info("standby display refused — another display holds the glasses")
        return True

    async def _accept(self, conn: ServerConnection) -> bool:
        """Send auth.accepted, unless this is a standby dial and the slot is
        held (checked again here: the upgrade-time check can race a newer
        arrival)."""
        request = conn.request
        if request is not None and self._standby_refused(request.path, conn):
            await conn.close(code=PLUGIN_SLOT_OCCUPIED_CLOSE_CODE, reason="glasses held by another display")
            return False
        await conn.send(json.dumps(AuthAccepted().to_wire()))
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
        if self._standby_refused(request.path):
            return conn.respond(HTTPStatus.CONFLICT, "another display holds the glasses\n")
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

    async def _authenticate(
        self, conn: ServerConnection,
    ) -> tuple[bytes | None, str | bytes | None]:
        """Authenticate before the peer can replace the active HUD session."""
        if self._pairing is None:
            # Local/test deployments intentionally have no pairing store, but
            # a real MacLink still sends its mandatory auth frame and waits
            # for an acknowledgement. Preserve compatibility with older raw
            # test clients that begin immediately with hello by buffering that
            # first non-auth frame for the normal dispatcher.
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=0.05)
            except asyncio.TimeoutError:
                return self._pairing_identity(None), None
            try:
                payload = json.loads(raw)
                first = parse_uplink(payload)
            except (json.JSONDecodeError, ProtocolError, TypeError):
                return self._pairing_identity(None), raw
            if isinstance(first, Auth):
                if not await self._accept(conn):
                    return None, None
                return self._pairing_identity(None), None
            return self._pairing_identity(None), raw
        if self._peer_is_exempt(conn):
            if not await self._accept(conn):
                return None, None
            return self._pairing_identity(None), None

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
                        return None, None
                    if not await self._accept(conn):
                        return None, None
                    return self._pairing_identity(expected), None

                if not self._pairing.window_open:
                    await conn.close(code=4403, reason="pairing window closed")
                    return None, None

                if first.token is not None:
                    # Recovery adoption is safe only because a local user
                    # explicitly opened this bounded window.
                    self._pairing.confirm(first.token)
                    log.info("pairing: adopted a persisted peer token — window closed")
                    if not await self._accept(conn):
                        return None, None
                    return self._pairing_identity(first.token), None

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
                    return None, None
                self._pairing.confirm(candidate)
                log.info("pairing: confirmed — remote peers now require auth")
                if not await self._accept(conn):
                    return None, None
                return self._pairing_identity(candidate), None
            except (asyncio.TimeoutError, ProtocolError, websockets.ConnectionClosed):
                try:
                    await conn.close(code=4401, reason="authentication required")
                except websockets.ConnectionClosed:
                    pass
            return None, None

    # ── receiving ────────────────────────────────────────────────────────────

    @staticmethod
    def _glasses(hello: Hello | None) -> str | None:
        """The glasses a hello names, or None when it names none."""
        if hello is None:
            return None
        serial = hello.sn.strip()
        return None if serial.casefold() in _NO_GLASSES_SERIALS else serial

    @staticmethod
    def _parse_hello(raw: str | bytes) -> Hello | None:
        if not isinstance(raw, str):
            return None
        try:
            message = parse_uplink(json.loads(raw))
        except (json.JSONDecodeError, ProtocolError, TypeError, ValueError):
            return None
        return message if isinstance(message, Hello) else None

    async def _newcomer_hello(
        self, conn: ServerConnection, frames: list[str | bytes],
    ) -> Hello | None:
        """Read a newcomer's frames, in order, until its hello or the timeout.

        Every frame read is kept in ``frames`` so an admitted newcomer loses
        nothing it sent while this was decided.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._takeover_hello_timeout_s
        checked = 0
        while True:
            for raw in frames[checked:]:
                hello = self._parse_hello(raw)
                if hello is not None:
                    return hello
            checked = len(frames)
            remaining = deadline - loop.time()
            if remaining <= 0 or len(frames) >= TAKEOVER_BUFFER_FRAMES:
                return None
            try:
                frames.append(await asyncio.wait_for(conn.recv(), timeout=remaining))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                return None

    async def _may_take_over(
        self, conn: ServerConnection, frames: list[str | bytes],
    ) -> bool:
        """Whether this authenticated newcomer may replace the current link.

        A link held by a display that named its glasses is handed only to a
        display naming the same glasses: the phone coming back, or the phone's
        own second page. Anything else (a simulator, a test harness, another
        phone) waits as a standby until the glasses' link is gone.
        """
        if self._conn is None or self._glasses(self.hello) is None:
            return True
        hello = await self._newcomer_hello(conn, frames)
        holder = self._glasses(self.hello) if self._conn is not None else None
        if holder is None:
            return True
        newcomer = self._glasses(hello)
        if newcomer == holder:
            return True
        log.warning(
            "refused a takeover: %s asked for the link while glasses %s hold it",
            f"a display naming glasses {newcomer}" if newcomer else "a display naming no glasses",
            holder,
        )
        try:
            await conn.close(
                code=PLUGIN_REPLACED_CLOSE_CODE, reason="glasses held by another device",
            )
        except websockets.ConnectionClosed:
            pass
        return False

    async def _session(self, conn: ServerConnection) -> None:
        pairing_identity_digest, initial_raw = await self._authenticate(conn)
        if pairing_identity_digest is None:
            return
        pending: list[str | bytes] = [] if initial_raw is None else [initial_raw]
        if not await self._may_take_over(conn, pending):
            return
        session_id = secrets.token_urlsafe(18)
        previous = self._conn
        if previous is not None:
            log.info("replacing existing plugin connection")
            self._retire_prompt()
            if self._session_id is not None:
                self._invalidate_confirms(self._session_id)
                self._clear_location(self._session_id)
                self._clear_reminder_due_acks(self._session_id)
        # Publish the authenticated owner without yielding. Another arrival
        # must replace this connection, not race us to close the same old one.
        # The previous handler's finally block can no longer clear our state.
        self._conn = conn
        self._session_id = session_id
        self._prompt_audio_source = "glasses"
        self._connection_generation += 1
        self._reminder_due_ack_unavailable_generation = None
        self._reminder_due_ack_reprobe_at_s = None
        self._reminder_due_ack_last_attempt_generation = None
        self._reminder_due_ack_last_attempt_at_s = None
        self._reminder_due_ack_inflight_generation = None
        self._session_pairing_identity_digest = pairing_identity_digest
        self._current_peer_is_loopback = self._peer_is_loopback(conn)
        self._last_seq = None
        self.hello = None
        self._clear_location()
        host = str(conn.remote_address[0]) if conn.remote_address else "?"
        self._refusals.pop(host, None)
        self._lockout_warned = False
        connected_at = time.monotonic()
        self._last_uplink_at = connected_at
        log.info("plugin connected")
        if self._dropped_while_disconnected:
            log.info(
                "dropped %d downlinks while no plugin was connected",
                self._dropped_while_disconnected,
            )
            self._dropped_while_disconnected = 0
        try:
            if previous is not None:
                await previous.close(
                    code=PLUGIN_REPLACED_CLOSE_CODE,
                    reason="plugin connection replaced",
                )
            # Closing can yield long enough for a newer authenticated arrival.
            # Never resume this superseded handler's startup or receive loop.
            if self._conn is not conn:
                return
            for raw in pending:
                self._dispatch(raw, session_id)
            if self._g2_diagnostics is not None:
                await conn.send(json.dumps(G2DiagnosticControl(enabled=True).to_wire()))
            if self._conn is not conn:
                return
            if self._location_probe:
                await conn.send(json.dumps(LocationProbeControl(enabled=True).to_wire()))
            if self._conn is not conn:
                return
            heartbeat = asyncio.create_task(self._heartbeat(conn, session_id))
            try:
                async for raw in conn:
                    self._dispatch(raw, session_id)
            finally:
                heartbeat.cancel()
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._conn is conn:
                self._retire_prompt()
                self._invalidate_confirms(session_id)
                self._conn = None
                self._session_id = None
                self._prompt_audio_source = "glasses"
                self._session_pairing_identity_digest = None
                self._current_peer_is_loopback = False
                self.hello = None
                self._clear_location(session_id)
                self._clear_reminder_due_acks(session_id)
                self._mic_meter.on_disconnect()
                for cb in self._disconnect_cbs:
                    cb()
            # Say HOW it ended. Every disconnect in the wearer's log read
            # "plugin disconnected" and nothing else, so the early-morning
            # drops on 2026-09-05 (audio gaps, then a 7-minute absence) had
            # no explanation on record. The close code and reason are the
            # WebSocket peer's own words; the duration says whether this was
            # a stable link that dropped or a dial that never settled.
            lifetime_s = time.monotonic() - connected_at
            close_code = getattr(conn, "close_code", None)
            log.info(
                "plugin disconnected code=%s reason=%r after=%.1fs replaced=%s",
                close_code,
                getattr(conn, "close_reason", None) or "",
                lifetime_s,
                self._conn is not None and self._conn is not conn,
            )
            self._note_link_end(lifetime_s, close_code)

    async def _heartbeat(self, conn: ServerConnection, session_id: str) -> None:
        """Ping a plugin that keeps JOAT's heartbeat; close it when it goes quiet.

        Any frame from the plugin counts as a sign of life. When this Mac's
        own loop stalled, frames that arrived meanwhile are still queued, so
        the silence is ours, not the plugin's: the clock restarts.
        """
        seq = 0
        last_tick = time.monotonic()
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            if self._conn is not conn or self._session_id != session_id:
                return
            now = time.monotonic()
            if now - last_tick > 2 * self._heartbeat_interval_s:
                self._last_uplink_at = now
            last_tick = now
            if self.hello is None or not self.hello.heartbeat:
                continue
            silent = now - self._last_uplink_at
            if silent > self._heartbeat_silence_s:
                log.warning("no heartbeat from the plugin for %.0fs — closing its link", silent)
                try:
                    await conn.close(
                        code=PLUGIN_HEARTBEAT_LOST_CLOSE_CODE, reason="no heartbeat",
                    )
                except websockets.ConnectionClosed:
                    pass
                return
            seq += 1
            try:
                await conn.send(json.dumps(LinkPing(seq=seq).to_wire()))
            except websockets.ConnectionClosed:
                return

    def _note_link_end(self, lifetime_s: float, close_code: object) -> None:
        """Name the plugin's downlink-watchdog loop when the link keeps dying
        on its beat. Content-free: a duration and a close code only."""
        watchdog_shaped = (
            isinstance(lifetime_s, (int, float))
            and abs(lifetime_s - PLUGIN_DOWNLINK_WATCHDOG_S) <= PLUGIN_WATCHDOG_TOLERANCE_S
            # 1005: the peer closed the TCP socket without a WebSocket close
            # frame, which is how MacLink.expireSocket ends a connection.
            and close_code in (1005, 1006)
        )
        if not watchdog_shaped:
            if self._watchdog_loop_warned and self._watchdog_shaped_drops:
                log.info(
                    "plugin link held past the watchdog after %d watchdog-shaped drops",
                    self._watchdog_shaped_drops,
                )
            self._watchdog_shaped_drops = 0
            self._watchdog_loop_warned = False
            return
        self._watchdog_shaped_drops += 1
        if (
            self._watchdog_shaped_drops >= PLUGIN_WATCHDOG_LOOP_THRESHOLD
            and not self._watchdog_loop_warned
        ):
            self._watchdog_loop_warned = True
            log.warning(
                "plugin link dropped %d times in a row about %.0f s after connecting "
                "(code %s): the plugin's downlink watchdog is firing because one "
                "Mac->plugin message is not handled in time on the glasses. Every open "
                "question (a spoken reply, a topic) is cancelled by each reconnect. "
                "Turn on Record G2 diagnostics and run scripts/reply_trace.py to see "
                "which write hangs.",
                self._watchdog_shaped_drops, PLUGIN_DOWNLINK_WATCHDOG_S, close_code,
            )

    def _dispatch(self, raw: str | bytes, session_id: str) -> None:
        # The newest authenticated WebSocket owns the HUD.  Its predecessor
        # can still have a buffered frame scheduled on this event loop after
        # replacement; that frame must never operate the replacement surface.
        if session_id != self._session_id:
            log.debug("dropping stale plugin frame")
            return
        self._last_uplink_at = time.monotonic()
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
            case LinkPong():
                # Its arrival (above) is the whole message.
                return
            case Hello():
                self.hello = message
                log.info("device %s / %s", message.model, message.sn)
                self._record_g2_diagnostic_session()
                for hello_cb in self._hello_cbs:
                    hello_cb(message)
            case Audio():
                self._handle_audio(message, session_id)
            case PromptAck():
                self._accept_prompt_ack(message, session_id)
            case PromptCancel():
                pending_prompt = self._prompt
                if (
                    pending_prompt is not None
                    and message.connection_generation == session_id == pending_prompt.request.connection_generation
                    and message.prompt_id == pending_prompt.request.prompt_id
                ):
                    self._retire_prompt(pending_prompt)
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
                self._mic_meter.on_device(message.mic)
                if message.mic_source is not None:
                    self._prompt_audio_source = message.mic_source
                if message.wearing is False or (message.mic is False and message.mic_prompt_id is None):
                    self._retire_prompt()
                elif (
                    message.mic is False and self._prompt is not None
                    and self._prompt.request.prompt_id == message.mic_prompt_id
                ):
                    self._retire_prompt(self._prompt)
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
                self.resume_policy = message.resume
            case Places():
                log.info("wearer saved places: %d", len(message.places))
                for places_cb in self._places_cbs:
                    places_cb(message.places)
            case Location():
                first = self.location is None
                self.location = message
                self._location_received_at_s = time.monotonic()
                if message.refresh_id is not None:
                    location_pending = self._pending_location_refreshes.get(message.refresh_id)
                    if (
                        location_pending is not None
                        and location_pending.session_id == session_id
                        and not location_pending.future.done()
                    ):
                        location_pending.future.set_result(message)
                # The resting dashboard's weather comes from here: the SDK
                # has no weather API, so the Mac resolves it for the
                # position the phone already sends.
                for location_cb in self._location_cbs:
                    try:
                        location_cb(message)
                    except Exception:
                        log.warning("location observer failed", exc_info=False)
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
                for trace_cb in self._trace_cbs:
                    try:
                        trace_cb(message.event)
                    except Exception:
                        log.warning("trace observer failed", exc_info=False)
            case BrainstormAction():
                # The plugin cannot choose this generation on the wire.  The
                # gateway attaches it only after selecting the authenticated
                # current connection, so an acceptance session can reject
                # stale gestures after a reconnect.
                message = replace(
                    message,
                    connection_generation=self._connection_generation,
                )
                for brainstorm_cb in self._brainstorm_action_cbs:
                    brainstorm_cb(message)
            case ControlAction():
                # Logged by action only: a pasted sign-in code rides some
                # of these and must never reach a log line.
                log.info("control action: %s", message.action)
                for control_cb in self._control_action_cbs:
                    try:
                        control_cb(message)
                    except Exception:
                        log.warning("control action observer failed", exc_info=False)
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
            case ReminderDueAck():
                reminder_pending = self._pending_reminder_due_acks.get(
                    message.reminder_due_id,
                )
                if (
                    reminder_pending is not None
                    and reminder_pending.session_id == session_id == self._session_id
                    and reminder_pending.generation == message.generation == self._connection_generation
                    and reminder_pending.attempt_id == message.reminder_due_attempt_id
                    and not reminder_pending.future.done()
                ):
                    reminder_pending.future.set_result(True)
                    self._accepted_reminder_dues[message.reminder_due_id] = (
                        message.reminder_due_attempt_id, message.generation,
                    )
            case ReminderDueComplete():
                accepted = self._accepted_reminder_dues.get(message.reminder_due_id)
                if (
                    accepted is not None
                    and session_id == self._session_id
                    and accepted == (message.reminder_due_attempt_id, message.generation)
                    and message.generation == self._connection_generation
                ):
                    # One tap completes one occurrence once.
                    self._accepted_reminder_dues.pop(message.reminder_due_id, None)
                    for reminder_due_complete_cb in self._reminder_due_complete_cbs:
                        reminder_due_complete_cb(message.reminder_due_id)
                else:
                    log.info("reminder due tap ignored reason=binding-mismatch")
            case ProductivityViewAck():
                if message.generation == self._connection_generation:
                    for productivity_view_ack_cb in self._productivity_view_ack_cbs:
                        productivity_view_ack_cb(message)

    def _handle_audio(self, message: Audio, session_id: str) -> None:
        if self._last_seq is not None and message.seq != self._last_seq + 1:
            # A discontinuity after plugin batching, including an explicit
            # mic pause. Sequence alone cannot identify loss at the SDK,
            # relay, or queue; forward the boundary so VAD cannot splice it.
            log.warning("audio gap: seq %d → %d", self._last_seq, message.seq)
        self._last_seq = message.seq
        self._mic_meter.on_audio(len(message.pcm))

        pcm = pack_pcm(message.pcm)
        frame = AudioFrame(
            pcm=pcm,
            connection_generation=session_id,
            seq=message.seq,
            source=message.source,
            speaker=message.speaker,
            received_at_us=time.time_ns() // 1_000,
            audio_epoch=message.audio_epoch,
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
