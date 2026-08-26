"""Resident WhisperKit STT via `whisperkit-cli serve`.

The batch engine reloads model weights into the ANE on every utterance
(~7 s each). Serve mode loads once and stays warm: measured 1.1 s per
utterance against the same audio. Same `SttEngine` interface — the
orchestrator cannot tell the difference.

Probe-verified server facts this adapter depends on:
- `serve` binds IPv6 `::1` by default — we spawn with an explicit
  `--host 127.0.0.1` and dial the same, removing dual-stack ambiguity.
- The `model` multipart field is REQUIRED (stricter than OpenAI's spec);
  omitting it returns `{"error": true}` with HTTP 200-ish speed.
- Readiness cannot be inferred from a TCP accept (the port answers while
  the model is still loading) — the only honest check is a successful
  canary transcription of a slice of silence.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import urllib.error
import urllib.request
import uuid
import wave
from typing import Any

from ..core.interfaces import SttEngine, Transcript
from .whisperkit import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH

log = logging.getLogger(__name__)

MIN_UTTERANCE_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 4  # < 250 ms is a VAD blip


def wav_bytes(pcm: bytes) -> bytes:
    """16 kHz mono s16le PCM → in-memory WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data body — keeps stdlib-only."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
        )
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
         f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
        + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WhisperServeEngine(SttEngine):
    """Buffer PCM; transcribe on flush against a resident serve process.

    For tests: pass `base_url` to skip subprocess management entirely, or
    `command` to substitute what gets spawned.
    """

    def __init__(
        self,
        binary: str = "whisperkit-cli",
        model: str = "large-v3_turbo",
        port: int = 50060,
        base_url: str | None = None,
        command: list[str] | None = None,
        prompt: str | None = None,
        max_buffer_s: float = 30.0,
        ready_timeout_s: float = 180.0,
    ) -> None:
        self._binary = binary
        self._model = model
        self._port = port
        self._base_url = base_url or f"http://127.0.0.1:{port}"
        # Vocabulary bias. Whisper renders unknown proper nouns phonetically
        # ("JOAT" -> "Jode"/"Jodh"), which no alias list fully covers. The
        # OpenAI-compatible `prompt` field is the sanctioned way to teach it
        # names it would otherwise guess at.
        self._prompt = prompt
        self._command = command
        self._external = base_url is not None and command is None
        self._max_bytes = int(max_buffer_s * SAMPLE_RATE * SAMPLE_WIDTH)
        self._ready_timeout_s = ready_timeout_s
        self._buffer = bytearray()
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def available(self) -> bool:
        return self._external or self._command is not None or shutil.which(self._binary) is not None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Spawn the server (unless external) and wait until it transcribes.

        The first-ever start also downloads the model, so the ready timeout
        is generous. Returns False (never raises) when serve can't come up —
        callers degrade the same way the batch engine does.
        """
        try:
            if not self._external:
                await self._spawn()
            await self._wait_ready()
            log.info("whisper serve ready at %s (model %s)", self._base_url, self._model)
            return True
        except Exception as exc:
            log.error("whisper serve failed to start: %s", exc)
            await self.close()
            return False

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5.0)
            except asyncio.TimeoutError:
                proc.kill()

    async def _spawn(self) -> None:
        cmd = self._command or [
            self._binary, "serve", "--model", self._model,
            "--host", "127.0.0.1", "--port", str(self._port),
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )

    async def _wait_ready(self) -> None:
        canary = wav_bytes(b"\x00\x00" * (SAMPLE_RATE // 10))  # 100 ms silence
        deadline = asyncio.get_running_loop().time() + self._ready_timeout_s
        while True:
            try:
                await asyncio.to_thread(self._post, canary)
                return
            except Exception as exc:
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError(f"serve not ready after {self._ready_timeout_s}s: {exc}") from exc
                await asyncio.sleep(1.0)

    # ── SttEngine ────────────────────────────────────────────────────────────

    async def feed(self, pcm: bytes) -> Transcript | None:
        self._buffer.extend(pcm)
        if len(self._buffer) > self._max_bytes:
            overflow = len(self._buffer) - self._max_bytes
            del self._buffer[:overflow]
            log.warning("stt buffer full, dropped %d bytes of old audio", overflow)
        return None

    async def flush(self) -> Transcript | None:
        if len(self._buffer) < MIN_UTTERANCE_BYTES:
            self._buffer.clear()
            return None
        payload = wav_bytes(bytes(self._buffer))
        self._buffer.clear()
        try:
            text = await asyncio.to_thread(self._post, payload)
        except Exception as exc:
            log.warning("whisper serve request failed (%s) — respawning once", exc)
            if not await self._respawn():
                return None
            try:
                text = await asyncio.to_thread(self._post, payload)
            except Exception as exc2:
                log.error("whisper serve retry failed: %s", exc2)
                return None
        return Transcript(text=text, is_final=True) if text else None

    def reset(self) -> None:
        self._buffer.clear()

    # ── internals ────────────────────────────────────────────────────────────

    async def _respawn(self) -> bool:
        if self._external:
            return False  # nothing we own to restart
        await self.close()
        try:
            await self._spawn()
            await self._wait_ready()
            return True
        except Exception as exc:
            log.error("whisper serve respawn failed: %s", exc)
            return False

    def _post(self, wav_payload: bytes) -> str:
        fields = {"model": self._model, "response_format": "json"}
        if self._prompt:
            fields["prompt"] = self._prompt
        body, content_type = multipart(
            fields, "file", "utterance.wav", wav_payload,
        )
        req = urllib.request.Request(
            f"{self._base_url}/v1/audio/transcriptions",
            data=body, headers={"Content-Type": content_type}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            parsed: Any = json.loads(resp.read().decode())
        if not isinstance(parsed, dict) or parsed.get("error"):
            raise RuntimeError(f"serve error response: {parsed!r}")
        text = parsed.get("text")
        return text.strip() if isinstance(text, str) else ""
