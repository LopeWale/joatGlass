"""MicMeter: content-free mic timing as the Mac receives it."""

from __future__ import annotations

import logging

import pytest

from joat.ble.mic_meter import FIRST_REPORT_AFTER_S, REPORT_EVERY_S, MicMeter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "joat.ble.mic_meter"]


def test_open_timing_names_ack_first_pcm_and_link(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="joat.ble.mic_meter")
    clock = Clock()
    meter = MicMeter(clock=clock, link_rtt_s=lambda: 0.042)
    meter.on_control(True)
    clock.t += 0.180
    meter.on_device(True)
    clock.t += 0.120
    meter.on_audio(3200)
    assert lines(caplog) == ["mic open: plugin ack +180 ms, first pcm +300 ms (link rtt 42 ms)"]


def test_continuous_stream_reports_full_pcm_and_no_stale_gaps(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="joat.ble.mic_meter")
    clock = Clock()
    meter = MicMeter(clock=clock)
    # 200 ms batches of 3200 samples, continuously, through the first report.
    for _ in range(int(FIRST_REPORT_AFTER_S / 0.2) + 1):
        meter.on_audio(3200)
        clock.t += 0.2
    [line] = lines(caplog)
    assert "longest gap 200 ms" in line and "gaps over 1.5 s: 0" in line
    assert "(100%)" in line


def test_a_silent_host_shows_as_gaps_over_the_plugin_threshold(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="joat.ble.mic_meter")
    clock = Clock()
    meter = MicMeter(clock=clock)
    meter.on_audio(3200)
    clock.t += 0.2
    meter.on_audio(3200)
    clock.t += 4.0  # the host sent nothing for 4 s
    meter.on_audio(3200)
    meter.on_disconnect()
    [line] = lines(caplog)
    assert "longest gap 4000 ms" in line and "gaps over 1.5 s: 1" in line
    assert line.endswith("(disconnected)")


def test_reports_again_every_five_minutes_with_gaps_continuous(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="joat.ble.mic_meter")
    clock = Clock()
    meter = MicMeter(clock=clock)
    total = FIRST_REPORT_AFTER_S + REPORT_EVERY_S + 1
    steps = int(total / 0.2)
    for _ in range(steps):
        meter.on_audio(3200)
        clock.t += 0.2
    assert len(lines(caplog)) == 2
    assert all("gaps over 1.5 s: 0" in line for line in lines(caplog))


def test_nothing_is_logged_without_audio(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="joat.ble.mic_meter")
    meter = MicMeter(clock=Clock())
    meter.on_control(True)
    meter.on_device(True)
    meter.on_control(False)
    meter.on_disconnect()
    assert lines(caplog) == []
