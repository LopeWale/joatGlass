"""One whole Brainstorm, through the real assembled parts.

Every other test exercises a piece. This drives a complete session through
the actual RoundTable, DebateSession, HudSink, DebateStateSink and
BrainstormHistory — the same objects __main__ wires together — and checks
what the wearer would actually experience end to end.

It cannot reach a model or a gateway (this environment has no network), so
the collaborators are scripted. Everything between the seat and the glass
is the shipping code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from joat.core.interfaces import ReplyEngine
from joat.services.brainstorm_history import BrainstormHistory
from joat.services.hud import HudWriter
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession
from joat.services.sinks import Broadcast, DebateStateSink, HudSink
from tests.unit.test_session import SinkTransport


class Judge(ReplyEngine):
    """Scores low once so a second round runs, then converges."""

    def __init__(self) -> None:
        self.calls = 0

    async def reply(self, text: str) -> str | None:
        if "verified synthesis" in text or "AGREED:" in text:
            return ("AGREED: keep a small model resident.\n"
                    "OPEN: who wins under memory pressure.")
        self.calls += 1
        if self.calls == 1:
            return "Not aligned.\nAGREEMENT: 0.30\nBLOCKING: yes"
        return "Aligned.\nAGREEMENT: 0.95\nBLOCKING: no"


def seat(name: str, role: str, replies: list[str]) -> Participant:
    pending = iter(replies)

    async def stream(prompt: str) -> AsyncIterator[str]:
        try:
            text = next(pending)
        except StopIteration:
            text = "nothing further"
        for word in text.split(" "):
            yield word + " "

    return Participant(name=name, role=role, stream=stream)


async def run_session(tmp_path: Path):
    transport = SinkTransport()
    history = BrainstormHistory(path=tmp_path / "b.json",
                                export_dir=tmp_path / "exports")
    snapshot = DebateStateSink(on_finished=history.record)
    table = RoundTable([
        seat("codex", "proposer",
             ["GIST: keep weights resident. Cold start costs 2.1s.",
              "GIST: gate on idle. A warm window costs nothing."]),
        seat("claude", "critic",
             ["GIST: the OS evicts it anyway. Sporadic queries dominate.",
              "GIST: agreed if the gate is measured, not guessed."]),
    ])
    session = DebateSession(
        "how should we cut cold-start latency",
        table,
        Broadcast([HudSink(HudWriter(transport)), snapshot]),
        Judge(),
        max_rounds=2,
    )
    await session.run()
    return session, transport, snapshot, history


def frames(transport):
    return [m for m in transport.sent
            if type(m).__name__ == "BrainstormHudView"]


class TestAWholeSession:
    async def test_the_wearer_sees_it_happen_and_keeps_the_answer(
        self, tmp_path: Path
    ) -> None:
        session, transport, snapshot, history = await run_session(tmp_path)

        # 1. It streamed live: many frames, not one summary at the end.
        painted = frames(transport)
        assert len(painted) > 10, "a session that only reports a verdict"

        # 2. Both collaborators spoke, and JOAT closed with a synthesis.
        speakers = [label for label, _text in session._transcript]
        assert any(s.startswith("CODEX") for s in speakers)
        assert any(s.startswith("CLAUDE") for s in speakers)
        assert speakers[-1].startswith("JOAT")

        # 3. The answer is still on the glass at the end — not replaced by
        #    a status line.
        final = painted[-1].activity
        assert "AGREED" in final or "OPEN" in final
        assert "converged" in final

        # 4. It survives the process.
        [kept] = history.recent()
        assert kept.topic == "how should we cut cold-start latency"
        assert "keep a small model resident" in kept.synthesis

        # 5. And it left the glasses as something openable.
        exports = list((tmp_path / "exports").iterdir())
        assert len(exports) == 1
        markdown = exports[0].read_text()
        assert markdown.startswith("# how should we cut cold-start latency")
        assert "## CODEX (proposer)" in markdown
        assert "## JOAT (synthesis)" in markdown

    async def test_the_starter_design_actually_ran(self, tmp_path: Path) -> None:
        """independent → cross-review → meta-review → synthesis."""
        phases: list[str] = []
        transport = SinkTransport()
        table = RoundTable([
            seat("codex", "proposer", ["one", "three"]),
            seat("claude", "critic", ["two", "four"]),
        ])
        await DebateSession(
            "topic", table, Broadcast([HudSink(HudWriter(transport))]),
            Judge(), max_rounds=2, on_phase=phases.append,
        ).run()
        assert phases == ["opening", "independent", "meta_review",
                          "cross_review", "meta_review", "synthesis", "closing"]

    async def test_the_panel_can_be_reopened_afterwards(
        self, tmp_path: Path
    ) -> None:
        _session, _transport, snapshot, _history = await run_session(tmp_path)
        rows = snapshot.snapshot.rows()
        assert rows, "nothing to show when the wearer reopens the panel"
        assert any("CODEX" in row for row in rows)
        assert snapshot.snapshot.running is False
        assert "converged" in snapshot.snapshot.verdict

    async def test_a_cancelled_session_still_keeps_what_it_reached(
        self, tmp_path: Path
    ) -> None:
        transport = SinkTransport()
        history = BrainstormHistory(path=tmp_path / "b.json",
                                    export_dir=tmp_path / "exports")
        snapshot = DebateStateSink(on_finished=history.record)
        holder: dict[str, DebateSession] = {}

        async def interrupted(prompt: str) -> AsyncIterator[str]:
            yield "a partial thought"
            holder["s"].cancel()
            yield " never reaches the glass"

        table = RoundTable([
            Participant(name="codex", role="proposer", stream=interrupted),
            seat("claude", "critic", ["unused"]),
        ])
        session = DebateSession(
            "abandoned question", table,
            Broadcast([HudSink(HudWriter(transport)), snapshot]), None,
        )
        holder["s"] = session
        await session.run()

        final = frames(transport)[-1]
        assert final.state == "cancelled"
        assert "a partial thought" in final.activity   # kept, not erased
        assert "never reaches the glass" not in final.activity
