"""Execute the A3 / A5 / A8 claims against the production classes.

Not a test suite: each section drives the real class with controlled
collaborators and prints what actually happened, with timings.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "mac"))

from joat.audio.whisperkit import WhisperKitEngine
from joat.core.interfaces import ReplyEngine, SttEngine, Transcript
from joat.core.protocol import HudText
from joat.services.agent_endpoint import AgentEndpoint, conversation_context, seat_prompt
from joat.services.orchestrator import VoiceOrchestrator


# ── A3: interrupt a streaming reply ──────────────────────────────────────

class Transport:
    """Records every HUD send with a timestamp."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, str]] = []
        self.t0 = time.monotonic()

    def on_audio(self, cb: Any) -> None:
        pass

    async def send(self, frame: Any) -> None:
        text = getattr(frame, "text", None)
        self.sent.append((round(time.monotonic() - self.t0, 2), f"{type(frame).__name__}:{text!r}"))


class Stt(SttEngine):
    """Maps each fed utterance (by its bytes) to a fixed transcript."""

    def __init__(self, table: dict[bytes, str]) -> None:
        self.table = table
        self.buffer = b""
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.buffer += pcm
        return None

    async def flush(self) -> Transcript | None:
        text = self.table.get(self.buffer, "")
        self.buffer = b""
        await asyncio.sleep(0.05)
        self.calls.append(text)
        return Transcript(text=text, is_final=True) if text else None

    def reset(self) -> None:
        self.buffer = b""


class SlowReplier(ReplyEngine):
    def __init__(self) -> None:
        self.streams: list[str] = []
        self.cancelled: list[str] = []

    async def reply(self, text: str) -> str | None:
        return "".join([c async for c in self.stream(text)])

    async def stream(self, text: str):
        self.streams.append(text)
        try:
            for word in ("one ", "two ", "three ", "four ", "five ", "six "):
                await asyncio.sleep(0.3)
                yield word
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise


async def a3() -> None:
    print("\n=== A3: a spoken correction interrupts the reply in progress ===")
    first, second, third = b"\x01" * 8000, b"\x02" * 8000, b"\x03" * 8000
    stt = Stt({first: "JOAT what is the capital of Peru", second: "JOAT no, of Chile", third: "hello there"})
    transport, replier = Transport(), SlowReplier()
    latency: list[tuple[int, str, str]] = []
    orch = VoiceOrchestrator(
        transport, stt, replier=replier, wake_words=("joat",),
        latency_observer=lambda t, s, b: latency.append((t, s, b)),
    )
    # A completed utterance arrives (VAD already closed it) — call the same
    # entry the transport would, by scheduling the worker directly.
    orch._utterance_worker = asyncio.get_running_loop().create_task(
        orch._drain_utterances(orch.__class__.__dict__["_QueuedUtterance"](first, "glasses", None, time.monotonic()))
        if "_QueuedUtterance" in orch.__class__.__dict__ else _queue(orch, first)
    )
    await asyncio.sleep(0.7)   # reply is streaming: "one two"
    _arrive(orch, second)      # addressed correction over the answer
    await asyncio.sleep(0.4)
    _arrive(orch, third)       # room talk while the new answer streams
    await orch._utterance_worker
    print("replier saw queries :", replier.streams)
    print("cancelled queries   :", replier.cancelled)
    print("stt order           :", stt.calls)
    print("latency stages      :", [(t, s) for t, s, _ in latency])
    painted = [entry for entry in transport.sent if "HudText" in entry[1]]
    print("HUD paints (t, text):")
    for entry in painted:
        print("   ", entry)
    ok = (replier.cancelled == ["what is the capital of Peru"]
          and replier.streams == ["what is the capital of Peru", "no, of Chile"]
          and any(s == "interrupted" for _, s, _ in latency)
          and stt.calls == ["JOAT what is the capital of Peru", "JOAT no, of Chile", "hello there"])
    print("A3 verdict:", "PASS" if ok else "FAIL")


def _queue(orch: VoiceOrchestrator, pcm: bytes) -> Any:
    from joat.services.orchestrator import _QueuedUtterance
    return orch._drain_utterances(_QueuedUtterance(pcm, "glasses", None, time.monotonic()))


def _arrive(orch: VoiceOrchestrator, pcm: bytes) -> None:
    from joat.services.orchestrator import _QueuedUtterance
    orch._pending_utterances.append(_QueuedUtterance(pcm, "glasses", None, time.monotonic()))
    orch._pending_arrived.set()


# ── A5: a hung transcription is bounded ───────────────────────────────────

async def a5() -> None:
    print("\n=== A5: batch WhisperKit gives up on a hung child ===")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "whisperkit-cli"
        fake.write_text("#!/bin/sh\nexec sleep 30\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        engine = WhisperKitEngine(binary=str(fake), inference_deadline_s=1.0)
        await engine.feed(b"\x00" * 16000)
        started = time.monotonic()
        result = await engine.flush()
        elapsed = time.monotonic() - started
        print(f"flush returned {result!r} after {elapsed:.2f}s (deadline 1.0s)")
        print("A5 verdict:", "PASS" if result is None and elapsed < 2.5 else "FAIL")


# ── A8: history honoured, words streamed as produced ──────────────────────

class DoorReplier(ReplyEngine):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def reply(self, text: str) -> str | None:
        return "".join([c async for c in self.stream(text)])

    async def stream(self, text: str):
        self.prompts.append(text)
        for piece in ("**Lima**, ", "since you asked ", "about Peru ", "a moment ago.\n"):
            await asyncio.sleep(0.4)
            yield piece


async def a8() -> None:
    print("\n=== A8: Even AI door honours supplied history and streams ===")
    body = {
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is the capital of Peru?"},
            {"role": "assistant", "content": "Lima."},
            {"role": "user", "content": "And what did I just ask you about?"},
        ],
    }
    print("context block:\n   ", conversation_context(body).replace("\n", "\n    "))
    replier = DoorReplier()
    endpoint = AgentEndpoint(replier, "tok", port=0)
    await endpoint.start()
    try:
        url = f"http://127.0.0.1:{endpoint.port}/v1/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
        )

        def fetch() -> list[tuple[float, str]]:
            arrivals: list[tuple[float, str]] = []
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=20) as resp:
                print("response headers:", dict(resp.headers))
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.startswith(b"data: "):
                        arrivals.append((round(time.monotonic() - t0, 2), line[6:].decode().strip()))
            return arrivals

        arrivals = await asyncio.to_thread(fetch)
        for t, line in arrivals:
            print(f"   t={t:5.2f}s  {line[:110]}")
        deltas = [json.loads(l)["choices"][0]["delta"].get("content", "") for _, l in arrivals if l != "[DONE]"]
        spread = arrivals[-1][0] - arrivals[0][0]
        prompt = replier.prompts[0]
        ok = ("Wearer: What is the capital of Peru?" in prompt and "JOAT: Lima." in prompt
              and prompt.rstrip().endswith("Wearer: And what did I just ask you about?")
              and "**" not in "".join(deltas) and spread > 0.8 and len(arrivals) >= 5)
        print(f"chunks spread over {spread:.2f}s; markup stripped: {'**' not in ''.join(deltas)}")
        print("A8 verdict:", "PASS" if ok else "FAIL")
    finally:
        await endpoint.stop()


async def main() -> None:
    await a3()
    await a5()
    await a8()


if __name__ == "__main__":
    asyncio.run(main())
