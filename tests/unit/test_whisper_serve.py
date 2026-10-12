"""WhisperServeEngine tests against a stub HTTP server — no real model."""

from __future__ import annotations

import asyncio
import json
import logging
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
from joat.core.interfaces import Transcript


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

    async def test_successful_stt_never_emits_exact_request_timing(
        self,
        stub_server,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="joat.audio.whisper_serve")
        engine = WhisperServeEngine(base_url=stub_server)

        await engine.feed(SPEECH)

        assert await engine.flush() is not None
        assert "stt timing:" not in caplog.text

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

    async def test_slow_post_is_not_timed_out_or_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A warm-tail request remains owned until its worker really returns."""
        started = threading.Event()
        release = threading.Event()
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1")

        def slow_post(_payload: bytes) -> str:
            started.set()
            release.wait()
            return "eventual transcript"

        async def unexpected_respawn() -> bool:
            raise AssertionError("a slow in-flight POST must not be retried")

        monkeypatch.setattr(engine, "_post", slow_post)
        monkeypatch.setattr(engine, "_respawn", unexpected_respawn)
        await engine.feed(SPEECH)
        flushing = asyncio.create_task(engine.flush())
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 0.5), 1.0)
            await asyncio.sleep(0)
            assert not flushing.done()
        finally:
            release.set()

        transcript = await flushing
        assert transcript is not None
        assert transcript.text == "eventual transcript"

class TestTranscribeOnce:
    """A QuickNote chunk (FEAT-002): stateless, and honest about failure."""

    async def test_transcribes_without_touching_the_utterance_buffer(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server)
        await engine.feed(SPEECH)  # a conversational utterance in progress

        chunk = await engine.transcribe_once(SPEECH)

        assert chunk is not None and chunk.text == "hello from stub"
        assert len(StubHandler.requests) == 1
        # The wearer's own utterance is still there, untouched.
        assert await engine.flush() is not None
        assert len(StubHandler.requests) == 2

    async def test_no_words_is_none_and_a_short_chunk_never_posts(self, stub_server) -> None:
        engine = WhisperServeEngine(base_url=stub_server)
        StubHandler.response_text = "   "
        try:
            assert await engine.transcribe_once(SPEECH) is None
        finally:
            StubHandler.response_text = "hello from stub"
        assert await engine.transcribe_once(b"\x01\x02" * 10) is None
        assert len(StubHandler.requests) == 1

    async def test_a_note_chunk_is_sent_without_the_wake_word_bias(self, stub_server) -> None:
        """Dictation is not addressed to JOAT: the bias prompt only slowed
        every chunk (~0.6 s measured) and once leaked into a note."""
        engine = WhisperServeEngine(base_url=stub_server, prompt="JOAT. Bias text.")

        assert await engine.transcribe_once(SPEECH) is not None
        await engine.feed(SPEECH)
        assert await engine.flush() is not None

        note_body, conversation_body = StubHandler.requests
        assert b'name="prompt"' not in note_body
        assert b'name="prompt"' in conversation_body
        assert b"JOAT. Bias text." in conversation_body

    async def test_a_dead_server_raises_instead_of_reporting_silence(self) -> None:
        from joat.core.interfaces import SttUnavailableError

        engine = WhisperServeEngine(base_url=f"http://127.0.0.1:{free_port()}")
        with pytest.raises(SttUnavailableError):
            await engine.transcribe_once(SPEECH)


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
    async def test_spawn_uses_one_text_only_decode_without_temperature_fallbacks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: list[object] = []

        class Process:
            returncode = None

        async def create_subprocess_exec(*args: object, **kwargs: object) -> Process:
            captured.extend(args)
            captured.append(kwargs)
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
        engine = WhisperServeEngine(port=50_061, model_dir=tmp_path / "models")

        await engine._spawn()

        assert captured[:-1] == [
            "whisperkit-cli", "serve", "--model", "large-v3_turbo",
            "--download-model-path", str(tmp_path / "models"),
            "--host", "127.0.0.1", "--port", "50061",
            "--temperature-fallback-count", "0", "--without-timestamps",
        ]

    async def test_the_weights_never_land_in_an_icloud_synced_folder(self) -> None:
        """whisperkit-cli defaults its download to ~/Documents/huggingface.

        On a Mac with Desktop & Documents sync on — as the wearer's is —
        that uploads ~3 GB of regenerable model weights to their iCloud,
        counted against their storage and pushed to every device, to hold
        a file any machine can fetch again.
        """
        from pathlib import Path

        from joat.audio.whisper_serve import DEFAULT_MODEL_DIR

        assert "Documents" not in DEFAULT_MODEL_DIR.parts
        assert "Mobile Documents" not in str(DEFAULT_MODEL_DIR)
        assert DEFAULT_MODEL_DIR.is_relative_to(
            Path.home() / "Library/Application Support"
        )

    async def test_spawn_decode_policy_remains_configurable_for_ab_trials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[object] = []

        class Process:
            returncode = None

        async def create_subprocess_exec(*args: object, **kwargs: object) -> Process:
            captured.extend(args)
            captured.append(kwargs)
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
        engine = WhisperServeEngine(
            port=50_062,
            temperature_fallback_count=2,
            without_timestamps=False,
        )

        await engine._spawn()

        assert captured[:-1][-2:] == ["--temperature-fallback-count", "2"]
        assert "--without-timestamps" not in captured[:-1]

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

    async def test_start_retains_recovery_when_command_dies(self, tmp_path: Path) -> None:
        engine = WhisperServeEngine(
            command=[sys.executable, "-c", "raise SystemExit(1)"],
            port=free_port(),
            ready_timeout_s=0.05,
        )
        try:
            assert await engine.start() is True  # temporary batch, still recoverable
            assert not engine._ready
        finally:
            await engine.close()

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


class TestColdStartRecovery:
    async def test_failed_spawns_retry_at_a_bounded_cadence_without_logging_content(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from joat.audio import whisper_serve

        monkeypatch.setattr(whisper_serve, "START_RETRY_DELAY_S", 0.1)
        engine = WhisperServeEngine(command=["test-only"], ready_timeout_s=0.02)
        attempts: list[float] = []

        async def spawn() -> None:
            attempts.append(asyncio.get_running_loop().time())
            raise OSError("private server body and prompt must not appear")

        monkeypatch.setattr(engine, "_spawn", spawn)
        try:
            assert await engine.start()
            async with asyncio.timeout(1):
                while len(attempts) < 3:
                    await asyncio.sleep(0.01)
            assert all(b - a >= 0.09 for a, b in zip(attempts, attempts[1:]))
            assert "private server body" not in caplog.text
            await engine.close()
            count = len(attempts)
            await asyncio.sleep(0.15)
            assert len(attempts) == count
        finally:
            await engine.close()

    async def test_model_switch_joins_old_canary_worker_before_spawning(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        joining = asyncio.Event()
        engine = WhisperServeEngine(command=["test-only"], ready_timeout_s=0.05)
        spawned: list[str] = []
        worker_models: list[str] = []
        original_join = engine._join_canary

        async def spawn() -> None:
            spawned.append(engine._model)

        def post(_payload: bytes) -> str:
            worker_models.append(engine._model)
            if len(worker_models) == 1:
                entered.set()
                release.wait(3)
            return ""

        async def join() -> None:
            joining.set()
            await original_join()

        monkeypatch.setattr(engine, "_spawn", spawn)
        monkeypatch.setattr(engine, "_post", post)
        monkeypatch.setattr(engine, "_join_canary", join)
        switching = None
        try:
            assert await engine.start()
            assert entered.is_set()
            switching = asyncio.create_task(engine.set_model("tiny"))
            await asyncio.wait_for(joining.wait(), 1)
            assert not switching.done()
            assert spawned == ["large-v3_turbo"]
            assert engine._model == "large-v3_turbo"
            release.set()
            assert await asyncio.wait_for(switching, 1)
            assert worker_models == ["large-v3_turbo", "tiny"]
            assert spawned == ["large-v3_turbo", "tiny"]
        finally:
            release.set()
            if switching is not None:
                await switching
            await engine.close()

    async def test_failed_model_switch_restores_old_model_without_reporting_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = WhisperServeEngine(command=["test-only"], ready_timeout_s=0.02)
        spawned: list[str] = []

        async def spawn() -> None:
            spawned.append(engine._model)

        async def wait_ready() -> None:
            if engine._model == "unavailable":
                await asyncio.Event().wait()

        monkeypatch.setattr(engine, "_spawn", spawn)
        monkeypatch.setattr(engine, "_wait_ready", wait_ready)
        try:
            assert await engine.start()
            assert not await engine.set_model("unavailable")
            assert engine._model == "large-v3_turbo"
            assert engine._ready
            assert spawned == ["large-v3_turbo", "unavailable", "large-v3_turbo"]
        finally:
            await engine.close()

    async def test_concurrent_start_and_close_cannot_await_a_cleared_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1")

        async def wait_ready() -> None:
            pass

        monkeypatch.setattr(engine, "_wait_ready", wait_ready)
        assert await engine.start()
        results = await asyncio.gather(engine.start(), engine.close(), return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert not engine._ready

    async def test_invalid_response_is_not_readiness_or_logged_content(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.request

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self):
                return b'{"unexpected": "private response"}'

        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Response())
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1")
        with pytest.raises(RuntimeError, match="missing transcription text"):
            engine._post(b"synthetic")

    async def test_late_canary_keeps_one_worker_and_recovers_after_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 30-second gate is scaled down; the same POST finishes late."""
        from joat.audio.whisperkit import WhisperKitEngine

        canary_started = threading.Event()
        release_canary = threading.Event()
        batch_started = asyncio.Event()
        release_batch = asyncio.Event()
        calls: list[bytes] = []
        batch_audio: list[bytes] = []
        engine = WhisperServeEngine(
            base_url="http://127.0.0.1:1", ready_timeout_s=0.05,
        )

        def post(payload: bytes) -> str:
            calls.append(payload)
            if len(calls) == 1:
                canary_started.set()
                release_canary.wait(3)
                return ""
            return "resident result"

        async def batch(_self: WhisperKitEngine, pcm: bytes) -> str:
            batch_audio.append(pcm)
            batch_started.set()
            await release_batch.wait()
            return "batch result"

        monkeypatch.setattr(engine, "_post", post)
        monkeypatch.setattr(WhisperKitEngine, "_transcribe", batch)
        flushing = None
        try:
            assert await asyncio.wait_for(engine.start(), 0.5)
            assert canary_started.is_set()
            assert not engine._ready
            await engine.feed(SPEECH)
            flushing = asyncio.create_task(engine.flush())
            await asyncio.wait_for(batch_started.wait(), 0.5)
            assert len(calls) == 1
            release_canary.set()
            await asyncio.wait_for(asyncio.shield(engine._startup_task), 1)
            assert engine._ready
            assert not flushing.done()
            release_batch.set()
            assert (await flushing).text == "batch result"
            await engine.feed(SPEECH)
            assert (await engine.flush()).text == "resident result"
            assert batch_audio == [SPEECH]
            assert len(calls) == 2  # no replay of the utterance that used batch
        finally:
            release_canary.set()
            release_batch.set()
            if flushing is not None:
                await flushing
            await engine.close()

    @pytest.mark.parametrize("stop", ["close", "shutdown", "cancel"])
    async def test_stopping_during_canary_cannot_publish_late_readiness(
        self, monkeypatch: pytest.MonkeyPatch, stop: str
    ) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        shutdown = asyncio.Event()
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1")

        async def wait_ready() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(engine, "_wait_ready", wait_ready)
        starting = asyncio.create_task(engine.start(shutdown=shutdown))
        await asyncio.wait_for(entered.wait(), 1)
        if stop == "close":
            await engine.close()
        elif stop == "shutdown":
            shutdown.set()
        else:
            starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 1)
        assert cancelled.is_set()
        assert engine._proc is None
        assert not engine._ready
        await engine.close()


class TestPromptEcho:
    """Whisper hallucinates its vocabulary-bias prompt on near-silence —
    observed live, and the assistant ANSWERED it."""

    PROMPT = "JOAT. The wearer addresses the assistant as JOAT."

    def test_the_observed_hallucination_is_dropped(self) -> None:
        assert is_prompt_echo("The wearer addresses the assistant as JOAT.", self.PROMPT)

    def test_dropped_echo_log_never_contains_transcript(self, caplog: pytest.LogCaptureFixture) -> None:
        transcript = "The wearer addresses the assistant as JOAT."
        engine = WhisperServeEngine(base_url="http://127.0.0.1:1", prompt=self.PROMPT)

        caplog.set_level(logging.INFO, logger="joat.audio.whisper_serve")

        assert engine._filter_echo(Transcript(text=transcript, is_final=True)) is None
        assert transcript not in caplog.text
        assert f"chars={len(transcript)}" in caplog.text

    def test_a_genuine_short_wake_survives(self) -> None:
        # 'JOAT' appears in the prompt, but eating it would make the
        # assistant deaf to its own name.
        assert not is_prompt_echo("JOAT.", self.PROMPT)
        assert not is_prompt_echo("JOAT", self.PROMPT)

    def test_real_speech_survives(self) -> None:
        assert not is_prompt_echo("What time is it, JOAT?", self.PROMPT)

    def test_no_prompt_never_matches(self) -> None:
        assert not is_prompt_echo("anything", None)


class TestWhereTheWeightsLive:
    """~3 GB of model, and where it lands is not a detail.

    whisperkit-cli downloads to ~/Documents/huggingface by default, which
    is iCloud-synced whenever Desktop & Documents sync is on — so the
    weights get uploaded, counted against the wearer's storage, and pushed
    to every device they own, to hold a file any machine can refetch.
    """

    def test_a_fresh_machine_downloads_somewhere_unsynced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from joat.audio import whisper_serve as ws

        monkeypatch.setattr(ws, "LEGACY_MODEL_DIR", tmp_path / "Documents/huggingface")
        assert ws._existing_model_dir(tmp_path / "prefer", "large-v3_turbo") \
            == tmp_path / "prefer"

    def test_weights_already_downloaded_are_used_where_they_are(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pointing straight at the new directory would be WORSE than
        leaving it alone: the next start re-downloads 3 GB while the old
        copy still sits in iCloud, so the wearer briefly owns six
        gigabytes of one model to fix a problem about three."""
        from joat.audio import whisper_serve as ws

        legacy = tmp_path / "Documents/huggingface"
        (legacy / "models/argmaxinc/whisperkit-coreml"
         / "openai_whisper-large-v3_turbo").mkdir(parents=True)
        monkeypatch.setattr(ws, "LEGACY_MODEL_DIR", legacy)

        assert ws._existing_model_dir(tmp_path / "prefer", "large-v3_turbo") == legacy

    def test_the_unsynced_copy_wins_once_it_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from joat.audio import whisper_serve as ws

        legacy = tmp_path / "Documents/huggingface"
        (legacy / "models/argmaxinc/whisperkit-coreml"
         / "openai_whisper-large-v3_turbo").mkdir(parents=True)
        monkeypatch.setattr(ws, "LEGACY_MODEL_DIR", legacy)
        preferred = tmp_path / "prefer"
        (preferred / "models").mkdir(parents=True)

        assert ws._existing_model_dir(preferred, "large-v3_turbo") == preferred

    def test_a_different_model_does_not_count_as_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy tree holding some OTHER model must not stop the
        wanted one downloading to the unsynced path."""
        from joat.audio import whisper_serve as ws

        legacy = tmp_path / "Documents/huggingface"
        (legacy / "models/argmaxinc/whisperkit-coreml"
         / "openai_whisper-tiny").mkdir(parents=True)
        monkeypatch.setattr(ws, "LEGACY_MODEL_DIR", legacy)

        assert ws._existing_model_dir(tmp_path / "prefer", "large-v3_turbo") \
            == tmp_path / "prefer"


class TestPromptEchoSurvivesWhisperFiller:
    """An echo that wakes the assistant is worse than a lost utterance.

    Our bias text CONTAINS the wake word, so an undetected echo does not
    merely pass through — it is treated as the wearer addressing JOAT,
    and the assistant answers its own prompt. The docstring records that
    happening live.

    Whole-string containment caught the clean echo and nothing else. It
    failed the moment Whisper added a word, and "Thank you." is among its
    most common hallucinations on exactly the near-silent audio that
    produces prompt echo.
    """

    PROMPT = "JOAT. The wearer addresses the assistant as JOAT."

    def test_an_echo_with_whisper_filler_is_still_an_echo(self) -> None:
        from joat.audio.whisper_serve import is_prompt_echo

        for heard in (
            "The wearer addresses the assistant as JOAT. Thank you.",
            "Okay. The wearer addresses the assistant as JOAT.",
            "The wearer addresses the assistant as JOAT and",
            "Thanks for watching! The wearer addresses the assistant as JOAT.",
        ):
            assert is_prompt_echo(heard, self.PROMPT), heard

    def test_the_clean_echo_is_still_caught(self) -> None:
        from joat.audio.whisper_serve import is_prompt_echo

        for heard in (
            "The wearer addresses the assistant as JOAT.",
            "the wearer addresses the assistant",
            "JOAT. The wearer addresses the assistant as JOAT.",
        ):
            assert is_prompt_echo(heard, self.PROMPT), heard

    def test_genuine_speech_is_never_eaten(self) -> None:
        """Including speech that mentions the assistant, and one long
        utterance that quotes prompt-shaped English on purpose — coverage
        is what keeps that from being mistaken for the prompt returning."""
        from joat.audio.whisper_serve import is_prompt_echo

        for heard in (
            "JOAT",
            "JOAT what is the weather",
            "JOAT the wearer needs a reminder",
            "JOAT remind me to address the team",
            "JOAT who is my assistant",
            "JOAT the assistant should wait",
            "JOAT tell the wearer addresses the assistant keeps for my "
            "meeting tomorrow",
        ):
            assert not is_prompt_echo(heard, self.PROMPT), heard

    def test_no_prompt_configured_eats_nothing(self) -> None:
        from joat.audio.whisper_serve import is_prompt_echo

        assert not is_prompt_echo("anything at all here", None)
        assert not is_prompt_echo("", self.PROMPT)
