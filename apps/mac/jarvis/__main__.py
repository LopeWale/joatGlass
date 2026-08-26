"""Run the Phase-0 brain:  python -m jarvis

Stands up the Hub gateway, wires the voice loop, and echoes transcripts to
the HUD. With no whisperkit-cli installed it still runs — the wearer just
sees "brain online (no STT)" instead of transcripts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .agents.reply_engine import DEFAULT_MODEL, MlxReplyEngine, mlx_available
from .agents.subprocess_agent import critic, proposer
from .audio.whisper_serve import WhisperServeEngine
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .channels.biome import DEFAULT_STREAM_DIR, BiomeNotificationStore
from .channels.jsonl_source import JsonlNotificationStore
from .channels.notifications import NotificationChannel, NotificationSource, NotificationStore
from .core.interfaces import ReplyEngine, SttEngine
from .core.protocol import Hello
from .services.hud import HudWriter
from .services.status import StatusPublisher
from .services.orchestrator import VoiceOrchestrator
from .services.session import DebateSession, Session, SessionManager
from .verifier.fetcher import fetch_page_text, wayback_page_text
from .verifier.nli import MiniCheckJudge, minicheck_available
from .verifier.gate import VerifierGate

log = logging.getLogger("jarvis")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stt-binary", default="whisperkit-cli")
    parser.add_argument("--stt-model", default="large-v3_turbo")
    parser.add_argument("--stt-mode", choices=["serve", "batch"], default="serve",
                        help="serve = resident model (~1s/utterance); batch = CLI per utterance (~7s)")
    parser.add_argument("--stt-port", type=int, default=50060)
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--always-answer", action="store_true",
                        help="answer ALL speech (dev only) instead of requiring 'Jarvis/JOAT ...'")
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
    transport = HubGatewayTransport(host=args.host, port=args.port)

    serve_engine: WhisperServeEngine | None = None
    stt: SttEngine
    if args.stt_mode == "serve":
        serve_engine = WhisperServeEngine(
            binary=args.stt_binary, model=args.stt_model, port=args.stt_port
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

    replier: ReplyEngine | None = None
    if args.no_llm:
        log.info("LLM disabled (--no-llm) — echoing transcripts")
    elif mlx_available():
        replier = MlxReplyEngine(model_id=args.llm_model)
        log.info("reply engine: %s (loads on first utterance)", args.llm_model)
    else:
        log.warning("mlx-lm not installed — echoing transcripts "
                    "(pip install -e '.[mlx]')")

    status.set(llm="ready" if replier is not None else "off")
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
    sessions = SessionManager(hud, make_debate)
    wake_words = None if args.always_answer else (
        "hey jarvis", "hey joat", "jarvis", "joat", "jot"
    )
    orchestrator = VoiceOrchestrator(
        transport, stt, stt_available=stt_available, replier=replier,
        sessions=sessions, wake_words=wake_words, status=status
    )
    orchestrator.attach()

    def greet(hello: Hello) -> None:
        status.set(glasses="connected", device=f"{hello.model} / {hello.sn}", mic="streaming")
        asyncio.get_running_loop().create_task(orchestrator.on_connect())

    transport.on_hello(greet)

    notif_task: asyncio.Task[None] | None = None
    if not args.no_notifications:
        jsonl = JsonlNotificationStore()
        store: NotificationSource
        if jsonl.available:
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
                                     status=status)
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
    try:
        await asyncio.Event().wait()
    finally:
        watchdog.cancel()
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
