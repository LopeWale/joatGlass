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
from enum import IntEnum
from typing import Any, Literal

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


UplinkMessage = (
    Auth | Hello | Audio | Imu | Input | Device | Confirm | Policy | PairingAck | Location | G2Diagnostic
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
class HudRebuild:
    layout: Literal["default", "list", "image"] = "default"

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "hud.rebuild", "layout": self.layout}


@dataclass(frozen=True, slots=True)
class HudList:
    items: list[str]
    title: str | None = None
    hint: str | None = None

    def to_wire(self) -> dict[str, Any]:
        # SDK caps: 20 items, 64 chars each. Truncate here rather than let the
        # plugin silently drop the overflow.
        message: dict[str, Any] = {
            "kind": "hud.list",
            "items": [s[:64] for s in self.items[:20]],
        }
        if self.title is not None:
            message["title"] = self.title[:46]
        if self.hint is not None:
            message["hint"] = self.hint[:46]
        return message


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
class ConfirmRequest:
    action_id: str
    prompt: str
    challenge_id: str
    session_id: str
    payload_digest: str
    preview_digest: str
    expires_at_ms: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": "confirm.request",
            "actionId": self.action_id,
            "prompt": self.prompt,
            "challengeId": self.challenge_id,
            "sessionId": self.session_id,
            "payloadDigest": self.payload_digest,
            "previewDigest": self.preview_digest,
            "expiresAtMs": self.expires_at_ms,
        }


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
    | HudRebuild
    | HudList
    | AudioControl
    | ImuControl
    | ImuTrace
    | G2DiagnosticControl
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


def _trace_enum(value: Any, name: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError(f"g2 diagnostic {name} is invalid")
    return value


def _trace_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    # bool is an int subclass, but a boolean tile order is not a valid receipt.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"g2 diagnostic {name} is invalid")
    return value


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
            "from": _trace_enum(event["from"], "from", ("overview", "sidebar", "list", "confirmation")),
            "to": _trace_enum(event["to"], "to", ("overview", "sidebar", "list", "confirmation")),
            "reason": _trace_enum(event["reason"], "reason", ("show_sidebar", "show_list", "show_overview", "show_confirmation", "dismiss_confirmation")),
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
    if event_type == "self_heal":
        _trace_shape(event, {"type", "surface", "target", "decision", "containerId"})
        return {
            "type": "self_heal",
            "surface": _trace_enum(event["surface"], "surface", ("overview", "sidebar", "list", "confirmation")),
            "target": _trace_enum(event["target"], "target", ("text", "visual")),
            "decision": _trace_enum(event["decision"], "decision", ("attempt", "recovered", "abandoned", "skipped")),
            "containerId": _trace_int(event["containerId"], "containerId", minimum=1, maximum=10),
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
            try:
                pcm = [int(v) for v in _require(payload, "pcm", kind)]
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"audio pcm contains a non-numeric sample: {exc}") from exc
            speaker = payload.get("speaker")
            return Audio(
                source=payload.get("source", "glasses"),
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
            return Input(
                event=InputEvent(
                    container_id=raw.get("containerId"),
                    container_name=raw.get("containerName"),
                    event_type=raw.get("eventType"),
                    event_source=raw.get("eventSource"),
                    selected_index=raw.get("selectedIndex"),
                    selected_name=raw.get("selectedName"),
                    trace_id=raw.get("traceId"),
                )
            )
        case "pairing":
            return PairingAck(token=str(_require(payload, "token", kind)))
        case "g2.diagnostic":
            return G2Diagnostic(event=_parse_g2_diagnostic(_require(payload, "event", kind)))
        case "location":
            acc = payload.get("accuracy")
            return Location(
                latitude=float(_require(payload, "latitude", kind)),
                longitude=float(_require(payload, "longitude", kind)),
                accuracy=float(acc) if acc is not None else None,
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
