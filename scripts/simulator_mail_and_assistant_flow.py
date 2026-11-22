#!/usr/bin/env python3
"""Drive the Mail capability + streaming JOAT Assistant flow through evenhub-simulator.

Exercises and visually captures on `evenhub-simulator`:
  1. `01-menu-with-mail.png`      — Firmware contextual menu with `Messages`, `Mail`, `Reminders`, ...
  2. `02-mail-inbox.png`          — `MAIL` Unified Inbox (`+ New email`, `[2m] [Work] Sarah • · Q4 Launch…`,
                                    `[18m] [Personal] Alex • · Dinner Friday…`)
  3. `03-mail-thread.png`         — `MAIL · CONVERSATION` scrollable thread reader (`via Work`, clean turns)
  4. `04-mail-reply-listening.png`— Tap-to-reply (`capmessage`) -> `prompt.present` + live `prompt.partial`
  5. `05-mail-review-card.png`    — 7-row `Reply (Work) to sarah@example.com?` review card (`JOAT:` + `You:`),
                                    plus `confirm.cancel` teardown & `2x tap` (`mail-list`) verification
  6. `06-assistant-streaming.png` — Live `You:` volatile speech-in-text -> `JOAT:` token-streamed answer

Isolation: binds only dedicated loopback ports (`18992`, `5297`, `9995`).

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_mail_and_assistant_flow.py
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image
import websockets

from scripts.simulator_agent_terminal_flow import OCR_SOURCE, http, until
from scripts.simulator_harness import _prepare_simulator_plugin_root

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PORT, PLUGIN_PORT, SIM_PORT = 18992, 5297, 9995
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def wait_http_ready(url: str, proc: subprocess.Popen[bytes], timeout_s: float = 25.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Vite exited early with code {proc.returncode}")
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=2.0).read())
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}")


class MockPhoneBrainServer:
    """Loopback `/glasses` server speaking the exact JOAT Brain wire protocol."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.connected = asyncio.Event()
        self.uplinks: list[dict[str, Any]] = []
        self.downlinks: list[dict[str, Any]] = []
        self.mic_source = "glasses"
        self.generation = "phone-sim01152"
        self._ws: Any = None
        self._server: Any = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, "127.0.0.1", self.port, max_size=2**22)

    async def stop(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def send(self, frame: dict[str, Any]) -> None:
        assert self._ws is not None, "No authenticated plugin WebSocket connected"
        self.downlinks.append(frame)
        await self._ws.send(json.dumps(frame))

    def latest_input(self, container_name: str, after_index: int = 0) -> dict[str, Any] | None:
        for msg in reversed(self.uplinks[after_index:]):
            if msg.get("kind") == "input":
                ev = msg.get("event") or {}
                if ev.get("containerName") == container_name:
                    return ev
        return None

    def latest_kind(self, kind: str, after_index: int = 0) -> dict[str, Any] | None:
        for msg in reversed(self.uplinks[after_index:]):
            if msg.get("kind") == kind:
                return msg
        return None

    async def _handle(self, ws: Any) -> None:
        self._ws = ws
        authed = False
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("kind")
                if not authed:
                    if kind == "auth":
                        authed = True
                        await ws.send(json.dumps({"kind": "auth.accepted"}))
                    continue
                if kind == "audio":
                    continue
                self.uplinks.append(msg)
                if kind == "device":
                    src = msg.get("micSource")
                    if src in ("glasses", "phone"):
                        self.mic_source = src
                elif kind == "hello":
                    self.connected.set()
                elif kind == "link.pong":
                    pass
        except websockets.ConnectionClosed:
            pass


async def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    for port in (ENGINE_PORT, PLUGIN_PORT, SIM_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    (out / "ocr.swift").write_text(OCR_SOURCE)
    subprocess.run(["swiftc", str(out / "ocr.swift"), "-o", str(out / "ocr")], check=True, capture_output=True)

    brain = MockPhoneBrainServer(ENGINE_PORT)
    processes: list[subprocess.Popen[bytes]] = []
    screens: list[dict[str, Any]] = []

    def start_proc(name: str, args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        with (out / f"{name}.log").open("wb") as log:
            proc = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            processes.append(proc)
            return proc

    async def press(action: str, delay: float = 0.7) -> None:
        await asyncio.to_thread(http, AUTOMATION + "/api/input", {"action": action})
        await asyncio.sleep(delay)

    async def screen(name: str, settle: float = 1.0) -> str:
        await asyncio.sleep(settle)
        previous = None
        raw = b""
        for _ in range(20):
            raw = await asyncio.to_thread(http, AUTOMATION + "/api/screenshot/glasses")
            visible = Image.open(io.BytesIO(raw)).crop((0, 30, 576, 288)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(0.3)
        source = Image.open(io.BytesIO(raw)).convert("RGBA")
        optical = Image.new("RGBA", source.size, "black")
        optical.alpha_composite(source)
        optical.convert("RGB").save(out / f"{name}.png")
        ocr_channel = source.getchannel("A").point(lambda p: 255 if p > 8 else 0)
        ocr_channel.resize((2304, 1152)).save(out / "ocr-input.png")
        ocr = await asyncio.to_thread(
            subprocess.run,
            [str(out / "ocr"), str(out / "ocr-input.png")],
            check=True,
            capture_output=True,
            text=True,
        )
        text = "\n".join(json.loads(ocr.stdout))
        screens.append({"name": name, "text": text})
        print(f"--- SCREEN {name}\n{text}\n", flush=True)
        return text

    scratch = tempfile.TemporaryDirectory(prefix="joat-mail-sim-")
    receipt: dict[str, Any] = {"result": "failed"}
    try:
        await brain.start()
        ws_url = f"ws://127.0.0.1:{ENGINE_PORT}/glasses"
        plugin_root = _prepare_simulator_plugin_root(ROOT, Path(scratch.name), ws_url)
        vite_env = dict(os.environ, VITE_MAC_WS_URL=ws_url)
        vite_proc = start_proc("vite", [
            str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"),
            str(plugin_root),
            "--config", str(ROOT / "apps/hub-plugin/vite.config.ts"),
            "--host", "127.0.0.1",
            "--port", str(PLUGIN_PORT),
            "--strictPort",
        ], env=vite_env)
        await wait_http_ready(f"http://127.0.0.1:{PLUGIN_PORT}/", vite_proc)

        start_proc("simulator", [
            str(Path.home() / ".local/bin/evenhub-simulator"),
            "--no-aid",
            f"http://127.0.0.1:{PLUGIN_PORT}/?joatTestPromptMic=1",
            "--automation-port", str(SIM_PORT),
        ])
        await asyncio.wait_for(brain.connected.wait(), 45)
        await asyncio.sleep(2.0)

        # 1. Contextual Menu -> highlight `Mail` (index 1) -> capture `01-menu-with-mail.png`
        await press("context_menu")
        await press("down", delay=0.5)
        menu_text = await screen("01-menu-with-mail", settle=0.8)
        assert "messages" in menu_text.lower() and "mail" in menu_text.lower(), (
            f"Expected Messages and Mail in contextual menu, got:\n{menu_text}"
        )

        # 2. Tap `Mail` -> answer `capmenu` with `MAIL` Unified Inbox -> `02-mail-inbox.png`
        mark = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capmenu", mark) is not None, 10)
        capmenu_ev = brain.latest_input("capmenu", mark)
        assert capmenu_ev is not None
        assert capmenu_ev.get("selectedName") == "Mail", f"Expected selectedName=='Mail', got {capmenu_ev}"
        req_id = capmenu_ev["requestId"]

        inbox_items = [
            "+ New email",
            "[2m] [Work] Sarah • · Q4 Launch Checklist — Awesome — reply…",
            "[18m] [Personal] Alex • · Dinner Friday at 7? — Booked the…",
        ]
        await brain.send({
            "kind": "hud.list",
            "title": "MAIL",
            "items": inbox_items,
            "hint": "tap: select · left x2: back",
            "selectable": True,
            "requestId": req_id,
        })
        inbox_text = await screen("02-mail-inbox", settle=1.0)
        assert "new email" in inbox_text.lower() and "sarah" in inbox_text.lower(), (
            f"Expected '+ New email' and Sarah row in 02-mail-inbox, got:\n{inbox_text}"
        )

        # 3. Scroll down to row 1 (`[2m] [Work] Sarah...`) and tap -> `MAIL · CONVERSATION` -> `03-mail-thread.png`
        mark = len(brain.uplinks)
        await press("down", delay=0.5)
        await press("click")
        await until(lambda: brain.latest_input("capdata", mark) is not None, 10)
        capdata_ev = brain.latest_input("capdata", mark)
        assert capdata_ev is not None
        assert capdata_ev.get("selectedName") == "MAIL"
        assert capdata_ev.get("selectedIndex") == 1
        sel_id = capdata_ev["traceId"]

        thread_body = (
            "Q4 Launch Checklist\n"
            "Sarah Chen · via Work (work@example.com)\n\n"
            "Sarah Chen: Awesome — reply here once TestFlight upload finishes and we'll ship.\n\n"
            "You: Reviewing the final build metrics right now — everything looks green."
        )
        await brain.send({
            "kind": "hud.list",
            "title": "MAIL · CONVERSATION",
            "items": [],
            "body": thread_body,
            "contextLabel": "Sarah Chen",
            "hint": "tap: reply · left x2: back",
            "selectable": True,
            "readerPage": {"id": sel_id, "index": 0, "count": 1},
            "requestId": req_id,
        })
        thread_text = await screen("03-mail-thread", settle=1.0)
        assert "via work" in thread_text.lower() and "sarah chen" in thread_text.lower(), (
            f"Expected 'via Work' and 'Sarah Chen' on 03-mail-thread, got:\n{thread_text}"
        )

        # 4. Tap on `MAIL · CONVERSATION` -> `capmessage` (`MAIL`) -> `prompt.present` + `prompt.partial` -> `04-mail-reply-listening.png`
        mark = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capmessage", mark) is not None, 10)
        capmsg_ev = brain.latest_input("capmessage", mark)
        assert capmsg_ev is not None
        assert capmsg_ev.get("selectedName") == "MAIL", f"Expected capmessage selectedName=='MAIL', got {capmsg_ev}"

        action_id = "messages-ui-01152aabbccddeeff"
        question = "What should I reply to Sarah Chen?"
        prompt_id = "prompt01152reply"
        await brain.send({
            "kind": "message.ui",
            "requestId": req_id,
            "selectionId": sel_id,
            "actionId": action_id,
            "state": "preparing",
            "text": "Preparing your reply…",
        })
        await brain.send({
            "kind": "message.ui",
            "requestId": req_id,
            "selectionId": sel_id,
            "actionId": action_id,
            "state": "awaiting_input",
            "text": question,
        })
        await brain.send({
            "kind": "prompt.present",
            "promptId": prompt_id,
            "question": question,
            "presentationBinding": sha256_hex(f"{prompt_id}|{question}|{brain.generation}"),
            "connectionGeneration": brain.generation,
            "source": brain.mic_source,
            "expiresInMs": 30000,
            "ownerKind": "messages",
            "ownerId": action_id,
            "field": "body",
        })
        await until(lambda: brain.latest_kind("prompt.ack", mark) is not None, 10)
        reply_words = "Sounds great, ship it right after QA sign-off."
        await brain.send({
            "kind": "prompt.partial",
            "promptId": prompt_id,
            "connectionGeneration": brain.generation,
            "text": "Sounds great, ship it",
        })
        await asyncio.sleep(0.3)
        await brain.send({
            "kind": "prompt.partial",
            "promptId": prompt_id,
            "connectionGeneration": brain.generation,
            "text": reply_words,
        })
        listening_text = await screen("04-mail-reply-listening", settle=0.9)
        assert "sarah chen" in listening_text.lower() and "ship it" in listening_text.lower(), (
            f"Expected question and partial speech-in-text on 04-mail-reply-listening, got:\n{listening_text}"
        )

        # 5. Transition to 7-row review card (`Reply (Work) to sarah@example.com?`) -> `05-mail-review-card.png`
        await brain.send({
            "kind": "prompt.cancel",
            "promptId": prompt_id,
            "connectionGeneration": brain.generation,
        })
        await brain.send({
            "kind": "message.ui",
            "requestId": req_id,
            "selectionId": sel_id,
            "actionId": action_id,
            "state": "awaiting_confirmation",
            "text": "Awaiting your confirmation on the glasses.",
        })
        confirm_action_id = "phone-confirm01152"
        confirm_prompt = f"Reply (Work) to sarah@example.com? “{reply_words}”"
        payload = f"mail.send|debug-work|sarah@example.com|Re: Q4 Launch Checklist|{reply_words}|{action_id}"
        await brain.send({
            "kind": "confirm.request",
            "actionId": confirm_action_id,
            "prompt": confirm_prompt,
            "challengeId": "challenge01152aabb",
            "sessionId": "session01152ccdd",
            "payloadDigest": sha256_hex(payload),
            "previewDigest": sha256_hex(confirm_prompt),
            "expiresAtMs": int((time.time() + 20) * 1000),
            "diagnosticKind": "messages",
            "messageUIActionId": action_id,
        })
        review_text = await screen("05-mail-review-card", settle=0.5)
        assert "reply (work) to sarah@example.com?" in review_text.lower(), (
            f"Expected 'Reply (Work) to sarah@example.com?' header on 05-mail-review-card, got:\n{review_text}"
        )
        assert "ship it" in review_text.lower(), (
            f"Expected draft body on 05-mail-review-card, got:\n{review_text}"
        )

        # Also prove remote `confirm.cancel` cleanly dismisses the confirmation gate without uplink `confirm`,
        # and `2x tap` returns to `MAIL` emitting `mail-list` -> `2x tap` returns to Home!
        await asyncio.sleep(0.05)
        mark_cancel = len(brain.uplinks)
        await brain.send({
            "kind": "confirm.cancel",
            "actionId": confirm_action_id,
        })
        await brain.send({
            "kind": "message.ui",
            "requestId": req_id,
            "selectionId": sel_id,
            "actionId": action_id,
            "state": "cancelled",
            "text": "Email cancelled.",
        })
        restored_thread = await screen("05b-after-confirm-cancel", settle=0.9)
        assert "reply (work) to sarah@example.com?" not in restored_thread.lower(), (
            f"Confirmation card should have dismissed on confirm.cancel, got:\n{restored_thread}"
        )
        assert brain.latest_kind("confirm", mark_cancel) is None, "confirm.cancel must NOT emit an uplink confirm verdict"

        mark_back = len(brain.uplinks)
        await press("double_click")
        await until(
            lambda: (brain.latest_input("capability-lifecycle", mark_back) or {}).get("selectedName") == "mail-list",
            10,
        )
        await press("double_click")
        await asyncio.sleep(0.8)

        # 6. Streaming `JOAT` Assistant: volatile `You:` speech-in-text -> streamed `JOAT:` SSE answer -> `06-assistant-streaming.png`
        conv_id = "phone-stream01152"
        await brain.send({
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conv_id,
            "revision": 1,
            "state": "thinking",
            "turns": [
                {"role": "user", "text": "Summarize Sarah's Q4 launch email…"},
            ],
            "continueAfterMs": 30000,
            "earlierTurnsOmitted": False,
        })
        volatile_text = await screen("06a-assistant-volatile-speech", settle=0.6)
        assert "summarize sarah" in volatile_text.lower(), (
            f"Expected volatile speech-in-text on 06a-assistant-volatile-speech, got:\n{volatile_text}"
        )

        await brain.send({
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conv_id,
            "revision": 2,
            "state": "answering",
            "turns": [
                {"role": "user", "text": "Summarize Sarah's Q4 launch email"},
                {"role": "assistant", "text": "Sarah asked to lock the Q4 release notes by 3 PM"},
            ],
            "continueAfterMs": 30000,
            "earlierTurnsOmitted": False,
        })
        await asyncio.sleep(0.35)
        await brain.send({
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": conv_id,
            "revision": 3,
            "state": "ready",
            "turns": [
                {"role": "user", "text": "Summarize Sarah's Q4 launch email"},
                {
                    "role": "assistant",
                    "text": "Sarah asked to lock the Q4 release notes before 3 PM today and reply once the TestFlight build is up.",
                },
            ],
            "continueAfterMs": 30000,
            "earlierTurnsOmitted": False,
        })
        streamed_text = await screen("06-assistant-streaming", settle=0.9)
        assert "testflight" in streamed_text.lower() and "sarah" in streamed_text.lower(), (
            f"Expected streamed JOAT assistant turn on 06-assistant-streaming, got:\n{streamed_text}"
        )

        receipt = {
            "result": "passed",
            "screens": screens,
            "verified_lifecycle": [
                "capmenu -> Mail",
                "capdata -> MAIL row 1",
                "capmessage -> MAIL",
                "prompt.present + prompt.partial",
                "confirm.request (Reply (Work) to sarah@example.com?) -> confirm.cancel",
                "2x tap -> capability-lifecycle: mail-list",
                "assistant.conversation volatile preview -> streamed ready answer",
            ],
        }
    finally:
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str))
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, 9)
        await brain.stop()
        scratch.cleanup()
    print("RESULT", receipt["result"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/mac-workspace/control-audit/mail-simulator",
    )
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
