#!/usr/bin/env python3
"""Replay the 2026-10-14 glasses freezes in the official simulator; see where taps go.

brain.log, 2026-10-14: twice the glasses stopped answering taps while the
plugin kept streaming the mic, until the wearer reopened JOAT:
  07:36:57  first tab opened right after connecting; no tap reached the Mac for 6 min
  08:15:02  QuickNote open and paused, then "JOAT." alone: the Mac sent the "Yes?"
            conversation card and "JOAT · go on"; no tap reached the Mac for 3.5 min

The Mac's side here is the production gateway, router, recorder and HudWriter on
a private port; the "Yes?" frames are exactly what VoiceOrchestrator sends for the
name alone. Records every input that reaches the Mac, frames and plugin console.

    (cd apps/hub-plugin && VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses ./node_modules/.bin/vite --port 5190 --strictPort)
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac .venv/bin/python -u scripts/simulator_freeze_probe.py /tmp/joat-freeze
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from joat.audio.router import AudioRouter
from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import Card, CardLog
from joat.core.interfaces import SttEngine, Transcript
from joat.core.protocol import AssistantConversationTurn
from joat.persistence.notes import NoteStore
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.orchestrator import CONVERSATION_WINDOW_S
from joat.services.status import StatusPublisher
from joat.services.voice_note_capture import VoiceNoteCaptureController

log = logging.getLogger("freeze-probe")
BASE = "http://127.0.0.1:9898"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "joat-freeze"
OUT.mkdir(parents=True, exist_ok=True)
CONTINUE_AFTER_MS = min(120_000, max(1_000, round(CONVERSATION_WINDOW_S * 1_000)))


class SilentStt(SttEngine):
    """The recorder needs an engine; the simulator sends no speech."""

    async def feed(self, pcm: bytes) -> Transcript | None:
        return None

    async def flush(self) -> Transcript | None:
        return None

    def reset(self) -> None:
        pass

    async def transcribe_once(self, pcm: bytes) -> Transcript | None:
        return None


def console_since(since_id: int) -> tuple[list[str], int]:
    with urllib.request.urlopen(f"{BASE}/api/console", timeout=5) as r:
        entries = json.load(r)["entries"]
    out, top = [], since_id
    for e in entries:
        if e["id"] <= since_id:
            continue
        top = max(top, e["id"])
        m = e["message"]
        if not m.startswith("[hud-timing]"):
            out.append(f'{e.get("level", "")} {m[:220]}')
    return out, top


def console_top() -> int:
    return console_since(0)[1]


async def snap(name: str, settle: float = 0.8) -> None:
    await asyncio.sleep(settle)
    with urllib.request.urlopen(f"{BASE}/api/screenshot/glasses", timeout=10) as r:
        (OUT / f"{name}.png").write_bytes(r.read())


async def press(action: str, settle: float = 0.6) -> None:
    req = urllib.request.Request(
        f"{BASE}/api/input", data=json.dumps({"action": action}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5):
        pass
    await asyncio.sleep(settle)


async def wait_until(predicate, timeout: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition did not become true")
        await asyncio.sleep(0.05)


async def main() -> None:
    connected = asyncio.Event()
    inputs: list[str] = []
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="joat-freeze-") as root:
        rootp = Path(root)
        with DurableStore(rootp / "joat.sqlite3") as durable:
            notes, voice_notes = NoteStore(durable), VoiceNoteStore(durable)
            cards = CardLog()
            cards.add(Card(source="Dele", channel="Messages", summary="Call me when you arrive."))
            gateway = HubGatewayTransport(host="127.0.0.1", port=18791)
            gateway.on_hello(lambda _h: connected.set())
            hud = HudWriter(gateway)
            audio = AudioRouter(gateway)
            capture = VoiceNoteCaptureController(voice_notes, audio, SilentStt(), stt_available=True)
            router = CapabilityRouter(
                hud=hud, transport=gateway, status=StatusPublisher(rootp / "status.json"),
                messages=None, cards=cards, notes=notes, voice_notes=voice_notes,  # type: ignore[arg-type]
                voice_capture=capture)

            def on_input(event) -> None:  # type: ignore[no-untyped-def]
                inputs.append(f"{time.monotonic():.2f} {type(event).__name__} {getattr(event, 'control', '')}"
                              f" idx={getattr(event, 'index', getattr(event, 'selected_index', ''))}"
                              f" {str(event)[:140]}")
                router.on_input(event)

            gateway.on_input(on_input)
            await gateway.start()

            async def step(name: str, action: str | None, settle: float = 1.0) -> None:
                before_inputs, before_console = len(inputs), console_top()
                if action is not None:
                    await press(action, settle)
                await snap(name, 0.4)
                lines, _ = console_since(before_console)
                report[name] = {"action": action, "inputs_reaching_mac": inputs[before_inputs:],
                                "console": lines[-25:]}
                log.info("%s: %s → %d input(s) at the Mac", name, action, len(inputs) - before_inputs)

            try:
                log.info("waiting for plugin hello on 18791")
                await asyncio.wait_for(connected.wait(), timeout=120)
                await asyncio.sleep(3.0)
                # A: the first tab right after connecting (07:36:57).
                await step("A0-overview", None)
                await step("A1-tap-first-tab", "click", 2.0)
                await step("A2-down", "down")
                await step("A3-double-tap", "double_click", 1.5)
                await step("A4-tap-again", "click", 2.0)
                await step("A5-double-tap", "double_click", 1.5)

                # B: an open, paused QuickNote, then "JOAT." alone (08:15:02).
                await router.open("QuickNotes", request_id="voice-freeze-quicknotes")
                await asyncio.sleep(2.0)
                await router._apply_recording_control("start_hold")
                await asyncio.sleep(1.5)
                await router._apply_recording_control("toggle")
                await asyncio.sleep(1.0)
                await router._apply_recording_control("toggle")
                report["capture_state"] = str(capture.snapshot.state)
                await step("B0-paused-note", None)
                await step("B1-down-on-note", "down")
                conversation_id = f"voice-{uuid4().hex}"
                await hud.conversation(conversation_id, 1, "ready",
                                       (AssistantConversationTurn("assistant", "Yes?"),),
                                       continue_after_ms=CONTINUE_AFTER_MS)
                await hud.status("JOAT · go on")
                await step("B2-yes-card", None, 1.5)
                await step("B3-tap", "click", 1.5)
                await step("B4-down", "down")
                await step("B5-up", "up")
                await step("B6-double-tap", "double_click", 1.5)
                await asyncio.sleep(CONTINUE_AFTER_MS / 1000 + 3)
                await step("B7-after-window-tap", "click", 1.5)
                await step("B8-after-window-double-tap", "double_click", 1.5)
                report["capture_state_end"] = str(capture.snapshot.state)
            finally:
                (OUT / "report.json").write_text(json.dumps(report, indent=2))
                await gateway.stop()
    print(json.dumps({k: (v if not isinstance(v, dict) else {"action": v["action"], "inputs": len(v["inputs_reaching_mac"])})
                      for k, v in report.items()}, indent=1))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main())
