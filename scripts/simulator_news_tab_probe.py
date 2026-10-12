#!/usr/bin/env python3
"""Push the News tab the way the brain does and watch the simulator open it.

A loopback brain: on hello it publishes the real News snapshot (the wearer's
news.json feeds, fetched now) and the Pomodoro tab, exactly as
``PanelPublisher.publish_all`` does for a freshly connected plugin. Then the
simulator's gestures: swipe to News on the overview rail, tap. Screenshots
along the way say whether the page painted.

    PYTHONPATH=apps/mac:. .venv/bin/python -u scripts/simulator_news_tab_probe.py --port 18791 --auto http://127.0.0.1:9901
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import time
import urllib.request

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import HudPanel
from joat.services.news import NewsService

log = logging.getLogger("joat.simulator_news_tab_probe")


def _input(base: str, action: str) -> None:
    request = urllib.request.Request(
        f"{base}/api/input", method="POST", data=json.dumps({"action": action}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=5).read()


def _shot(base: str, out: Path, name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(f"{base}/api/screenshot/glasses", timeout=5).read()
    (out / f"{name}.png").write_bytes(data)
    log.info("frame %s (%d bytes)", name, len(data))


async def run(port: int, auto: str | None, out: Path, swipes: int) -> None:
    connected = asyncio.Event()
    news = NewsService()
    log.info("fetching the wearer's feeds (%s)…", ", ".join(news.config.categories))
    await news.refresh_once()
    snapshot = news.snapshot()
    log.info("news rows=%d empty=%r last_error=%r partial=%r", len(snapshot.rows), snapshot.empty, news.last_error, news.partial)
    for i, row in enumerate(snapshot.rows):
        log.info("  row %2d (%3d chars): %s", i, len(row), row)

    gateway = HubGatewayTransport(host="127.0.0.1", port=port)
    gateway.on_hello(lambda _hello: connected.set())
    await gateway.start()
    try:
        log.info("waiting for the simulator on port %d", port)
        await asyncio.wait_for(connected.wait(), timeout=120)
        await asyncio.sleep(1.5)
        await gateway.send(HudPanel(tab="news", revision=1, rows=snapshot.rows, headline=snapshot.headline, empty=snapshot.empty, pomodoro=None, activate=False))
        log.info("stage=news-published rows=%d", len(snapshot.rows))
        if auto is not None:
            await asyncio.sleep(2)
            _shot(auto, out, "N0-overview")
            for n in range(swipes):
                await asyncio.to_thread(_input, auto, "down")
                await asyncio.sleep(1.2)
                _shot(auto, out, f"N1-rail-{n + 1}")
            await asyncio.to_thread(_input, auto, "click")
            await asyncio.sleep(0.8)
            _shot(auto, out, "N2-after-tap-0.8s")
            await asyncio.sleep(2.5)
            _shot(auto, out, "N3-after-tap-3.3s")
            log.info("stage=done")
        while True:
            await asyncio.sleep(3600)
    finally:
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--auto", metavar="AUTOMATION_BASE")
    parser.add_argument("--out", type=Path, default=Path("/tmp/news-frames"))
    parser.add_argument("--swipes", type=int, default=1, help="rail swipes from Notifications to reach News")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port, args.auto, args.out, args.swipes))


if __name__ == "__main__":
    main()
