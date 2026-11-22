"""The plugin's visual fixtures may only claim what the engine sends."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from joat.core.protocol import AgentTerminalHudView

FIXTURES = (
    Path(__file__).resolve().parents[2] / "apps/hub-plugin/src/VisualFixtures.ts"
)

RENDER_ONLY = {"screen", "terminalState", "terminalReason"}


def _wire_keys(view: object) -> set[str]:
    payload = view.to_wire()  # type: ignore[attr-defined]
    body = next(v for _k, v in payload.items() if isinstance(v, dict))
    return set(body)


def _fixture_block(source: str, marker: str) -> str:
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


class TestTheFixturesClaimOnlyWhatTheEngineSends:
    def test_the_terminal_fixture_uses_real_fields(self) -> None:
        source = FIXTURES.read_text()
        marker = (
            "const terminalReady: AgentTerminalHudModel = "
            if "const terminalReady: AgentTerminalHudModel = " in source
            else "const terminalReady = "
        )
        block = _fixture_block(source, marker)
        keys = set(re.findall(r"^\s{4,}([a-zA-Z]+):", block, re.M))
        allowed = (
            _wire_keys(AgentTerminalHudView(
                state="ready",
                focus="f",
                lanes=[],
                activity="a",
                conversation_id="c",
            ))
            | RENDER_ONLY
        )
        assert keys <= allowed, f"fixture invents {sorted(keys - allowed)}"
