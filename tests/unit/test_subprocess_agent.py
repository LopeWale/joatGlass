"""SubprocessAgent tests against stub scripts — no real codex/claude."""

from __future__ import annotations

import re
import sys

from joat.agents.subprocess_agent import _CODEX_NOISE, SubprocessAgent, find_claude_binary


async def collect(agent: SubprocessAgent, prompt: str) -> str:
    return "".join([chunk async for chunk in agent.stream(prompt)])


class TestStreaming:
    async def test_streams_stdout_and_passes_prompt(self) -> None:
        agent = SubprocessAgent(
            [sys.executable, "-c",
             "import sys; print('echo:', sys.argv[1]); print('line two')"],
        )
        out = await collect(agent, "the prompt")
        assert "echo: the prompt" in out and "line two" in out

    async def test_codex_noise_lines_are_filtered(self) -> None:
        agent = SubprocessAgent(
            [sys.executable, "-c",
             "import sys\n"
             "print('workdir: /somewhere')\n"
             "print('model: gpt-x')\n"
             "print('tokens used')\n"
             "print('the actual answer')"],
            noise=_CODEX_NOISE,
        )
        out = await collect(agent, "p")
        assert "the actual answer" in out
        assert "workdir" not in out and "model:" not in out and "tokens used" not in out

    async def test_env_strip_removes_key_from_child(self) -> None:
        import os
        os.environ["FAKE_SECRET_XYZ"] = "sk-should-not-leak"
        try:
            agent = SubprocessAgent(
                [sys.executable, "-c",
                 "import os; print('key=' + os.environ.get('FAKE_SECRET_XYZ', 'ABSENT'))"],
                env_strip=("FAKE_SECRET_XYZ",),
            )
            out = await collect(agent, "p")
            assert "key=ABSENT" in out
        finally:
            del os.environ["FAKE_SECRET_XYZ"]

    async def test_timeout_kills_hung_agent(self) -> None:
        agent = SubprocessAgent(
            [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(60)"],
            timeout_s=1.0,
        )
        out = await collect(agent, "p")  # returns promptly instead of hanging
        assert "start" in out


class TestDiscovery:
    def test_find_claude_binary_returns_path_or_none(self) -> None:
        found = find_claude_binary()
        assert found is None or found.endswith("claude")
