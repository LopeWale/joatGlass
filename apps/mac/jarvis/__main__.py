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
from .audio.whisperkit import WhisperKitEngine
from .ble.hub_gateway import HubGatewayTransport
from .core.interfaces import ReplyEngine
from .core.protocol import Hello
from .services.orchestrator import VoiceOrchestrator

log = logging.getLogger("jarvis")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stt-binary", default="whisperkit-cli")
    parser.add_argument("--stt-model", default="large-v3_turbo")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--no-llm", action="store_true", help="echo transcripts only")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    transport = HubGatewayTransport(host=args.host, port=args.port)
    stt = WhisperKitEngine(binary=args.stt_binary, model=args.stt_model)
    if not stt.available:
        log.warning("%s not found — voice loop runs without transcription "
                    "(brew install whisperkit-cli)", args.stt_binary)

    replier: ReplyEngine | None = None
    if args.no_llm:
        log.info("LLM disabled (--no-llm) — echoing transcripts")
    elif mlx_available():
        replier = MlxReplyEngine(model_id=args.llm_model)
        log.info("reply engine: %s (loads on first utterance)", args.llm_model)
    else:
        log.warning("mlx-lm not installed — echoing transcripts "
                    "(pip install -e '.[mlx]')")

    orchestrator = VoiceOrchestrator(
        transport, stt, stt_available=stt.available, replier=replier
    )
    orchestrator.attach()

    def greet(_hello: Hello) -> None:
        asyncio.get_running_loop().create_task(orchestrator.on_connect())

    transport.on_hello(greet)

    await transport.start()
    log.info("brain up — plugin dial target is ws://<this-mac>:%d/glasses", args.port)
    try:
        await asyncio.Event().wait()
    finally:
        await transport.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
