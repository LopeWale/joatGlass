"""WhisperServeEngine tests against a stub HTTP server — no real model."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from joat.audio.whisper_serve import (
    DEFAULT_READY_TIMEOUT_S,
    MIN_UTTERANCE_BYTES,
    WhisperServeEngine,
    is_prompt_echo,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class StubHandler(BaseHTTPRequestHandler):
    """Canned serve endpoint. Class attrs configure behavior per test."""

    response_text = "hello from stub"
    requests: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        StubHandler.requests.append(body)
        payload = json.dumps({"text": StubHandler.response_text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


@pytest.fixture
def stub_server():
    StubHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


SPEECH = b"\x01\x02" * MIN_UTTERANCE_BYTES  # comfortably over the gate


class TestFlush:
    async def test_transcribes_via_http_with_required_fields(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server, model="large-v3_turbo")
        await engine.feed(SPEECH)
        t = await engine.flush()
        assert t is not None and t.text == "hello from stub"
        body = StubHandler.requests[0]
        assert b'name="model"' in body and b"large-v3_turbo" in body
        assert b'name="response_format"' in body
        assert b"RIFF" in body  # the WAV actually made it into the multipart

    async def test_below_min_length_is_dropped_without_http(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server)
        await engine.feed(b"\x01\x02" * 10)
        assert await engine.flush() is None
        assert StubHandler.requests == []

    async def test_overflow_drops_oldest(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server, max_buffer_s=0.5)
        await engine.feed(b"\x01\x00" * 32000)  # 2 s of audio into a 0.5 s buffer
        t = await engine.flush()
        assert t is not None  # trimmed, not exploded

    async def test_server_down_returns_none_gracefully(self) -> None:
        engine = WhisperServeEngine(base_url=f"http://127.0.0.1:{free_port()}")
        await engine.feed(SPEECH)
        assert await engine.flush() is None  # external: no respawn possible

    async def test_reset_drops_buffer(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server)
        await engine.feed(SPEECH)
        engine.reset()
        assert await engine.flush() is None
        assert StubHandler.requests == []


STUB_SERVER_SCRIPT = textwrap.dedent("""
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            p = json.dumps({"text": "spawned stub"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(p)))
            self.end_headers()
            self.wfile.write(p)
        def log_message(self, *a):
            pass

    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")


class TestLifecycle:
    def test_default_readiness_budget_does_not_block_other_capabilities(self) -> None:
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1")

        assert DEFAULT_READY_TIMEOUT_S == 30.0
        assert engine._ready_timeout_s == DEFAULT_READY_TIMEOUT_S

    async def test_spawns_waits_ready_and_terminates(self, tmp_path: Path) -> None:
        script = tmp_path / "stub_serve.py"
        script.write_text(STUB_SERVER_SCRIPT)
        port = free_port()
        engine = WhisperServeEngine(
            command=[sys.executable, str(script), str(port)],
            port=port,
            ready_timeout_s=10.0,
        )
        assert engine.available
        assert await engine.start() is True
        await engine.feed(SPEECH)
        t = await engine.flush()
        assert t is not None and t.text == "spawned stub"
        await engine.close()
        assert engine._proc is None

    async def test_start_fails_soft_when_command_dies(self, tmp_path: Path) -> None:
        engine = WhisperServeEngine(
            command=[sys.executable, "-c", "raise SystemExit(1)"],
            port=free_port(),
            ready_timeout_s=2.0,
        )
        assert await engine.start() is False  # returned, not raised

    async def test_shutdown_interrupts_readiness_and_reaps_child(self, tmp_path: Path) -> None:
        script = tmp_path / "never_ready.py"
        script.write_text("import time\ntime.sleep(60)\n")
        shutdown = asyncio.Event()
        engine = WhisperServeEngine(
            command=[sys.executable, str(script)],
            port=free_port(),
            ready_timeout_s=60.0,
        )
        starting = asyncio.create_task(engine.start(shutdown=shutdown))
        await asyncio.sleep(0.1)

        shutdown.set()

        with pytest.raises(asyncio.CancelledError):
            await starting
        assert engine._proc is None


class TestPromptEcho:
    """Whisper hallucinates its vocabulary-bias prompt on near-silence —
    observed live, and the assistant ANSWERED it."""

    PROMPT = "JOAT. The wearer addresses the assistant as JOAT."

    def test_the_observed_hallucination_is_dropped(self) -> None:
        assert is_prompt_echo("The wearer addresses the assistant as JOAT.", self.PROMPT)

    def test_a_genuine_short_wake_survives(self) -> None:
        # 'JOAT' appears in the prompt, but eating it would make the
        # assistant deaf to its own name.
        assert not is_prompt_echo("JOAT.", self.PROMPT)
        assert not is_prompt_echo("JOAT", self.PROMPT)

    def test_real_speech_survives(self) -> None:
        assert not is_prompt_echo("What time is it, JOAT?", self.PROMPT)

    def test_no_prompt_never_matches(self) -> None:
        assert not is_prompt_echo("anything", None)
