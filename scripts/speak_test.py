#!/usr/bin/env python3
"""Deterministic end-to-end voice test — no microphone, no acoustics.

Impersonates the Hub plugin: connects to the engine's WebSocket, sends a
hello, then streams a synthesized utterance as PCM exactly as the real
plugin would. Lets us measure VAD + STT + wake-word + reply repeatably.

Why this exists: testing through speakers into the mic proved worthless —
at usable volume the loop feeds back, holds the VAD open for the full 30 s
cap, and Whisper hallucinates ("*laughing* Aloha, Jesus!") on the noise.

    ./scripts/speak_test.py "JOAT, what is the capital of France?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

BATCH_SAMPLES = 3200  # 200 ms at 16 kHz, matching the plugin's cadence


def synthesize(phrase: str, voice: str | None, rate: int) -> bytes:
    """macOS `say` → 16 kHz mono s16le PCM."""
    tmp = Path(tempfile.mkdtemp())
    aiff, wav = tmp / "s.aiff", tmp / "s.wav"
    cmd = ["say", "-r", str(rate), "-o", str(aiff)]
    if voice:
        cmd += ["-v", voice]
    subprocess.run(cmd + [phrase], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    str(aiff), str(wav)], check=True)
    with wave.open(str(wav), "rb") as w:
        return w.readframes(w.getnframes())


async def run(phrase: str, url: str, voice: str | None, rate: int,
              lead_s: float, tail_s: float) -> None:
    import websockets

    pcm = synthesize(phrase, voice, rate)
    silence = b"\x00\x00" * BATCH_SAMPLES

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "kind": "hello", "sn": "TESTHARNESS", "model": "g2",
            "launchSource": "test",
        }))
        seq = 0

        async def send_batch(chunk: bytes) -> None:
            nonlocal seq
            samples = [int.from_bytes(chunk[i:i + 2], "little", signed=True)
                       for i in range(0, len(chunk), 2)]
            await ws.send(json.dumps({
                "kind": "audio", "source": "glasses", "seq": seq, "pcm": samples,
            }))
            seq += 1
            await asyncio.sleep(0.2)  # real-time cadence; the VAD is time-based

        # Lead-in silence lets the adaptive VAD measure a noise floor before
        # any speech arrives — exactly what happens on a real connection.
        for _ in range(int(lead_s / 0.2)):
            await send_batch(silence)
        for i in range(0, len(pcm), BATCH_SAMPLES * 2):
            batch = pcm[i:i + BATCH_SAMPLES * 2]
            if len(batch) < BATCH_SAMPLES * 2:
                batch += b"\x00" * (BATCH_SAMPLES * 2 - len(batch))
            await send_batch(batch)
        # Trailing silence closes the utterance via the hangover.
        for _ in range(int(tail_s / 0.2)):
            await send_batch(silence)

        print(f"sent {seq} batches ({seq * 0.2:.1f}s) for: {phrase!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phrase")
    ap.add_argument("--url", default="ws://127.0.0.1:8787/glasses")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", type=int, default=175)
    ap.add_argument("--lead", type=float, default=3.0)
    ap.add_argument("--tail", type=float, default=2.0)
    ap.add_argument("--live", action="store_true",
                    help="allow the wearer's own JOAT on port 8787 (it acts as a display)")
    args = ap.parse_args()
    # This harness impersonates the plugin. Pointed at the wearer's running
    # JOAT it is a second display beside the glasses (2026-10-13: "how is it
    # possible for you to take over my glasses when you're testing"), so it
    # only goes there when asked by name.
    if ":8787/" in args.url and not args.live:
        print("refusing the wearer's live JOAT on :8787 — run a separate engine "
              "on another --port and pass --url, or add --live", file=sys.stderr)
        raise SystemExit(2)
    try:
        asyncio.run(run(args.phrase, args.url, args.voice, args.rate,
                        args.lead, args.tail))
    except OSError as exc:
        print(f"cannot reach the engine at {args.url}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
