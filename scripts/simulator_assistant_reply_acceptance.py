#!/usr/bin/env python3
"""Capture and verify JOAT's retained conversation in the official simulator.

This runner exercises production Mac orchestration, the production WebSocket
transport, the Hub plug-in, the Even SDK bridge, the official simulator
framebuffer, and the simulator's actual input endpoint. Speech recognition and
model chunks are deterministic so screenshots can be compared to exact text.

The receipt is simulator evidence. It deliberately does not claim physical G2
latency, optical brightness, packaged-build parity, or a portal deployment.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time
from typing import Any, cast
import urllib.error

from joat.audio.vad import RmsVad
from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.interfaces import ReplyEngine, SttEngine, Transcript
from joat.core.protocol import (
    AssistantConversationDismiss,
    AssistantConversationView,
    DownlinkMessage,
    InputEvent,
)
from joat.services.orchestrator import VoiceOrchestrator
from scripts.hud_visual_evidence import decode_rgba8_png
from scripts.simulator_brainstorm_action_flow_acceptance import (
    BrainstormActionFlowError,
    ManagedProcess,
    _capture,
    _http_bytes,
    _input,
    _optical_preview,
    _port_busy,
    _prepare_simulator_plugin_root,
    _start_process,
    _wait_for,
    _wait_http,
)


OPENING_UTTERANCE = "JOAT, what is the capital of France?"
OPENING_PROMPT = "what is the capital of France?"
OPENING_ANSWER = "Paris."
JOKE_PROMPT = "tell me a developer joke"
JOKE_SETUP = "Why did the developer bring glasses?"
JOKE_PUNCHLINE = "Because every bug deserved a closer look."
JOKE_ANSWER = f"{JOKE_SETUP} {JOKE_PUNCHLINE}"
LONG_PROMPT = "in four full sentences, explain why the sky is blue and sunsets are red"
LONG_ANSWER = (
    "Sunlight contains many colors, and Earth's atmosphere scatters shorter blue "
    "wavelengths more strongly than longer red wavelengths, making the daytime sky look blue. "
    "At sunset, sunlight travels through more atmosphere, so most blues are scattered away "
    "before the light reaches you. The remaining direct light is richer in reds and oranges, "
    "which paints the Sun and nearby sky in warm colors. Dust, smoke, clouds, and pollution "
    "can intensify or mute those sunset colors."
)
EXPECTED_STATES = [
    "thinking", "answering", "ready",
    "thinking", "answering", "answering", "ready",
    "thinking", "answering", "answering", "ready",
]
EXPECTED_REVISIONS = list(range(1, 12))
FRAME_MARKER = "[assistant-raster-frame]"
BRIDGE_MARKER = "[assistant-raster-bridge]"
CONVERSATION_PIXEL_REGION = (35, 50, 541, 263)
# The dashboard clock is intentionally live during this minute-long journey.
# Everything outside its authored region must return byte-for-byte after the
# conversation is dismissed; the clock itself must remain visibly populated.
DASHBOARD_LIVE_CLOCK_REGION = (60, 60, 225, 235)
INTER_TILE_STABLE_S = 0.24
ACTIVITY_FIXTURE_DELAY_S = 3.2
VOICE_BATCH_SAMPLES = 3_200
LOUD_PCM = struct.pack(
    f"<{VOICE_BATCH_SAMPLES}h",
    *(8_000 if index % 2 == 0 else -8_000 for index in range(VOICE_BATCH_SAMPLES)),
)
QUIET_PCM = b"\x00\x00" * VOICE_BATCH_SAMPLES
class SequenceStt(SttEngine):
    def __init__(self, transcripts: list[str]) -> None:
        self.transcripts = deque(transcripts)
        self.received = False

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.received = self.received or bool(pcm)
        return None

    async def flush(self) -> Transcript | None:
        if not self.received or not self.transcripts:
            return None
        self.received = False
        return Transcript(text=self.transcripts.popleft(), is_final=True)

    def reset(self) -> None:
        self.received = False


class ScriptedReplyEngine(ReplyEngine):
    """Expose thinking and partial-answer frames without an external service."""

    async def reply(self, text: str) -> str | None:
        raise AssertionError("stream owns this acceptance")

    async def stream(self, text: str) -> AsyncIterator[str]:
        # The opening turn deliberately leaves enough quiet time for a fully
        # accepted activity-only frame. Later turns keep the shorter streaming
        # fixture used by the response-latency ledger.
        await asyncio.sleep(ACTIVITY_FIXTURE_DELAY_S if text == OPENING_PROMPT else 1.8)
        if text == OPENING_PROMPT:
            yield OPENING_ANSWER
            return
        if text == JOKE_PROMPT:
            yield f"{JOKE_SETUP} "
            await asyncio.sleep(1.5)
            yield JOKE_PUNCHLINE
            return
        if text == LONG_PROMPT:
            first, rest = LONG_ANSWER.split(". ", 1)
            yield f"{first}. "
            await asyncio.sleep(0.3)
            yield rest
            return
        raise AssertionError(f"unexpected deterministic query: {text!r}")


class RecordingGateway(HubGatewayTransport):
    def __init__(self, port: int) -> None:
        super().__init__(host="127.0.0.1", port=port)
        self.sent: list[DownlinkMessage] = []
        self.conversations: list[dict[str, Any]] = []
        self.inputs: list[InputEvent] = []

    async def send(self, message: DownlinkMessage) -> None:
        started = time.monotonic()
        await super().send(message)
        completed = time.monotonic()
        self.sent.append(message)
        if isinstance(message, AssistantConversationView):
            self.conversations.append({
                "at": started,
                "completedAt": completed,
                "conversationId": message.conversation_id,
                "revision": message.revision,
                "state": message.state,
                "turns": [
                    {"role": turn.role, "text": turn.text}
                    for turn in message.turns
                ],
                "continueAfterMs": message.continue_after_ms,
                "earlierTurnsOmitted": message.earlier_turns_omitted,
            })


def _inject_fixture_probe(root: Path) -> None:
    """Instrument only the disposable source copy used by this fixture."""

    presenter = root / "src/HudPresenter.ts"
    presenter_source = presenter.read_text(encoding="utf-8")
    presenter_needle = """    const frame: HudVisualFrame = {
      ...this.visualFrame(),
      assistantConversation: conversation,
    };"""
    if presenter_source.count(presenter_needle) != 1:
        raise BrainstormActionFlowError("could not place disposable raster-frame probe")
    presenter_probe = f"""    console.info('{FRAME_MARKER}' + JSON.stringify({{
      ...conversation, paintRequestedAtUnixMs: Date.now(),
    }}));
{presenter_needle}"""
    presenter.write_text(
        presenter_source.replace(presenter_needle, presenter_probe),
        encoding="utf-8",
    )

    main = root / "src/main.ts"
    source = main.read_text(encoding="utf-8")
    needle = "  await client.init();"
    if source.count(needle) != 1:
        raise BrainstormActionFlowError("could not place disposable SDK bridge probe")
    probe = f"""
  const fixtureRequire = client.require.bind(client);
  const fixtureBridge = fixtureRequire() as any;
  const fixtureRecord = (value: unknown) =>
    console.info('{BRIDGE_MARKER}' + JSON.stringify(value));
  const fixtureStartup = async (page: any) => {{
    const result = await fixtureBridge.createStartUpPageContainer(page);
    fixtureRecord({{
      operation: 'startup', result: String(result), acceptedAtUnixMs: Date.now(),
      imageCount: (page.imageObject ?? []).length,
      textCount: (page.textObject ?? []).length,
      menuCount: (page.menuObject?.menuItems ?? []).length,
    }});
    return result;
  }};
  const fixtureRebuild = async (page: any) => {{
    const ok = await fixtureBridge.rebuildPageContainer(page);
    fixtureRecord({{
      operation: 'rebuild', ok, acceptedAtUnixMs: Date.now(),
      imageCount: (page.imageObject ?? []).length,
      textCount: (page.textObject ?? []).length,
      menuCount: (page.menuObject?.menuItems ?? []).length,
    }});
    return ok;
  }};
  const fixtureImage = async (update: any) => {{
    const result = await fixtureBridge.updateImageRawData(update);
    fixtureRecord({{
      operation: 'image', acceptedAtUnixMs: Date.now(),
      containerID: update.containerID, result: String(result),
    }});
    return result;
  }};
  const fixtureObservedBridge = new Proxy(fixtureBridge, {{
    get(target, property) {{
      if (property === 'createStartUpPageContainer') return fixtureStartup;
      if (property === 'rebuildPageContainer') return fixtureRebuild;
      if (property === 'updateImageRawData') return fixtureImage;
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    }},
  }});
  (client as any).require = () => fixtureObservedBridge;
"""
    main.write_text(source.replace(needle, f"{needle}{probe}"), encoding="utf-8")


async def _console_payloads(automation_base: str, marker: str) -> list[dict[str, Any]]:
    raw = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrainstormActionFlowError("official simulator returned invalid console JSON") from exc
    entries = decoded.get("entries") if isinstance(decoded, dict) else None
    if not isinstance(entries, list):
        raise BrainstormActionFlowError("official simulator console has no entries")
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        message = cast(str, entry["message"])
        offset = message.find(marker)
        if offset < 0:
            continue
        try:
            payload = json.loads(message[offset + len(marker):].strip())
        except json.JSONDecodeError as exc:
            raise BrainstormActionFlowError(f"invalid fixture probe payload for {marker}") from exc
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


async def _poll_raster_frame(
    automation_base: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    label: str,
    after: int = 0,
    timeout_s: float = 12.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            frames = await _console_payloads(automation_base, FRAME_MARKER)
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.05)
            continue
        for item in reversed(frames[after:]):
            if predicate(item):
                return item
        await asyncio.sleep(0.05)
    raise BrainstormActionFlowError(f"timed out waiting for {label}")


async def _screenshot(automation_base: str) -> bytes:
    return await asyncio.to_thread(
        _http_bytes,
        f"{automation_base}/api/screenshot/glasses",
    )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _region_digest(raw: bytes, *, left: int, top: int, right: int, bottom: int) -> str:
    width, height, rgba = decode_rgba8_png(raw)
    left = max(0, min(width, left))
    right = max(left, min(width, right))
    top = max(0, min(height, top))
    bottom = max(top, min(height, bottom))
    digest = hashlib.sha256()
    for y in range(top, bottom):
        start = (y * width + left) * 4
        end = (y * width + right) * 4
        digest.update(rgba[start:end])
    return digest.hexdigest()


def _changed_tile_regions(before: bytes, after: bytes) -> list[int]:
    """Return the production image-container ids whose framebuffer pixels differ."""

    before_width, before_height, before_rgba = decode_rgba8_png(before)
    after_width, after_height, after_rgba = decode_rgba8_png(after)
    if (before_width, before_height) != (after_width, after_height):
        raise BrainstormActionFlowError("activity frame dimensions changed")
    changed: list[int] = []
    for container_id, left, top in (
        (4, 0, 0), (8, 288, 0), (9, 0, 144), (10, 288, 144),
    ):
        different = False
        for y in range(top, min(top + 144, after_height)):
            start = (y * after_width + left) * 4
            end = (y * after_width + min(left + 288, after_width)) * 4
            if before_rgba[start:end] != after_rgba[start:end]:
                different = True
                break
        if different:
            changed.append(container_id)
    return changed


def _assert_dashboard_underlay_restored(before: bytes, after: bytes) -> None:
    """Prove dismissal restored the dashboard, allowing only its live clock."""

    before_width, before_height, before_rgba = decode_rgba8_png(before)
    after_width, after_height, after_rgba = decode_rgba8_png(after)
    if (before_width, before_height) != (after_width, after_height):
        raise BrainstormActionFlowError("restored underlay dimensions changed")
    left, top, right, bottom = DASHBOARD_LIVE_CLOCK_REGION
    changed_outside_clock = 0
    for y in range(after_height):
        for x in range(after_width):
            if left <= x < right and top <= y < bottom:
                continue
            offset = (y * after_width + x) * 4
            if before_rgba[offset:offset + 4] != after_rgba[offset:offset + 4]:
                changed_outside_clock += 1
    if changed_outside_clock:
        raise BrainstormActionFlowError(
            "dismissal did not restore the exact dashboard outside its live clock: "
            f"{changed_outside_clock} pixels changed"
        )
    lit_clock_pixels = sum(
        1
        for y in range(top, bottom)
        for x in range(left, right)
        if after_rgba[(y * after_width + x) * 4 + 3] >= 100
    )
    if lit_clock_pixels < 1_000:
        raise BrainstormActionFlowError("restored dashboard clock is blank or incomplete")


def _assert_context_menu_visible(raw: bytes) -> int:
    """Bind the SDK menu declaration to the menu pixels drawn by the simulator."""

    width, height, rgba = decode_rgba8_png(raw)
    divider_x = 227
    if width <= divider_x or height < 280:
        raise BrainstormActionFlowError("context menu framebuffer dimensions changed")
    divider_pixels = sum(
        1 for y in range(height)
        if rgba[(y * width + divider_x) * 4 + 3] >= 100
    )
    if divider_pixels < 250:
        raise BrainstormActionFlowError("context menu divider was not visibly drawn")
    visible_row_bands = 0
    for top, bottom in ((28, 60), (78, 110), (128, 160), (178, 210), (228, 260)):
        lit = sum(
            1
            for y in range(top, min(bottom, height))
            for x in range(16, min(215, width))
            if rgba[(y * width + x) * 4 + 3] >= 100
        )
        if lit >= 100:
            visible_row_bands += 1
    if visible_row_bands != 5:
        raise BrainstormActionFlowError(
            f"context menu showed {visible_row_bands} populated row bands instead of five"
        )
    return visible_row_bands


async def _changed_frame(
    automation_base: str,
    prior: str,
    *,
    label: str,
    timeout_s: float = 12.0,
    stable_s: float = 0.16,
    region: tuple[int, int, int, int] | None = None,
    accept: Callable[[bytes], bool] | None = None,
) -> bytes:
    deadline = time.monotonic() + timeout_s
    candidate = ""
    candidate_since = 0.0
    latest: bytes | None = None
    while time.monotonic() < deadline:
        try:
            raw = await _screenshot(automation_base)
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.04)
            continue
        digest = (
            _region_digest(
                raw,
                left=region[0],
                top=region[1],
                right=region[2],
                bottom=region[3],
            )
            if region is not None else _digest(raw)
        )
        if digest == prior or (accept is not None and not accept(raw)):
            await asyncio.sleep(0.04)
            continue
        now = time.monotonic()
        latest = raw
        if digest != candidate:
            candidate = digest
            candidate_since = now
        elif now - candidate_since >= stable_s:
            return latest
        await asyncio.sleep(0.04)
    raise BrainstormActionFlowError(f"{label}: framebuffer never changed")


def _conversation_pixel_digest(raw: bytes) -> str:
    left, top, right, bottom = CONVERSATION_PIXEL_REGION
    return _region_digest(raw, left=left, top=top, right=right, bottom=bottom)


async def _changed_conversation_frame(
    automation_base: str,
    prior_raw: bytes,
    *,
    label: str,
    timeout_s: float = 12.0,
    stable_s: float = INTER_TILE_STABLE_S,
    model: dict[str, Any] | None = None,
) -> bytes:
    """Wait for complete conversation pixels, ignoring status-only changes.

    The default stable window outlasts the production 110ms inter-tile
    pacing. Initial-card and one-tile activity captures may request the short
    two-sample window because their full edge geometry or quadrant invariant
    independently rejects a partial frame.
    """

    return await _changed_frame(
        automation_base,
        _conversation_pixel_digest(prior_raw),
        label=label,
        timeout_s=timeout_s,
        stable_s=max(0.04, stable_s),
        region=CONVERSATION_PIXEL_REGION,
        accept=(
            (lambda raw: _conversation_card_matches(raw, model))
            if model is not None else None
        ),
    )


def _save_frame(raw: bytes, output: Path, name: str) -> dict[str, Any]:
    raw_path = output / f"{name}.rgba.png"
    raw_path.write_bytes(raw)
    raw_path.chmod(0o600)
    optical = _optical_preview(raw_path)
    width, height, rgba = decode_rgba8_png(raw)
    visible = [
        (index // 4 % width, index // 4 // width)
        for index in range(0, len(rgba), 4)
        if rgba[index + 3] > 0
    ]
    return {
        "sha256": _digest(raw),
        "raw": raw_path.name,
        "optical": optical.name,
        "visibleBounds": {
            "left": min(x for x, _ in visible),
            "top": min(y for _, y in visible),
            "right": max(x for x, _ in visible),
            "bottom": max(y for _, y in visible),
        } if visible else None,
    }


def _conversation_card_matches(raw: bytes, model: dict[str, Any]) -> bool:
    capacity = model.get("lineCapacity")
    if not isinstance(capacity, int):
        return False
    width, height, rgba = decode_rgba8_png(raw)
    top = 260 - (13 + capacity * 18 + 28)
    points = (
        (100, top), (288, top), (500, top),
        (36, top + 6), (539, top + 6),
        (100, 260), (500, 260),
    )
    return all(
        0 <= x < width and 0 <= y < height
        and rgba[(y * width + x) * 4 + 3] >= 100
        for x, y in points
    )


def _assert_conversation_card(
    raw: bytes,
    model: dict[str, Any],
    *,
    label: str,
) -> None:
    """Bind a model probe to the card geometry present in its screenshot."""

    if not _conversation_card_matches(raw, model):
        raise BrainstormActionFlowError(
            f"{label}: framebuffer does not contain the probed card geometry"
        )


def _role_pixel_stats(raw: bytes, line_capacity: int) -> dict[str, Any]:
    """Measure the two deterministic opening rows in actual framebuffer pixels."""

    width, _, rgba = decode_rgba8_png(raw)
    top = 260 - (13 + line_capacity * 18 + 28)
    baselines = {"user": top + 20, "assistant": top + 38}
    result: dict[str, Any] = {}
    for role, baseline in baselines.items():
        values: list[int] = []
        ink_rows: set[int] = set()
        for y in range(baseline - 8, baseline + 9):
            for x in range(54, 520):
                alpha = rgba[(y * width + x) * 4 + 3]
                if alpha:
                    values.append(alpha)
                    ink_rows.add(y)
        if not values:
            raise BrainstormActionFlowError(f"opening {role} row has no framebuffer ink")
        values.sort()
        result[role] = {
            "medianAlpha": values[len(values) // 2],
            "maximumAlpha": values[-1],
            "glyphHeightPx": max(ink_rows) - min(ink_rows) + 1,
        }
    if result["assistant"]["medianAlpha"] <= result["user"]["medianAlpha"]:
        raise BrainstormActionFlowError("assistant row is not brighter than wearer row")
    return result


def _normalized_role_text(rows: list[dict[str, str]], role: str) -> str:
    return " ".join(" ".join(
        row["text"] for row in rows if row["role"] == role
    ).split())


def _latest_conversation(
    gateway: RecordingGateway,
    *,
    after: int = 0,
    state: str | None = None,
    answer: str | None = None,
) -> dict[str, Any] | None:
    for item in reversed(gateway.conversations[after:]):
        if state is not None and item["state"] != state:
            continue
        turns = cast(list[dict[str, str]], item["turns"])
        if answer is not None and not any(
            turn == {"role": "assistant", "text": answer} for turn in turns
        ):
            continue
        return item
    return None


@dataclass
class FramebufferLatencyProbe:
    automation_base: str
    baseline: str
    reference: float | None = None
    first_changed_at: float | None = None
    stable_at: float | None = None

    def __post_init__(self) -> None:
        self._candidate = ""
        self._candidate_since = 0.0
        self._done = asyncio.Event()
        self._task = asyncio.create_task(self._sample())

    @classmethod
    async def start(cls, automation_base: str) -> "FramebufferLatencyProbe":
        return cls(
            automation_base,
            _conversation_pixel_digest(await _screenshot(automation_base)),
        )

    def mark_closed(self, when: float) -> None:
        self.reference = when

    async def result(self, timeout_s: float = 4.0) -> dict[str, int]:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            self._task.cancel()
            raise BrainstormActionFlowError("no stable transcript pixels after VAD closure") from exc
        await self._task
        if self.reference is None or self.first_changed_at is None or self.stable_at is None:
            raise BrainstormActionFlowError("incomplete framebuffer latency span")
        return {
            "firstPixelsMs": round((self.first_changed_at - self.reference) * 1_000),
            "stablePixelsMs": round((self.stable_at - self.reference) * 1_000),
        }

    async def _sample(self) -> None:
        while not self._done.is_set():
            tick = time.monotonic()
            try:
                digest = _conversation_pixel_digest(
                    await _screenshot(self.automation_base)
                )
            except (OSError, urllib.error.URLError):
                await asyncio.sleep(0.04)
                continue
            now = time.monotonic()
            if self.reference is None:
                self.baseline = digest
            elif digest != self.baseline:
                if self.first_changed_at is None:
                    self.first_changed_at = now
                if digest != self._candidate:
                    self._candidate = digest
                    self._candidate_since = now
                elif now - self._candidate_since >= INTER_TILE_STABLE_S:
                    self.stable_at = now
                    self._done.set()
                    return
            await asyncio.sleep(max(0.0, 0.04 - (time.monotonic() - tick)))


async def _drive_voice_turn(
    orchestrator: VoiceOrchestrator,
    next_seq: int,
) -> tuple[int, asyncio.Task[None], float, float]:
    for pcm in (LOUD_PCM, QUIET_PCM, QUIET_PCM, QUIET_PCM):
        orchestrator.on_audio(pcm, next_seq)
        next_seq += 1
    closed_monotonic = time.monotonic()
    closed_unix_ms = time.time() * 1_000
    worker = orchestrator._utterance_worker
    if worker is None:
        raise BrainstormActionFlowError("public audio ingress scheduled no utterance worker")
    return next_seq, worker, closed_monotonic, closed_unix_ms


async def _bridge_image_latency(
    automation_base: str,
    *,
    after: int,
    closed_unix_ms: float,
    paint_requested_unix_ms: int,
) -> int:
    event = await _poll_bridge_image(
        automation_base,
        after=after,
        accepted_after_unix_ms=paint_requested_unix_ms,
    )
    accepted = event.get("acceptedAtUnixMs")
    if not isinstance(accepted, int):
        raise BrainstormActionFlowError("bridge image receipt has no timestamp")
    elapsed = round(accepted - closed_unix_ms)
    if elapsed < 0:
        raise BrainstormActionFlowError("raster image preceded utterance closure")
    return elapsed


async def _poll_bridge_image(
    automation_base: str,
    *,
    after: int,
    timeout_s: float = 6.0,
    accepted_after_unix_ms: int = 0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = await _console_payloads(automation_base, BRIDGE_MARKER)
        for event in events[after:]:
            if (
                event.get("operation") == "image"
                and isinstance(event.get("acceptedAtUnixMs"), int)
                and cast(int, event["acceptedAtUnixMs"]) >= accepted_after_unix_ms
            ):
                return event
        await asyncio.sleep(0.04)
    raise BrainstormActionFlowError("timed out waiting for accepted raster image")


async def _scroll_to_boundary(
    automation_base: str,
    *,
    direction: str,
    start_frame: dict[str, Any],
    start_raw: bytes,
) -> tuple[int, dict[str, Any], bytes, list[dict[str, str]]]:
    current = start_frame
    current_raw = start_raw
    reconstructed = [dict(line) for line in cast(list[dict[str, str]], current["visibleLines"])]
    steps = 0
    while steps < 40:
        maximum = cast(int, current["maximumScrollFromTail"])
        position = cast(int, current["scrollFromTail"])
        at_boundary = position == (maximum if direction == "up" else 0)
        before_frames = len(await _console_payloads(automation_base, FRAME_MARKER))
        await _input(automation_base, direction)
        if at_boundary:
            await asyncio.sleep(0.3)
            after_frames = len(await _console_payloads(automation_base, FRAME_MARKER))
            if after_frames != before_frames:
                raise BrainstormActionFlowError(f"{direction} repainted beyond its boundary")
            return steps, current, current_raw, reconstructed
        next_frame = await _poll_raster_frame(
            automation_base,
            lambda item: (
                item.get("conversationId") == current.get("conversationId")
                and item.get("revision") == current.get("revision")
                and item.get("scrollFromTail") != current.get("scrollFromTail")
            ),
            label=f"{direction} conversation viewport",
            after=before_frames,
        )
        next_raw = await _changed_conversation_frame(
            automation_base,
            current_raw,
            label=f"{direction} framebuffer step",
            model=next_frame,
        )
        previous_rows = cast(list[dict[str, str]], current["visibleLines"])
        next_rows = cast(list[dict[str, str]], next_frame["visibleLines"])
        if direction == "up":
            if next_rows[1:] != previous_rows[:-1]:
                raise BrainstormActionFlowError("scroll up skipped or reordered a row")
            reconstructed.insert(0, dict(next_rows[0]))
        elif next_rows[:-1] != previous_rows[1:]:
            raise BrainstormActionFlowError("scroll down skipped or reordered a row")
        current = next_frame
        current_raw = next_raw
        steps += 1
    raise BrainstormActionFlowError(f"{direction} never reached a bounded scroll position")


async def _continue_after_inactivity(
    automation_base: str,
    gateway: RecordingGateway,
    *,
    prior_raw: bytes,
    output: Path,
    name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for the inactivity question, then accept it with a real tap."""

    before_inputs = len(gateway.inputs)
    before_frames = len(await _console_payloads(automation_base, FRAME_MARKER))
    prompt_model = await _poll_raster_frame(
        automation_base,
        lambda item: item.get("continuePrompt") is True,
        label=f"{name} continue prompt",
        after=before_frames,
        timeout_s=20.0,
    )
    prompt_raw = await _changed_conversation_frame(
        automation_base,
        prior_raw,
        label=f"{name} continue prompt pixels",
        model=prompt_model,
    )
    _assert_conversation_card(prompt_raw, prompt_model, label=f"{name} continue prompt")
    prompt_frame = _save_frame(prompt_raw, output, f"{name}-continue-prompt")
    if len(gateway.inputs) != before_inputs:
        raise BrainstormActionFlowError("inactivity prompt emitted an action without a tap")
    await _input(automation_base, "click")
    await _wait_for(
        lambda: gateway.inputs[-1]
        if len(gateway.inputs) > before_inputs
        and gateway.inputs[-1].selected_name == "continue"
        else None,
        label=f"{name} continue action at Mac",
    )
    resumed_raw = await _changed_conversation_frame(
        automation_base,
        prompt_raw,
        label=f"{name} continued pixels",
        model=prompt_model,
    )
    _assert_conversation_card(resumed_raw, prompt_model, label=f"{name} continued")
    line_capacity = cast(int, prompt_model["lineCapacity"])
    card_top = 260 - (13 + line_capacity * 18 + 28)
    if _region_digest(
        resumed_raw, left=36, top=card_top, right=541, bottom=263,
    ) != _region_digest(
        prior_raw, left=36, top=card_top, right=541, bottom=263,
    ):
        raise BrainstormActionFlowError(
            f"{name} continuation did not restore the same conversation card"
        )
    return prompt_frame, {
        "model": prompt_model,
        "frame": _save_frame(resumed_raw, output, f"{name}-continued"),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    for port in (args.ws_port, args.vite_port, args.automation_port):
        if _port_busy(port):
            raise BrainstormActionFlowError(f"isolated acceptance port {port} is occupied")
    if not args.simulator.is_file():
        raise BrainstormActionFlowError("official evenhub-simulator is not installed")
    if args.out.exists() and any(args.out.iterdir()):
        raise BrainstormActionFlowError("output directory must be empty")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)

    processes: list[ManagedProcess] = []
    gateway = RecordingGateway(args.ws_port)
    orchestrator = VoiceOrchestrator(
        gateway,
        SequenceStt([OPENING_UTTERANCE, JOKE_PROMPT, LONG_PROMPT]),
        vad=RmsVad(adaptive=False, start_batches=1, hangover_batches=3),
        replier=ScriptedReplyEngine(),
        wake_words=("joat",),
        conversation_window_s=12.0,
    )

    def on_input(event: InputEvent) -> None:
        gateway.inputs.append(event)
        if (
            event.container_name == "assistant-conversation"
            and event.selected_name in {"continue", "exit"}
        ):
            orchestrator.handle_conversation_action(
                event.selected_name,
                event.conversation_id,
                event.conversation_revision,
            )

    gateway.on_input(on_input)
    source_paths = (
        "apps/hub-plugin/src/AssistantConversationSurface.ts",
        "apps/hub-plugin/src/EventRouter.ts",
        "apps/hub-plugin/src/HudCanvasRenderer.ts",
        "apps/hub-plugin/src/HudPresenter.ts",
        "apps/hub-plugin/src/HudTypography.ts",
        "apps/hub-plugin/src/HudVisualSurface.ts",
        "apps/hub-plugin/src/main.ts",
        "apps/hub-plugin/src/types.ts",
        "apps/mac/joat/core/protocol.py",
        "apps/mac/joat/services/hud.py",
        "apps/mac/joat/services/orchestrator.py",
        "scripts/simulator_assistant_reply_acceptance.py",
    )
    source_hashes = {
        path: hashlib.sha256((args.repo / path).read_bytes()).hexdigest()
        for path in source_paths
    }

    await gateway.start()
    try:
        with tempfile.TemporaryDirectory(prefix="joat-conversation-source-") as scratch:
            root = _prepare_simulator_plugin_root(
                args.repo,
                Path(scratch),
                f"ws://127.0.0.1:{args.ws_port}/glasses",
            )
            _inject_fixture_probe(root)
            env = os.environ.copy()
            env["VITE_MAC_WS_URL"] = f"ws://127.0.0.1:{args.ws_port}/glasses"
            vite = _start_process(
                "conversation-vite",
                [
                    "npx", "vite", str(root), "--host", "127.0.0.1",
                    "--port", str(args.vite_port), "--strictPort",
                ],
                cwd=args.repo / "apps/hub-plugin",
                env=env,
                log_path=args.out / "vite.log",
            )
            processes.append(vite)
            plugin_url = f"http://127.0.0.1:{args.vite_port}/"
            await _wait_http(plugin_url, vite)
            simulator = _start_process(
                "official-evenhub-simulator",
                [
                    str(args.simulator), plugin_url,
                    "--automation-port", str(args.automation_port),
                    "--aid", args.audio_input,
                ],
                cwd=args.repo,
                env=env,
                log_path=args.out / "simulator.log",
            )
            processes.append(simulator)
            base = f"http://127.0.0.1:{args.automation_port}"
            await _wait_http(f"{base}/api/ping", simulator)
            await _wait_for(
                lambda: True if gateway.connected else None,
                label="simulator plug-in WebSocket connection",
            )
            await orchestrator.on_connect()
            baseline_hash, baseline_raw_name, baseline_optical_name = await _capture(
                base, args.out, "00-baseline", None,
            )
            # Vite may perform one dev-only full reload while it notices the
            # disposable root's tsconfig. Establish the bridge-ledger boundary
            # after that startup settles; the captured 576x288 raster and the
            # later OS menu frame are the actual page/menu evidence.
            await asyncio.sleep(0.5)
            boot_bridge_events = await _console_payloads(base, BRIDGE_MARKER)
            boot_page_declarations = [
                item for item in boot_bridge_events
                if item.get("operation") in {"startup", "rebuild"}
            ]
            if not boot_page_declarations:
                raise BrainstormActionFlowError("mounted visual page has no SDK declaration")
            declared_menu_rows = boot_page_declarations[-1].get("menuCount")
            if declared_menu_rows != 8:
                raise BrainstormActionFlowError(
                    f"mounted visual page declared {declared_menu_rows!r} menu rows instead of eight"
                )
            journey_bridge_start = len(boot_bridge_events)

            next_seq = 0
            frames: dict[str, Any] = {
                "baseline": {
                    "sha256": baseline_hash,
                    "raw": baseline_raw_name,
                    "optical": baseline_optical_name,
                },
            }
            timings: dict[str, Any] = {}

            # Opening prompt and answer.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            probe = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            probe.mark_closed(closed)
            await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="opening thinking downlink",
            )
            thinking_model = await _poll_raster_frame(
                base,
                lambda item: item.get("state") == "thinking"
                and any(
                    line.get("role") == "user" and OPENING_PROMPT in line.get("text", "")
                    for line in item.get("visibleLines", [])
                ),
                label="opening prompt raster frame",
            )
            baseline_raw = (args.out / baseline_raw_name).read_bytes()
            thinking_raw = await _changed_conversation_frame(
                base,
                baseline_raw,
                label="opening thinking pixels",
                stable_s=0.04,
                model=thinking_model,
            )
            _assert_conversation_card(
                thinking_raw, thinking_model, label="opening thinking",
            )
            frames["openingThinking"] = _save_frame(
                thinking_raw, args.out, "01-opening-thinking",
            )
            next_indicator_raw = await _changed_conversation_frame(
                base,
                thinking_raw,
                label="advancing activity marker pixels",
                stable_s=0.04,
                model=thinking_model,
            )
            _assert_conversation_card(
                next_indicator_raw, thinking_model,
                label="opening activity tick",
            )
            activity_models = [
                item for item in await _console_payloads(base, FRAME_MARKER)
                if item.get("conversationId") == thinking_model.get("conversationId")
                and item.get("revision") == thinking_model.get("revision")
                and item.get("state") == "thinking"
                and item.get("indicatorFrame") != thinking_model.get("indicatorFrame")
            ]
            if not activity_models:
                raise BrainstormActionFlowError("activity pixels advanced without a model tick")
            if any(
                item.get("visibleLines") != thinking_model.get("visibleLines")
                for item in activity_models
            ):
                raise BrainstormActionFlowError("activity tick changed the transcript")
            frames["openingThinkingNext"] = _save_frame(
                next_indicator_raw, args.out, "01-opening-thinking-next",
            )
            activity_changed_tile_regions = _changed_tile_regions(
                thinking_raw, next_indicator_raw,
            )
            if activity_changed_tile_regions != [9]:
                raise BrainstormActionFlowError(
                    "activity-only pixels crossed more than the lower-left tile: "
                    f"{activity_changed_tile_regions!r}"
                )
            opening_latency = await probe.result()
            opening_latency["firstConversationImageAcceptedMs"] = await _bridge_image_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(int, thinking_model["paintRequestedAtUnixMs"]),
            )
            timings["openingPrompt"] = opening_latency
            # Geometry binds these screenshots to the one-line thinking card.
            # Do not infer display order from task.done() here: the fixture may
            # finish while the harness is hashing and saving an already-seen
            # frame.
            await task
            opening_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=OPENING_ANSWER,
            )
            if opening_ready is None:
                raise BrainstormActionFlowError("opening answer never reached ready")
            ready_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == opening_ready["revision"]
                and item.get("state") == "ready",
                label="opening ready raster frame",
            )
            opening_raw = await _changed_conversation_frame(
                base,
                next_indicator_raw,
                label="opening ready pixels",
                stable_s=0.45,
                model=ready_model,
            )
            _assert_conversation_card(
                opening_raw, ready_model, label="opening ready",
            )
            frames["openingReady"] = _save_frame(
                opening_raw, args.out, "02-opening-ready",
            )
            role_pixels = _role_pixel_stats(opening_raw, cast(int, ready_model["lineCapacity"]))
            if cast(int, thinking_model["lineCapacity"]) >= cast(int, ready_model["lineCapacity"]):
                raise BrainstormActionFlowError("opening card did not grow upward for the answer")
            opening_prompt_frame, opening_continued = await _continue_after_inactivity(
                base,
                gateway,
                prior_raw=opening_raw,
                output=args.out,
                name="03-opening",
            )
            frames["openingContinuePrompt"] = opening_prompt_frame
            frames["openingContinued"] = opening_continued["frame"]
            opening_continued_raw = (
                args.out / cast(str, frames["openingContinued"]["raw"])
            ).read_bytes()

            # Follow-up joke: retain the first pair and expose a partial answer.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            probe = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            probe.mark_closed(closed)
            joke_thinking = await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="joke thinking downlink",
            )
            joke_thinking_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == joke_thinking["revision"],
                label="retained joke prompt raster",
            )
            joke_thinking_raw = await _changed_conversation_frame(
                base,
                opening_continued_raw,
                label="joke thinking pixels",
                model=joke_thinking_model,
            )
            _assert_conversation_card(
                joke_thinking_raw, joke_thinking_model, label="joke thinking",
            )
            frames["jokeThinking"] = _save_frame(
                joke_thinking_raw, args.out, "03-joke-thinking",
            )
            joke_latency = await probe.result()
            joke_latency["firstConversationImageAcceptedMs"] = await _bridge_image_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(
                    int, joke_thinking_model["paintRequestedAtUnixMs"],
                ),
            )
            timings["jokePrompt"] = joke_latency
            setup = await _wait_for(
                lambda: _latest_conversation(
                    gateway, after=before, state="answering", answer=JOKE_SETUP,
                ),
                label="partial joke downlink",
            )
            setup_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == setup["revision"],
                label="partial joke raster frame",
            )
            setup_raw = await _changed_conversation_frame(
                base,
                joke_thinking_raw,
                label="partial joke pixels",
                model=setup_model,
            )
            _assert_conversation_card(
                setup_raw, setup_model, label="partial joke",
            )
            frames["jokeSetup"] = _save_frame(setup_raw, args.out, "04-joke-setup")
            joke_setup_pixels_ms = round((time.monotonic() - closed) * 1_000)
            # The five-line card geometry above distinguishes this partial
            # setup from the six-line completed joke. Task completion can race
            # with screenshot encoding after those pixels were already seen.
            await task
            joke_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=JOKE_ANSWER,
            )
            if joke_ready is None:
                raise BrainstormActionFlowError("complete joke never reached ready")
            joke_ready_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == joke_ready["revision"],
                label="complete joke raster frame",
            )
            joke_raw = await _changed_conversation_frame(
                base,
                setup_raw,
                label="complete joke pixels",
                stable_s=0.45,
                model=joke_ready_model,
            )
            _assert_conversation_card(
                joke_raw, joke_ready_model, label="complete joke",
            )
            frames["jokeReady"] = _save_frame(joke_raw, args.out, "05-joke-ready")
            joke_ready_pixels_ms = round((time.monotonic() - closed) * 1_000)
            timings["jokeAnswer"] = {
                "fixtureFirstChunkDownlinkMs": round(
                    (cast(float, setup["at"]) - closed) * 1_000
                ),
                "fixtureSetupStablePixelsMs": joke_setup_pixels_ms,
                "fixtureCompleteStablePixelsMs": joke_ready_pixels_ms,
            }
            if not (
                cast(int, joke_thinking_model["lineCapacity"])
                < cast(int, setup_model["lineCapacity"])
                <= cast(int, joke_ready_model["lineCapacity"])
            ):
                raise BrainstormActionFlowError("joke card did not grow with streamed answer lines")
            joke_prompt_frame, joke_continued = await _continue_after_inactivity(
                base,
                gateway,
                prior_raw=joke_raw,
                output=args.out,
                name="06-joke",
            )
            frames["jokeContinuePrompt"] = joke_prompt_frame
            frames["jokeContinued"] = joke_continued["frame"]
            joke_continued_raw = (
                args.out / cast(str, frames["jokeContinued"]["raw"])
            ).read_bytes()

            # Long answer: preserve the transcript and prove bounded scrolling.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            probe = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            probe.mark_closed(closed)
            long_thinking = await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="long thinking downlink",
            )
            long_thinking_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == long_thinking["revision"],
                label="long prompt raster frame",
            )
            long_thinking_raw = await _changed_conversation_frame(
                base,
                joke_continued_raw,
                label="long thinking pixels",
                model=long_thinking_model,
            )
            _assert_conversation_card(
                long_thinking_raw, long_thinking_model, label="long thinking",
            )
            frames["longThinking"] = _save_frame(
                long_thinking_raw, args.out, "06-long-thinking",
            )
            long_latency = await probe.result()
            long_latency["firstConversationImageAcceptedMs"] = await _bridge_image_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(
                    int, long_thinking_model["paintRequestedAtUnixMs"],
                ),
            )
            timings["longPrompt"] = long_latency
            await task
            long_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=LONG_ANSWER,
            )
            if long_ready is None:
                raise BrainstormActionFlowError("long answer never reached ready")
            long_model = await _poll_raster_frame(
                base,
                lambda item: item.get("revision") == long_ready["revision"]
                and item.get("state") == "ready",
                label="long answer raster frame",
            )
            long_raw = await _changed_conversation_frame(
                base,
                long_thinking_raw,
                label="long answer pixels",
                stable_s=0.45,
                model=long_model,
            )
            _assert_conversation_card(
                long_raw, long_model, label="long ready",
            )
            frames["longTail"] = _save_frame(long_raw, args.out, "07-long-tail")
            if cast(int, long_model["lineCapacity"]) != 9:
                raise BrainstormActionFlowError("long conversation did not reach the nine-line visual cap")
            if cast(int, long_model["maximumScrollFromTail"]) <= 0:
                raise BrainstormActionFlowError("long conversation has no scrollable overflow")

            up_steps, top_model, top_raw, reconstructed = await _scroll_to_boundary(
                base, direction="up", start_frame=long_model, start_raw=long_raw,
            )
            frames["longTop"] = _save_frame(top_raw, args.out, "08-long-top")
            expected_user = " ".join((OPENING_PROMPT, JOKE_PROMPT, LONG_PROMPT))
            expected_assistant = " ".join((OPENING_ANSWER, JOKE_ANSWER, LONG_ANSWER))
            if _normalized_role_text(reconstructed, "user") != expected_user:
                raise BrainstormActionFlowError("scrollback changed or omitted wearer text")
            if _normalized_role_text(reconstructed, "assistant") != expected_assistant:
                raise BrainstormActionFlowError("scrollback changed or omitted assistant text")
            down_steps, tail_model, restored_raw, _ = await _scroll_to_boundary(
                base, direction="down", start_frame=top_model, start_raw=top_raw,
            )
            if up_steps != down_steps or cast(int, tail_model["scrollFromTail"]) != 0:
                raise BrainstormActionFlowError("scroll did not return through the same bounded rows")
            if _conversation_pixel_digest(restored_raw) != _conversation_pixel_digest(long_raw):
                raise BrainstormActionFlowError(
                    "scroll cycle did not restore exact conversation-tail pixels"
                )
            frames["longTailRestored"] = _save_frame(
                restored_raw, args.out, "09-long-tail-restored",
            )

            # The simulator API does not identify a left-temple source for a
            # double-click, so it cannot prove JOAT's source-specific Back.
            # It can prove the inactivity question and its ordinary tap path.
            prompt_frame, continued = await _continue_after_inactivity(
                base,
                gateway,
                prior_raw=restored_raw,
                output=args.out,
                name="10-long",
            )
            frames["continuePrompt"] = prompt_frame
            frames["continued"] = continued["frame"]

            # Exercise the production Mac-to-Hub dismissal contract to prove
            # the latest feature state is restored without rebuilding the page.
            await gateway.send(AssistantConversationDismiss(
                conversation_id=cast(str, long_ready["conversationId"]),
                revision=cast(int, long_ready["revision"]),
            ))
            restored_underlay_raw = await _changed_conversation_frame(
                base,
                (
                    args.out / cast(str, frames["continued"]["raw"])
                ).read_bytes(),
                label="underlay restored after dismissal",
                stable_s=0.45,
            )
            _assert_dashboard_underlay_restored(baseline_raw, restored_underlay_raw)
            frames["underlayRestored"] = _save_frame(
                restored_underlay_raw, args.out, "12-underlay-restored",
            )

            # The official simulator draws the SDK menu itself. Capturing it
            # after the conversation journey proves our page still carries the
            # global menu declaration and that the gesture path remains live.
            await _input(base, "context_menu")
            menu_raw = await _changed_frame(
                base,
                cast(str, frames["underlayRestored"]["sha256"]),
                label="official contextual menu",
                stable_s=0.25,
            )
            frames["contextMenu"] = _save_frame(
                menu_raw, args.out, "13-context-menu",
            )
            visible_menu_row_bands = _assert_context_menu_visible(menu_raw)

            expected_turns = [
                {"role": "user", "text": OPENING_PROMPT},
                {"role": "assistant", "text": OPENING_ANSWER},
                {"role": "user", "text": JOKE_PROMPT},
                {"role": "assistant", "text": JOKE_ANSWER},
                {"role": "user", "text": LONG_PROMPT},
                {"role": "assistant", "text": LONG_ANSWER},
            ]
            if long_ready["turns"] != expected_turns:
                raise BrainstormActionFlowError("Mac transcript lost or rewrote a turn")
            states = [cast(str, item["state"]) for item in gateway.conversations]
            revisions = [cast(int, item["revision"]) for item in gateway.conversations]
            if states != EXPECTED_STATES or revisions != EXPECTED_REVISIONS:
                raise BrainstormActionFlowError(
                    f"conversation stream ledger drifted: {states!r}, {revisions!r}"
                )
            identities = {cast(str, item["conversationId"]) for item in gateway.conversations}
            if identities != {cast(str, opening_ready["conversationId"])}:
                raise BrainstormActionFlowError("follow-ups created detached conversation identities")

            bridge_events = await _console_payloads(base, BRIDGE_MARKER)
            journey_bridge_events = bridge_events[journey_bridge_start:]
            post_boot_rebuilds = [
                item for item in journey_bridge_events
                if item.get("operation") == "rebuild"
            ]
            if post_boot_rebuilds:
                raise BrainstormActionFlowError("conversation or exit replaced the mounted visual page")
            image_events = [
                item for item in journey_bridge_events if item.get("operation") == "image"
            ]
            if not image_events:
                raise BrainstormActionFlowError("no raster frame crossed the official SDK bridge")

            return {
                "schemaVersion": 4,
                "result": "official-simulator-retained-conversation-pass",
                "evidence": {
                    "renderer": "current source snapshot through JOAT four-tile raster renderer",
                    "transport": "production Mac protocol, WebSocket, Hub plug-in, Even SDK bridge",
                    "simulator": "official Even Realities simulator 0.9.3 framebuffer and input API",
                    "fixtureProbe": "disposable deterministic-source copy; absent from product source",
                    "sttAndModel": "deterministic local fixtures; no external AI service",
                    "physicalG2": "not run",
                    "packageUpload": "not run",
                },
                "checks": {
                    "conversationId": opening_ready["conversationId"],
                    "exactTranscript": expected_turns,
                    "streamStates": states,
                    "streamRevisions": revisions,
                    "singleConversationIdentity": True,
                    "cardGrowthLineCapacity": {
                        "openingThinking": thinking_model["lineCapacity"],
                        "openingReady": ready_model["lineCapacity"],
                        "jokeThinking": joke_thinking_model["lineCapacity"],
                        "jokeSetup": setup_model["lineCapacity"],
                        "jokeReady": joke_ready_model["lineCapacity"],
                        "longReady": long_model["lineCapacity"],
                    },
                    "rolePixels": role_pixels,
                    "scrollbar": {
                        "totalLines": long_model["totalLines"],
                        "visibleLines": long_model["lineCapacity"],
                        "upSteps": up_steps,
                        "downSteps": down_steps,
                        "topReached": top_model["scrollFromTail"]
                        == top_model["maximumScrollFromTail"],
                        "tailConversationPixelsRestoredExactly": (
                            _conversation_pixel_digest(restored_raw)
                            == _conversation_pixel_digest(long_raw)
                        ),
                    },
                    "partialJokeVisibleBeforeCompletion": True,
                    "activityIndicatorAdvanced": (
                        _conversation_pixel_digest(next_indicator_raw)
                        != _conversation_pixel_digest(thinking_raw)
                    ),
                    "activityIndicatorChangedTileRegions": (
                        activity_changed_tile_regions
                    ),
                    "inactivityContinuePrompt": True,
                    "continueReachedMac": True,
                    "dismissCommandRestoredUnderlay": True,
                    "underlayRestoredOutsideLiveClockExactly": True,
                    "restoredLiveClockPopulated": True,
                    "contextMenuDeclaredRows": declared_menu_rows,
                    "contextMenuVisibleRowBands": visible_menu_row_bands,
                    "contextMenuVisiblyOpened": True,
                    "nativePageRebuildsDuringJourney": len(post_boot_rebuilds),
                    "acceptedRasterImageOperations": len(image_events),
                },
                "timingsMs": {
                    "scope": "local deterministic STT/model through official simulator, from VAD closure",
                    "activityFixtureFirstChunkDelay": round(
                        ACTIVITY_FIXTURE_DELAY_S * 1_000
                    ),
                    **timings,
                },
                "frames": frames,
                "sourceSha256": source_hashes,
            }
    finally:
        for process in reversed(processes):
            process.stop()
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ws-port", type=int, default=8801)
    parser.add_argument("--vite-port", type=int, default=5301)
    parser.add_argument("--automation-port", type=int, default=9901)
    parser.add_argument(
        "--simulator",
        type=Path,
        default=Path.home() / ".local/bin/evenhub-simulator",
    )
    parser.add_argument("--audio-input", default="coreaudio:BuiltInMicrophoneDevice")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.out / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
