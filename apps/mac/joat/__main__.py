"""Run the Phase-0 brain:  python -m joat

Stands up the Hub gateway, wires the voice loop, and echoes transcripts to
the HUD. With no whisperkit-cli installed it still runs — the wearer just
sees "brain online (no STT)" instead of transcripts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .agents.builtin_tools import (
    messages_tool,
    notifications_tool,
    weather_tool,
)
from .agents.grounding import (
    Grounding,
    device_facts,
    local_clock,
    location_facts,
    static_fact,
)
from .agents.tools import ToolBelt
from .agents.reply_engine import DEFAULT_MODEL, MlxReplyEngine, mlx_available
from .agents.subprocess_agent import critic, proposer
from .audio.whisper_serve import WhisperServeEngine
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .core.pairing import PairingStore
from .channels.biome import DEFAULT_STREAM_DIR, BiomeNotificationStore
from .channels.cards import CardLog
from .channels.imessage import IMessageStore
from .channels.jsonl_source import JsonlNotificationStore
from .channels.notifications import NotificationChannel, NotificationSource, NotificationStore
from .core.interfaces import ReplyEngine, SttEngine
from .core.protocol import Device, Hello, InputEvent
from .services.hud import HudWriter
from .services.status import StatusPublisher
from .services.orchestrator import VoiceOrchestrator
from .services.capabilities import CapabilityRouter
from .services.session import DebateSession, Session, SessionManager
from .verifier.fetcher import fetch_page_text, wayback_page_text
from .verifier.nli import MiniCheckJudge, minicheck_available
from .verifier.gate import VerifierGate

log = logging.getLogger("joat")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="joat", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stt-binary", default="whisperkit-cli")
    parser.add_argument("--stt-model", default="large-v3_turbo")
    parser.add_argument("--stt-mode", choices=["serve", "batch"], default="serve",
                        help="serve = resident model (~1s/utterance); batch = CLI per utterance (~7s)")
    parser.add_argument("--stt-port", type=int, default=50060)
    parser.add_argument("--stt-prompt",
                        default="JOAT. The wearer addresses the "
                                "assistant as JOAT.",
                        help="vocabulary bias so STT spells the wake word right")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--wearer-name", default=os.environ.get("JOAT_WEARER", ""),
                        help="the wearer's name, grounded into every reply "
                             "(env JOAT_WEARER)")
    parser.add_argument("--always-answer", action="store_true",
                        help="answer ALL speech (dev only) instead of requiring 'JOAT ...'")
    parser.add_argument("--no-llm", action="store_true", help="echo transcripts only")
    parser.add_argument("--no-notifications", action="store_true",
                        help="disable the Notification Center channel")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    status = StatusPublisher()
    transport = HubGatewayTransport(host=args.host, port=args.port,
                                    pairing=PairingStore())

    serve_engine: WhisperServeEngine | None = None
    stt: SttEngine
    if args.stt_mode == "serve":
        serve_engine = WhisperServeEngine(
            binary=args.stt_binary, model=args.stt_model, port=args.stt_port,
            prompt=args.stt_prompt,
        )
        stt = serve_engine
    else:
        stt = WhisperKitEngine(binary=args.stt_binary, model=args.stt_model)

    stt_available = getattr(stt, "available", True)
    status.set(stt="ready" if stt_available else "missing")
    if not stt_available:
        log.warning("%s not found — voice loop runs without transcription "
                    "(brew install whisperkit-cli)", args.stt_binary)
    elif serve_engine is not None:
        # Warm the resident model at boot, not on the first utterance.
        log.info("starting whisper serve (model loads now, stays resident)…")
        if not await serve_engine.start():
            log.warning("serve mode failed — falling back to batch engine")
            stt = WhisperKitEngine(binary=args.stt_binary, model=args.stt_model)
            serve_engine = None
            stt_available = getattr(stt, "available", True)
    status.set(stt="ready" if stt_available else "missing")

    # Cards shown on the HUD are remembered here so "what did I miss?" has a
    # real source instead of a model with no record.
    card_log = CardLog()

    replier: ReplyEngine | None = None
    if args.no_llm:
        log.info("LLM disabled (--no-llm) — echoing transcripts")
    elif mlx_available():
        # Facts the model cannot know, refreshed per reply. Without this it
        # confabulates them — it answered "2:47 PM" at 4:44 PM.
        grounding = Grounding(
            sources=[local_clock(), device_facts(transport),
                     location_facts(transport)],
            # Named explicitly, not merely absent: a small model asked to
            # infer "it is not listed, so I should decline" does not — it
            # invents. Weather, messages and location have sources now —
            # the assistant's job is to go get them, not disclaim.
            unknowns=[
                "their calendar or schedule",
            ],
        )
        if args.wearer_name:
            grounding.add(static_fact(f"The wearer's name is {args.wearer_name}"))
        # The belt: what the model can GO GET instead of declining. The
        # wearer's ruling — an assistant that stops at "I don't know" is as
        # fired as one that invents the answer.
        def wearer_coords() -> tuple[float, float] | None:
            loc = transport.location
            return (loc.latitude, loc.longitude) if loc is not None else None

        belt_tools = [weather_tool(wearer_coords), notifications_tool(card_log)]
        tools_store = IMessageStore()
        if tools_store.available:
            belt_tools.append(messages_tool(tools_store))
        belt = ToolBelt(belt_tools)
        replier = MlxReplyEngine(model_id=args.llm_model, grounding=grounding,
                                 tools=belt,
                                 on_loaded=lambda: status.set(llm="ready"))
        log.info("reply engine: %s (+%d tools; loads on first utterance)",
                 args.llm_model, len(belt_tools))
    else:
        log.warning("mlx-lm not installed — echoing transcripts "
                    "(pip install -e '.[mlx]')")

    # "available" is a claim about pip; "ready" is a claim about resident
    # weights. The pill said ready before the model had ever loaded (27).
    status.set(llm="available" if replier is not None else "off")
    hud = HudWriter(transport)
    propose_agent, critic_agent = proposer(), critic()
    if propose_agent and critic_agent:
        log.info("debate agents ready: codex proposer + claude critic + local judge")
    else:
        log.warning("debate unavailable (codex: %s, claude: %s)",
                    "ok" if propose_agent else "missing", "ok" if critic_agent else "missing")

    claim_judge = MiniCheckJudge() if minicheck_available() else None
    if claim_judge:
        log.info("NLI claim judge ready (MiniCheck flan-t5-large; loads on first use)")
    else:
        log.info("NLI claim judge absent — install the 'nli' extra to enable")
    gate = VerifierGate(fetch_page_text, wayback_page_text, claim_judge=claim_judge)

    def make_debate(topic: str) -> Session | None:
        if propose_agent is None or critic_agent is None:
            return None
        return DebateSession(
            topic, hud, propose_agent.stream, critic_agent.stream, replier, gate=gate
        )

    status.set(debate="ready" if (propose_agent and critic_agent) else "unavailable")
    sessions = SessionManager(hud, make_debate, card_log=card_log)
    # The assistant's name is JOAT — plus mishearings observed LIVE from
    # whisper on short utterances: a one-word utterance gives the model no
    # context, so "JOAT" comes back as "Jokes"/"Jote"/"Jolt". Rejecting
    # those made the assistant look deaf to its own name.
    wake_words = None if args.always_answer else (
        "hey joat",
        "joat", "jote", "jolt", "jokes", "jot",
    )
    orchestrator = VoiceOrchestrator(
        transport, stt, stt_available=stt_available, replier=replier,
        sessions=sessions, wake_words=wake_words, status=status
    )
    orchestrator.attach()

    # The wearer's name from the host account (getUserInfo -> hello), unless
    # the CLI already set one. Grounded once — greet fires on every reconnect.
    named = {"done": bool(args.wearer_name)}

    def greet(hello: Hello) -> None:
        if hello.user_name and not named["done"] and replier is not None:
            grounding.add(static_fact(f"The wearer's name is {hello.user_name}"))
            named["done"] = True
        status.set(glasses="connected", device=f"{hello.model} / {hello.sn}")
        asyncio.get_running_loop().create_task(orchestrator.on_connect())

    transport.on_hello(greet)

    def farewell() -> None:
        # The pills must not lie about a link that is gone.
        status.set(glasses="disconnected", mic="off")

    def on_device(device: Device) -> None:
        # Battery reaches the dashboard, not just the model.
        hello = transport.hello
        who = f"{hello.model} / {hello.sn}" if hello else "glasses"
        if device.battery is not None:
            charge = "+" if device.charging else ""
            who += f" · {device.battery}%{charge}"
        fields: dict[str, str] = {"device": who}
        if device.mic is not None:
            # TRUTH from the plugin, not the hello-latched guess.
            fields["mic"] = "streaming" if device.mic else "off"
        status.set(**fields)

    transport.on_disconnect(farewell)
    transport.on_device(on_device)

    # One chat.db reader serves BOTH the notification channel and the
    # capability menu's Messages view.
    imessage = IMessageStore()

    # Glasses gestures in, drawn views out: the capability menu (Messages /
    # Notifications / Calendar / Debate), the contextual menu's repeat, and
    # the repaint after a menu close blanked the regions.
    capabilities = CapabilityRouter(
        hud=hud, transport=transport, status=status,
        messages=imessage if imessage.available else None, cards=card_log,
    )
    transport.on_input(capabilities.on_input)

    notif_task: asyncio.Task[None] | None = None
    if not args.no_notifications:
        jsonl = JsonlNotificationStore()
        store: NotificationSource
        if imessage.available:
            # Preferred: a per-source channel with a stable contract and real
            # content (sender + full text). Banner scraping is undocumented,
            # differs per macOS release, and reaches nothing at all on 27.
            store = imessage
            log.info("notification source: Messages (chat.db)")
        elif jsonl.available:
            # Preferred: JOAT.app's Accessibility watcher — the only source
            # that carries the notification's actual text.
            store = jsonl
            log.info("notification source: JOAT.app accessibility watcher")
        elif DEFAULT_STREAM_DIR.is_dir():
            store = BiomeNotificationStore()
            log.info("notification source: Biome store (identity+time only, no body text)")
        else:
            store = NotificationStore()
        channel = NotificationChannel(store, triage=replier, deliver=sessions.notify,
                                     status=status, poll_interval_s=1.0)
        notif_task = asyncio.get_running_loop().create_task(channel.run())

    await transport.start()
    log.info("brain up — plugin dial target is ws://<this-mac>:%d/glasses", args.port)

    async def orphan_watchdog() -> None:
        """Exit when the supervising shell dies without stopping us (kill -9,
        crash) — reparenting to PID 1 is the tell. Prevents orphan engines
        holding :8787 and crash-looping every future launch."""
        import os
        if os.getppid() == 1:
            return  # started detached (terminal/launchd) — no parent to watch
        while os.getppid() != 1:
            await asyncio.sleep(2)
        log.warning("supervisor died — engine shutting down")
        raise SystemExit(0)

    watchdog = asyncio.get_running_loop().create_task(orphan_watchdog())
    # Diagnostic: a starved loop delays every reply, and looks exactly
    # like a slow model in the logs unless it is measured directly.
    lag_task = asyncio.get_running_loop().create_task(orchestrator.watch_loop_lag())
    # Prove liveness between events, or the shell blanks the dashboard.
    beat_task = asyncio.get_running_loop().create_task(status.heartbeat())
    try:
        await asyncio.Event().wait()
    finally:
        watchdog.cancel()
        lag_task.cancel()
        beat_task.cancel()
        await transport.stop()
        if notif_task is not None:
            notif_task.cancel()
        if serve_engine is not None:
            await serve_engine.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
