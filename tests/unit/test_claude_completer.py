"""The billing/auth boundary on the SECOND `claude -p` launch path.

docs/BRAINSTORM.md:27-28 — "JOAT strips OPENAI_API_KEY and ANTHROPIC_API_KEY
before launching those CLIs so an ambient API key cannot silently change the
billing/auth boundary." SubprocessAgent held that boundary on every Brainstorm
seat; ClaudeCompleter — the explicit "with claude" QuickNote summary, the same
official binary — passed the whole ambient environment through. Found by a
blind-pair audit; no test had ever spawned this path.

The proof is a real spawn: the "binary" is a script that prints whether the
keys reached it, so the assertion is about the child's actual environment,
not about a dict the test built for itself.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from joat.agents.claude_completer import ClaudeCompleter

_PROBE = """#!/bin/sh
printf '%s|%s' "${ANTHROPIC_API_KEY:-ABSENT}" "${OPENAI_API_KEY:-ABSENT}"
"""


@pytest.mark.asyncio
async def test_ambient_api_keys_never_reach_the_claude_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "claude"
    probe.write_text(_PROBE)
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKED")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-LEAKED")

    result = await ClaudeCompleter(str(probe), timeout_s=30.0).complete(
        system="say the word", text="hello"
    )

    assert result == "ABSENT|ABSENT"


@pytest.mark.asyncio
async def test_the_rest_of_the_environment_still_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strip is two names, not a scrubbed env: the CLI needs HOME and
    PATH to find its own login session, which is the whole point of running
    on the wearer's subscription."""
    probe = tmp_path / "claude"
    probe.write_text('#!/bin/sh\nprintf %s "${JOAT_CANARY:-ABSENT}"\n')
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("JOAT_CANARY", "arrived")

    result = await ClaudeCompleter(str(probe), timeout_s=30.0).complete(
        system="say the word", text="hello"
    )

    assert result == "arrived"
