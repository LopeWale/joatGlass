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

from joat.services.brainstorm_history import BrainstormHistory
from joat.services.hud import HudWriter
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession
from joat.services.sinks import Broadcast, DebateStateSink, HudSink
from tests.unit.test_session import SinkTransport, play


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
             ["Keep weights resident. Cold start costs 2.1s.",
              "Gate on idle. A warm window costs nothing."]),
        seat("claude", "critic",
             ["The OS evicts it anyway. Sporadic queries dominate.",
              "Agreed if the gate is measured, not guessed."]),
    ])
    session = DebateSession(
        "how should we cut cold-start latency",
        table,
        Broadcast([HudSink(HudWriter(transport)), snapshot]),
    )
    # The wearer reads the first round, replies by voice, reads the second,
    # and ends the room.
    await play(session, "what happens under memory pressure?")
    return session, transport, snapshot, history


def frames(transport):
    return [m for m in transport.sent
            if type(m).__name__ == "BrainstormHudView"]


class TestAWholeSession:
    async def test_the_wearer_sees_it_happen_and_keeps_the_conversation(
        self, tmp_path: Path
    ) -> None:
        session, transport, snapshot, history = await run_session(tmp_path)

        # 1. It streamed live: many frames, not one summary at the end.
        painted = frames(transport)
        assert len(painted) > 10, "a session that only reports a verdict"

        # 2. Both collaborators spoke in both rounds, and the wearer's own
        #    reply sits between them. Nobody judged or summarised.
        speakers = [label for label, _text in session._transcript]
        assert [s.split(" ")[0] for s in speakers] == [
            "CODEX", "CLAUDE", "YOU", "CODEX", "CLAUDE",
        ]
        assert not any(s.startswith("JOAT") for s in speakers)

        # 3. The whole conversation is still on the glass at the end — not
        #    replaced by a status line, and not collapsed.
        final = painted[-1].activity
        assert "Keep weights resident. Cold start costs 2.1s." in final
        assert "The OS evicts it anyway. Sporadic queries dominate." in final
        assert "Agreed if the gate is measured, not guessed." in final
        assert "cancelled" in final

        # 4. It survives the process.
        [kept] = history.recent()
        assert kept.topic == "how should we cut cold-start latency"
        assert "Agreed if the gate is measured, not guessed." in kept.synthesis

        # 5. And it left the glasses as something openable.
        exports = list((tmp_path / "exports").iterdir())
        assert len(exports) == 1
        markdown = exports[0].read_text()
        assert markdown.startswith("# how should we cut cold-start latency")
        assert "## CODEX (proposer)" in markdown
        assert "## YOU" in markdown

    async def test_the_room_stops_after_each_round(self, tmp_path: Path) -> None:
        """independent → (the wearer) → cross-review → (the wearer) → end."""
        phases: list[str] = []
        transport = SinkTransport()
        table = RoundTable([
            seat("codex", "proposer", ["one", "three"]),
            seat("claude", "critic", ["two", "four"]),
        ])
        session = DebateSession(
            "topic", table, Broadcast([HudSink(HudWriter(transport))]),
            on_phase=phases.append,
        )
        await play(session, "go on")
        assert phases == ["opening", "independent", "waiting",
                          "cross_review", "waiting", "closing"]

    async def test_the_panel_can_be_reopened_afterwards(
        self, tmp_path: Path
    ) -> None:
        _session, _transport, snapshot, _history = await run_session(tmp_path)
        rows = snapshot.snapshot.rows()
        assert rows, "nothing to show when the wearer reopens the panel"
        assert any("CODEX" in row for row in rows)
        assert snapshot.snapshot.running is False
        assert "cancelled" in snapshot.snapshot.verdict

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
            Broadcast([HudSink(HudWriter(transport)), snapshot]),
        )
        holder["s"] = session
        await session.run()

        final = frames(transport)[-1]
        assert final.state == "cancelled"
        assert "a partial thought" in final.activity   # kept, not erased
        assert "never reaches the glass" not in final.activity
