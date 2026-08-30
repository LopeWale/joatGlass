#!/usr/bin/env python3
"""Summarize a no-send Messages run from the live production G2 trace.

This script never starts a gateway, changes a Messages database, or sends a
text.  It consumes the opt-in trace emitted by the running JOAT engine after
``--g2-diagnostics`` is enabled.  The trusted Mac adds ``g2_session`` itself,
so a loopback simulator cannot be relabelled as physical proof by this tool.

The receipt preserves only fixed event names and raster SHA-256 values.  It
does not retain Messages rows, sender names, recipient handles, spoken text,
confirmation copy, or lens video; an optional lens recording is represented
only by its file digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MAC_PACKAGE_ROOT = _REPOSITORY_ROOT / "apps" / "mac"
if str(_MAC_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAC_PACKAGE_ROOT))

from joat.services.g2_diagnostics import TRACE_SCHEMA


RUN_SCHEMA = "joat.g2-messages-acceptance.v1"
_SHA256_LENGTH = 64
_MESSAGES_SURFACES = frozenset({
    "picker", "conversation", "compose", "access", "setup", "retry",
    "empty", "read_only", "confirmation",
})
_RECOVERY_SURFACES = frozenset({"access", "setup", "retry", "empty", "read_only"})
_GESTURES = frozenset({
    "tap", "double_press", "long_press", "long_press_release", "scroll_up",
    "scroll_down", "menu_item", "foreground_enter", "foreground_exit",
    "system_exit", "abnormal_exit", "imu_report", "unknown",
})
_DELIVERIES = frozenset({"sys", "text", "list", "menu"})
_NO_SEND_OUTCOMES = frozenset({"declined", "timed_out"})


def _object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _events(receipt: dict[str, object]) -> list[dict[str, object]]:
    if receipt.get("schema") != TRACE_SCHEMA:
        raise ValueError("receipt is not an opt-in JOAT G2 diagnostic trace")
    raw = receipt.get("events")
    if not isinstance(raw, list):
        raise ValueError("receipt events must be a list")
    return [_object(event, "receipt event must be an object") for event in raw]


def _frame(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "visual_frame" or event.get("kind") != "messages":
        return None
    expected = {"sequence", "recordedAtMs", "type", "kind", "surface", "sha256"}
    if set(event) != expected:
        raise ValueError("Messages visual_frame contains unsupported fields")
    surface = event.get("surface")
    if surface not in _MESSAGES_SURFACES:
        raise ValueError("Messages visual_frame surface is invalid")
    return {
        "type": "visual_frame",
        "kind": "messages",
        "surface": str(surface),
        "sha256": _sha256(event.get("sha256"), "sha256"),
    }


def _gesture(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "gesture":
        return None
    expected = {"sequence", "recordedAtMs", "type", "gesture", "delivery"}
    if set(event) != expected:
        raise ValueError("gesture contains unsupported fields")
    gesture = event.get("gesture")
    delivery = event.get("delivery")
    if gesture not in _GESTURES or delivery not in _DELIVERIES:
        raise ValueError("gesture contains an invalid fixed value")
    return {"type": "gesture", "gesture": str(gesture), "delivery": str(delivery)}


def _session(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "g2_session":
        return None
    expected = {"sequence", "recordedAtMs", "type", "source"}
    if set(event) != expected or event.get("source") not in {"physical_g2", "nonphysical"}:
        raise ValueError("g2_session contains an invalid source classification")
    return {"type": "g2_session", "source": str(event["source"])}


def _confirmation(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "messages_confirmation":
        return None
    expected = {"sequence", "recordedAtMs", "type", "outcome"}
    if set(event) != expected or event.get("outcome") not in {"accepted", "declined", "timed_out"}:
        raise ValueError("messages_confirmation contains an invalid outcome")
    return {"type": "messages_confirmation", "outcome": str(event["outcome"])}


def _retained(receipt: dict[str, object]) -> list[dict[str, str]]:
    retained: list[dict[str, str]] = []
    for event in _events(receipt):
        simplified = _session(event) or _gesture(event) or _frame(event) or _confirmation(event)
        if simplified is not None:
            retained.append(simplified)
    return retained


def _find_after(
    events: list[dict[str, str]],
    start: int,
    predicate: Callable[[dict[str, str]], bool],
) -> tuple[int, dict[str, str]] | None:
    for index in range(start + 1, len(events)):
        if predicate(events[index]):
            return index, events[index]
    return None


def _find_before(
    events: list[dict[str, str]],
    stop: int,
    predicate: Callable[[dict[str, str]], bool],
) -> tuple[int, dict[str, str]] | None:
    for index in range(stop - 1, -1, -1):
        if predicate(events[index]):
            return index, events[index]
    return None


def _selection_gesture(event: dict[str, str]) -> bool:
    return event.get("type") == "gesture" and event.get("delivery") in {"menu", "list"}


def _summary(
    result: str,
    retained: list[dict[str, str]],
    *,
    physical: bool,
    core_result: str,
    outcome: str | None = None,
    recovery: str = "not_exercised",
    lens_video_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "schema": RUN_SCHEMA,
        "mode": "physical_g2",
        "result": result,
        "coreResult": core_result,
        "physicalG2Evidence": physical,
        "messagesAppSend": "not_requested" if result == "pass" else "not_proven",
        "confirmationOutcome": outcome,
        "recovery": recovery,
        "wearerVisibleHud": {
            "bridge": "verified" if result == "pass" else "not_verified",
            "lens": "video_digest_recorded" if lens_video_sha256 else "not_recorded",
        },
        "lensVideoSha256": lens_video_sha256,
        "retainedEventCount": len(retained),
        "events": retained,
    }


def summarize_receipt(
    receipt: dict[str, object],
    *,
    lens_video_sha256: str | None = None,
    require_recovery: bool = False,
) -> dict[str, object]:
    """Validate a physical no-send list → reply/compose → cancel route.

    ``accepted`` can initiate the real macOS Messages handoff, so it is never
    labelled a no-send pass.  A decline or timeout is the safe field-run
    ending: the production confirmation gate has returned without dispatch.
    """

    if lens_video_sha256 is not None:
        _sha256(lens_video_sha256, "lensVideoSha256")
    retained = _retained(receipt)
    session = next(
        (
            (index, event) for index, event in enumerate(retained)
            if event["type"] == "g2_session" and event["source"] == "physical_g2"
        ),
        None,
    )
    if session is None:
        return _summary(
            "inconclusive", retained, physical=False, core_result="not_started",
            lens_video_sha256=lens_video_sha256,
        )

    recovery_seen = any(
        event["type"] == "visual_frame" and event.get("surface") in _RECOVERY_SURFACES
        for event in retained[session[0] + 1:]
    )
    recovery = "exercised" if recovery_seen else "not_exercised"
    picker = _find_after(
        retained, session[0],
        lambda event: event["type"] == "visual_frame" and event.get("surface") == "picker",
    )
    if picker is None or _find_before(retained, picker[0], _selection_gesture) is None:
        return _summary(
            "inconclusive", retained, physical=True, core_result="not_started",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    conversation = _find_after(
        retained, picker[0],
        lambda event: event["type"] == "visual_frame" and event.get("surface") == "conversation",
    )
    if conversation is None:
        return _summary(
            "fail", retained, physical=True, core_result="failed",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    if _find_before(retained, conversation[0], _selection_gesture) is None:
        return _summary(
            "fail", retained, physical=True, core_result="failed",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    compose = _find_after(
        retained, conversation[0],
        lambda event: event["type"] == "visual_frame" and event.get("surface") == "compose",
    )
    confirmation_start = compose[0] if compose is not None else conversation[0]
    confirmation = _find_after(
        retained, confirmation_start,
        lambda event: event["type"] == "visual_frame" and event.get("surface") == "confirmation",
    )
    if confirmation is None:
        return _summary(
            "inconclusive", retained, physical=True, core_result="not_complete",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    visible_frames = [picker[1], conversation[1], confirmation[1]]
    if compose is not None:
        visible_frames.append(compose[1])
    if len({frame["sha256"] for frame in visible_frames}) != len(visible_frames):
        return _summary(
            "fail", retained, physical=True, core_result="failed",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    outcome = _find_after(
        retained, confirmation[0],
        lambda event: event["type"] == "messages_confirmation",
    )
    if outcome is None:
        return _summary(
            "inconclusive", retained, physical=True, core_result="not_complete",
            recovery=recovery, lens_video_sha256=lens_video_sha256,
        )
    if outcome[1]["outcome"] not in _NO_SEND_OUTCOMES:
        return _summary(
            "inconclusive", retained, physical=True, core_result="not_no_send",
            outcome=outcome[1]["outcome"], recovery=recovery,
            lens_video_sha256=lens_video_sha256,
        )
    returned = _find_after(
        retained, outcome[0],
        lambda event: event["type"] == "visual_frame"
        and event.get("surface") in {"picker", "conversation", "compose"},
    )
    if returned is None:
        return _summary(
            "fail", retained, physical=True, core_result="failed",
            outcome=outcome[1]["outcome"], recovery=recovery,
            lens_video_sha256=lens_video_sha256,
        )
    result = "pass" if recovery_seen or not require_recovery else "inconclusive"
    return _summary(
        result, retained, physical=True, core_result="pass",
        outcome=outcome[1]["outcome"], recovery=recovery,
        lens_video_sha256=lens_video_sha256,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise FileExistsError("refusing to overwrite an acceptance receipt")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="owner-only --g2-diagnostics trace")
    parser.add_argument("--out", type=Path, required=True, help="new owner-only summary receipt")
    parser.add_argument("--lens-video", type=Path, help="optional physical lens recording; only its SHA-256 is retained")
    parser.add_argument("--require-recovery", action="store_true", help="require an actual safe recovery surface")
    args = parser.parse_args()
    try:
        receipt = _object(json.loads(args.trace.read_text(encoding="utf-8")), "trace must be a JSON object")
        summary = summarize_receipt(
            receipt,
            lens_video_sha256=_file_sha256(args.lens_video) if args.lens_video else None,
            require_recovery=args.require_recovery,
        )
        _write_owner_only_json(args.out, summary)
    except (FileNotFoundError, FileExistsError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0 if summary["result"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
