"""Abstract interfaces the rest of the brain programs against.

RULES.md #1: every external dependency sits behind an ABC here, and concrete
adapters live in their own package. The orchestrator imports from this module
and nothing else — swapping the Hub relay for direct-BLE, or WhisperKit for
Parakeet, must not touch a line of orchestration code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from .protocol import Device, DownlinkMessage, InputEvent

AudioCallback = Callable[[bytes, int], None]
"""(pcm_bytes, seq). A jump in seq means frames were dropped upstream."""

InputCallback = Callable[[InputEvent], None]
DeviceCallback = Callable[[Device], None]


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    is_final: bool
    confidence: float | None = None


class GlassesTransport(ABC):
    """A link to the G2, however it is carried.

    Implemented by HubGatewayTransport (WebSocket to the Hub plugin) and later
    DirectBleTransport (CoreBluetooth). Both deliver 16 kHz mono 16-bit LE PCM
    — the Hub SDK decodes it for us, direct-BLE will decode LC3 itself, and
    neither difference escapes the adapter.
    """

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, message: DownlinkMessage) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def on_audio(self, callback: AudioCallback) -> None: ...

    @abstractmethod
    def on_input(self, callback: InputCallback) -> None: ...

    @abstractmethod
    def on_device(self, callback: DeviceCallback) -> None: ...


class SttEngine(ABC):
    """Speech-to-text over a PCM stream.

    Stateful by design: feed() accepts chunks as they arrive and may emit
    partial transcripts; flush() forces a final result at an utterance
    boundary (VAD silence, or the wearer releasing a push-to-talk).
    """

    @abstractmethod
    async def feed(self, pcm: bytes) -> Transcript | None: ...

    @abstractmethod
    async def flush(self) -> Transcript | None: ...

    @abstractmethod
    def reset(self) -> None:
        """Drop buffered audio — call on a seq gap, so a dropped-frame
        discontinuity is never spliced into one bogus utterance."""


class Confirmer(ABC):
    """Explicit human approval for a consequential action (RULES.md #6)."""

    @abstractmethod
    async def confirm(self, prompt: str, timeout_s: float = 8.0) -> bool: ...


class ReplyEngine(ABC):
    """The fast-path local LLM (MAC_APP.md): transcript in, terse reply out.

    Implementations must keep the model resident — the whole point of the
    fast path is no per-utterance cold start. Return None to decline (engine
    unavailable, empty prompt), in which case the orchestrator shows the
    transcript alone.
    """

    @abstractmethod
    async def reply(self, text: str) -> str | None: ...

    async def stream(self, text: str) -> AsyncIterator[str]:
        """Yield the reply in chunks for live HUD streaming.

        Default adapts reply(): one chunk. Engines with true token streaming
        override — same wearer-visible contract either way (the streaming
        HUD requirement in docs/SESSION_MODEL.md)."""
        result = await self.reply(text)
        if result:
            yield result
