"""SubprocessAgent tests against stub scripts — no real codex/claude."""

from __future__ import annotations

import re
import sys

from joat.services.roundtable import is_failure
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

    def test_find_claude_binary_falls_back_to_the_native_install(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from joat.agents import subprocess_agent

        monkeypatch.setattr(subprocess_agent.shutil, "which", lambda _name: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        binary = tmp_path / ".claude" / "local" / "claude"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert find_claude_binary() == str(binary)


class TestFailureIsVisibleButNotQuotable:
    """A collaborator's failure prints on STDOUT, not stderr.

    Observed live: `claude -p` exits 1 and prints "You're out of usage
    credits" to stdout. Read as an answer it becomes a position the next
    speaker argues with and the judge scores — a debate about a billing
    notice. Only the exit code can tell the two apart.
    """

    async def test_a_clean_exit_adds_nothing(self) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(["/bin/echo"], name="echo")
        out = "".join([c async for c in agent.stream("a real answer")])
        assert "a real answer" in out
        assert not is_failure(out.strip().splitlines()[-1])  # no false alarm

    async def test_a_nonzero_exit_is_marked_even_when_stdout_looks_like_prose(
        self,
    ) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(
            ["/bin/sh", "-c", "echo \"You're out of usage credits.\"; exit 1; :"],
            name="claude",
        )
        out = "".join([c async for c in agent.stream("critique this")])
        assert "out of usage credits" in out          # the wearer still sees it
        assert is_failure(out.strip().splitlines()[-1])  # but it is not a position

    async def test_a_hung_agent_costs_one_seat_not_the_round(self) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(["/bin/sh", "-c", "sleep 30; :"],
                                timeout_s=0.3, name="codex")
        out = "".join([c async for c in agent.stream("q")])
        assert is_failure(out.strip())
        assert "no answer within" in out


class TestModelIsChosenPerCall:
    """Boot-time capture is what made the model a launch flag."""

    async def test_the_flag_reflects_the_provider_at_call_time(self) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        chosen: list[str | None] = ["spark"]
        agent = SubprocessAgent(["/bin/echo", "ran"], name="codex",
                                model=lambda: chosen[0], model_flag="-m")
        first = "".join([c async for c in agent.stream("q")])
        assert "-m spark" in first

        chosen[0] = "terra"                      # the wearer switches mid-session
        second = "".join([c async for c in agent.stream("q")])
        assert "-m terra" in second              # next call, no restart

    async def test_no_model_means_no_flag(self) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(["/bin/echo", "ran"], name="claude",
                                model=lambda: None, model_flag="--model")
        out = "".join([c async for c in agent.stream("q")])
        assert "--model" not in out              # the CLI keeps its own default


class TestStreamJsonReading:
    """Reading the CLI's incremental protocol instead of its final stdout.

    Measured on the real binary: plain `-p` delivered a whole answer in one
    chunk with the first byte at 10.4s of a 10.7s turn — the wearer watched
    "Thinking…" for 98% of it. With deltas the first words land at 4.7s, in
    ten chunks. Codex has no equivalent (`exec --json` emits one
    item.completed at the end), so this is opt-in per seat.

    Event shapes below are copied from a real run, not from documentation —
    the documentation for this project's other CLI protocol was wrong about
    an enum this same evening.
    """

    @staticmethod
    def _read(line: str):
        from joat.agents.subprocess_agent import _stream_json_event
        return _stream_json_event(line)

    def test_a_text_delta_is_the_incremental_output(self) -> None:
        text, final, failed = self._read(
            '{"type":"stream_event","event":{"type":"content_block_delta",'
            '"index":0,"delta":{"type":"text_delta","text":"Ca"}}}'
        )
        assert (text, final, failed) == ("Ca", None, False)

    def test_the_result_carries_the_whole_answer_and_the_verdict(self) -> None:
        text, final, failed = self._read(
            '{"type":"result","result":"the whole answer","is_error":false}'
        )
        assert (text, final, failed) == ("", "the whole answer", False)
        _t, _f, failed = self._read(
            '{"type":"result","result":"","is_error":true}'
        )
        assert failed is True

    def test_unrecognised_events_are_inert(self) -> None:
        """A new event type must be ignored, never mistaken for output."""
        for line in (
            '{"type":"system","subtype":"init"}',
            '{"type":"rate_limit_event","x":1}',
            '{"type":"stream_event","event":{"type":"message_start"}}',
            '{"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"thinking_delta","thinking":"hmm"}}}',
            "not json at all",
            "[]",
            "",
        ):
            assert self._read(line) == ("", None, False), line

    async def test_a_parse_gap_never_loses_the_answer(self) -> None:
        """If no delta is understood, the reported result still reaches the
        wearer. A silent truncation is worse than no streaming at all."""
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(
            ["/bin/sh", "-c",
             'printf \'%s\\n\' \'{"type":"stream_event","event":'
             '{"type":"content_block_delta","delta":'
             '{"type":"UNKNOWN_FUTURE","text":"x"}}}\' '
             '\'{"type":"result","result":"the whole answer","is_error":false}\'; '
             'exit 0'],
            stream_json=True, name="fake",
        )
        out = "".join([chunk async for chunk in agent.stream("ignored")])
        assert "the whole answer" in out

    async def test_streamed_deltas_are_not_duplicated_by_the_fallback(self) -> None:
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(
            ["/bin/sh", "-c",
             'printf \'%s\\n\' \'{"type":"stream_event","event":'
             '{"type":"content_block_delta","delta":'
             '{"type":"text_delta","text":"half "}}}\' '
             '\'{"type":"stream_event","event":'
             '{"type":"content_block_delta","delta":'
             '{"type":"text_delta","text":"answer"}}}\' '
             '\'{"type":"result","result":"half answer","is_error":false}\'; '
             'exit 0'],
            stream_json=True, name="fake",
        )
        out = "".join([chunk async for chunk in agent.stream("ignored")])
        assert out.count("half answer") == 1, out

    async def test_deltas_arriving_together_become_one_repaint(self) -> None:
        """Every yield is a HUD repaint and a BLE frame.

        Deltas inside one read are simultaneous as far as the wearer is
        concerned, so emitting them separately buys no responsiveness and
        spends link budget the glasses have to carry.
        """
        from joat.agents.subprocess_agent import SubprocessAgent

        delta = ('{"type":"stream_event","event":{"type":"content_block_delta",'
                 '"delta":{"type":"text_delta","text":"%s"}}}')
        lines = " ".join(f"'{delta % word}'" for word in ("one ", "two ", "three"))
        agent = SubprocessAgent(
            ["/bin/sh", "-c", f"printf '%s\\n' {lines}; exit 0"],
            stream_json=True, name="fake",
        )
        chunks = [chunk async for chunk in agent.stream("ignored")]
        assert chunks == ["one two three"], chunks

    async def test_a_final_line_without_a_newline_still_reaches_the_wearer(
        self,
    ) -> None:
        """The last line carries `result` — the whole answer — and nothing
        promises it ends in a newline. Leaving it in the pending fragment
        put the fallback out of reach in the one case it exists for."""
        from joat.agents.subprocess_agent import SubprocessAgent

        agent = SubprocessAgent(
            ["/bin/sh", "-c",
             # An unrecognised delta type, so ONLY the fallback can save it,
             # and no trailing newline on the result line.
             'printf \'%s\\n\' \'{"type":"stream_event","event":'
             '{"type":"content_block_delta","delta":'
             '{"type":"UNKNOWN_FUTURE","text":"x"}}}\'; '
             'printf \'%s\' \'{"type":"result","result":"the whole answer",'
             '"is_error":false}\'; exit 0'],
            stream_json=True, name="fake",
        )
        out = "".join([chunk async for chunk in agent.stream("ignored")])
        assert "the whole answer" in out, out


class TestFailureReadsTheCliNotTheAnswer:
    """`last_diagnostic` is what the sign-in / limit classifier reads. A
    model's answer may say "sign in"; a failed turn must not become a
    signed-out seat because of it (2026-10-06)."""

    async def test_stream_json_answer_text_is_not_a_complaint(self) -> None:
        delta = ('{"type":"stream_event","event":{"type":"content_block_delta",'
                 '"delta":{"type":"text_delta","text":"Users sign in, then log in again."}}}')
        agent = SubprocessAgent(
            [sys.executable, "-c", f"print({delta!r}); raise SystemExit(1)"],
            stream_json=True, capture=True, notices=False,
        )
        out = await collect(agent, "p")
        assert "sign in" in out and agent.last_failed
        assert "sign in" in agent.last_output, "the capture still holds everything"
        assert "sign in" not in agent.last_diagnostic

    async def test_stream_json_prose_and_error_results_are_complaints(self) -> None:
        script = ("print('Failed to authenticate: OAuth session expired')\n"
                  "print('{\"type\":\"result\",\"result\":\"You have hit your usage limit\",\"is_error\":true}')\n"
                  "raise SystemExit(1)")
        agent = SubprocessAgent([sys.executable, "-c", script],
                                stream_json=True, capture=True, notices=False)
        await collect(agent, "p")
        assert "Failed to authenticate" in agent.last_diagnostic
        assert "usage limit" in agent.last_diagnostic

    async def test_plain_mode_reads_errors_and_prose_tail_not_answer_events(self) -> None:
        answer = '{"type": "item.completed", "item": {"type": "agent_message", "text": "sign in first"}}'
        failed = '{"type": "turn.failed", "error": {"message": "usage limit reached"}}'
        script = (f"import sys\nprint({answer!r})\nprint({failed!r})\n"
                  "print('Error: not logged in', file=sys.stderr)\nraise SystemExit(1)")
        agent = SubprocessAgent([sys.executable, "-c", script], capture=True, notices=False)
        await collect(agent, "p")
        assert "sign in first" not in agent.last_diagnostic
        assert "usage limit reached" in agent.last_diagnostic
        assert "not logged in" in agent.last_diagnostic

    async def test_a_timed_out_turn_was_talking_not_refusing(self) -> None:
        script = "import time\nprint('please sign in again', flush=True)\ntime.sleep(30)"
        agent = SubprocessAgent([sys.executable, "-c", script],
                                timeout_s=1.0, capture=True, notices=False)
        await collect(agent, "p")
        assert agent.last_failed
        assert "sign in" not in agent.last_diagnostic
