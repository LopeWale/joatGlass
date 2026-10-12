#!/usr/bin/env python3
"""Replay the 2026-10-12 freeze in the official simulator and watch it recover.

That morning a second copy of the plugin connected and replaced the first
(4409); 8 minutes later the second copy died (1005) and the first never
dialed again for 3 h 45 min — only a host foreground event could revive it.

Here the simulator runs copy A. Copy B is a bare WebSocket client that
authenticates, takes the glasses, holds them, then dies. A must stay yielded
while B lives (its standby dials refused), come back by itself within one
standby interval after B dies, open its mic again, and take taps.

    PYTHONPATH=apps/mac:. .venv/bin/python -u scripts/simulator_display_takeover_probe.py --auto http://127.0.0.1:9901
    evenhub-simulator --no-aid 'http://127.0.0.1:5193/' --automation-port 9901
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import time
import urllib.request

import websockets

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import AudioControl, HudPanel
from joat.services.news import NewsService

log = logging.getLogger("joat.simulator_display_takeover_probe")


def _input(base: str, action: str) -> None:
    request = urllib.request.Request(
        f"{base}/api/input", method="POST", data=json.dumps({"action": action}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=5).read()


def _shot(base: str, out: Path, name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.png").write_bytes(urllib.request.urlopen(f"{base}/api/screenshot/glasses", timeout=5).read())
    log.info("frame %s", name)


async def run(port: int, auto: str, out: Path, hold_s: float) -> None:
    news = NewsService()
    await news.refresh_once()
    snapshot = news.snapshot()
    hellos: list[float] = []
    audio_frames: list[float] = []
    gateway = HubGatewayTransport(host="127.0.0.1", port=port)

    def on_hello(_hello: object) -> None:
        hellos.append(time.monotonic())
        log.info("stage=hello #%d", len(hellos))

        async def greet() -> None:
            await asyncio.sleep(0.5)
            await gateway.send(HudPanel(tab="news", revision=len(hellos), rows=snapshot.rows,
                                        headline=snapshot.headline, empty=snapshot.empty))
            await gateway.send(AudioControl(open=True, source="glasses"))
        asyncio.get_running_loop().create_task(greet())

    gateway.on_hello(on_hello)
    gateway.on_audio(lambda *_a, **_k: audio_frames.append(time.monotonic()))
    await gateway.start()
    try:
        log.info("waiting for the simulator (copy A)")
        while not hellos:
            await asyncio.sleep(0.2)
        await asyncio.sleep(3)
        _shot(auto, out, "T0-copyA-live")
        frames_before = len(audio_frames)
        log.info("stage=copyA-audio frames=%d", frames_before)

        # Copy B takes the glasses, as the second WebView did at 07:49:36.
        b = await websockets.connect(f"ws://127.0.0.1:{port}/glasses")
        await b.send(json.dumps({"kind": "auth", "token": None}))
        assert json.loads(await b.recv())["kind"] == "auth.accepted"
        await b.send(json.dumps({"kind": "hello", "sn": "copy-B", "model": "g2", "launchSource": "unknown"}))
        log.info("stage=copyB-took-the-glasses")
        await asyncio.sleep(2)
        _shot(auto, out, "T1-copyA-yielded")
        hellos_while_b = len(hellos)
        await asyncio.sleep(hold_s)
        log.info("stage=copyB-held %.0fs; copy A hellos meanwhile=%d (must be 0)", hold_s, len(hellos) - hellos_while_b)

        # Copy B dies without a close frame, as at 07:57:43 (1005).
        b.transport.abort()
        died = time.monotonic()
        log.info("stage=copyB-died")
        while len(hellos) == hellos_while_b and time.monotonic() - died < 30:
            await asyncio.sleep(0.2)
        if len(hellos) == hellos_while_b:
            log.error("stage=FAILED copy A did not come back within 30 s")
            return
        log.info("stage=copyA-back after %.1fs", hellos[-1] - died)
        await asyncio.sleep(3)
        _shot(auto, out, "T2-copyA-back")
        back_frames = [t for t in audio_frames if t > hellos[-1]]
        log.info("stage=copyA-mic frames since takeover=%d", len(back_frames))

        # Taps work again: swipe to NEWS on the rail and open it.
        await asyncio.to_thread(_input, auto, "down")
        await asyncio.sleep(1.2)
        await asyncio.to_thread(_input, auto, "click")
        await asyncio.sleep(2.5)
        _shot(auto, out, "T3-copyA-tap-opens-news")
        log.info("stage=done")
    finally:
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--auto", required=True, metavar="AUTOMATION_BASE")
    parser.add_argument("--out", type=Path, default=Path("/tmp/takeover-frames"))
    parser.add_argument("--hold", type=float, default=25.0, help="seconds copy B holds the glasses")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port, args.auto, args.out, args.hold))


if __name__ == "__main__":
    main()
