"""The Mac's half of the IMU calibration path: arm on hello, append samples."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from joat.core.protocol import Hello, Imu, ImuControl, ImuTrace
from joat.services.imu_trace import ImuTraceRecorder


class FakeTransport:
    def __init__(self) -> None:
        self.hello_cbs: list[Callable[[Hello], None]] = []
        self.imu_cbs: list[Callable[[Imu], None]] = []
        self.sent: list[object] = []

    def on_hello(self, callback: Callable[[Hello], None]) -> None:
        self.hello_cbs.append(callback)

    def on_imu(self, callback: Callable[[Imu], None]) -> None:
        self.imu_cbs.append(callback)

    async def send(self, message: object) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_arms_the_plugin_on_hello_and_appends_samples(tmp_path: Path) -> None:
    transport = FakeTransport()
    recorder = ImuTraceRecorder(tmp_path / "traces" / "imu.jsonl", pace_ms=200)
    recorder.attach(transport)

    for callback in transport.hello_cbs:
        callback(Hello(sn="sim", model="g2", launch_source="test"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert transport.sent == [ImuControl(open=True, pace_ms=200), ImuTrace(enabled=True)]

    for callback in transport.imu_cbs:
        callback(Imu(x=0.1, y=-0.2, z=9.8, at=1234))
        callback(Imu(x=0.0, y=0.0, z=9.7, at=1334))
    lines = (tmp_path / "traces" / "imu.jsonl").read_text().splitlines()
    assert [json.loads(line)["at"] for line in lines] == [1234, 1334]
    assert json.loads(lines[0])["z"] == 9.8
    assert recorder.samples == 2


def test_refuses_a_pace_the_sdk_does_not_offer(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ImuTraceRecorder(tmp_path / "imu.jsonl", pace_ms=150)
