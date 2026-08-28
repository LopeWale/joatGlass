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
import time
from pathlib import Path

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
from .agents.openclaw import OpenClawAgent
from .agents.subprocess_agent import critic, proposer
from .audio.whisper_serve import WhisperServeEngine
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .core.pairing import PairingStore
from .channels.biome import DEFAULT_STREAM_DIR, BiomeNotificationStore
from .channels.cards import CardLog
from .channels.imessage import IMessageStore
from .channels.message_sender import MessagesAppSender
from .channels.jsonl_source import JsonlNotificationStore
from .channels.notifications import NotificationChannel, NotificationSource, NotificationStore
from .core.interfaces import ReplyEngine, SttEngine
from .core.protocol import Device, Hello, InputEvent
from .native.bridge import NativeBridgeClient
from .persistence.actions import ActionJournal
from .persistence.store import DurableStore
from .services.hud import HudWriter
from .services.g2_diagnostics import G2DiagnosticStore
from .services.status import StatusPublisher
from .services.orchestrator import VoiceOrchestrator
from .services.capabilities import CapabilityRouter
from .services.commands import VoiceCommandRouter
from .services.message_reply import MessageReplyController
from .services.productivity_actions import ProductivityActionController
from .services.session import (
    DebateSession, Session, SessionManager, seat_participants,
)
from .services.models import (
    DEFAULT_CODEX_MODEL, ModelRegistry, default_slots,
)
from .services.sinks import Broadcast, DebateStateSink, HudSink, TerminalSink
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
    parser.add_argument("--g2-diagnostics", action="store_true",
                        help="opt in to a content-free real-G2 support trace")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    status = StatusPublisher()
    durable_store = DurableStore(
        Path.home() / "Library/Application Support/JOAT/joat.sqlite3"
    )
    action_journal = ActionJournal(durable_store)
    recovered = action_journal.recover_after_restart(
        now_us=time.time_ns() // 1000
    )
    if recovered:
        counts: dict[str, int] = {}
        for recovered_state in recovered.values():
            counts[recovered_state] = counts.get(recovered_state, 0) + 1
        log.warning("durable action recovery states=%s", counts)
    transport = HubGatewayTransport(
        host=args.host,
        port=args.port,
        pairing=PairingStore(),
        g2_diagnostics=G2DiagnosticStore() if args.g2_diagnostics else None,
    )
    native_bridge_path = os.environ.get("JOAT_NATIVE_BRIDGE")
    personal_data = (
        NativeBridgeClient(Path(native_bridge_path))
        if native_bridge_path
        else None
    )

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
    imessage = IMessageStore()

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
        if imessage.available:
            belt_tools.append(messages_tool(imessage))
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
    # Who is at the table is a RUNTIME fact: each collaborator is a binary or
    # a daemon that may simply not be installed. OpenClaw is the optional
    # third seat, so it is probed rather than assumed — an unreachable
    # gateway costs the debate a voice, never the debate itself.
    # Models the wearer can change by voice while this is running. Slots are
    # read PER TURN, so a switch lands on the next turn instead of the next
    # launch. Whisper is the one that needs work to apply: it holds weights
    # in a resident process, so its slot respawns serve and rolls back if
    # the new model will not load.
    async def _switch_stt(model: str | None) -> bool:
        setter = getattr(stt, "set_model", None)
        if model is None or setter is None:
            return False
        result: bool = await setter(model)
        return result

    models = ModelRegistry(default_slots(
        codex_default=os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL),
        stt_default=args.stt_model,
        stt_apply=_switch_stt,
    ))
    log.info("switchable models — %s", models.summary())

    openclaw = OpenClawAgent()
    openclaw_up, openclaw_why = openclaw.readiness()
    if not openclaw_up:
        # Named, not merely absent: OpenClaw ships with Chat Completions
        # DISABLED, so the most likely reason the third seat is empty is a
        # config key nobody would guess from "openclaw: missing".
        log.info("openclaw seat empty — %s", openclaw_why)
    seats: list[tuple[str, str, object]] = [
        ("codex", "proposer", proposer(models.provider("codex"))),
        ("claude", "critic", critic(models.provider("claude"))),
        ("openclaw", "third opinion", openclaw if openclaw_up else None),
    ]
    seated = [(name, role, a) for name, role, a in seats if a]
    absent = [name for name, _r, a in seats if not a]
    if len(seated) >= 2:
        log.info("round table: %s%s",
                 ", ".join(f"{n} ({r})" for n, r, _ in seated),
                 f" — absent: {', '.join(absent)}" if absent else "")
    else:
        log.warning("debate unavailable — need 2 collaborators, have %d (absent: %s)",
                    len(seated), ", ".join(absent))

    claim_judge = MiniCheckJudge() if minicheck_available() else None
    if claim_judge:
        log.info("NLI claim judge ready (MiniCheck flan-t5-large; loads on first use)")
    else:
        log.info("NLI claim judge absent — install the 'nli' extra to enable")
    gate = VerifierGate(fetch_page_text, wayback_page_text, claim_judge=claim_judge)

    # One snapshot outlives any single debate: the wearer opens the Debate
    # panel after the fact and must still see what was said.
    debate_state = DebateStateSink()

    def make_debate(topic: str) -> Session | None:
        # A debate needs someone to disagree with; one seat is a monologue.
        if len(seated) < 2:
            return None
        return DebateSession(
            topic,
            seat_participants(seated),
            Broadcast([HudSink(hud), TerminalSink(), debate_state]),
            replier,
            gate=gate,
        )

    status.set(debate=f"{len(seated)} seats" if len(seated) >= 2 else "unavailable")
    sessions = SessionManager(hud, make_debate, card_log=card_log)
    message_replies = MessageReplyController(
        MessagesAppSender(),
        transport.request_confirmation,
        action_journal,
    )
    productivity_actions = ProductivityActionController(
        personal_data,
        transport.request_confirmation,
        action_journal,
    )
    voice_commands = VoiceCommandRouter(
        message_replies,
        models,
        productivity=productivity_actions,
    )
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
        sessions=sessions, wake_words=wake_words, status=status,
        commands=voice_commands,
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
    # Glasses gestures in, drawn views out: the capability menu (Messages /
    # Notifications / Calendar / Tasks / Brainstorm), the contextual menu's repeat, and
    # the repaint after a menu close blanked the regions.
    capabilities = CapabilityRouter(
        hud=hud, transport=transport, status=status,
        messages=imessage if imessage.available else None, cards=card_log,
        replies=message_replies,
        personal_data=personal_data,
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
        if personal_data is not None:
            await personal_data.close()
        durable_store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
