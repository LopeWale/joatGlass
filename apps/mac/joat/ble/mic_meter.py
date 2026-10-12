"""Content-free timing of the glasses' microphone, as the Mac receives it.

Even Hub's contract (SDK device features): ``audioControl(true, Glasses)``
opens the G2 four-mic array and PCM arrives as ``audioEvent`` buffers,
16 kHz signed 16-bit mono; the plugin batches them into ~200 ms ``audio``
messages. What the contract does not say, and the experience depends on:

- how long ``audioControl(true)`` takes on the glasses — measured from the
  Mac's ``audio.control`` to the plugin's ``device mic=true`` (sent once the
  host call resolved) and to the first PCM, with the link round trip beside
  it so the network can be subtracted;
- whether PCM keeps flowing while nobody speaks. The plugin treats 1.5 s
  without PCM as a stream the host stopped (``STREAM_STALE_MS``) and
  re-arms it; that is only right if the G2 streams continuously.

Nothing here touches audio content: counts, sample totals and times only.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
#: The plugin's STREAM_STALE_MS twin: a gap longer than this would re-arm.
PLUGIN_STALE_GAP_S = 1.5
FIRST_REPORT_AFTER_S = 60.0
REPORT_EVERY_S = 300.0


class MicMeter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        link_rtt_s: Callable[[], float | None] = lambda: None,
    ) -> None:
        self._clock = clock
        self._link_rtt_s = link_rtt_s
        self._open_requested_at: float | None = None
        self._ack_ms: int | None = None
        self._window_start: float | None = None
        self._report_at: float | None = None
        self._last_audio_at: float | None = None
        self._messages = 0
        self._samples = 0
        self._max_gap_s = 0.0
        self._stale_gaps = 0

    def on_control(self, open_: bool) -> None:
        """The Mac sent ``audio.control``."""
        if open_:
            self._open_requested_at = self._clock()
            self._ack_ms = None
        else:
            self._open_requested_at = None
            self._flush_window(reason="mic closed")

    def on_device(self, mic: bool | None) -> None:
        """The plugin reported its mic state (sent after the host call)."""
        if mic is True and self._open_requested_at is not None and self._ack_ms is None:
            self._ack_ms = round((self._clock() - self._open_requested_at) * 1000)

    def on_audio(self, samples: int) -> None:
        now = self._clock()
        if self._open_requested_at is not None:
            first_ms = round((now - self._open_requested_at) * 1000)
            log.info(
                "mic open: plugin ack %s, first pcm +%d ms (link rtt %s)",
                f"+{self._ack_ms} ms" if self._ack_ms is not None else "not seen",
                first_ms, self._rtt_text(),
            )
            self._open_requested_at = None
            self._start_window(now, FIRST_REPORT_AFTER_S)
        elif self._window_start is None:
            self._start_window(now, FIRST_REPORT_AFTER_S)
        if self._last_audio_at is not None:
            gap = now - self._last_audio_at
            self._max_gap_s = max(self._max_gap_s, gap)
            if gap > PLUGIN_STALE_GAP_S:
                self._stale_gaps += 1
        self._last_audio_at = now
        self._messages += 1
        self._samples += max(0, samples)
        if self._report_at is not None and now >= self._report_at:
            self._flush_window(reason=None)
            self._start_window(now, REPORT_EVERY_S)
            self._last_audio_at = now  # gaps stay continuous across windows

    def on_disconnect(self) -> None:
        self._open_requested_at = None
        self._flush_window(reason="disconnected")

    def _start_window(self, now: float, report_after_s: float) -> None:
        self._window_start = now
        self._report_at = now + report_after_s
        self._messages = 0
        self._samples = 0
        self._max_gap_s = 0.0
        self._stale_gaps = 0
        self._last_audio_at = None

    def _flush_window(self, *, reason: str | None) -> None:
        start = self._window_start
        if start is None or self._messages == 0:
            self._window_start = None
            self._report_at = None
            self._last_audio_at = None
            return
        wall_s = max(1e-6, (self._last_audio_at or self._clock()) - start)
        pcm_s = self._samples / SAMPLE_RATE
        log.info(
            "mic cadence: %d messages, %.1f s of pcm in %.1f s (%.0f%%), "
            "longest gap %d ms, gaps over %.1f s: %d, link rtt %s%s",
            self._messages, pcm_s, wall_s, 100.0 * pcm_s / wall_s,
            round(self._max_gap_s * 1000), PLUGIN_STALE_GAP_S, self._stale_gaps,
            self._rtt_text(), f" ({reason})" if reason else "",
        )
        self._window_start = None
        self._report_at = None
        self._last_audio_at = None

    def _rtt_text(self) -> str:
        try:
            rtt = self._link_rtt_s()
        except Exception:
            rtt = None
        return f"{round(rtt * 1000)} ms" if isinstance(rtt, (int, float)) and rtt > 0 else "unknown"
