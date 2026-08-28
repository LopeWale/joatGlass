"""OpenClawAgent: the third collaborator.

The wearer named this integration critical. What matters most is that it
satisfies the SAME contract as the codex and claude agents — so a third
voice is a seat, not a redesign — and that its ABSENCE degrades to a
readable transcript line rather than taking the debate down, because the
daemon is optional and often simply not running.
"""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import AsyncIterator

import pytest

from joat.agents.openclaw import OpenClawAgent, _delta_text


def sse(*frames: dict[str, object]) -> bytes:
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    return (body + "data: [DONE]\n\n").encode()


async def drain(it: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in it])


class TestDeltaParsing:
    def test_reads_streaming_delta_frames(self) -> None:
        assert _delta_text(json.dumps(
            {"choices": [{"delta": {"content": "hello"}}]})) == "hello"

    def test_reads_non_streaming_message_frames(self) -> None:
        assert _delta_text(json.dumps(
            {"choices": [{"message": {"content": "hi"}}]})) == "hi"

    def test_an_unparsable_frame_is_a_dropped_token_not_a_crash(self) -> None:
        assert _delta_text("{not json") == ""
        assert _delta_text(json.dumps({"choices": []})) == ""
        assert _delta_text(json.dumps({})) == ""


class TestStreaming:
    async def test_yields_chunks_in_order_and_stops_at_DONE(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        payload = sse(
            {"choices": [{"delta": {"content": "Tabs "}}]},
            {"choices": [{"delta": {"content": "indent "}}]},
            {"choices": [{"delta": {"content": "faster."}}]},
        ) + b"data: {\"choices\":[{\"delta\":{\"content\":\"AFTER DONE\"}}]}\n\n"

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            assert req.full_url.endswith("/v1/chat/completions")
            assert json.loads(req.data)["stream"] is True
            return io.BytesIO(payload)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        agent = OpenClawAgent(token="t")
        assert await drain(agent.stream("tabs or spaces?")) == "Tabs indent faster."

    async def test_sends_the_bearer_token(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        seen: dict[str, str] = {}

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            seen.update({k.lower(): v for k, v in req.header_items()})
            return io.BytesIO(sse())

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        await drain(OpenClawAgent(token="sekrit").stream("q"))
        assert seen.get("Authorization".lower()) == "Bearer sekrit"

    async def test_an_absent_daemon_yields_a_readable_line_not_an_exception(
        self, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        def boom(req, timeout=None):  # type: ignore[no-untyped-def]
            raise OSError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        out = await drain(OpenClawAgent().stream("q"))
        assert "openclaw could not answer" in out  # the debate continues without it

    async def test_a_bad_token_says_so_in_the_transcript(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def denied(req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", denied)
        out = await drain(OpenClawAgent(token="wrong").stream("q"))
        assert "OPENCLAW_GATEWAY_TOKEN" in out  # names the fix, not just the failure


class TestAvailability:
    """Probed against the endpoint the debate actually calls.

    An earlier version probed GET /healthz — a path OpenClaw does not
    document. On a real gateway that probe always failed, so the third seat
    was never filled, silently. The stub tests could not see it because the
    stub implemented the invented endpoint too.
    """

    def test_probes_the_chat_completions_endpoint_not_an_invented_one(
        self, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        seen: list[str] = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def spy(req, timeout=None):  # type: ignore[no-untyped-def]
            seen.append(req.full_url)
            return Resp()

        monkeypatch.setattr("urllib.request.urlopen", spy)
        assert OpenClawAgent().available() is True
        assert seen == ["http://127.0.0.1:18789/v1/chat/completions"]
        assert not any("healthz" in u for u in seen)

    def test_unreachable_gateway_says_it_is_not_running(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def boom(req, timeout=None):  # type: ignore[no-untyped-def]
            raise OSError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        ok, why = OpenClawAgent().readiness()
        assert ok is False and "not reachable" in why

    def test_404_names_the_config_key_that_fixes_it(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Chat Completions is DISABLED by default in OpenClaw, so this is
        # the likeliest reason a running gateway refuses the seat.
        def gone(req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", gone)
        ok, why = OpenClawAgent().readiness()
        assert ok is False
        assert "chatCompletions.enabled" in why

    def test_401_names_the_token(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        def denied(req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr("urllib.request.urlopen", denied)
        ok, why = OpenClawAgent().readiness()
        assert ok is False and "OPENCLAW_GATEWAY_TOKEN" in why

    def test_the_password_auth_mode_env_var_also_supplies_the_bearer(
        self, monkeypatch  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
        monkeypatch.setenv("OPENCLAW_GATEWAY_PASSWORD", "pw")
        assert OpenClawAgent()._token == "pw"

    def test_base_url_comes_from_the_environment_when_unset(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("OPENCLAW_BASE_URL", "http://127.0.0.1:9999")
        assert OpenClawAgent()._base_url == "http://127.0.0.1:9999"
