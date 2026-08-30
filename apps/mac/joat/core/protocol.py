"""Python mirror of apps/hub-plugin/src/types.ts.

The two ends of the WebSocket must agree exactly on message shapes. Keeping
this as hand-written dataclasses (rather than raw dicts) means a protocol
change breaks at parse time with a clear error, instead of surfacing as a
KeyError three layers into the orchestrator.

Wire format is JSON with a `kind` discriminator, matching the TS unions.
When you change types.ts, change this file in the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum
import math
from typing import Any, Literal
from uuid import UUID

# ── Constants mirrored from the SDK ──────────────────────────────────────────

CANVAS_WIDTH = 576
CANVAS_HEIGHT = 288
CANVAS_GREY_LEVELS = 16
CONFIRM_PROMPT_MAX_UTF16_UNITS = 120


def utf16_units(text: str) -> int:
    """Match JavaScript String.length so the plugin cannot truncate a preview."""
    return len(text.encode("utf-16-le")) // 2


class ContainerId(IntEnum):
    """Stable across rebuilds so text upgrades can target them."""

    STATUS_BAR = 1
    MAIN = 2
    LIST = 3
    IMAGE = 4
    # Dashboard regions (docs/HUD_DASHBOARD.md): each has ONE owner, so
    # content stops colliding — the reply no longer erases the wearer's
    # words, and a card no longer erases the reply.
    CONTEXT = 5   # what the wearer last said
    TRAY = 6      # what is waiting (queued cards, counts)
    HINT = 7      # gesture hints, plugin-owned


class EventSource(IntEnum):
    """SDK EventSourceType. Note LEFT is 3, not 2 — 2 is the ring."""

    DUMMY = 0
    GLASSES_RIGHT = 1
    RING = 2
    GLASSES_LEFT = 3


class EventType(IntEnum):
    """SDK OsEventTypeList."""

    CLICK = 0
    SCROLL_TOP = 1
    SCROLL_BOTTOM = 2
    DOUBLE_CLICK = 3
    FOREGROUND_ENTER = 4
    FOREGROUND_EXIT = 5
    ABNORMAL_EXIT = 6
    SYSTEM_EXIT = 7
    IMU_DATA_REPORT = 8


ResumePolicy = Literal["auto", "mac", "confirm"]
AudioSource = Literal["glasses", "phone"]

RESUME_POLICIES: tuple[str, ...] = ("auto", "mac", "confirm")
NAVIGATION_ACTIONS: tuple[str, ...] = (
    "confirm_destination",
    "retry_destination",
    "select_destination",
    "select_mode",
    "start",
    "change_mode",
    "change_destination",
    "finish",
    "retry",
    "cancel",
)
BRAINSTORM_ACTIONS: tuple[str, ...] = (
    "begin_acceptance",
    "begin_topic_capture",
    "show_terminal",
    "show_brainstorm",
    "cancel",
    "retry_terminal",
)


# ── Uplink: plugin → Mac ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Auth:
    """First application frame on every protected socket."""

    token: str | None


@dataclass(frozen=True, slots=True)
class Hello:
    sn: str
    model: str
    launch_source: str
    user_name: str = ""
    """From the host's getUserInfo — grounds the wearer's name without
    configuration. Optional: older hosts omit it."""


@dataclass(frozen=True, slots=True)
class Audio:
    """A batch of PCM samples: 16 kHz, signed 16-bit LE, mono.

    `seq` is monotonic per plugin session. A gap means frames were dropped
    (MacLink sheds the oldest when the socket is down) — the STT layer should
    treat a gap as a discontinuity rather than splicing across it.
    """

    source: AudioSource
    seq: int
    pcm: list[int]
    speaker: str | None = None
    """SDK 0.0.14 speaker attribution: self / other / unknown. Logged for
    calibration now; a future gate keeps non-wearer speech out of STT,
    which STRENGTHENS the explicit-invocation rule."""


@dataclass(frozen=True, slots=True)
class Imu:
    x: float
    y: float
    z: float
    at: int


@dataclass(frozen=True, slots=True)
class InputEvent:
    container_id: int | None = None
    container_name: str | None = None
    event_type: int | None = None
    event_source: int | None = None
    selected_index: int | None = None
    selected_name: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    planning_anchor_unix_ms: int | None = None

    @property
    def is_right_temple_click(self) -> bool:
        return (
            self.event_source == EventSource.GLASSES_RIGHT
            and self.event_type == EventType.CLICK
        )

    @property
    def is_ring_click(self) -> bool:
        return self.event_source == EventSource.RING and self.event_type == EventType.CLICK


@dataclass(frozen=True, slots=True)
class Input:
    event: InputEvent


@dataclass(frozen=True, slots=True)
class Device:
    connect: str
    battery: int | None = None
    wearing: bool | None = None
    charging: bool | None = None
    mic: bool | None = None
    """The plugin's ACTUAL mic state. The status pill used to latch
    'streaming' at hello and never verify — a mic that failed to start was
    indistinguishable from a working one (observed live: a whole session
    with zero audio while the dashboard said streaming)."""


@dataclass(frozen=True, slots=True)
class Confirm:
    action_id: str
    accepted: bool
    challenge_id: str | None = None
    session_id: str | None = None
    payload_digest: str | None = None
    preview_digest: str | None = None


@dataclass(frozen=True, slots=True)
class Policy:
    resume: ResumePolicy


@dataclass(frozen=True, slots=True)
class Location:
    """Wearer position from the phone (bridge.getAppLocation — the
    sideload-safe API). Powers weather-without-asking and, later, timezone
    correctness when the wearer travels."""

    latitude: float
    longitude: float
    accuracy: float | None = None
    # The optional phone fix time supplements the Mac's monotonic receive
    # time, so an old location cannot become fresh merely by being replayed.
    timestamp_ms: int | None = None
    # Present only when answering a Mac-requested one-shot refresh.
    refresh_id: str | None = None


@dataclass(frozen=True, slots=True)
class PairingAck:
    """Plugin confirms it PERSISTED a freshly issued token.

    Without the ack the Mac would persist a token the phone may never have
    stored (a dropped frame mid-pairing), permanently 401-ing the wearer's
    own phone. The plugin acks only after its localStorage write succeeds,
    so this is proof of durability, not just delivery.
    """

    token: str


@dataclass(frozen=True, slots=True)
class G2Diagnostic:
    """A content-free, allowlisted real-G2 support event."""

    event: dict[str, str | int]


@dataclass(frozen=True, slots=True)
class NavigationAction:
    """A semantic gesture bound to the exact navigation view it acted on."""

    session_id: str
    request_id: str
    generation: int
    action: str
    choice_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrainstormAction:
    """A bounded wearer gesture while the Brainstorm HUD owns input."""

    action: str
    surface: Literal["brainstorm"] | None = None
    source: Literal["right", "ring"] | None = None
    gesture: Literal["double_press"] | None = None
    # Set only by HubGatewayTransport after the authenticated WebSocket has
    # been selected as the current wearer connection.  It is deliberately not
    # a wire field: a plugin must never be able to nominate a generation.
    connection_generation: int | None = None


@dataclass(frozen=True, slots=True)
class NavigationRouteAck:
    session_id: str
    route_id: str
    revision: int
    outcome: Literal["installed", "rejected"]


@dataclass(frozen=True, slots=True)
class NavigationViewAck:
    session_id: str
    request_id: str
    generation: int
    outcome: Literal["bridge_acknowledged", "rejected"]


@dataclass(frozen=True, slots=True)
class NavigationState:
    session_id: str
    route_id: str
    revision: int
    sequence: int
    state: Literal["guidance", "uncertain", "wrong_way", "off_route", "arrived"]


@dataclass(frozen=True, slots=True)
class NavigationRerouteRequest:
    session_id: str
    route_id: str
    base_revision: int
    request_id: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class NavigationOrigin:
    session_id: str
    request_id: str
    generation: int
    latitude: float
    longitude: float
    accuracy: float | None = None


@dataclass(frozen=True, slots=True)
class NavigationOriginFailure:
    session_id: str
    request_id: str
    generation: int
    reason: Literal["unavailable"]


@dataclass(frozen=True, slots=True)
class ReminderDueAck:
    """Plugin acknowledgement of a content-free Reminder due presentation.

    This proves that the current admitted Hub client accepted the typed
    due-view operation.
    It does not prove a native Reminders banner, physical G2 pixels, or wearer
    attention; those are deliberately separate delivery boundaries.
    """

    reminder_due_id: str
    reminder_due_attempt_id: str
    generation: int
    outcome: Literal["accepted"]


UplinkMessage = (
    Auth | Hello | Audio | Imu | Input | Device | Confirm | Policy | PairingAck
    | Location | G2Diagnostic | BrainstormAction | NavigationAction | NavigationRouteAck
    | NavigationViewAck | NavigationState | NavigationRerouteRequest
    | NavigationOrigin | NavigationOriginFailure | ReminderDueAck
)


# ── Downlink: Mac → plugin ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuthAccepted:
    def to_wire(self) -> dict[str, Any]:
        return {"kind": "auth.accepted"}


@dataclass(frozen=True, slots=True)
class HudText:
    text: str
    container_id: ContainerId = ContainerId.MAIN
    offset: int | None = None
    length: int | None = None
    append: bool = False
    """append=True streams onto the container (debate turns, token streams);
    False repaints it. Mirrors types.ts hud.text."""
    text_color: int | None = None
    """Firmware brightness 0-4 per paint (SDK 0.0.14) — urgency can be
    BRIGHT now, not merely punctuated."""
    content_kind: Literal["assistant", "notification", "calendar", "system"] | None = None
    """Semantic provenance for MAIN. The plugin must never guess that an
    incoming card is an assistant reply merely because both share a region."""
    trace_id: str | None = None
    """Privacy-safe correlation id. Content never belongs in an audit key."""

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "kind": "hud.text",
            "containerId": int(self.container_id),
            "text": self.text,
        }
        if self.offset is not None:
            msg["offset"] = self.offset
        if self.length is not None:
            msg["length"] = self.length
        if self.append:
            msg["append"] = True
        if self.text_color is not None:
            msg["textColor"] = self.text_color
        if self.content_kind is not None:
            msg["contentKind"] = self.content_kind
        if self.trace_id is not None:
            msg["traceId"] = self.trace_id
        return msg


@dataclass(frozen=True, slots=True)
class AssistantOverlay:
    """JOAT's transient spoken response, independent from feature pages.

    This is deliberately not ``HudText(MAIN)``. Messages, Calendar,
    Navigation, Brainstorm, and Hermes each keep their own durable surface;
    the plugin composites this one transient layer above whichever one is
    currently visible, then removes it without rebuilding that surface.
    """

    text: str = ""
    operation: Literal["replace", "append", "settle", "dismiss"] = "replace"
    dismiss_after_ms: int | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.operation not in {"replace", "append", "settle", "dismiss"}:
            raise ProtocolError("assistant overlay operation is invalid")
        if self.operation in {"replace", "append"}:
            if not self.text or len(self.text) > 512:
                raise ProtocolError("assistant overlay text is invalid")
        elif self.text:
            raise ProtocolError("assistant overlay control carries no text")
        if self.dismiss_after_ms is not None and not 1_000 <= self.dismiss_after_ms <= 15_000:
            raise ProtocolError("assistant overlay dismiss timeout is invalid")
        message: dict[str, Any] = {
            "kind": "assistant.overlay",
            "operation": self.operation,
        }
        if self.operation in {"replace", "append"}:
            message["text"] = self.text
        if self.dismiss_after_ms is not None:
            message["dismissAfterMs"] = self.dismiss_after_ms
        return message


@dataclass(frozen=True, slots=True)
class HudRebuild:
    layout: Literal["default", "list", "image"] = "default"
    reason: Literal["brainstorm"] | None = None

    def to_wire(self) -> dict[str, Any]:
        message: dict[str, Any] = {"kind": "hud.rebuild", "layout": self.layout}
        if self.reason is not None:
            message["reason"] = self.reason
        return message


@dataclass(frozen=True, slots=True)
class PlanningGridCell:
    """One redacted native workload total for a Planning month day.

    This is deliberately not a title, calendar identifier, or reminder
    identifier. The glasses need only date placement and bounded event/task
    totals to avoid presenting a scheduled day as free.
    """

    day: date
    event_count: int
    reminder_count: int
    in_month: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "eventCount": min(99, max(0, self.event_count)),
            "reminderCount": min(99, max(0, self.reminder_count)),
            "inMonth": self.in_month,
        }


@dataclass(frozen=True, slots=True)
class HudList:
    items: list[str]
    title: str | None = None
    hint: str | None = None
    request_id: str | None = None
    selectable: bool = False
    planning_grid: tuple[PlanningGridCell, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        # SDK caps: 20 items, 64 chars each. Truncate here rather than let the
        # plugin silently drop the overflow.
        message: dict[str, Any] = {
            "kind": "hud.list",
            "items": [s[:64] for s in self.items[:20]],
            "selectable": self.selectable,
        }
        if self.title is not None:
            message["title"] = self.title[:46]
        if self.hint is not None:
            message["hint"] = self.hint[:46]
        if self.request_id is not None:
            message["requestId"] = self.request_id[:128]
        if self.planning_grid is not None:
            # A Planning month has a fixed Monday-first six-week grid. Keep
            # the payload redacted and bounded even if a caller misbehaves;
            # the plugin requires exactly 42 cells before opting into the
            # grid renderer and otherwise falls back to the ordinary list.
            message["planningGrid"] = [cell.to_wire() for cell in self.planning_grid[:42]]
        return message


_BRAINSTORM_HUD_STATES = frozenset({
    "unavailable", "approval_required", "ready", "topic_capture", "acceptance_ready", "running",
    "settled", "cancelled", "failed", "blocked",
})
_PRODUCTIVITY_HUD_DOMAINS = frozenset({"calendar", "reminders"})
_PRODUCTIVITY_HUD_PHASES = frozenset({
    "verifying", "saved", "monitoring", "completed", "failed", "check", "cancelled", "not_saved", "due",
})
_PLANNING_ANCHOR_MAX_UNIX_MS = 32_503_680_000_000  # 3000-01-01T00:00:00Z


@dataclass(frozen=True, slots=True)
class BrainstormHudView:
    """Bounded, non-secret state for the four-tile Brainstorm HUD."""

    screen: Literal["brainstorm", "agent_terminal"]
    state: str
    topic: str
    collaborators: list[str]
    activity: str
    terminal_state: str
    active_speaker: str = ""
    endpoint_host: str = ""
    terminal_reason: str = ""
    no_tools_checked: bool = False
    # The diagnostic acceptance gesture is never inferred from readiness.
    # It is an explicit local-Mac opt-in and contains no wearer content.
    acceptance_armed: bool = False

    def to_wire(self) -> dict[str, Any]:
        if self.screen not in {"brainstorm", "agent_terminal"}:
            raise ProtocolError("Brainstorm HUD screen is invalid")
        if self.state not in _BRAINSTORM_HUD_STATES:
            raise ProtocolError("Brainstorm HUD state is invalid")
        if self.terminal_state not in _BRAINSTORM_HUD_STATES:
            raise ProtocolError("Brainstorm terminal state is invalid")
        if len(self.collaborators) > 3:
            raise ProtocolError("Brainstorm HUD has too many collaborators")
        return {
            "kind": "brainstorm.view",
            "brainstorm": {
                "screen": self.screen,
                "state": self.state,
                "topic": _brainstorm_text(self.topic, "topic", 96),
                "collaborators": [
                    _brainstorm_text(item, "collaborator", 48)
                    for item in self.collaborators
                ],
                "activeSpeaker": _brainstorm_text(
                    self.active_speaker, "active speaker", 64, allow_empty=True,
                ),
                "activity": _brainstorm_text(
                    self.activity, "activity", 180, allow_empty=True,
                ),
                "endpointHost": _brainstorm_text(
                    self.endpoint_host, "endpoint host", 253, allow_empty=True,
                ),
                "terminalState": self.terminal_state,
                "terminalReason": _brainstorm_text(
                    self.terminal_reason, "terminal reason", 64, allow_empty=True,
                ),
                "noToolsChecked": self.no_tools_checked,
                "acceptanceArmed": self.acceptance_armed,
            },
        }


@dataclass(frozen=True, slots=True)
class ProductivityHudView:
    """Bounded lifecycle state for one Calendar or Reminders HUD page.

    The preceding ``ConfirmRequest`` is the only interactive authority for a
    native write. This post-preview view is a safe projection of outcome.
    """

    domain: Literal["calendar", "reminders"]
    phase: Literal["verifying", "saved", "monitoring", "completed", "failed", "check", "cancelled", "not_saved", "due"]
    title: str
    primary: str
    secondary: str
    hint: str
    # An absolute instant is compact and avoids sending a title, native ID, or
    # an ambiguous local date back through the wearable transport. The Mac
    # converts it to its current planning zone when the wearer returns.
    planning_anchor_unix_ms: int | None = None
    # This is intentionally a small boolean rather than Reminder text, an
    # EventKit ID, or an OS notification claim. It is true only after both
    # native read-back and durable monitor-occurrence registration succeeded.
    monitoring_armed: bool = False
    # A one-time opaque UUID assigned by the local task-occurrence store. The
    # gateway fills the connection generation and a fresh per-attempt UUID
    # immediately before it writes the view. The client echoes all three only
    # after accepting that exact due surface.
    reminder_due_id: str | None = None
    reminder_due_attempt_id: str | None = None
    reminder_due_generation: int | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.domain not in _PRODUCTIVITY_HUD_DOMAINS:
            raise ProtocolError("Productivity HUD domain is invalid")
        if self.phase not in _PRODUCTIVITY_HUD_PHASES:
            raise ProtocolError("Productivity HUD phase is invalid")
        if self.phase == "monitoring" and self.domain != "reminders":
            raise ProtocolError("Productivity monitoring is reminders-only")
        if self.monitoring_armed and (self.domain != "reminders" or self.phase != "saved"):
            raise ProtocolError("Productivity monitoring marker requires a saved reminder")
        receipt_fields = (
            self.reminder_due_id,
            self.reminder_due_attempt_id,
            self.reminder_due_generation,
        )
        if any(value is None for value in receipt_fields) and any(
            value is not None for value in receipt_fields
        ):
            raise ProtocolError("Productivity reminder due receipt fields must be paired")
        if self.reminder_due_id is not None and (
            self.domain != "reminders" or self.phase != "due"
        ):
            raise ProtocolError("Productivity reminder due receipt requires a reminder due view")
        productivity: dict[str, Any] = {
            "domain": self.domain,
            "phase": self.phase,
            "title": _productivity_text(self.title, "title", 72),
            "primary": _productivity_text(self.primary, "primary", 72),
            "secondary": _productivity_text(self.secondary, "secondary", 112),
            "hint": _productivity_text(self.hint, "hint", 46),
        }
        if self.planning_anchor_unix_ms is not None:
            anchor = _planning_anchor(
                self.planning_anchor_unix_ms,
                field_name="productivity planning anchor",
            )
            assert anchor is not None
            productivity["planningAnchorUnixMs"] = anchor
        if self.monitoring_armed:
            productivity["monitoringArmed"] = True
        if self.reminder_due_id is not None:
            productivity["reminderDueId"] = _reminder_due_identifier(self.reminder_due_id)
            productivity["reminderDueAttemptId"] = _reminder_due_identifier(
                self.reminder_due_attempt_id,
            )
            productivity["reminderDueGeneration"] = _reminder_due_generation(
                self.reminder_due_generation,
            )
        return {
            "kind": "productivity.view",
            "productivity": productivity,
        }


@dataclass(frozen=True, slots=True)
class AudioControl:
    open: bool
    source: AudioSource | None = None

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"kind": "audio.control", "open": self.open}
        if self.source is not None:
            msg["source"] = self.source
        return msg


@dataclass(frozen=True, slots=True)
class ImuControl:
    open: bool
    pace_ms: int | None = None

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"kind": "imu.control", "open": self.open}
        if self.pace_ms is not None:
            msg["paceMs"] = self.pace_ms
        return msg


@dataclass(frozen=True, slots=True)
class ImuTrace:
    """Calibration only — ~10 messages/second at P100."""

    enabled: bool

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "imu.trace", "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class G2DiagnosticControl:
    """Explicit Mac-shell opt-in for the otherwise dormant G2 trace."""

    enabled: bool

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "g2.diagnostic.control", "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class LocationProbeControl:
    """Explicit high-accuracy Gate-0 session; never inferred from movement."""

    enabled: bool

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "location.probe.control", "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class LocationRefresh:
    """Request one medium-accuracy phone fix for an assistant tool call."""

    request_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "location.refresh",
            "requestId": _navigation_identifier(self.request_id, "requestId", maximum=64),
        }


@dataclass(frozen=True, slots=True)
class NavigationHudView:
    view: Literal[
        "destination", "repair", "mode", "preview", "guidance", "uncertain",
        "wrong_way", "off_route", "rerouting", "arrived", "error",
    ]
    primary: str
    secondary: str
    hint: str
    interaction: Literal["stationary", "none"]
    options: list[str] = field(default_factory=list)
    selected_index: int = 0
    choice_ids: list[str] | None = None
    mode: Literal["walking", "cycling"] | None = None
    maneuver: Literal[
        "depart", "continue", "turn_left", "turn_right", "slight_left",
        "slight_right", "u_turn", "arrive",
    ] | None = None
    maneuver_distance_m: float | None = None
    remaining_distance_m: float | None = None
    eta_minutes: float | None = None
    progress: float | None = None


@dataclass(frozen=True, slots=True)
class NavigationView:
    session_id: str
    request_id: str
    generation: int
    navigation: NavigationHudView

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "navigation.view",
            "sessionId": _navigation_identifier(self.session_id, "sessionId"),
            "requestId": _navigation_identifier(self.request_id, "requestId"),
            "generation": _navigation_generation(self.generation),
            "navigation": _navigation_hud_to_wire(self.navigation),
        }


@dataclass(frozen=True, slots=True)
class NavigationOriginRequest:
    session_id: str
    request_id: str
    generation: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "navigation.origin.request",
            "sessionId": _navigation_identifier(self.session_id, "sessionId"),
            "requestId": _navigation_identifier(self.request_id, "requestId"),
            "generation": _navigation_generation(self.generation),
        }


@dataclass(frozen=True, slots=True)
class NavigationCoordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class NavigationRouteStep:
    id: str
    maneuver: Literal[
        "depart", "continue", "turn_left", "turn_right", "slight_left",
        "slight_right", "u_turn", "arrive",
    ]
    instruction: str
    distance_along_route_m: float


@dataclass(frozen=True, slots=True)
class NavigationProgressPolicy:
    max_accuracy_m: float
    stale_after_ms: float
    corridor_radius_m: float
    ambiguity_margin_m: float
    ambiguity_progress_delta_m: float
    search_back_m: float
    search_forward_m: float
    min_course_displacement_m: float
    wrong_way_angle_deg: float
    wrong_way_min_backtrack_m: float
    wrong_way_samples: int
    off_route_samples: int
    arrival_radius_m: float
    arrival_samples: int
    u_turn_suppression_m: float


@dataclass(frozen=True, slots=True)
class NavigationRoutePlan:
    mode: Literal["walking", "cycling"]
    geometry: list[NavigationCoordinate]
    steps: list[NavigationRouteStep]
    policy: NavigationProgressPolicy


@dataclass(frozen=True, slots=True)
class NavigationRoute:
    session_id: str
    request_id: str
    generation: int
    route_id: str
    revision: int
    destination_label: str
    eta_minutes: float
    plan: NavigationRoutePlan

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "navigation.route",
            "sessionId": _navigation_identifier(self.session_id, "sessionId"),
            "requestId": _navigation_identifier(self.request_id, "requestId"),
            "generation": _navigation_generation(self.generation),
            "routeId": _navigation_identifier(self.route_id, "routeId"),
            "revision": _navigation_generation(self.revision),
            "destinationLabel": _navigation_text(
                self.destination_label, "destinationLabel", 96,
            ),
            "etaMinutes": _navigation_number(
                self.eta_minutes, "etaMinutes", minimum=0, maximum=1_000_000,
            ),
            "plan": _navigation_route_plan_to_wire(self.plan),
        }


@dataclass(frozen=True, slots=True)
class NavigationStop:
    session_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "navigation.stop",
            "sessionId": _navigation_identifier(self.session_id, "sessionId"),
        }


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    action_id: str
    prompt: str
    challenge_id: str
    session_id: str
    payload_digest: str
    preview_digest: str
    expires_at_ms: int
    diagnostic_kind: Literal["messages"] | None = None

    def to_wire(self) -> dict[str, Any]:
        wire = {
            "kind": "confirm.request",
            "actionId": self.action_id,
            "prompt": self.prompt,
            "challengeId": self.challenge_id,
            "sessionId": self.session_id,
            "payloadDigest": self.payload_digest,
            "previewDigest": self.preview_digest,
            "expiresAtMs": self.expires_at_ms,
        }
        if self.diagnostic_kind is not None:
            wire["diagnosticKind"] = self.diagnostic_kind
        return wire


@dataclass(frozen=True, slots=True)
class PolicySet:
    resume: ResumePolicy

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "policy.set", "resume": self.resume}


@dataclass(frozen=True, slots=True)
class Shutdown:
    exit_mode: Literal[0, 1] = 1

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "shutdown", "exitMode": self.exit_mode}


@dataclass(frozen=True, slots=True)
class PairingSet:
    """Issued during an explicit pairing window after a null auth frame.

    The plugin persists it in localStorage and presents it in the first
    application frame on later dials; the gateway rejects mismatches.
    """

    token: str

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "pairing.set", "token": self.token}


DownlinkMessage = (
    AuthAccepted
    | HudText
    | AssistantOverlay
    | HudRebuild
    | HudList
    | BrainstormHudView
    | ProductivityHudView
    | AudioControl
    | ImuControl
    | ImuTrace
    | G2DiagnosticControl
    | LocationProbeControl
    | LocationRefresh
    | NavigationView
    | NavigationOriginRequest
    | NavigationRoute
    | NavigationStop
    | ConfirmRequest
    | PolicySet
    | Shutdown
    | PairingSet
)


# ── Parsing ──────────────────────────────────────────────────────────────────


class ProtocolError(ValueError):
    """Malformed or unknown uplink message."""


def _require(payload: dict[str, Any], key: str, kind: str) -> Any:
    if key not in payload:
        raise ProtocolError(f"{kind!r} message missing required field {key!r}")
    return payload[key]


def _navigation_identifier(value: Any, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(
            f"navigation {name} must be a non-empty string of at most {maximum} characters"
        )
    return value


def _brainstorm_text(
    value: Any, name: str, maximum: int, *, allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ProtocolError(f"Brainstorm HUD {name} is invalid")
    return value


def _productivity_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(f"Productivity HUD {name} is invalid")
    return value


def _planning_anchor(value: Any, *, field_name: str) -> int | None:
    """Validate the bounded, content-free instant used to reopen Planning."""

    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _PLANNING_ANCHOR_MAX_UNIX_MS
    ):
        raise ProtocolError(f"{field_name} must be a valid Unix millisecond instant")
    return value


def _navigation_generation(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise ProtocolError("navigation generation is invalid")
    return value


def _reminder_due_identifier(value: Any) -> str:
    """Accept only a UUID occurrence key, never Reminder content or an ID."""

    if not isinstance(value, str):
        raise ProtocolError("reminder due identifier is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("reminder due identifier is invalid") from exc
    if str(parsed) != value:
        raise ProtocolError("reminder due identifier is invalid")
    return value


def _reminder_due_generation(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ProtocolError("reminder due generation is invalid")
    return value


def _navigation_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(f"navigation {name} is invalid")
    return value


def _navigation_number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"navigation {name} is invalid")
    resolved = float(value)
    if not math.isfinite(resolved) or not minimum <= resolved <= maximum:
        raise ProtocolError(f"navigation {name} is invalid")
    return resolved


def _location_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9_007_199_254_740_991:
        raise ProtocolError("location timestamp is invalid")
    return value


def _navigation_hud_to_wire(view: NavigationHudView) -> dict[str, Any]:
    allowed_views = (
        "destination", "repair", "mode", "preview", "guidance", "uncertain",
        "wrong_way", "off_route", "rerouting", "arrived", "error",
    )
    if view.view not in allowed_views:
        raise ProtocolError("navigation view is invalid")
    if view.interaction not in ("stationary", "none"):
        raise ProtocolError("navigation interaction is invalid")
    if len(view.options) > 3:
        raise ProtocolError("navigation options exceed HUD capacity")
    options = [_navigation_text(value, "option", 64) for value in view.options]
    if options:
        if not 0 <= view.selected_index < len(options):
            raise ProtocolError("navigation selectedIndex is invalid")
    elif view.selected_index != 0:
        raise ProtocolError("navigation selectedIndex is invalid")
    if view.choice_ids is not None and len(view.choice_ids) != len(options):
        raise ProtocolError("navigation choiceIds must match options")

    message: dict[str, Any] = {
        "view": view.view,
        "primary": _navigation_text(view.primary, "primary", 96),
        "secondary": _navigation_text(view.secondary, "secondary", 128),
        "hint": _navigation_text(view.hint, "hint", 80),
        "interaction": view.interaction,
        "options": options,
        "selectedIndex": view.selected_index,
    }
    if view.choice_ids is not None:
        message["choiceIds"] = [
            _navigation_identifier(value, "choiceId", 64) for value in view.choice_ids
        ]
    if view.mode is not None:
        if view.mode not in ("walking", "cycling"):
            raise ProtocolError("navigation mode is invalid")
        message["mode"] = view.mode
    if view.maneuver is not None:
        if view.maneuver not in (
            "depart", "continue", "turn_left", "turn_right", "slight_left",
            "slight_right", "u_turn", "arrive",
        ):
            raise ProtocolError("navigation maneuver is invalid")
        message["maneuver"] = view.maneuver
    for wire_name, value, maximum in (
        ("maneuverDistanceM", view.maneuver_distance_m, 10_000_000.0),
        ("remainingDistanceM", view.remaining_distance_m, 10_000_000.0),
        ("etaMinutes", view.eta_minutes, 1_000_000.0),
        ("progress", view.progress, 1.0),
    ):
        if value is not None:
            message[wire_name] = _navigation_number(
                value, wire_name, minimum=0.0, maximum=maximum,
            )
    return message


def _navigation_coordinate_to_wire(coordinate: NavigationCoordinate) -> dict[str, float]:
    latitude = _navigation_number(
        coordinate.latitude, "latitude", minimum=-90, maximum=90,
    )
    longitude = _navigation_number(
        coordinate.longitude, "longitude", minimum=-180, maximum=180,
    )
    return {"latitude": latitude, "longitude": longitude}


def _navigation_route_plan_to_wire(plan: NavigationRoutePlan) -> dict[str, Any]:
    if plan.mode not in ("walking", "cycling"):
        raise ProtocolError("navigation route mode is invalid")
    if not 2 <= len(plan.geometry) <= 4_096:
        raise ProtocolError("navigation route geometry size is invalid")
    if not 1 <= len(plan.steps) <= 512:
        raise ProtocolError("navigation route steps size is invalid")
    maneuvers = (
        "depart", "continue", "turn_left", "turn_right", "slight_left",
        "slight_right", "u_turn", "arrive",
    )
    steps: list[dict[str, Any]] = []
    for step in plan.steps:
        if step.maneuver not in maneuvers:
            raise ProtocolError("navigation route maneuver is invalid")
        steps.append({
            "id": _navigation_identifier(step.id, "stepId", 64),
            "maneuver": step.maneuver,
            "instruction": _navigation_text(step.instruction, "instruction", 128),
            "distanceAlongRouteM": _navigation_number(
                step.distance_along_route_m,
                "distanceAlongRouteM",
                minimum=0,
                maximum=10_000_000,
            ),
        })
    policy = plan.policy
    policy_wire: dict[str, int | float] = {}
    for wire_name, value, maximum in (
        ("maxAccuracyM", policy.max_accuracy_m, 1_000.0),
        ("staleAfterMs", policy.stale_after_ms, 600_000.0),
        ("corridorRadiusM", policy.corridor_radius_m, 10_000.0),
        ("ambiguityMarginM", policy.ambiguity_margin_m, 10_000.0),
        ("ambiguityProgressDeltaM", policy.ambiguity_progress_delta_m, 100_000.0),
        ("searchBackM", policy.search_back_m, 100_000.0),
        ("searchForwardM", policy.search_forward_m, 100_000.0),
        ("minCourseDisplacementM", policy.min_course_displacement_m, 1_000.0),
        ("wrongWayAngleDeg", policy.wrong_way_angle_deg, 180.0),
        ("wrongWayMinBacktrackM", policy.wrong_way_min_backtrack_m, 100_000.0),
        ("arrivalRadiusM", policy.arrival_radius_m, 10_000.0),
        ("uTurnSuppressionM", policy.u_turn_suppression_m, 10_000.0),
    ):
        policy_wire[wire_name] = _navigation_number(
            value, wire_name, minimum=0.0001, maximum=maximum,
        )
    for wire_name, value in (
        ("wrongWaySamples", policy.wrong_way_samples),
        ("offRouteSamples", policy.off_route_samples),
        ("arrivalSamples", policy.arrival_samples),
    ):
        if type(value) is not int or not 1 <= value <= 100:
            raise ProtocolError(f"navigation {wire_name} is invalid")
        policy_wire[wire_name] = value
    if not 90 < policy.wrong_way_angle_deg <= 180:
        raise ProtocolError("navigation wrongWayAngleDeg is invalid")
    return {
        "mode": plan.mode,
        "geometry": [_navigation_coordinate_to_wire(value) for value in plan.geometry],
        "steps": steps,
        "policy": policy_wire,
    }


def _trace_enum(value: Any, name: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError(f"g2 diagnostic {name} is invalid")
    return value


def _trace_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    # bool is an int subclass, but a boolean tile order is not a valid receipt.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"g2 diagnostic {name} is invalid")
    return value


def _trace_cadence_bucket(value: Any) -> str:
    milliseconds = _trace_int(value, "cadenceMs", minimum=0, maximum=600_000)
    if milliseconds == 0:
        return "first"
    if milliseconds <= 1_000:
        return "lte1s"
    if milliseconds <= 5_000:
        return "1_5s"
    if milliseconds <= 30_000:
        return "5_30s"
    return "gt30s"


def _trace_shape(payload: dict[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise ProtocolError("g2 diagnostic has unsupported fields")


def _trace_tile_container(value: Any) -> int:
    container_id = _trace_int(value, "containerId", minimum=4, maximum=10)
    if container_id not in (4, 8, 9, 10):
        raise ProtocolError("g2 diagnostic containerId is invalid")
    return container_id


def _parse_g2_diagnostic(event: Any) -> dict[str, str | int]:
    """Return only safe diagnostic metadata; reject arbitrary plugin fields."""

    if not isinstance(event, dict):
        raise ProtocolError("g2 diagnostic event must be an object")
    event_type = event.get("type")
    if event_type == "page_transition":
        _trace_shape(event, {"type", "from", "to", "reason", "outcome"})
        return {
            "type": "page_transition",
            "from": _trace_enum(event["from"], "from", ("overview", "sidebar", "list", "confirmation", "navigation")),
            "to": _trace_enum(event["to"], "to", ("overview", "sidebar", "list", "confirmation", "navigation")),
            "reason": _trace_enum(event["reason"], "reason", ("show_sidebar", "show_list", "show_overview", "show_confirmation", "show_navigation", "dismiss_confirmation")),
            "outcome": _trace_enum(event["outcome"], "outcome", ("success", "failed")),
        }
    if event_type == "gesture":
        _trace_shape(event, {"type", "gesture", "delivery"})
        return {
            "type": "gesture",
            "gesture": _trace_enum(event["gesture"], "gesture", ("tap", "double_press", "long_press", "long_press_release", "scroll_up", "scroll_down", "menu_item", "foreground_enter", "foreground_exit", "system_exit", "abnormal_exit", "imu_report", "unknown")),
            "delivery": _trace_enum(event["delivery"], "delivery", ("sys", "text", "list", "menu")),
        }
    if event_type == "tile_upload":
        _trace_shape(event, {"type", "frame", "order", "containerId", "outcome"})
        return {
            "type": "tile_upload",
            "frame": _trace_int(event["frame"], "frame", minimum=1, maximum=2_147_483_647),
            "order": _trace_int(event["order"], "order", minimum=0, maximum=3),
            "containerId": _trace_tile_container(event["containerId"]),
            "outcome": _trace_enum(event["outcome"], "outcome", ("success", "failed", "error")),
        }
    if event_type == "hud_surface":
        _trace_shape(event, {"type", "stage", "surface", "outcome"})
        return {
            "type": "hud_surface",
            "stage": _trace_enum(
                event["stage"], "stage", ("visual_rebuild", "first_frame", "selected"),
            ),
            "surface": _trace_enum(
                event["surface"], "surface", ("visual", "text", "minimal"),
            ),
            "outcome": _trace_enum(event["outcome"], "outcome", ("success", "failed")),
        }
    if event_type == "self_heal":
        _trace_shape(event, {"type", "surface", "target", "decision", "containerId"})
        return {
            "type": "self_heal",
            "surface": _trace_enum(event["surface"], "surface", ("overview", "sidebar", "list", "confirmation")),
            "target": _trace_enum(event["target"], "target", ("text", "visual")),
            "decision": _trace_enum(event["decision"], "decision", ("attempt", "recovered", "abandoned", "skipped")),
            "containerId": _trace_int(event["containerId"], "containerId", minimum=1, maximum=10),
        }
    if event_type == "location_probe_state":
        _trace_shape(event, {"type", "requested", "outcome"})
        return {
            "type": "location_probe_state",
            "requested": _trace_enum(
                event["requested"], "requested", ("high", "ambient"),
            ),
            "outcome": _trace_enum(
                event["outcome"], "outcome", ("success", "failed"),
            ),
        }
    if event_type in ("location_probe", "navigation_location"):
        required = {
            "type", "sampleSequence", "lifecycle", "fields", "cadence",
            "accuracy", "timestampAge", "displacement", "headingDelta",
        }
        if event_type == "location_probe":
            required.add("profile")
        # v0.1.22 used a numeric cadence. Accept it only at the boundary, and
        # retain the coarser bucket so owner-only receipts never accumulate a
        # movement timeline with raw millisecond values.
        legacy_required = (required - {"cadence"}) | {"cadenceMs"}
        if set(event) == legacy_required:
            cadence = _trace_cadence_bucket(event["cadenceMs"])
        else:
            _trace_shape(event, required)
            cadence = _trace_enum(
                event["cadence"], "cadence", ("first", "lte1s", "1_5s", "5_30s", "gt30s"),
            )
        parsed: dict[str, str | int] = {
            "type": event_type,
            "sampleSequence": _trace_int(
                event["sampleSequence"], "sampleSequence", minimum=1, maximum=2_147_483_647,
            ),
            "lifecycle": _trace_enum(
                event["lifecycle"], "lifecycle", ("foreground", "background"),
            ),
            "fields": _trace_int(event["fields"], "fields", minimum=0, maximum=7),
            "cadence": cadence,
            "accuracy": _trace_enum(
                event["accuracy"], "accuracy", ("unknown", "lte5", "lte10", "lte25", "gt25"),
            ),
            "timestampAge": _trace_enum(
                event["timestampAge"], "timestampAge", ("missing", "future", "fresh", "stale"),
            ),
            "displacement": _trace_enum(
                event["displacement"], "displacement", ("first", "lt5", "5_20", "20_100", "gt100"),
            ),
            "headingDelta": _trace_enum(
                event["headingDelta"], "headingDelta",
                ("unavailable", "insufficient_motion", "0_30", "31_90", "91_180"),
            ),
        }
        if event_type == "location_probe":
            parsed["profile"] = _trace_enum(event["profile"], "profile", ("high",))
        return parsed
    if event_type == "navigation_cue":
        _trace_shape(event, {"type", "cue", "outcome"})
        return {
            "type": "navigation_cue",
            "cue": _trace_enum(
                event["cue"], "cue",
                ("destination", "repair", "mode", "preview", "guidance", "uncertain", "wrong_way", "off_route", "rerouting", "arrived", "error", "stopped"),
            ),
            "outcome": _trace_enum(event["outcome"], "outcome", ("success", "failed")),
        }
    if event_type == "brainstorm_action":
        _trace_shape(event, {"type", "action", "surface", "source", "gesture", "delivery"})
        return {
            "type": "brainstorm_action",
            "action": _trace_enum(
                event["action"], "action",
                ("begin_acceptance", "begin_topic_capture", "show_terminal", "show_brainstorm", "cancel", "retry_terminal"),
            ),
            "surface": _trace_enum(event["surface"], "surface", ("brainstorm", "agent_terminal")),
            "source": _trace_enum(event["source"], "source", ("left", "right", "ring")),
            "gesture": _trace_enum(event["gesture"], "gesture", ("tap", "double_press")),
            "delivery": _trace_enum(event["delivery"], "delivery", ("sys",)),
        }
    if event_type == "visual_frame":
        kind = event.get("kind")
        if kind == "brainstorm":
            _trace_shape(event, {"type", "kind", "surface", "state", "sha256"})
        elif kind == "messages":
            _trace_shape(event, {"type", "kind", "surface", "sha256"})
        else:
            raise ProtocolError("g2 diagnostic visual frame kind is invalid")
        digest = event["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ProtocolError("g2 diagnostic sha256 is invalid")
        if kind == "brainstorm":
            return {
                "type": "visual_frame",
                "kind": "brainstorm",
                "surface": _trace_enum(event["surface"], "surface", ("brainstorm", "agent_terminal")),
                "state": _trace_enum(
                    event["state"], "state",
                    (
                        "unavailable", "approval_required", "ready", "topic_capture", "acceptance_ready",
                        "running", "settled", "cancelled", "failed", "blocked",
                    ),
                ),
                "sha256": digest,
            }
        return {
            "type": "visual_frame",
            "kind": "messages",
            "surface": _trace_enum(
                event["surface"], "surface",
                ("picker", "conversation", "compose", "access", "setup", "retry", "empty", "read_only", "confirmation"),
            ),
            "sha256": digest,
        }
    if event_type == "messages_confirmation":
        _trace_shape(event, {"type", "outcome"})
        return {
            "type": "messages_confirmation",
            "outcome": _trace_enum(event["outcome"], "outcome", ("accepted", "declined", "timed_out")),
        }
    raise ProtocolError("g2 diagnostic type is invalid")


def parse_uplink(payload: dict[str, Any]) -> UplinkMessage:
    """Turn a decoded JSON object into a typed uplink message.

    Raises ProtocolError on anything unrecognised. Callers should log and drop
    rather than crash: a newer plugin talking to an older brain is a normal
    deploy-skew situation, not a fatal one.
    """
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise ProtocolError(f"message has no string 'kind': {payload!r}")

    match kind:
        case "auth":
            token = _require(payload, "token", kind)
            if token is not None and (not isinstance(token, str) or not token):
                raise ProtocolError("auth token must be a non-empty string or null")
            return Auth(token=token)
        case "hello":
            return Hello(
                sn=_require(payload, "sn", kind),
                model=_require(payload, "model", kind),
                launch_source=payload.get("launchSource", "unknown"),
                user_name=str(payload.get("userName", "")),
            )
        case "audio":
            # Coerce samples HERE so one malformed value is a ProtocolError
            # (logged and dropped by the caller) — not a TypeError escaping
            # pack_pcm later and killing the whole session.
            source = payload.get("source")
            if source not in ("glasses", "phone"):
                raise ProtocolError("audio source is invalid")
            try:
                pcm = [int(v) for v in _require(payload, "pcm", kind)]
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"audio pcm contains a non-numeric sample: {exc}") from exc
            speaker = payload.get("speaker")
            return Audio(
                source=source,
                seq=int(_require(payload, "seq", kind)),
                pcm=pcm,
                speaker=str(speaker) if speaker is not None else None,
            )
        case "imu":
            return Imu(
                x=float(payload.get("x", 0.0)),
                y=float(payload.get("y", 0.0)),
                z=float(payload.get("z", 0.0)),
                at=int(payload.get("at", 0)),
            )
        case "input":
            raw = payload.get("event") or {}
            if not isinstance(raw, dict):
                raise ProtocolError("input event must be an object")
            request_id = raw.get("requestId")
            if request_id is not None and (
                not isinstance(request_id, str)
                or not request_id
                or len(request_id) > 128
            ):
                raise ProtocolError(
                    "input requestId must be a non-empty string of at most 128 characters"
                )
            try:
                planning_anchor = _planning_anchor(
                    raw.get("planningAnchorUnixMs"),
                    field_name="input planningAnchorUnixMs",
                )
            except ProtocolError:
                # A Planning return anchor is optional navigation context, not
                # authority. A stale/corrupt plugin must still be able to
                # reopen the generic native plan instead of leaving the
                # wearer stranded on an outcome screen.
                if (
                    raw.get("containerName") == "capmenu"
                    and raw.get("selectedName") in {"Planning", "Calendar", "Tasks", "Reminders"}
                ):
                    planning_anchor = None
                else:
                    raise
            return Input(
                event=InputEvent(
                    container_id=raw.get("containerId"),
                    container_name=raw.get("containerName"),
                    event_type=raw.get("eventType"),
                    event_source=raw.get("eventSource"),
                    selected_index=raw.get("selectedIndex"),
                    selected_name=raw.get("selectedName"),
                    trace_id=raw.get("traceId"),
                    request_id=request_id,
                    planning_anchor_unix_ms=planning_anchor,
                )
            )
        case "pairing":
            return PairingAck(token=str(_require(payload, "token", kind)))
        case "g2.diagnostic":
            _trace_shape(payload, {"kind", "event"})
            return G2Diagnostic(event=_parse_g2_diagnostic(_require(payload, "event", kind)))
        case "navigation.action":
            allowed = {
                "kind", "sessionId", "requestId", "generation", "action", "choiceId",
            }
            if not set(payload) <= allowed:
                raise ProtocolError("navigation action has unsupported fields")
            action = _require(payload, "action", kind)
            if not isinstance(action, str) or action not in NAVIGATION_ACTIONS:
                raise ProtocolError("navigation action is invalid")
            choice_id = payload.get("choiceId")
            if action in ("select_destination", "select_mode"):
                choice_id = _navigation_identifier(choice_id, "choiceId", 64)
            elif choice_id is not None:
                raise ProtocolError("navigation choiceId is invalid for action")
            return NavigationAction(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                request_id=_navigation_identifier(
                    _require(payload, "requestId", kind), "requestId",
                ),
                generation=_navigation_generation(
                    _require(payload, "generation", kind),
                ),
                action=action,
                choice_id=choice_id,
            )
        case "brainstorm.action":
            action = payload.get("action")
            if not isinstance(action, str) or action not in BRAINSTORM_ACTIONS:
                raise ProtocolError("brainstorm action is invalid")
            if action == "begin_acceptance":
                if set(payload) != {"kind", "action", "surface", "source", "gesture"}:
                    raise ProtocolError("brainstorm action acceptance has unsupported fields")
                surface = payload.get("surface")
                source = payload.get("source")
                gesture = payload.get("gesture")
                if surface != "brainstorm" or source not in ("right", "ring") \
                        or gesture != "double_press":
                    raise ProtocolError("brainstorm action acceptance is invalid")
                return BrainstormAction(
                    action=action,
                    surface="brainstorm",
                    source=source,
                    gesture="double_press",
                )
            if set(payload) != {"kind", "action"}:
                raise ProtocolError("brainstorm action has unsupported fields")
            return BrainstormAction(action=action)
        case "navigation.route.ack":
            if set(payload) != {"kind", "sessionId", "routeId", "revision", "outcome"}:
                raise ProtocolError("navigation route ack has unsupported fields")
            outcome = payload.get("outcome")
            if outcome not in ("installed", "rejected"):
                raise ProtocolError("navigation route ack outcome is invalid")
            return NavigationRouteAck(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                route_id=_navigation_identifier(
                    _require(payload, "routeId", kind), "routeId",
                ),
                revision=_navigation_generation(_require(payload, "revision", kind)),
                outcome=outcome,
            )
        case "navigation.view.ack":
            if set(payload) != {"kind", "sessionId", "requestId", "generation", "outcome"}:
                raise ProtocolError("navigation view ack has unsupported fields")
            outcome = payload.get("outcome")
            if outcome not in ("bridge_acknowledged", "rejected"):
                raise ProtocolError("navigation view ack outcome is invalid")
            return NavigationViewAck(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                request_id=_navigation_identifier(
                    _require(payload, "requestId", kind), "requestId",
                ),
                generation=_navigation_generation(
                    _require(payload, "generation", kind),
                ),
                outcome=outcome,
            )
        case "navigation.state":
            if set(payload) != {
                "kind", "sessionId", "routeId", "revision", "sequence", "state",
            }:
                raise ProtocolError("navigation state has unsupported fields")
            state = payload.get("state")
            if state not in ("guidance", "uncertain", "wrong_way", "off_route", "arrived"):
                raise ProtocolError("navigation state is invalid")
            return NavigationState(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                route_id=_navigation_identifier(
                    _require(payload, "routeId", kind), "routeId",
                ),
                revision=_navigation_generation(_require(payload, "revision", kind)),
                sequence=_navigation_generation(_require(payload, "sequence", kind)),
                state=state,
            )
        case "navigation.reroute.request":
            if set(payload) != {
                "kind", "sessionId", "routeId", "baseRevision", "requestId",
                "latitude", "longitude",
            }:
                raise ProtocolError("navigation reroute request has unsupported fields")
            return NavigationRerouteRequest(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                route_id=_navigation_identifier(
                    _require(payload, "routeId", kind), "routeId",
                ),
                base_revision=_navigation_generation(
                    _require(payload, "baseRevision", kind),
                ),
                request_id=_navigation_identifier(
                    _require(payload, "requestId", kind), "requestId",
                ),
                latitude=_navigation_number(
                    _require(payload, "latitude", kind), "latitude", minimum=-90, maximum=90,
                ),
                longitude=_navigation_number(
                    _require(payload, "longitude", kind), "longitude", minimum=-180, maximum=180,
                ),
            )
        case "navigation.origin":
            allowed = {
                "kind", "sessionId", "requestId", "generation",
                "latitude", "longitude", "accuracy",
            }
            if not set(payload) <= allowed:
                raise ProtocolError("navigation origin has unsupported fields")
            accuracy_value = payload.get("accuracy")
            accuracy = None if accuracy_value is None else _navigation_number(
                accuracy_value, "accuracy", minimum=0, maximum=100_000,
            )
            return NavigationOrigin(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                request_id=_navigation_identifier(
                    _require(payload, "requestId", kind), "requestId",
                ),
                generation=_navigation_generation(
                    _require(payload, "generation", kind),
                ),
                latitude=_navigation_number(
                    _require(payload, "latitude", kind), "latitude", minimum=-90, maximum=90,
                ),
                longitude=_navigation_number(
                    _require(payload, "longitude", kind), "longitude", minimum=-180, maximum=180,
                ),
                accuracy=accuracy,
            )
        case "navigation.origin.failed":
            if set(payload) != {"kind", "sessionId", "requestId", "generation", "reason"}:
                raise ProtocolError("navigation origin failure has unsupported fields")
            reason = payload.get("reason")
            if reason != "unavailable":
                raise ProtocolError("navigation origin failure reason is invalid")
            return NavigationOriginFailure(
                session_id=_navigation_identifier(
                    _require(payload, "sessionId", kind), "sessionId",
                ),
                request_id=_navigation_identifier(
                    _require(payload, "requestId", kind), "requestId",
                ),
                generation=_navigation_generation(
                    _require(payload, "generation", kind),
                ),
                reason=reason,
            )
        case "reminder_due.ack":
            if set(payload) != {
                "kind", "reminderDueId", "reminderDueAttemptId", "generation", "outcome",
            }:
                raise ProtocolError("reminder due acknowledgement has unsupported fields")
            outcome = _require(payload, "outcome", kind)
            if outcome != "accepted":
                raise ProtocolError("reminder due acknowledgement outcome is invalid")
            return ReminderDueAck(
                reminder_due_id=_reminder_due_identifier(
                    _require(payload, "reminderDueId", kind),
                ),
                reminder_due_attempt_id=_reminder_due_identifier(
                    _require(payload, "reminderDueAttemptId", kind),
                ),
                generation=_reminder_due_generation(_require(payload, "generation", kind)),
                outcome="accepted",
            )
        case "location":
            if not set(payload) <= {
                "kind", "latitude", "longitude", "accuracy", "timestamp", "refreshId",
            }:
                raise ProtocolError("location has unsupported fields")
            acc = payload.get("accuracy")
            refresh_id = payload.get("refreshId")
            return Location(
                latitude=_navigation_number(
                    _require(payload, "latitude", kind), "latitude", minimum=-90, maximum=90,
                ),
                longitude=_navigation_number(
                    _require(payload, "longitude", kind), "longitude", minimum=-180, maximum=180,
                ),
                accuracy=(
                    _navigation_number(acc, "accuracy", minimum=0, maximum=100_000)
                    if acc is not None else None
                ),
                timestamp_ms=_location_timestamp(payload.get("timestamp")),
                refresh_id=(
                    _navigation_identifier(refresh_id, "refreshId", maximum=64)
                    if refresh_id is not None else None
                ),
            )
        case "device":
            return Device(
                connect=payload.get("connect", "none"),
                battery=payload.get("battery"),
                wearing=payload.get("wearing"),
                charging=payload.get("charging"),
                mic=payload.get("mic"),
            )
        case "confirm":
            action_id = _require(payload, "actionId", kind)
            accepted = _require(payload, "accepted", kind)
            if not isinstance(action_id, str) or not action_id:
                raise ProtocolError("confirm actionId must be a non-empty string")
            if not isinstance(accepted, bool):
                raise ProtocolError("confirm accepted must be a boolean")
            binding: dict[str, str | None] = {}
            for wire_key, field_name in (
                ("challengeId", "challenge_id"),
                ("sessionId", "session_id"),
                ("payloadDigest", "payload_digest"),
                ("previewDigest", "preview_digest"),
            ):
                value = payload.get(wire_key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ProtocolError(f"confirm {wire_key} must be a non-empty string")
                binding[field_name] = value
            return Confirm(
                action_id=action_id,
                accepted=accepted,
                challenge_id=binding["challenge_id"],
                session_id=binding["session_id"],
                payload_digest=binding["payload_digest"],
                preview_digest=binding["preview_digest"],
            )
        case "policy":
            resume = payload.get("resume")
            if resume not in RESUME_POLICIES:
                raise ProtocolError(f"unknown resume policy {resume!r}")
            return Policy(resume=resume)
        case _:
            raise ProtocolError(f"unknown uplink kind {kind!r}")
