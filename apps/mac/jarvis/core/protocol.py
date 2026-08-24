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


class ContainerId(IntEnum):
    """Stable across rebuilds so text upgrades can target them."""

    STATUS_BAR = 1
    MAIN = 2
    LIST = 3
    IMAGE = 4


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
class Hello:
    sn: str
    model: str
    launch_source: str


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


@dataclass(frozen=True, slots=True)
class Confirm:
    action_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class Policy:
    resume: ResumePolicy


UplinkMessage = Hello | Audio | Imu | Input | Device | Confirm | Policy


# ── Downlink: Mac → plugin ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HudText:
    text: str
    container_id: ContainerId = ContainerId.MAIN
    offset: int | None = None
    length: int | None = None

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
        return msg


@dataclass(frozen=True, slots=True)
class HudRebuild:
    layout: Literal["default", "list", "image"] = "default"

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "hud.rebuild", "layout": self.layout}


@dataclass(frozen=True, slots=True)
class HudList:
    items: list[str]

    def to_wire(self) -> dict[str, Any]:
        # SDK caps: 20 items, 64 chars each. Truncate here rather than let the
        # plugin silently drop the overflow.
        return {"kind": "hud.list", "items": [s[:64] for s in self.items[:20]]}


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
class ConfirmRequest:
    action_id: str
    prompt: str

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "confirm.request", "actionId": self.action_id, "prompt": self.prompt}


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


DownlinkMessage = (
    HudText
    | HudRebuild
    | HudList
    | AudioControl
    | ImuControl
    | ImuTrace
    | ConfirmRequest
    | PolicySet
    | Shutdown
)


# ── Parsing ──────────────────────────────────────────────────────────────────


class ProtocolError(ValueError):
    """Malformed or unknown uplink message."""


def _require(payload: dict[str, Any], key: str, kind: str) -> Any:
    if key not in payload:
        raise ProtocolError(f"{kind!r} message missing required field {key!r}")
    return payload[key]


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
        case "hello":
            return Hello(
                sn=_require(payload, "sn", kind),
                model=_require(payload, "model", kind),
                launch_source=payload.get("launchSource", "unknown"),
            )
        case "audio":
            # Coerce samples HERE so one malformed value is a ProtocolError
            # (logged and dropped by the caller) — not a TypeError escaping
            # pack_pcm later and killing the whole session.
            try:
                pcm = [int(v) for v in _require(payload, "pcm", kind)]
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"audio pcm contains a non-numeric sample: {exc}") from exc
            return Audio(
                source=payload.get("source", "glasses"),
                seq=int(_require(payload, "seq", kind)),
                pcm=pcm,
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
                )
            )
        case "device":
            return Device(
                connect=payload.get("connect", "none"),
                battery=payload.get("battery"),
                wearing=payload.get("wearing"),
                charging=payload.get("charging"),
            )
        case "confirm":
            return Confirm(
                action_id=_require(payload, "actionId", kind),
                accepted=bool(_require(payload, "accepted", kind)),
            )
        case "policy":
            resume = payload.get("resume")
            if resume not in RESUME_POLICIES:
                raise ProtocolError(f"unknown resume policy {resume!r}")
            return Policy(resume=resume)
        case _:
            raise ProtocolError(f"unknown uplink kind {kind!r}")
