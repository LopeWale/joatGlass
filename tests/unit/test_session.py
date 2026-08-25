"""SessionManager + DebateSession tests with fake agents and HUD."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from jarvis.core.interfaces import GlassesTransport, ReplyEngine
from jarvis.core.protocol import ContainerId, DownlinkMessage, HudText
from jarvis.services.hud import HudWriter
from jarvis.services.session import DebateSession, Session, SessionManager


class SinkTransport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: object) -> None: ...  # type: ignore[override]
    def on_input(self, callback: object) -> None: ...  # type: ignore[override]
    def on_device(self, callback: object) -> None: ...  # type: ignore[override]


def agent(chunks: list[str]):
    async def stream(prompt: str) -> AsyncIterator[str]:
        for c in chunks:
            yield c
    return stream


class OneShotJudge(ReplyEngine):
    async def reply(self, text: str) -> str | None:
        return "Verdict: proposal stands."


def hud_texts(transport: SinkTransport) -> list[tuple[str, bool, int]]:
    return [(m.text, m.append, int(m.container_id))
            for m in transport.sent if isinstance(m, HudText)]


class TestDebateSession:
    async def test_turns_stream_in_order_visibly(self) -> None:
        transport = SinkTransport()
        session = DebateSession(
            "tabs or spaces", HudWriter(transport),
            agent(["Use spaces. ", "Simple."]), agent(["Weak claim: simplicity."]),
            OneShotJudge(),
        )
        await session.run()
        texts = [t for t, _a, c in hud_texts(transport) if c == int(ContainerId.MAIN)]
        joined = "".join(texts)
        # order: proposer header+chunks, critic header+chunks, judge header+verdict
        assert joined.index("PROPOSER") < joined.index("Use spaces.")
        assert joined.index("Use spaces.") < joined.index("CRITIC")
        assert joined.index("CRITIC") < joined.index("Weak claim")
        assert joined.index("Weak claim") < joined.index("JUDGE")
        assert "Verdict: proposal stands." in joined
        # streaming means appends, not one repaint
        appends = [t for t, a, c in hud_texts(transport) if a]
        assert len(appends) >= 5

    async def test_cancel_stops_midstream(self) -> None:
        transport = SinkTransport()

        class Sess:
            pass

        session = DebateSession(
            "x", HudWriter(transport), agent(["a", "b", "c"]), agent(["never"]), None,
        )

        async def cancelling_stream(prompt: str) -> AsyncIterator[str]:
            yield "first"
            session.cancel()
            yield "second"

        session._propose = cancelling_stream  # type: ignore[method-assign]
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "never" not in joined
        assert "cancelled" in joined


class TestSessionManager:
    async def test_trigger_starts_and_consumes(self) -> None:
        transport = SinkTransport()
        started: list[str] = []

        class FastSession(Session):
            def __init__(self, topic: str) -> None:
                self.topic = topic
                self.gate = asyncio.Event()

            async def run(self) -> None:
                started.append(self.topic)
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        made: list[FastSession] = []

        def factory(topic: str) -> Session:
            s = FastSession(topic)
            made.append(s)
            return s

        mgr = SessionManager(HudWriter(transport), factory)
        assert mgr.handle_transcript("hello there") is False
        assert mgr.handle_transcript("Debate tabs versus spaces.") is True
        await asyncio.sleep(0)
        assert started == ["tabs versus spaces"]
        assert mgr.active
        # transcripts consumed while active; 'stop' cancels
        assert mgr.handle_transcript("what is the weather") is True
        assert mgr.handle_transcript("stop") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not mgr.active
        assert mgr.handle_transcript("hello again") is False

    async def test_factory_declining_falls_through(self) -> None:
        mgr = SessionManager(HudWriter(SinkTransport()), lambda topic: None)
        assert mgr.handle_transcript("debate anything") is False
