"""Owner-only, opt-in latency receipts for the live voice path.

The receipt intentionally stores only a per-process turn ordinal, a fixed
stage name, and a coarse elapsed-time bucket.  It never stores audio,
transcripts, replies, timestamps, identifiers, pixels, or physical-lens
claims.  It exists to tell endpointing, STT, and first-reply delay apart in a
user-authorized diagnostic run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


VOICE_LATENCY_SCHEMA = "joat.voice-latency.v1"
MAX_EVENTS = 128
STAGES = frozenset({
    # Every stage is measured from the wearer's end of speech (VAD close),
    # so utterance_ready is the queue wait and reply_complete the whole
    # wait. reply_complete is also emitted for deterministic command
    # answers and for prompted answers handed to their owner, and
    # interrupted marks a reply the wearer cut short.
    "utterance_ready",
    "ack_sent",
    "stt_complete",
    # Provider output exists, but it is not yet necessarily on the HUD.
    "first_model_chunk",
    "first_reply_sent",
    "reply_complete",
    "interrupted",
    # The plugin's first successful tile upload after a raster receipt's
    # first_reply_sent — a host-side paint receipt, only while G2 diagnostics
    # are enabled. Native conversation text has no image-tile paint stage.
    "painted",
})
BUCKETS = frozenset({
    "lte250ms",
    "250_500ms",
    "500ms_1s",
    "1_2s",
    "2_5s",
    "5_10s",
    "gt10s",
})

log = logging.getLogger(__name__)
VoiceLatencyEvent = dict[str, object]
VoiceLatencyWriter = Callable[[Path, list[VoiceLatencyEvent]], None]


def default_voice_latency_path() -> Path:
    return Path.home() / "Library/Application Support/JOAT/voice-latency.json"


def write_voice_latency_receipt(path: Path, events: list[VoiceLatencyEvent]) -> None:
    """Atomically replace one minimal receipt with owner-only permissions."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(
        {"schema": VOICE_LATENCY_SCHEMA, "events": events},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(slots=True)
class VoiceLatencyStore:
    """A bounded receipt sink that cannot block or break a voice turn."""

    path: Path = field(default_factory=default_voice_latency_path)
    writer: VoiceLatencyWriter = field(default=write_voice_latency_receipt, repr=False)
    _events: list[VoiceLatencyEvent] = field(default_factory=list, init=False)
    _persist_task: asyncio.Task[None] | None = field(default=None, init=False)
    _dirty: bool = field(default=False, init=False)
    _disabled: bool = field(default=False, init=False)

    def record(self, turn: int, stage: str, bucket: str) -> None:
        """Queue one validated event without delaying the active conversation."""

        if self._disabled:
            return
        if type(turn) is not int or turn < 1 or stage not in STAGES or bucket not in BUCKETS:
            # Do not echo untrusted values in a diagnostic log.
            log.warning("voice latency receipt rejected an invalid event")
            return
        self._events.append({
            "sequence": len(self._events) + 1,
            "turn": turn,
            "stage": stage,
            "bucket": bucket,
        })
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]
            for sequence, event in enumerate(self._events, start=1):
                event["sequence"] = sequence
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist_synchronously()
            return
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = loop.create_task(self._persist_pending())

    async def flush(self) -> None:
        """Flush one pending background write at orderly engine shutdown."""

        if self._persist_task is not None:
            await self._persist_task

    async def _persist_pending(self) -> None:
        while self._dirty and not self._disabled:
            self._dirty = False
            try:
                await asyncio.to_thread(self.writer, self.path, self._snapshot())
            except Exception as exc:
                self._disable(exc)
                return

    def _persist_synchronously(self) -> None:
        try:
            self.writer(self.path, self._snapshot())
        except Exception as exc:
            self._disable(exc)
            return
        self._dirty = False

    def _snapshot(self) -> list[VoiceLatencyEvent]:
        return [dict(event) for event in self._events]

    def _disable(self, exc: Exception) -> None:
        self._disabled = True
        self._dirty = False
        # Error contents can contain a username or path; retain only its class.
        log.warning("voice latency persistence disabled after %s", type(exc).__name__)
