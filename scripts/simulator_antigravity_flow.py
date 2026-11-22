#!/usr/bin/env python3
"""Drive the Agent Terminal's Antigravity lane through the official simulator.

Exercises the complete 3-level wearer voice-and-touch flow on `evenhub-simulator`:
  1. Contextual menu -> Agent Terminal -> `03-agent-picker` (`Antigravity · idle`, no model IDs)
  2. Tap `Antigravity` -> `03b-session-picker` (`AGENT TERMINAL · ANTIGRAVITY`, `+ New session`)
  3. Tap `+ New session` -> `04-antigravity-open` (5-container framed card, `tap: speak · 2x: sessions`)
  4. Turn 1: Tap to speak (`● Listening · tap: done · 2x: cancel`) -> live `PromptPartial` (`> <words>`,
     no `QUESTION >` or `YOU >`) -> tap to commit (`◐ Transcribing…`) -> confirm gate -> bright reply
     (no `AGENT >`)
  5. Turn 2: Tap to speak -> shell command (`git rev-parse --abbrev-ref HEAD`) -> Unicode
     `╭── Approve command ──╮` callout (`tap: approve · swipe: deny`) -> tap to approve ->
     `>> run_command · git rev-parse --abbrev-ref HEAD` on dim layer + bright reply
  6. `2x tap` -> `12-back-to-sessions` (`Active · …`, `+ New session`, saved session) ->
     `2x tap` -> `13-back-to-agents` (`AGENT TERMINAL`)

Isolation: runs on dedicated ports (`18990`, `5299`, `9997`), whitelists only
its own loopback gateway in the disposable plugin manifest, and stores
conversation state under `--output` so the live Mac engine and user sessions
are untouched.

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_antigravity_flow.py
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from joat.agents.antigravity_terminal import (
    AntigravityTerminalAgent,
    load_antigravity_lane,
    load_antigravity_model,
)
from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.interfaces import Transcript
from joat.core.protocol import (
    CONFIRM_PROMPT_MAX_UTF16_UNITS,
    AgentTerminalHudView,
    PromptPartial,
    utf16_units,
)
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.session import SessionManager
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController
from scripts.simulator_agent_terminal_flow import OCR_SOURCE, NeverSender, http, until
from scripts.simulator_harness import _prepare_simulator_plugin_root

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PORT, PLUGIN_PORT, SIM_PORT = 18990, 5299, 9997
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"
LANE = "antigravity"
TURN_TIMEOUT = 240
FORBIDDEN_MODEL_RE = re.compile(
    r"\b(?:gemini-[\w.-]+|claude-(?:sonnet|opus|haiku|3|4)[\w.-]*|gpt-[\w.-]+|sonnet|opus|haiku)\b",
    re.IGNORECASE,
)


class Gateway(HubGatewayTransport):
    def __init__(self) -> None:
        super().__init__(host="127.0.0.1", port=ENGINE_PORT)
        self.frames: list[dict[str, Any]] = []

    async def send(self, message: Any) -> None:
        self.frames.append(message.to_wire())
        await super().send(message)


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


async def run(out: Path, model: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out / "antigravity-state", ignore_errors=True)
    for old in out.glob("*.png"):
        old.unlink()
    for port in (ENGINE_PORT, PLUGIN_PORT, SIM_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    (out / "ocr.swift").write_text(OCR_SOURCE)
    subprocess.run(["swiftc", str(out / "ocr.swift"), "-o", str(out / "ocr")], check=True, capture_output=True)

    _, cwd = load_antigravity_lane()
    gateway = Gateway()
    agent = AntigravityTerminalAgent(
        LANE, cwd, state_dir=out / "antigravity-state", model=model or load_antigravity_model(),
    )
    hud = HudWriter(gateway)
    screens: list[dict[str, Any]] = []
    processes: list[subprocess.Popen[bytes]] = []
    committed_prompts: list[str] = []
    confirmations_pending: list[str] = []
    router_ref: dict[str, CapabilityRouter] = {}

    async def relay(frame: Any) -> None:
        await opening.paint(frame)

    async def paint(frame: Any, request_id: str | None, selection_id: str | None) -> None:
        cap_router = router_ref.get("router")
        if cap_router is not None and not cap_router.terminal_view_current:
            return
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state, focus=frame.topic, lanes=frame.lanes,
            activity="\n".join(frame.lines), approvals=frame.approvals, reason=frame.reason,
            opening_request_id=request_id, opening_selection_id=selection_id,
            conversation_id=frame.conversation_id, messages=frame.messages,
            history_truncated=frame.history_truncated, input_required=frame.input_required,
            task_recovery=frame.task_recovery,
        ))

    roster = AgentTerminalRoster([(LANE, agent)], paint=relay, visible_lanes=(LANE,))
    agent.set_approval_callback(
        lambda approval_id, kind, summary: roster.note_approval(LANE, approval_id, kind, summary),
    )
    opening = TerminalOpeningController(
        roster, paint=paint, connection_generation=lambda: gateway.audio_connection_generation,
    )
    router = CapabilityRouter(
        hud, gateway, StatusPublisher(out / "status.json"), None, CardLog(),
        terminal_rows=roster.picker_rows, terminal_busy=lambda: roster.busy,
        terminal_current=lambda: roster.focused, terminal_open=opening.open,
        terminal_roster=roster,
    )
    router_ref["router"] = router
    commands = AgentTerminalCommands(
        roster, on_explicit_view=lambda: opening.claim_legacy(router.claim_terminal_voice),
    )

    async def capture() -> None:
        current = commands.begin_capture()
        assert current is not None
        assert await runtime.publish_terminal_capture(current)

    async def retry_terminal() -> None:
        await opening.retry()

    sessions = SessionManager(
        hud,
        lambda _: None,
        terminal_capture=capture,
        terminal_retry=retry_terminal,
        terminal_cancel=lambda: commands.cancel_from_glasses(),
        terminal_view_current=lambda: opening.current,
        terminal_decide=lambda verdict: roster.decide_receipt(verdict, None),
    )
    replies = MessageReplyController(NeverSender(), gateway.request_confirmation, contact_search=lambda _: [])

    async def check_terminal_input(lane_name: str, text: str) -> bool | None:
        words = text.strip()
        prompt = f"Send to {lane_name}? “{words}”"
        if utf16_units(prompt) <= CONFIRM_PROMPT_MAX_UTF16_UNITS:
            confirmations_pending.append(prompt)
            try:
                result = await gateway.request_confirmation(
                    f"terminal-input-{uuid.uuid4().hex[:16]}", prompt, 30.0,
                    purpose="terminal_input",
                )
                return None if result is None else result.accepted
            finally:
                confirmations_pending.clear()
        reviewed = await router.review_terminal_input(lane_name, words)
        return True if reviewed else None

    runtime = PromptedSpeechRuntime(
        hud,
        replies,
        sessions=lambda: sessions,
        connection_generation=lambda: gateway.audio_connection_generation,
        audio_source=lambda: gateway.prompt_audio_source,
        fence_audio=lambda: 10,
        capturing=lambda: False,
        terminal=lambda: commands,
        check_terminal_input=check_terminal_input,
    )

    def on_input(event: Any) -> None:
        if event.container_name == "prompt-commit" and event.selected_name:
            committed_prompts.append(event.selected_name)
            return
        terminal_was_current = opening.current
        router.on_input(event)
        if terminal_was_current and not opening.current:
            runtime.revoke_current()

    gateway.on_input(on_input)
    gateway.on_device(lambda event: runtime.handle_device(event))
    gateway.on_terminal_action(lambda event: sessions.handle_terminal_action(event))
    connected = asyncio.Event()
    gateway.on_hello(lambda _: connected.set())

    async def press(action: str) -> None:
        await asyncio.to_thread(http, AUTOMATION + "/api/input", {"action": action})
        await asyncio.sleep(0.7)

    async def screen(name: str, settle: float = 1.2) -> str:
        await asyncio.sleep(settle)
        previous = None
        raw = b""
        for _ in range(20):
            raw = await asyncio.to_thread(http, AUTOMATION + "/api/screenshot/glasses")
            visible = Image.open(io.BytesIO(raw)).crop((0, 30, 576, 288)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(0.35)
        source = Image.open(io.BytesIO(raw)).convert("RGBA")
        optical = Image.new("RGBA", source.size, "black")
        optical.alpha_composite(source)
        optical.convert("RGB").save(out / f"{name}.png")
        # Boost both dim (textColor: 2) and bright (textColor: 4) pixels for Vision OCR.
        ocr_channel = source.getchannel("A").point(lambda p: 255 if p > 8 else 0)
        ocr_channel.resize((2304, 1152)).save(out / "ocr-input.png")
        ocr = subprocess.run(
            [str(out / "ocr"), str(out / "ocr-input.png")],
            check=True, capture_output=True, text=True,
        )
        text = "\n".join(json.loads(ocr.stdout))
        screens.append({"name": name, "text": text})
        print(f"--- SCREEN {name}\n{text}\n", flush=True)
        return text

    def terminal() -> dict[str, Any]:
        for frame in reversed(gateway.frames):
            if frame.get("kind") == "agent_terminal.view":
                return frame["terminal"]
        raise AssertionError("No agent_terminal.view frame found")

    def latest_hud_list(title: str | None = None) -> dict[str, Any] | None:
        for frame in reversed(gateway.frames):
            if frame.get("kind") != "hud.list":
                continue
            if title is None or frame.get("title") == title:
                return frame
        return None

    async def ask(text: str, label: str) -> None:
        # 1. Wearer taps touchpad on READY terminal -> opens listening prompt.
        await press("click")
        await until(lambda: runtime.dispatcher.is_armed, 15)
        ticket = runtime.dispatcher.current_ticket
        assert ticket is not None
        listening_text = await screen(f"{label}-listening", settle=0.8)
        assert "listening" in listening_text.lower(), f"Expected Listening footer/row, got:\n{listening_text}"
        assert "tap: done" in listening_text.lower(), f"Expected 'tap: done' footer, got:\n{listening_text}"
        assert "cancel" in listening_text.lower(), f"Expected '2x: cancel' footer, got:\n{listening_text}"

        # 2. Live partial transcription streams wearer's speech-in-text (`> <words>`) onto the HUD.
        words = text.split()
        mid = max(2, len(words) // 2)
        await gateway.send(PromptPartial(
            prompt_id=ticket.prompt_id,
            connection_generation=ticket.connection_generation,
            text=" ".join(words[:mid]),
        ))
        await asyncio.sleep(0.35)
        await gateway.send(PromptPartial(
            prompt_id=ticket.prompt_id,
            connection_generation=ticket.connection_generation,
            text=text,
        ))
        partial_screen = await screen(f"{label}-speech-in-text", settle=0.8)
        assert "QUESTION >" not in partial_screen.upper(), (
            f"Forbidden 'QUESTION >' prefix on speech-in-text HUD:\n{partial_screen}"
        )
        assert "YOU >" not in partial_screen.upper(), (
            f"Forbidden 'YOU >' prefix on speech-in-text HUD:\n{partial_screen}"
        )
        assert words[1].lower() in partial_screen.lower(), (
            f"Expected spoken words on speech-in-text HUD, got:\n{partial_screen}"
        )

        # 3. Wearer taps touchpad (`tap: done`) -> sends `prompt-commit` and enters `◐ Transcribing…`
        await press("click")
        await until(lambda: ticket.prompt_id in committed_prompts, 10)
        transcribing_screen = await screen(f"{label}-transcribing", settle=0.6)
        assert "transcribing" in transcribing_screen.lower(), (
            f"Expected '◐ Transcribing…' footer after tap-to-done, got:\n{transcribing_screen}"
        )

        # 4. Final STT completes -> shows confirmation gate ("Send to antigravity? ..."), wearer taps to send.
        dispatch_task = asyncio.create_task(runtime.dispatcher.dispatch(
            Transcript(text=text, is_final=True),
            UtteranceProvenance(
                source=ticket.source,
                connection_generation=ticket.connection_generation,
                audio_epoch=ticket.prompt_id,
                first_ingress_ordinal=11,
                last_ingress_ordinal=12,
                speaker_roles=frozenset({"self"}),
            ),
        ))
        pending.append(dispatch_task)
        await until(lambda: bool(confirmations_pending), 15)
        confirm_screen = await screen(f"{label}-confirm-gate", settle=0.8)
        assert "Send to antigravity" in confirm_screen, (
            f"Expected confirmation prompt on HUD, got:\n{confirm_screen}"
        )
        await press("click")
        assert await dispatch_task is True

    def start(name: str, args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        with (out / f"{name}.log").open("wb") as log:
            proc = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            processes.append(proc)
            return proc

    scratch = tempfile.TemporaryDirectory(prefix="joat-antigravity-sim-")
    receipt: dict[str, Any] = {"result": "failed"}
    pending: list[asyncio.Task[Any]] = []
    try:
        await gateway.start()
        url = f"ws://127.0.0.1:{ENGINE_PORT}/glasses"
        root = _prepare_simulator_plugin_root(ROOT, Path(scratch.name), url)
        vite_env = dict(os.environ, VITE_MAC_WS_URL=url)
        vite_proc = start("vite", [
            str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"),
            str(root),
            "--config", str(ROOT / "apps/hub-plugin/vite.config.ts"),
            "--host", "127.0.0.1",
            "--port", str(PLUGIN_PORT),
            "--strictPort",
        ], env=vite_env)
        await wait_http_ready(f"http://127.0.0.1:{PLUGIN_PORT}/", vite_proc)
        start("simulator", [
            str(Path.home() / ".local/bin/evenhub-simulator"),
            "--no-aid",
            f"http://127.0.0.1:{PLUGIN_PORT}/?joatTestPromptMic=1",
            "--automation-port", str(SIM_PORT),
        ])
        await asyncio.wait_for(connected.wait(), 45)
        await screen("00-home", settle=2.5)

        # Level 1: Overview -> Contextual Menu -> Agent Terminal -> Agent Picker
        await press("context_menu")
        await screen("01-menu")
        for _ in range(4):
            await press("down")
        await screen("02-menu-agent-terminal")
        await press("click")
        await until(lambda: latest_hud_list("AGENT TERMINAL") is not None, 20)
        agent_picker_screen = await screen("03-agent-picker")
        agent_picker_frame = latest_hud_list("AGENT TERMINAL")
        assert agent_picker_frame is not None
        assert agent_picker_frame["items"] == ["Antigravity · idle"], (
            f"Expected ['Antigravity · idle'], got {agent_picker_frame['items']}"
        )
        assert not FORBIDDEN_MODEL_RE.search(" ".join(agent_picker_frame["items"])), (
            f"Model name leaked into Agent Picker wire items: {agent_picker_frame['items']}"
        )
        assert not FORBIDDEN_MODEL_RE.search(agent_picker_screen), (
            f"Model name leaked onto Agent Picker HUD:\n{agent_picker_screen}"
        )

        # Level 2: Tap `Antigravity` -> Session Picker (`AGENT TERMINAL · ANTIGRAVITY`)
        await press("click")
        await until(lambda: latest_hud_list("AGENT TERMINAL · ANTIGRAVITY") is not None, 20)
        session_picker_screen = await screen("03b-session-picker")
        session_picker_frame = latest_hud_list("AGENT TERMINAL · ANTIGRAVITY")
        assert session_picker_frame is not None
        assert session_picker_frame["items"] == ["+ New session"], (
            f"Expected fresh Session Picker ['+ New session'], got {session_picker_frame['items']}"
        )
        assert "new session" in session_picker_screen.lower(), (
            f"Expected '+ New session' on Session Picker HUD, got:\n{session_picker_screen}"
        )

        # Level 3: Tap `+ New session` -> Framed Session Surface
        await press("click")
        await until(lambda: roster.focused == LANE and agent.capture_context_generation is not None, 30)
        await until(lambda: gateway.frames[-1].get("kind") == "agent_terminal.view", 20)
        open_screen = await screen("04-antigravity-open")
        assert "tap: speak" in open_screen.lower(), (
            f"Expected 'tap: speak' footer on open terminal card, got:\n{open_screen}"
        )

        lane = roster.lane(LANE)
        assert lane is not None
        approvals: list[dict[str, Any]] = []

        async def settle_turn(label: str) -> None:
            """Wait out a turn, approving any shell request via real touchpad tap."""
            async with asyncio.timeout(TURN_TIMEOUT):
                while roster.busy:
                    if lane.approvals:
                        asked = await screen(f"{label}-approval-{len(approvals) + 1}", settle=0.8)
                        assert "approve command" in asked.lower(), (
                            f"Expected 'Approve command' box on approval screen, got:\n{asked}"
                        )
                        assert "tap: approve" in asked.lower() and "swipe: deny" in asked.lower(), (
                            f"Expected 'tap: approve · swipe: deny' footer on approval screen, got:\n{asked}"
                        )
                        await press("click")
                        await until(lambda: not lane.approvals, 15)
                        approvals.append({"screen": asked, "gesture": "click (tap: approve)"})
                    await asyncio.sleep(0.2)

        # Turn 1: workspace question via tap-to-speak + live speech-in-text + tap-to-commit + confirm.
        await ask("Read pyproject.toml and tell me the project name in one short sentence.", "05-turn1")
        await until(lambda: terminal().get("state") == "running", 15)
        await screen("06-turn1-working", settle=1.5)
        await settle_turn("07-turn1")
        turn1 = await screen("08-turn1-reply")
        assert "AGENT >" not in turn1.upper(), f"Forbidden 'AGENT >' prefix in Turn 1 reply:\n{turn1}"
        assert not FORBIDDEN_MODEL_RE.search(turn1), f"Forbidden model ID in Turn 1 HUD:\n{turn1}"
        replies_1 = [m["text"] for m in terminal()["messages"] if m["role"] == "assistant"]
        assert replies_1 and replies_1[-1].strip(), "turn 1 produced no reply"

        # Turn 2: shell command that pauses for wearer touchpad approval.
        before = len(approvals)
        await ask(
            "Run the shell command git rev-parse --abbrev-ref HEAD and tell me the branch name.",
            "09-turn2",
        )
        await until(lambda: roster.busy, 15)
        await settle_turn("10-turn2")
        turn2 = await screen("11-turn2-reply")
        assert "AGENT >" not in turn2.upper(), f"Forbidden 'AGENT >' prefix in Turn 2 reply:\n{turn2}"
        replies_2 = [m["text"] for m in terminal()["messages"] if m["role"] == "assistant"]
        assert len(approvals) > before, "the shell command ran without the wearer's approval"

        history = await commands.handle_command("terminal history")
        print("history receipt:", history, flush=True)

        # Return Navigation: Session Surface -> 2x tap -> Session Picker -> 2x tap -> Agent Picker
        prior_frame_count = len(gateway.frames)
        await press("double_click")
        await until(
            lambda: any(
                f.get("kind") == "hud.list" and f.get("title") == "AGENT TERMINAL · ANTIGRAVITY"
                for f in gateway.frames[prior_frame_count:]
            ),
            15,
        )
        back_sessions_screen = await screen("12-back-to-sessions")
        refreshed_sessions = latest_hud_list("AGENT TERMINAL · ANTIGRAVITY")
        assert refreshed_sessions is not None
        session_rows = refreshed_sessions["items"]
        assert any(row.startswith("Active · ") for row in session_rows), (
            f"Expected 'Active · …' row on refreshed Session Picker, got {session_rows}"
        )
        assert "+ New session" in session_rows, (
            f"Expected '+ New session' row on refreshed Session Picker, got {session_rows}"
        )
        assert len(session_rows) >= 3, (
            f"Expected Active + '+ New session' + saved session in sessions.json, got {session_rows}"
        )
        assert "active" in back_sessions_screen.lower() and "new session" in back_sessions_screen.lower(), (
            f"Expected Active & + New session visible on 12-back-to-sessions HUD, got:\n{back_sessions_screen}"
        )

        await press("double_click")
        back_agents_screen = await screen("13-back-to-agents")
        assert "antigravity" in back_agents_screen.lower(), (
            f"Expected Antigravity row on 13-back-to-agents HUD, got:\n{back_agents_screen}"
        )
        assert "new session" not in back_agents_screen.lower(), (
            f"Expected return from Session Picker to Agent Picker, got:\n{back_agents_screen}"
        )

        receipt = {
            "result": "passed",
            "workspace": cwd,
            "model": model or load_antigravity_model() or "SDK default",
            "conversation_id": agent.conversation_id,
            "turn1_reply": replies_1[-1],
            "turn2_reply": replies_2[-1] if replies_2 else "",
            "approvals": approvals,
            "session_picker_rows_after_turns": session_rows,
            "history_receipt": history,
            "committed_prompts": committed_prompts,
            "input_route": "Agent Picker -> Session Picker -> + New session -> tap-to-speak -> PromptPartial -> tap-to-done -> confirm -> 2x tap Sessions -> 2x tap Agents",
            "screens": screens,
        }
        assert turn1 and turn2
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str))
        (out / "frames.json").write_text(json.dumps(gateway.frames, indent=2, default=str))
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, 15)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, 9)
        await runtime.aclose()
        await sessions.close()
        await agent.close()
        await gateway.stop()
        scratch.cleanup()
    print("RESULT", receipt["result"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/mac-workspace/control-audit/antigravity-simulator",
    )
    parser.add_argument("--model", help="override the lane's model for this run (e.g. a less busy one)")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.model))
