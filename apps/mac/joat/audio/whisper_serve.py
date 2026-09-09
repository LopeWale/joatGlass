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
import difflib
import io
import json
import logging
import re
import shutil
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

from ..core.interfaces import SttEngine, Transcript
from .whisperkit import (
    CHANNELS, DEFAULT_INFERENCE_DEADLINE_S, SAMPLE_RATE, SAMPLE_WIDTH, WhisperKitEngine,
)

log = logging.getLogger(__name__)

MIN_UTTERANCE_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 4  # < 250 ms is a VAD blip
DEFAULT_READY_TIMEOUT_S = 30.0
DEFAULT_BATCH_DEADLINE_S = DEFAULT_INFERENCE_DEADLINE_S
START_RETRY_DELAY_S = 30.0

DEFAULT_MODEL_DIR = Path.home() / "Library/Application Support/JOAT/models"
"""Where NEW WhisperKit weights are downloaded.

Said explicitly because whisperkit-cli's own default is
`~/Documents/huggingface`, and `~/Documents` is an iCloud-synced folder
whenever Desktop & Documents sync is on — as it is on this Mac. That put
three gigabytes of regenerable model weights into the wearer's iCloud:
uploaded, counted against their storage, and pushed to every device they
own, to hold a file any machine can fetch again in minutes.

Application Support is where the rest of JOAT's state already lives, and
is never synced.
"""

LEGACY_MODEL_DIR = Path.home() / "Documents/huggingface"
"""whisperkit-cli's own default, which earlier runs already populated."""


def _existing_model_dir(preferred: Path, model: str) -> Path:
    """Prefer weights that are already on disk, wherever they landed.

    Pointing straight at the new directory would have been worse than
    leaving it alone: the next start would re-download ~3 GB while the old
    copy still sat in iCloud, so the wearer would briefly own SIX
    gigabytes of the same model to fix a problem about three.

    So an existing download wins, and only a genuinely absent model goes
    to the unsynced path. Migrating the old copy is the wearer's call —
    it is several gigabytes inside their Documents, not ours to move.
    """
    if (preferred / "models").exists():
        return preferred
    legacy = LEGACY_MODEL_DIR / "models/argmaxinc/whisperkit-coreml"
    if any(legacy.glob(f"*{model}*")) if legacy.is_dir() else False:
        log.info(
            "whisper weights found in %s (iCloud-synced); using them rather "
            "than re-downloading. Move them to %s to keep them out of iCloud.",
            LEGACY_MODEL_DIR, preferred,
        )
        return LEGACY_MODEL_DIR
    return preferred


ECHO_RUN_CHARS = 24
"""How much verbatim prompt an echo must carry to be called one."""

ECHO_COVERAGE = 0.6
"""...and how much of the TRANSCRIPT that run must account for."""


_SOUND_EVENT = re.compile(
    r"^[\s\-–—]*(?:"
    r"\*[^*]*\*"        # *phone rings*, *sniff*
    r"|\[[^\]]*\]"      # [BLANK_AUDIO], [Music]
    r"|\([^)]*\)"        # (door closes)
    r"|[\u266a\u266b\u2669\u266c]+[^\u266a\u266b\u2669\u266c]*[\u266a\u266b\u2669\u266c]*"
    r")[\s\-–—.]*$",
    re.IGNORECASE,
)


def is_sound_event(text: str) -> bool:
    """True when Whisper described a NOISE instead of transcribing speech.

    Whisper annotates non-speech audio in its own notation — "*phone
    rings*", "*sniff*", "[BLANK_AUDIO]", "(door closes)", a musical note
    run. Nothing downstream knew that convention, so an annotation was
    treated as the wearer talking: seen on the wearer's lens as
    `LAST QUERY  you  *phone rings*`, and seen answering itself when a
    stray `*sniff*` opened a turn and painted over the reply being read.

    Only a transcript that is ENTIRELY one annotation is dropped. Speech
    that merely contains a parenthetical is the wearer talking, and the
    wearer saying "call it (a) draft" must survive intact.
    """
    return bool(_SOUND_EVENT.fullmatch(text.strip())) if text.strip() else False


def is_prompt_echo(text: str, prompt: str | None) -> bool:
    """True when STT 'heard' its own vocabulary-bias prompt.

    Whisper hallucinates its initial prompt on near-silent audio — observed
    live: the transcript 'The wearer addresses the assistant as JOAT.' is
    OUR bias text, and the assistant then answered it.

    Matched on the longest run of prompt shared with the transcript, not on
    whole-string containment. Containment failed the moment Whisper added a
    word: "… as JOAT. Thank you." is not a substring of the prompt, and
    "Thank you." is one of Whisper's most common hallucinations on the
    near-silent audio that produces prompt echo in the first place. Such an
    echo then WAKES the assistant, because our bias text contains the wake
    word, and it answers its own prompt.

    Two thresholds, both measured against real prompt text rather than
    chosen: echoes share 34-47 characters with the prompt while genuine
    wearer speech shares at most 16, so 24 separates them with room either
    side. Coverage is what protects a long utterance that merely quotes
    prompt-shaped English — the run must also account for most of what was
    said, or it is speech that mentions the assistant, not the prompt
    coming back.
    """
    if not prompt or not text:
        return False

    def norm(s: str) -> str:
        return " ".join(
            "".join(c for c in s.lower() if c.isalnum() or c == " ").split()
        )

    heard, bias = norm(text), norm(prompt)
    if not heard or not bias:
        return False
    run = difflib.SequenceMatcher(None, heard, bias).find_longest_match(
        0, len(heard), 0, len(bias)
    ).size
    return run >= ECHO_RUN_CHARS and run / len(heard) >= ECHO_COVERAGE


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
        ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S,
        temperature_fallback_count: int = 0,
        without_timestamps: bool = True,
        model_dir: Path | None = None,
        batch_deadline_s: float = DEFAULT_BATCH_DEADLINE_S,
    ) -> None:
        if temperature_fallback_count < 0:
            raise ValueError("temperature_fallback_count must be non-negative")
        if batch_deadline_s <= 0:
            raise ValueError("batch_deadline_s must be positive")
        self._binary = binary
        self._model = model
        # The batch fallback (used while serve is still warming) inherits a
        # deadline too; an unbounded child there is the same silent hang.
        self._batch_deadline_s = batch_deadline_s
        self._model_dir = (
            model_dir if model_dir is not None
            else _existing_model_dir(DEFAULT_MODEL_DIR, model)
        )
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
        self._temperature_fallback_count = temperature_fallback_count
        self._without_timestamps = without_timestamps
        self._buffer = bytearray()
        self._proc: asyncio.subprocess.Process | None = None
        self._ready = False
        self._started = False
        self._closed = False
        self._shutdown: asyncio.Event | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._canary_task: asyncio.Task[str] | None = None
        self._batch_tasks: set[asyncio.Task[Transcript | None]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._external or self._command is not None or shutil.which(self._binary) is not None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, shutdown: asyncio.Event | None = None) -> bool:
        """Wait at most the foreground budget, then recover behind batch STT.

        True means this adapter owns STT (resident OR temporary batch), not
        that the model is already warm. Keep the same object in the session.
        A slow child and its in-flight canary survive the foreground timeout.
        """
        if self._closed:
            return False
        if shutdown is not None:
            self._shutdown = shutdown
        if self._shutdown is not None and self._shutdown.is_set():
            await self.close()
            raise asyncio.CancelledError
        if self._startup_task is None:
            self._started = True
            self._startup_task = asyncio.create_task(self._recover_until_ready())
        startup = self._startup_task
        try:
            done, _ = await asyncio.wait(
                (startup,), timeout=self._ready_timeout_s,
            )
            if done:
                await startup
            else:
                log.warning("whisper serve warming — temporary batch STT; recovery active")
            if self._closed or (self._shutdown is not None and self._shutdown.is_set()):
                raise asyncio.CancelledError
            return True
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _recover_until_ready(self) -> None:
        """One lifecycle owner, including after start() returns to its caller."""
        loading = asyncio.create_task(self._load_resident())
        stopped = (
            asyncio.create_task(self._shutdown.wait())
            if self._shutdown is not None else None
        )
        try:
            if stopped is not None:
                done, _ = await asyncio.wait(
                    (loading, stopped), return_when=asyncio.FIRST_COMPLETED,
                )
                if stopped in done:
                    self._closed = True
                    raise asyncio.CancelledError
            await loading
        finally:
            loading.cancel()
            if stopped is not None:
                stopped.cancel()
            await asyncio.gather(
                loading, *((stopped,) if stopped is not None else ()),
                return_exceptions=True,
            )
            if self._closed:
                self._ready = False
                await self._close_process()
                await self._join_canary()
                await self._stop_batches()

    async def _load_resident(self) -> None:
        while not self._closed:
            try:
                if not self._external:
                    await self._spawn()
                await self._wait_ready()
                if not self._closed:
                    self._ready = True
                    log.info("whisper serve ready at %s (model %s)", self._base_url, self._model)
                return
            except Exception as exc:
                # Never retain CLI output, server response bodies or audio.
                log.warning("whisper serve startup retry error=%s", type(exc).__name__)
                await self._close_process()
                await asyncio.sleep(START_RETRY_DELAY_S)

    async def close(self) -> None:
        self._closed = True
        self.reset()
        await self._stop_resident()
        await self._stop_batches()

    async def _stop_batches(self) -> None:
        tasks = tuple(self._batch_tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_resident(self) -> None:
        self._ready = False
        task = self._startup_task
        if task is not None:
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._startup_task is task:
                self._startup_task = None
        await self._close_process()
        await self._join_canary()

    async def _join_canary(self) -> None:
        task = self._canary_task
        if task is not None:
            # Cancelling to_thread's awaiter does not stop its worker. Kill
            # the owned server FIRST, then join before mutating/restarting.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            if self._canary_task is task:
                self._canary_task = None

    async def _close_process(self) -> None:
        async with self._process_lock:
            proc = self._proc
            if proc is None:
                return
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 5.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            # Keep the handle until reaped, even if cleanup is interrupted.
            if proc.returncode is None:
                await proc.wait()
            self._proc = None

    async def _spawn(self) -> None:
        if self._command is not None:
            cmd = self._command
        else:
            self._model_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                self._binary, "serve", "--model", self._model,
                "--download-model-path", str(self._model_dir),
                "--host", "127.0.0.1", "--port", str(self._port),
                "--temperature-fallback-count", str(self._temperature_fallback_count),
            ]
            if self._without_timestamps:
                cmd.append("--without-timestamps")
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        ))
        try:
            self._proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self._proc = await spawning
            raise

    async def _wait_ready(self) -> None:
        canary = wav_bytes(b"\x00\x00" * (SAMPLE_RATE // 10))  # 100 ms silence
        while True:
            if self._proc is not None and self._proc.returncode is not None:
                raise ChildProcessError("whisper serve exited before readiness")
            try:
                self._canary_task = asyncio.create_task(asyncio.to_thread(self._post, canary))
                await asyncio.shield(self._canary_task)
                if self._proc is not None and self._proc.returncode is not None:
                    raise ChildProcessError("whisper serve exited during readiness")
                return
            except ChildProcessError:
                raise
            except Exception:
                # Sequential probes: never abandon a canary just because the
                # foreground budget elapsed, then start an overlapping one.
                await asyncio.sleep(1.0)
            finally:
                if self._canary_task is not None and self._canary_task.done():
                    self._canary_task = None

    # ── SttEngine ────────────────────────────────────────────────────────────

    async def feed(self, pcm: bytes) -> Transcript | None:
        if self._closed:
            return None
        self._buffer.extend(pcm)
        if len(self._buffer) > self._max_bytes:
            overflow = len(self._buffer) - self._max_bytes
            del self._buffer[:overflow]
            log.warning("stt buffer full, dropped %d bytes of old audio", overflow)
        return None

    async def flush(self) -> Transcript | None:
        return self._filter_echo(await self._flush_raw())

    def _filter_echo(self, transcript: Transcript | None) -> Transcript | None:
        if transcript is not None and is_prompt_echo(transcript.text, self._prompt):
            # This transcription is discarded specifically because it is not
            # wearer speech.  Do not turn it into durable diagnostic content.
            log.info("dropped prompt-echo hallucination chars=%d", len(transcript.text))
            return None
        if transcript is not None and is_sound_event(transcript.text):
            log.info("dropped sound-event annotation chars=%d", len(transcript.text))
            return None
        return transcript

    async def _flush_raw(self) -> Transcript | None:
        if self._closed or (self._shutdown is not None and self._shutdown.is_set()):
            self._buffer.clear()
            return None
        if len(self._buffer) < MIN_UTTERANCE_BYTES:
            self._buffer.clear()
            return None
        pcm = bytes(self._buffer)
        self._buffer.clear()
        if self._started and not self._ready:
            return await self._batch_transcribe(pcm)
        payload = wav_bytes(pcm)
        try:
            text = await asyncio.to_thread(self._post, payload)
        except Exception as exc:
            log.warning(
                "whisper serve request failed error=%s — respawning once",
                type(exc).__name__,
            )
            if not await self._respawn():
                return await self._batch_transcribe(pcm) if not self._external else None
            try:
                text = await asyncio.to_thread(self._post, payload)
            except Exception as exc2:
                log.error("whisper serve retry failed error=%s", type(exc2).__name__)
                return None
        return Transcript(text=text, is_final=True) if text else None

    async def _batch_transcribe(self, pcm: bytes) -> Transcript | None:
        if self._closed or (self._shutdown is not None and self._shutdown.is_set()):
            return None
        # Capture one complete utterance/model. Late readiness affects ONLY
        # the next flush, never replays or splits an in-flight batch request.
        batch = WhisperKitEngine(
            binary=self._binary, model=self._model, model_dir=self._model_dir,
            inference_deadline_s=self._batch_deadline_s,
        )
        await batch.feed(pcm)
        task = asyncio.create_task(batch.flush())
        self._batch_tasks.add(task)
        task.add_done_callback(self._batch_tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Session cancellation and close may race. Send cancellation
            # once; a second one must not interrupt child reaping.
            if not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise

    def reset(self) -> None:
        self._buffer.clear()

    # ── internals ────────────────────────────────────────────────────────────

    async def set_model(self, model: str) -> bool:
        """Swap the resident model, keeping the old one if the new one fails.

        Unlike a debate seat — where the model is just an argv field on the
        next call — serve holds weights in the ANE, so switching means
        killing and respawning, and the first use of an undownloaded model
        also downloads it. Failure is the case that matters: an unknown name
        leaves serve unable to start, and a wearer whose glasses have gone
        silently deaf cannot ask for the old model back. So a failed switch
        restores the previous one before returning False.
        """
        if self._external or self._closed:
            log.info("STT model is owned by an external server — not switching")
            return False
        async with self._lifecycle_lock:
            if self._closed:
                return False
            if model == self._model:
                return True
            # Stop the old canary before changing the model it refers to.
            await self._stop_resident()
            previous, self._model = self._model, model
            if await self.start() and self._ready:
                log.info("whisper serve now on %s", model)
                return True
            await self._stop_resident()
            self._model = previous
            log.warning("whisper serve could not load %s — restoring %s", model, previous)
            await self.start()
            return False

    async def _respawn(self) -> bool:
        if self._external or self._closed:
            return False  # nothing we own to restart
        async with self._lifecycle_lock:
            await self._stop_resident()
            return await self.start() and self._ready

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
            raise RuntimeError("serve error response")
        text = parsed.get("text")
        if not isinstance(text, str):
            raise RuntimeError("serve response missing transcription text")
        return text.strip()
