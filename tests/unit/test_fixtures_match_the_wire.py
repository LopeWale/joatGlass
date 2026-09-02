"""The plugin's visual fixtures may only claim what the engine sends.

A fixture is a CLAIM about the engine's output, and three of them were
lying this session — each one redirecting real work:

  - one seated HERMES in a Brainstorm transcript, the single boundary the
    two features are defined against each other by;
  - one hand-wrapped the transcript into short lines, so the word-wrapper
    under test was never exercised by the fixture used to test it, and the
    lens looked two-thirds empty for a reason that had nothing to do with
    the renderer;
  - one gave the Agent Terminal a topic of "Hermes Agent Terminal" and no
    lanes, so the header rendered a truncation production cannot produce
    and the lane strip could not be seen at all.

None was catchable by a passing suite. This pins the one property that
would have caught all three: every field a fixture sets must be a field
the wire actually carries.
"""

from __future__ import annotations

import re
from pathlib import Path

from joat.core.protocol import AgentTerminalHudView, BrainstormHudView

FIXTURES = (
    Path(__file__).resolve().parents[2] / "apps/hub-plugin/src/VisualFixtures.ts"
)


def _wire_keys(view: object) -> set[str]:
    payload = view.to_wire()  # type: ignore[attr-defined]
    body = next(v for k, v in payload.items() if isinstance(v, dict))
    return set(body)


def _fixture_block(source: str, marker: str) -> str:
    """The object literal following a marker, to its closing brace."""
    start = source.index(marker)
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    raise AssertionError(f"unterminated fixture block for {marker!r}")


#: Render-side only: the plugin's model keeps `screen` as a discriminator
#: and `terminalState` for the surface it is drawing, which the wire no
#: longer carries. Anything NOT here must exist on the wire.
RENDER_ONLY = {"screen", "terminalState"}


class TestTheFixturesClaimOnlyWhatTheEngineSends:
    def test_the_brainstorm_fixture_uses_real_fields(self) -> None:
        source = FIXTURES.read_text()
        block = _fixture_block(source, "case 'brainstorm-live':")
        keys = set(re.findall(r"^\s{6,}([a-zA-Z]+):", block, re.M))
        allowed = _wire_keys(
            BrainstormHudView(state="ready", topic="t", collaborators=[],
                              activity="a")
        ) | RENDER_ONLY
        assert keys <= allowed, f"fixture invents {sorted(keys - allowed)}"

    def test_the_terminal_fixture_uses_real_fields(self) -> None:
        source = FIXTURES.read_text()
        block = _fixture_block(source, "const terminalReady = ")
        keys = set(re.findall(r"^\s{4,}([a-zA-Z]+):", block, re.M))
        # The terminal's render model reuses the Brainstorm shape, so both
        # field sets are legal here; what must NOT appear is anything from
        # neither.
        allowed = (
            _wire_keys(AgentTerminalHudView(state="ready", focus="f",
                                            lanes=[], activity="a"))
            | _wire_keys(BrainstormHudView(state="ready", topic="t",
                                           collaborators=[], activity="a"))
            | RENDER_ONLY
        )
        assert keys <= allowed, f"fixture invents {sorted(keys - allowed)}"

    def test_no_brainstorm_fixture_seats_an_agent_terminal_worker(self) -> None:
        """The boundary, as a fixture rule. Hermes and OpenClaw are Agent
        Terminal workers; seating one in a Brainstorm is the confusion the
        two features exist apart to prevent."""
        source = FIXTURES.read_text()
        block = _fixture_block(source, "case 'brainstorm-live':")
        collaborators = re.search(r"collaborators: \[(.*?)\]", block, re.S)
        assert collaborators is not None
        seated = collaborators.group(1).lower()
        for worker in ("hermes", "openclaw"):
            assert worker not in seated, (
                f"{worker} is an Agent Terminal worker and must never be "
                "seated in a Brainstorm"
            )

    def test_the_transcript_fixture_is_not_hand_wrapped(self) -> None:
        """Its lines must be whole utterances. Pre-wrapped strings make the
        renderer's wrapper untested by the fixture used to test it."""
        source = FIXTURES.read_text()
        block = _fixture_block(source, "case 'brainstorm-live':")
        rows = re.findall(r"^\s+'([A-Z]{3,}[^']*)',$", block, re.M)
        assert rows, "the transcript fixture has moved"
        speakers = [r for r in rows if " > " in r]
        assert len(speakers) == len(rows), (
            "every transcript line must start with a speaker; a line without "
            "one is a hand-inserted wrap, and the wrapper is what is under "
            f"test — offenders: {[r for r in rows if ' > ' not in r]}"
        )
