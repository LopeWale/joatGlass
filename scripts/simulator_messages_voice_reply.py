#!/usr/bin/env python3
"""Drive the Messages reply-by-voice flow in the official simulator.

Composes the production pieces the brain uses for a spoken reply —
``HubGatewayTransport``, ``CapabilityRouter``, ``MessageUIController``,
``PromptedSpeechRuntime``, ``MessageReplyController`` — over the disposable
two-conversation chat.db from ``simulator_messages_flow_acceptance`` and a
recording sender, so nothing can reach Messages.app. The simulator has no
microphone, so the one stand-in is the transcript: once the plugin has
acknowledged the question (``?joatTestPromptMic=1``), the words arrive as
partials and then a final answer exactly where Whisper's would.

The wearer's gestures are the simulator's: context_menu → Messages, down +
click to open a thread, click to reply, then (listening) tap = review / 2x =
cancel, (review) tap = send / 2x = retry. Drive them by hand through the
automation API, or pass ``--auto`` for the whole happy path.

    PYTHONPATH=apps/mac .venv/bin/python -u scripts/simulator_messages_voice_reply.py --port 18791
    VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses npx vite apps/hub-plugin --config apps/hub-plugin/vite.config.ts --host 127.0.0.1 --port 5193 --strictPort
    evenhub-simulator --no-aid 'http://127.0.0.1:5193/?joatTestPromptMic=1' --automation-port 9901
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import urllib.request

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.interfaces import MessageSender, Transcript
from joat.core.protocol import PromptPartial
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.message_ui import MessageUIController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.status import StatusPublisher
from scripts.simulator_messages_flow_acceptance import build_fixture_store

log = logging.getLogger("joat.simulator_messages_voice_reply")

WORDS = "On my way, see you at four thirty."


class RecordingSender(MessageSender):
    """The last boundary before Messages.app, replaced: nothing is sent."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send(self, recipient: str, body: str) -> None:
        self.sent.append(len(body))
        log.info("stage=recording-sender-reached chars=%d (nothing sent)", len(body))


def _input(base: str, action: str) -> None:
    request = urllib.request.Request(
        f"{base}/api/input", method="POST", data=json.dumps({"action": action}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=5).read()


async def run(port: int, *, auto: str | None, words: str) -> None:
    connected = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="joat-voice-reply-sim-") as root:
        base = Path(root)
        store = build_fixture_store(base / "chat.db")
        gateway = HubGatewayTransport(host="127.0.0.1", port=port)
        gateway.on_hello(lambda _hello: connected.set())
        hud = HudWriter(gateway)
        sender = RecordingSender()
        fence = {"n": 0}

        def fence_audio() -> int:
            fence["n"] += 100
            return fence["n"]

        message_ui: MessageUIController | None = None
        replies = MessageReplyController(
            sender, gateway.request_confirmation,
            begin_reply=lambda: message_ui.begin_reply() if message_ui else asyncio.sleep(0, ""),
            selection_on_glass=lambda: bool(message_ui and message_ui.conversation_on_glass()),
            on_status=lambda status: message_ui.on_status(status) if message_ui else None,
        )
        runtime = PromptedSpeechRuntime(
            hud, replies,
            sessions=lambda: SimpleNamespace(clear_topic_capture=lambda: None),  # type: ignore[arg-type]
            connection_generation=lambda: gateway.audio_connection_generation,
            audio_source=lambda: gateway.prompt_audio_source,
            fence_audio=fence_audio, capturing=lambda: False,
            on_message_prompt_retired=lambda action_id: message_ui.cancel(action_id) if message_ui else None,
        )

        def revoke_prompt(action_id: str) -> None:
            ticket = runtime.dispatcher.current_ticket
            if ticket is not None and ticket.owner_kind == "messages" and ticket.owner_id == action_id:
                runtime.dispatcher.revoke(ticket.prompt_id)

        router: CapabilityRouter | None = None
        message_ui = MessageUIController(
            replies, publish=gateway.send, after_input=runtime.after_message_input,
            connection_generation=lambda: gateway.audio_connection_generation,
            fence_audio=fence_audio, capturing=lambda: False, revoke_prompt=revoke_prompt,
            on_sent=lambda body: router.show_sent_reply(body) if router else asyncio.sleep(0),
            on_reply_status=lambda body, state: router.show_reply_status(body, state) if router else asyncio.sleep(0),
        )
        router = CapabilityRouter(
            hud=hud, transport=gateway, status=StatusPublisher(base / "status.json"),
            messages=store, cards=CardLog(), replies=replies, message_ui=message_ui,
        )
        gateway.on_input(router.on_input)

        async def speak_when_asked() -> None:
            """The stand-in microphone: words for every question the plugin acknowledges."""
            answered: set[str] = set()
            while True:
                await asyncio.sleep(0.2)
                ticket = runtime.dispatcher.current_ticket
                if ticket is None or ticket.owner_kind != "messages" or not runtime.dispatcher.is_armed:
                    continue
                if ticket.prompt_id in answered:
                    continue
                answered.add(ticket.prompt_id)
                log.info("stage=question-armed field=%s — speaking", ticket.field)
                text = "Simulator Contact" if ticket.field == "recipient" else words
                pieces = text.split()
                for n in range(1, len(pieces)):
                    await asyncio.sleep(0.45)
                    if runtime.dispatcher.current_ticket is not ticket:
                        break
                    await gateway.send(PromptPartial(
                        prompt_id=ticket.prompt_id, connection_generation=ticket.connection_generation,
                        text=" ".join(pieces[:n]),
                    ))
                await asyncio.sleep(0.6)
                if runtime.dispatcher.current_ticket is not ticket:
                    continue
                taken = await runtime.dispatcher.dispatch(
                    Transcript(text, True),
                    UtteranceProvenance(
                        "glasses", ticket.connection_generation, ticket.prompt_id,
                        fence["n"] + 1, fence["n"] + 2, frozenset({"self"}),
                    ),
                )
                log.info("stage=answer-%s", "taken" if taken else "refused")

        await gateway.start()
        speaker = asyncio.create_task(speak_when_asked())
        try:
            log.info("stage=waiting-for-official-simulator port=%d", port)
            await asyncio.wait_for(connected.wait(), timeout=120)
            log.info("stage=connected — drive the simulator (context_menu → Messages → thread → tap to reply)")
            if auto is not None:
                async def step(action: str, wait: float) -> None:
                    await asyncio.to_thread(_input, auto, action)
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)
                for action, wait in (("context_menu", 2.5), ("click", 3), ("down", 1.5), ("click", 3), ("click", 2)):
                    await step(action, wait)
                log.info("stage=auto-reply-tapped — listening box up; words arrive; review follows")
            while True:
                await asyncio.sleep(3600)
        finally:
            speaker.cancel()
            await message_ui.aclose()
            await runtime.aclose()
            await replies.aclose()
            await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--auto", metavar="AUTOMATION_BASE", help="e.g. http://127.0.0.1:9901: drive the gestures up to the listening box")
    parser.add_argument("--words", default=WORDS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port, auto=args.auto, words=args.words))


if __name__ == "__main__":
    main()
