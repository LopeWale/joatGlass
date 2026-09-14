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
    AssistantConversationView,
    DownlinkMessage,
    InputEvent,
)
from joat.services.orchestrator import VoiceOrchestrator
from scripts.hud_visual_evidence import decode_rgba8_png
from scripts.simulator_brainstorm_action_flow_acceptance import (
    BrainstormActionFlowError,
    ManagedProcess,
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
VIEW_MARKER = "[assistant-native-view]"
BRIDGE_MARKER = "[assistant-native-bridge]"
CONVERSATION_PIXEL_REGION = (35, 50, 541, 263)
# The dashboard clock is intentionally live during this minute-long journey.
# Everything outside its authored region must return byte-for-byte after the
# conversation is dismissed; the clock itself must remain visibly populated.
DASHBOARD_LIVE_CLOCK_REGION = (60, 60, 225, 235)
NATIVE_STABLE_S = 0.12
ACTIVITY_FIXTURE_DELAY_S = 3.2
_CONSOLE_LAST_IDS: dict[str, int] = {}
_CONSOLE_LEDGERS: dict[tuple[str, str], list[dict[str, Any]]] = {}
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

    surface = root / "src/AssistantConversationSurface.ts"
    surface_source = surface.read_text(encoding="utf-8")
    surface_needle = """    const needsRebuild = this.pageLineCapacity !== view.lineCapacity || this.lastView === undefined;"""
    if surface_source.count(surface_needle) != 1:
        raise BrainstormActionFlowError("could not place disposable native-view probe")
    surface_probe = f"""    console.info('{VIEW_MARKER}' + JSON.stringify({{
      conversationId: current.conversationId,
      revision: current.revision,
      state: current.state,
      turns: current.turns,
      continuePrompt: this.continuePrompt,
      indicatorFrame: this.indicatorFrame,
      paintRequestedAtUnixMs: Date.now(),
      ...view,
    }}));
{surface_needle}"""
    surface.write_text(
        surface_source.replace(surface_needle, surface_probe),
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
  const fixturePage = (page: any) => ({{
    containerTotalNum: page.containerTotalNum,
    imageCount: (page.imageObject ?? []).length,
    text: (page.textObject ?? []).map((item: any) => ({{
      containerID: item.containerID,
      containerName: item.containerName,
      isEventCapture: item.isEventCapture,
      content: item.content,
    }})),
    list: (page.listObject ?? []).map((item: any) => ({{
      containerID: item.containerID,
      containerName: item.containerName,
      isEventCapture: item.isEventCapture,
      itemNames: item.itemContainer?.itemName ?? [],
    }})),
    menuCount: (page.menuObject?.menuItems ?? []).length,
  }});
  const fixtureStartup = async (page: any) => {{
    fixtureRecord({{ operation: 'startup-start', atUnixMs: Date.now(), ...fixturePage(page) }});
    const result = await fixtureBridge.createStartUpPageContainer(page);
    fixtureRecord({{
      operation: 'startup', result: String(result), acceptedAtUnixMs: Date.now(),
      ...fixturePage(page),
    }});
    return result;
  }};
  const fixtureRebuild = async (page: any) => {{
    fixtureRecord({{ operation: 'rebuild-start', atUnixMs: Date.now(), ...fixturePage(page) }});
    const ok = await fixtureBridge.rebuildPageContainer(page);
    fixtureRecord({{
      operation: 'rebuild', ok, acceptedAtUnixMs: Date.now(),
      ...fixturePage(page),
    }});
    return ok;
  }};
  const fixtureImage = async (update: any) => {{
    fixtureRecord({{
      operation: 'image-start', atUnixMs: Date.now(),
      containerID: update.containerID,
    }});
    const result = await fixtureBridge.updateImageRawData(update);
    fixtureRecord({{
      operation: 'image', acceptedAtUnixMs: Date.now(),
      containerID: update.containerID, result: String(result),
    }});
    return result;
  }};
  const fixtureText = async (update: any) => {{
    fixtureRecord({{
      operation: 'text-start', atUnixMs: Date.now(),
      containerID: update.containerID, containerName: update.containerName,
      content: update.content, contentLength: update.contentLength,
    }});
    const result = await fixtureBridge.textContainerUpgrade(update);
    fixtureRecord({{
      operation: 'text', acceptedAtUnixMs: Date.now(),
      containerID: update.containerID, containerName: update.containerName,
      content: update.content, contentLength: update.contentLength,
      result: String(result),
    }});
    return result;
  }};
  const fixtureObservedBridge = new Proxy(fixtureBridge, {{
    get(target, property) {{
      if (property === 'createStartUpPageContainer') return fixtureStartup;
      if (property === 'rebuildPageContainer') return fixtureRebuild;
      if (property === 'updateImageRawData') return fixtureImage;
      if (property === 'textContainerUpgrade') return fixtureText;
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    }},
  }});
  (client as any).require = () => fixtureObservedBridge;
"""
    main.write_text(source.replace(needle, f"{needle}{probe}"), encoding="utf-8")


async def _console_payloads(automation_base: str, marker: str) -> list[dict[str, Any]]:
    url = f"{automation_base}/api/console"
    if automation_base in _CONSOLE_LAST_IDS:
        url += f"?since_id={_CONSOLE_LAST_IDS[automation_base]}"
    raw = await asyncio.to_thread(_http_bytes, url)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrainstormActionFlowError("official simulator returned invalid console JSON") from exc
    entries = decoded.get("entries") if isinstance(decoded, dict) else None
    if not isinstance(entries, list):
        raise BrainstormActionFlowError("official simulator console has no entries")
    ids = [
        entry.get("id") for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
    ]
    if ids:
        _CONSOLE_LAST_IDS[automation_base] = max(cast(list[int], ids))
    for known_marker in (VIEW_MARKER, BRIDGE_MARKER):
        _CONSOLE_LEDGERS.setdefault((automation_base, known_marker), [])
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        message = cast(str, entry["message"])
        for known_marker in (VIEW_MARKER, BRIDGE_MARKER):
            offset = message.find(known_marker)
            if offset < 0:
                continue
            try:
                payload = json.loads(message[offset + len(known_marker):].strip())
            except json.JSONDecodeError as exc:
                raise BrainstormActionFlowError(
                    f"invalid fixture probe payload for {known_marker}"
                ) from exc
            if isinstance(payload, dict):
                _CONSOLE_LEDGERS[(automation_base, known_marker)].append(payload)
    return list(_CONSOLE_LEDGERS[(automation_base, marker)])


async def _poll_native_view(
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
            frames = await _console_payloads(automation_base, VIEW_MARKER)
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


async def _wait_dashboard_underlay_restored(
    automation_base: str,
    baseline: bytes,
    *,
    timeout_s: float = 8.0,
) -> bytes:
    """Wait past serialized tile writes for a settled restored Overview."""

    deadline = time.monotonic() + timeout_s
    matching_since: float | None = None
    latest_error: BrainstormActionFlowError | None = None
    while time.monotonic() < deadline:
        raw = await _screenshot(automation_base)
        try:
            _assert_dashboard_underlay_restored(baseline, raw)
        except BrainstormActionFlowError as exc:
            latest_error = exc
            matching_since = None
        else:
            now = time.monotonic()
            if matching_since is None:
                matching_since = now
            elif now - matching_since >= NATIVE_STABLE_S:
                return raw
        await asyncio.sleep(0.04)
    if latest_error is not None:
        raise latest_error
    raise BrainstormActionFlowError("restored Overview never reached a stable framebuffer")


async def _wait_visual_idle_after(
    automation_base: str,
    *,
    after: int,
    timeout_s: float = 12.0,
    stable_s: float = 1.0,
) -> bytes:
    """Wait for a post-marker visual write and its settled framebuffer.

    A quiet screenshot alone is not a boot receipt. The simulator can hold its
    local "Connecting" Overview long enough to look stable while the Mac's
    already-sent Ready frame is still queued behind it. Tie the baseline to a
    completed image write after ``VoiceOrchestrator.on_connect`` and then wait
    until both the bridge ledger and framebuffer stop changing.
    """

    deadline = time.monotonic() + timeout_s
    last_signature: tuple[int, int, str] | None = None
    stable_since = 0.0
    latest: bytes | None = None
    while time.monotonic() < deadline:
        events = (await _console_payloads(automation_base, BRIDGE_MARKER))[after:]
        starts = sum(item.get("operation") == "image-start" for item in events)
        completed = sum(item.get("operation") == "image" for item in events)
        try:
            raw = await _screenshot(automation_base)
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.04)
            continue
        signature = (starts, completed, _digest(raw))
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            stable_since = now
            latest = raw
        elif completed > 0 and starts == completed and now - stable_since >= stable_s:
            return latest or raw
        await asyncio.sleep(0.04)
    raise BrainstormActionFlowError(
        "Mac-connected Overview did not finish a post-connect visual frame"
    )


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
    stable_s: float = NATIVE_STABLE_S,
) -> bytes:
    """Wait for a settled native conversation framebuffer change."""

    return await _changed_frame(
        automation_base,
        _conversation_pixel_digest(prior_raw),
        label=label,
        timeout_s=timeout_s,
        stable_s=max(0.04, stable_s),
        region=CONVERSATION_PIXEL_REGION,
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
                elif now - self._candidate_since >= NATIVE_STABLE_S:
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


async def _bridge_native_latency(
    automation_base: str,
    *,
    after: int,
    closed_unix_ms: float,
    paint_requested_unix_ms: int,
) -> int:
    event = await _poll_bridge_event(
        automation_base,
        after=after,
        accepted_after_unix_ms=paint_requested_unix_ms,
        operations={"rebuild", "text"},
    )
    accepted = event.get("acceptedAtUnixMs")
    if not isinstance(accepted, int):
        raise BrainstormActionFlowError("native bridge receipt has no timestamp")
    elapsed = round(accepted - closed_unix_ms)
    if elapsed < 0:
        raise BrainstormActionFlowError("native write preceded utterance closure")
    return elapsed


async def _poll_bridge_event(
    automation_base: str,
    *,
    after: int,
    operations: set[str],
    timeout_s: float = 6.0,
    accepted_after_unix_ms: int = 0,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = await _console_payloads(automation_base, BRIDGE_MARKER)
        for event in events[after:]:
            if (
                event.get("operation") in operations
                and isinstance(event.get("acceptedAtUnixMs"), int)
                and cast(int, event["acceptedAtUnixMs"]) >= accepted_after_unix_ms
                and (predicate is None or predicate(event))
            ):
                return event
        await asyncio.sleep(0.04)
    raise BrainstormActionFlowError(
        f"timed out waiting for accepted bridge operation {sorted(operations)!r}"
    )


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
        before_frames = len(await _console_payloads(automation_base, VIEW_MARKER))
        await _input(automation_base, direction)
        if at_boundary:
            await asyncio.sleep(0.3)
            after_frames = len(await _console_payloads(automation_base, VIEW_MARKER))
            if after_frames != before_frames:
                raise BrainstormActionFlowError(f"{direction} repainted beyond its boundary")
            return steps, current, current_raw, reconstructed
        next_frame = await _poll_native_view(
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
    before_bridge = len(await _console_payloads(automation_base, BRIDGE_MARKER))
    prompt_footer = await _poll_bridge_event(
        automation_base,
        after=before_bridge,
        operations={"text"},
        timeout_s=20.0,
        predicate=lambda item: item.get("containerName") == "generation"
        and "Continue? tap: stay · double-click: back" in str(item.get("content", "")),
    )
    prompt_raw = await _changed_conversation_frame(
        automation_base,
        prior_raw,
        label=f"{name} continue prompt pixels",
    )
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
    )
    if _region_digest(
        resumed_raw, left=36, top=50, right=541, bottom=235,
    ) != _region_digest(
        prior_raw, left=36, top=50, right=541, bottom=235,
    ):
        raise BrainstormActionFlowError(
            f"{name} continuation did not restore the same conversation card"
        )
    return prompt_frame, {
        "footer": prompt_footer,
        "frame": _save_frame(resumed_raw, output, f"{name}-continued"),
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    _CONSOLE_LAST_IDS.clear()
    _CONSOLE_LEDGERS.clear()
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
        "apps/hub-plugin/src/HudPageBuilder.ts",
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

    def is_assistant_layout(event: dict[str, Any]) -> bool:
        text = event.get("text")
        if not isinstance(text, list):
            return False
        return (
            event.get("operation") == "rebuild"
            and event.get("containerTotalNum") == 5
            and event.get("imageCount") == 0
            and event.get("list") == []
            and [item.get("containerName") for item in text] == [
                "conversation", "wearer-turns", "joat-turns",
                "scroll-position", "generation",
            ]
            and sum(item.get("isEventCapture") == 1 for item in text) == 1
            and text[0].get("containerID") == 11
            and text[0].get("isEventCapture") == 1
        )

    def is_visual_layout(event: dict[str, Any]) -> bool:
        return (
            event.get("operation") in {"startup", "rebuild"}
            and event.get("containerTotalNum") == 5
            and event.get("imageCount") == 4
            and event.get("list") == []
        )

    async def stable_after(
        prior_raw: bytes,
        *,
        label: str,
        name: str,
        stable_s: float = NATIVE_STABLE_S,
    ) -> bytes:
        raw = await _changed_conversation_frame(
            base, prior_raw, label=label, stable_s=stable_s,
        )
        frames[name] = _save_frame(raw, args.out, name)
        return raw

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
            connect_bridge_start = len(await _console_payloads(base, BRIDGE_MARKER))
            await orchestrator.on_connect()
            baseline_raw = await _wait_visual_idle_after(
                base,
                after=connect_bridge_start,
            )
            baseline_frame = _save_frame(baseline_raw, args.out, "00-baseline")
            baseline_hash = cast(str, baseline_frame["sha256"])
            baseline_raw_name = cast(str, baseline_frame["raw"])
            baseline_optical_name = cast(str, baseline_frame["optical"])
            boot_bridge_events = await _console_payloads(base, BRIDGE_MARKER)
            boot_pages = [item for item in boot_bridge_events if is_visual_layout(item)]
            if not boot_pages:
                raise BrainstormActionFlowError("mounted Overview has no four-image SDK declaration")
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

            # Opening turn: capture one immediate transition frame before waiting
            # for a settled native page, then verify the one entry rebuild.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            latency = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            latency.mark_closed(closed)
            opening_thinking = await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="opening thinking downlink",
            )
            thinking_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == opening_thinking["revision"]
                and item.get("state") == "thinking"
                and any(
                    line.get("role") == "user" and OPENING_PROMPT in line.get("text", "")
                    for line in item.get("visibleLines", [])
                ),
                label="opening native thinking view",
            )
            immediate_raw = await _screenshot(base)
            frames["openingTransitionImmediate"] = _save_frame(
                immediate_raw, args.out, "01-opening-transition-immediate",
            )
            entry_rebuild = await _poll_bridge_event(
                base,
                after=bridge_before,
                operations={"rebuild"},
                predicate=is_assistant_layout,
            )
            thinking_raw = await stable_after(
                baseline_raw,
                label="opening native thinking pixels",
                name="02-opening-thinking-stable",
            )
            opening_latency = await latency.result()
            opening_latency["firstNativeWriteAcceptedMs"] = await _bridge_native_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(int, thinking_model["paintRequestedAtUnixMs"]),
            )
            timings["openingPrompt"] = opening_latency

            bridge_before_tick = len(await _console_payloads(base, BRIDGE_MARKER))
            entry_generation = next(
                item.get("content") for item in entry_rebuild["text"]
                if item.get("containerName") == "generation"
            )
            activity_event = await _poll_bridge_event(
                base,
                after=bridge_before_tick,
                operations={"text"},
                predicate=lambda event: event.get("containerName") == "generation"
                and event.get("content") != entry_generation,
            )
            tick_events = (await _console_payloads(base, BRIDGE_MARKER))[bridge_before_tick:]
            if any(
                item.get("operation") in {"rebuild", "image"}
                or (
                    item.get("operation") == "text"
                    and item.get("containerName") != "generation"
                )
                for item in tick_events
            ):
                raise BrainstormActionFlowError(
                    "activity tick changed more than the native generation footer"
                )
            activity_raw = await stable_after(
                thinking_raw,
                label="native activity tick pixels",
                name="03-opening-activity-stable",
            )

            await task
            opening_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=OPENING_ANSWER,
            )
            if opening_ready is None:
                raise BrainstormActionFlowError("opening answer never reached ready")
            ready_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == opening_ready["revision"]
                and item.get("state") == "ready",
                label="opening native ready view",
            )
            opening_raw = await stable_after(
                activity_raw,
                label="opening native ready pixels",
                name="04-opening-ready-stable",
            )

            # The inactivity question must be a footer upgrade and an actual tap
            # must restore the same transcript pixels.
            continue_bridge_start = len(await _console_payloads(base, BRIDGE_MARKER))
            opening_prompt_frame, opening_continued = await _continue_after_inactivity(
                base,
                gateway,
                prior_raw=opening_raw,
                output=args.out,
                name="05-opening",
            )
            frames["openingContinuePrompt"] = opening_prompt_frame
            frames["openingContinued"] = opening_continued["frame"]
            opening_continued_raw = (
                args.out / cast(str, frames["openingContinued"]["raw"])
            ).read_bytes()
            continue_events = (
                await _console_payloads(base, BRIDGE_MARKER)
            )[continue_bridge_start:]
            completed_continue = [item for item in continue_events if item.get("operation") == "text"]
            if not completed_continue or any(
                item.get("containerName") != "generation" for item in completed_continue
            ):
                raise BrainstormActionFlowError("continue prompt changed more than the native footer")

            # Follow-up stream: retain the opening exchange and expose the partial
            # answer before the completed reply, all through text upgrades.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            latency = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            latency.mark_closed(closed)
            joke_thinking = await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="joke thinking downlink",
            )
            joke_thinking_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == joke_thinking["revision"],
                label="retained joke native view",
            )
            joke_thinking_raw = await stable_after(
                opening_continued_raw,
                label="joke thinking native pixels",
                name="06-joke-thinking-stable",
            )
            joke_latency = await latency.result()
            joke_latency["firstNativeWriteAcceptedMs"] = await _bridge_native_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(int, joke_thinking_model["paintRequestedAtUnixMs"]),
            )
            timings["jokePrompt"] = joke_latency
            setup = await _wait_for(
                lambda: _latest_conversation(
                    gateway, after=before, state="answering", answer=JOKE_SETUP,
                ),
                label="partial joke downlink",
            )
            setup_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == setup["revision"],
                label="partial joke native view",
            )
            setup_raw = await stable_after(
                joke_thinking_raw,
                label="partial joke native pixels",
                name="07-joke-partial-stable",
            )
            await task
            joke_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=JOKE_ANSWER,
            )
            if joke_ready is None:
                raise BrainstormActionFlowError("complete joke never reached ready")
            joke_ready_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == joke_ready["revision"],
                label="complete joke native view",
            )
            joke_raw = await stable_after(
                setup_raw,
                label="complete joke native pixels",
                name="08-joke-ready-stable",
            )

            # Long answer: verify a real native swipe moves exactly one viewport
            # row, reaches both bounds, and restores the tail pixels.
            before = len(gateway.conversations)
            bridge_before = len(await _console_payloads(base, BRIDGE_MARKER))
            latency = await FramebufferLatencyProbe.start(base)
            next_seq, task, closed, closed_unix = await _drive_voice_turn(orchestrator, next_seq)
            latency.mark_closed(closed)
            long_thinking = await _wait_for(
                lambda: _latest_conversation(gateway, after=before, state="thinking"),
                label="long thinking downlink",
            )
            long_thinking_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == long_thinking["revision"],
                label="long thinking native view",
            )
            long_thinking_raw = await stable_after(
                joke_raw,
                label="long thinking native pixels",
                name="09-long-thinking-stable",
            )
            long_latency = await latency.result()
            long_latency["firstNativeWriteAcceptedMs"] = await _bridge_native_latency(
                base,
                after=bridge_before,
                closed_unix_ms=closed_unix,
                paint_requested_unix_ms=cast(int, long_thinking_model["paintRequestedAtUnixMs"]),
            )
            timings["longPrompt"] = long_latency
            await task
            long_ready = _latest_conversation(
                gateway, after=before, state="ready", answer=LONG_ANSWER,
            )
            if long_ready is None:
                raise BrainstormActionFlowError("long answer never reached ready")
            long_model = await _poll_native_view(
                base,
                lambda item: item.get("revision") == long_ready["revision"]
                and item.get("state") == "ready",
                label="long answer native view",
            )
            long_raw = await stable_after(
                long_thinking_raw,
                label="long answer native pixels",
                name="10-long-tail-stable",
            )
            if cast(int, long_model["lineCapacity"]) != 7:
                raise BrainstormActionFlowError("native conversation geometry changed from seven lines")
            if cast(int, long_model["maximumScrollFromTail"]) <= 0:
                raise BrainstormActionFlowError("long native conversation has no scrollable overflow")

            scroll_bridge_start = len(await _console_payloads(base, BRIDGE_MARKER))
            up_steps, top_model, top_raw, reconstructed = await _scroll_to_boundary(
                base, direction="up", start_frame=long_model, start_raw=long_raw,
            )
            frames["longTop"] = _save_frame(top_raw, args.out, "11-long-top-stable")
            expected_user = " ".join((OPENING_PROMPT, JOKE_PROMPT, LONG_PROMPT))
            expected_assistant = " ".join((OPENING_ANSWER, JOKE_ANSWER, LONG_ANSWER))
            if _normalized_role_text(reconstructed, "user") != expected_user:
                raise BrainstormActionFlowError("native scrollback changed or omitted wearer text")
            if _normalized_role_text(reconstructed, "assistant") != expected_assistant:
                raise BrainstormActionFlowError("native scrollback changed or omitted assistant text")
            down_steps, tail_model, restored_raw, _ = await _scroll_to_boundary(
                base, direction="down", start_frame=top_model, start_raw=top_raw,
            )
            if up_steps != down_steps or cast(int, tail_model["scrollFromTail"]) != 0:
                raise BrainstormActionFlowError("native scroll did not return through the same rows")
            if _conversation_pixel_digest(restored_raw) != _conversation_pixel_digest(long_raw):
                raise BrainstormActionFlowError("native scroll cycle did not restore tail pixels")
            frames["longTailRestored"] = _save_frame(
                restored_raw, args.out, "12-long-tail-restored-stable",
            )
            scroll_events = (
                await _console_payloads(base, BRIDGE_MARKER)
            )[scroll_bridge_start:]
            scroll_text = [item for item in scroll_events if item.get("operation") == "text"]
            if not scroll_text or any(
                item.get("containerName") not in {"wearer-turns", "joat-turns", "scroll-position"}
                for item in scroll_text
            ):
                raise BrainstormActionFlowError("native swipe updated an unrelated container")

            # First Back asks for confirmation through one footer update. Second
            # Back exits and recreates the visual Overview with its four images.
            back_bridge_start = len(await _console_payloads(base, BRIDGE_MARKER))
            before_inputs = len(gateway.inputs)
            await _input(base, "double_click")
            back_immediate_raw = await _screenshot(base)
            frames["backPromptImmediate"] = _save_frame(
                back_immediate_raw, args.out, "13-back-prompt-immediate",
            )
            back_footer = await _poll_bridge_event(
                base,
                after=back_bridge_start,
                operations={"text"},
                predicate=lambda item: item.get("containerName") == "generation"
                and "Continue? tap: stay · double-click: back" in str(item.get("content", "")),
            )
            back_prompt_raw = await stable_after(
                restored_raw,
                label="Back confirmation native pixels",
                name="14-back-prompt-stable",
            )
            if len(gateway.inputs) != before_inputs:
                raise BrainstormActionFlowError("first Back exited without confirmation")
            first_back_events = (
                await _console_payloads(base, BRIDGE_MARKER)
            )[back_bridge_start:]
            completed_first_back = [
                item for item in first_back_events if item.get("operation") == "text"
            ]
            if not completed_first_back or any(
                item.get("containerName") != "generation" for item in completed_first_back
            ):
                raise BrainstormActionFlowError("first Back changed more than the native footer")

            exit_bridge_start = len(await _console_payloads(base, BRIDGE_MARKER))
            await _input(base, "double_click")
            exit_immediate_raw = await _screenshot(base)
            frames["overviewTransitionImmediate"] = _save_frame(
                exit_immediate_raw, args.out, "15-overview-transition-immediate",
            )
            await _wait_for(
                lambda: gateway.inputs[-1]
                if len(gateway.inputs) > before_inputs
                and gateway.inputs[-1].selected_name == "exit"
                else None,
                label="Back exit action at Mac",
            )
            await _poll_bridge_event(
                base,
                after=exit_bridge_start,
                operations={"rebuild"},
                predicate=is_visual_layout,
            )
            # Wait for all four serialized Overview image writes without a
            # fixed renderer-delay assumption.
            deadline = time.monotonic() + 8.0
            while True:
                exit_events = (
                    await _console_payloads(base, BRIDGE_MARKER)
                )[exit_bridge_start:]
                exit_images = [item for item in exit_events if item.get("operation") == "image"]
                if len(exit_images) >= 4:
                    break
                if time.monotonic() >= deadline:
                    raise BrainstormActionFlowError("Overview did not restore all four visual tiles")
                await asyncio.sleep(0.04)
            overview_raw = await _changed_frame(
                base,
                _digest(back_prompt_raw),
                label="stable visual Overview after Back",
                stable_s=NATIVE_STABLE_S,
            )
            overview_raw = await _wait_dashboard_underlay_restored(
                base, baseline_raw,
            )
            frames["overviewStable"] = _save_frame(
                overview_raw, args.out, "16-overview-stable",
            )

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

            all_bridge_events = await _console_payloads(base, BRIDGE_MARKER)
            active_events = all_bridge_events[journey_bridge_start:exit_bridge_start]
            active_completed = [
                item for item in active_events
                if item.get("operation") in {"rebuild", "text", "image"}
            ]
            active_rebuilds = [item for item in active_completed if item.get("operation") == "rebuild"]
            active_images = [item for item in active_completed if item.get("operation") == "image"]
            active_text = [item for item in active_completed if item.get("operation") == "text"]
            if len(active_rebuilds) != 1 or active_rebuilds[0] != entry_rebuild:
                raise BrainstormActionFlowError(
                    f"active native conversation rebuilt {len(active_rebuilds)} times instead of once"
                )
            if active_images:
                raise BrainstormActionFlowError("active native conversation wrote raster images")
            allowed_text = {"wearer-turns", "joat-turns", "scroll-position", "generation"}
            if not active_text or any(item.get("containerName") not in allowed_text for item in active_text):
                raise BrainstormActionFlowError("active native conversation wrote an unknown text container")
            if any(item.get("containerID") == 11 for item in active_text):
                raise BrainstormActionFlowError("native conversation rewrote its event-capture frame")
            if any(item.get("result") not in {"true", "undefined"} for item in active_text):
                raise BrainstormActionFlowError("native text upgrade was rejected by the SDK bridge")

            exit_events = all_bridge_events[exit_bridge_start:]
            exit_rebuilds = [item for item in exit_events if item.get("operation") == "rebuild"]
            exit_images = [item for item in exit_events if item.get("operation") == "image"]
            if len(exit_rebuilds) != 1 or not is_visual_layout(exit_rebuilds[0]):
                raise BrainstormActionFlowError("Back did not perform one visual Overview rebuild")
            if [item.get("containerID") for item in exit_images] != [4, 8, 9, 10]:
                raise BrainstormActionFlowError("Overview tiles were not restored once in serial order")

            return {
                "schemaVersion": 5,
                "result": "official-simulator-native-conversation-pass",
                "evidence": {
                    "renderer": "current source snapshot through SDK-native text containers",
                    "transport": "production Mac protocol, WebSocket, Hub plug-in, Even SDK bridge",
                    "simulator": "official Even Realities simulator framebuffer, console, and input API",
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
                    "nativeEntryLayout": {
                        "containerTotalNum": entry_rebuild["containerTotalNum"],
                        "textContainerNames": [
                            item["containerName"] for item in cast(list[dict[str, Any]], entry_rebuild["text"])
                        ],
                        "imageCount": entry_rebuild["imageCount"],
                    },
                    "nativePageRebuildsWhileActive": len(active_rebuilds),
                    "nativeTextUpgradesWhileActive": len(active_text),
                    "acceptedRasterImageOperationsWhileActive": len(active_images),
                    "partialJokeVisibleBeforeCompletion": setup_model["revision"] < joke_ready_model["revision"],
                    "activityIndicatorAdvanced": activity_event.get("content") != entry_generation,
                    "scrollbar": {
                        "totalLines": long_model["totalLines"],
                        "visibleLines": long_model["lineCapacity"],
                        "upSteps": up_steps,
                        "downSteps": down_steps,
                        "topReached": top_model["scrollFromTail"] == top_model["maximumScrollFromTail"],
                        "tailConversationPixelsRestoredExactly": (
                            _conversation_pixel_digest(restored_raw)
                            == _conversation_pixel_digest(long_raw)
                        ),
                    },
                    "firstBackWasFooterOnly": True,
                    "secondBackReachedMac": True,
                    "overviewVisualRebuilds": len(exit_rebuilds),
                    "overviewImageOperations": len(exit_images),
                    "underlayRestoredOutsideLiveClockExactly": True,
                    "immediateTransitionFramesCaptured": [
                        "01-opening-transition-immediate",
                        "13-back-prompt-immediate",
                        "15-overview-transition-immediate",
                    ],
                },
                "timingsMs": {
                    "scope": "local deterministic STT/model through official simulator, from VAD closure",
                    "activityFixtureFirstChunkDelay": round(ACTIVITY_FIXTURE_DELAY_S * 1_000),
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
