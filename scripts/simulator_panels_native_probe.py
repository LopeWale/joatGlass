#!/usr/bin/env python3
"""Probe the News, Pomodoro and Search tabs in the official simulator and count tiles.

Plays the Mac's side of the overview's right panel with the real gateway:
pushes a `hud.panel` frame per tab (headlines, a running timer that crosses
a minute boundary during the run, results), expands each tab from the rail
by gesture, answers a headline tap with the article page the Mac would
send, and records every [hud-timing] console entry per step. A step with no
entry painted no image tile.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_panels_native_probe.py /tmp/probe
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import HudList, HudPanel, HudPanelPomodoro, InputEvent

log = logging.getLogger('panels-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-panels-probe'
OUT.mkdir(parents=True, exist_ok=True)

ARTICLE = [
    '1/2 · Fixture Wire',
    'The simulator build of the plugin now draws every reader on a firmware',
    'text page. The article body is the Mac\'s own paged rows; the glasses page',
    'the rest of this page in place and ask the Mac only at its edge.',
]


def console() -> list[dict]:
    with urllib.request.urlopen(f'{BASE}/api/console', timeout=5) as r:
        return json.load(r)['entries']


def timing_since(entries: list[dict], since_id: int) -> list[dict]:
    out = []
    for e in entries:
        if e['id'] <= since_id:
            continue
        m = e['message']
        if m.startswith('[hud-timing] '):
            out.append(json.loads(m[len('[hud-timing] '):]))
        elif 'rebuildPageContainer' in m or 'textContainerUpgrade' in m or '[joat]' in m:
            out.append({'log': m[:160]})
    return out


def last_id(entries: list[dict]) -> int:
    return max((e['id'] for e in entries), default=0)


async def snap(name: str, settle: float = 0.6) -> None:
    await asyncio.sleep(settle)
    with urllib.request.urlopen(f'{BASE}/api/screenshot/glasses', timeout=10) as r:
        (OUT / f'{name}.png').write_bytes(r.read())


async def press(action: str, settle: float = 0.3) -> None:
    req = urllib.request.Request(f'{BASE}/api/input', data=json.dumps({'action': action}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5):
        pass
    await asyncio.sleep(settle)


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {}
    taps: list[InputEvent] = []
    gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
    gateway.on_hello(lambda _h: connected.set())

    def on_input(event: InputEvent) -> None:
        taps.append(event)
        if event.container_name == 'capdata' and event.selected_name == 'NEWS':
            # The Mac answers a headline tap with the article page.
            asyncio.get_running_loop().create_task(gateway.send(HudList(
                items=ARTICLE, title='NEWS · ARTICLE', hint='down more · left x2: back',
                request_id=event.request_id)))

    gateway.on_input(on_input)
    await gateway.start()
    try:
        log.info('waiting for plugin hello on 18791')
        await asyncio.wait_for(connected.wait(), timeout=120)
        await asyncio.sleep(2.0)
        now_ms = int(time.time() * 1000)
        await gateway.send(HudPanel(tab='news', revision=7, rows=[
            'Glasses firmware list adopted by JOAT panels',
            'Simulator sweep finds zero tiles on readers',
            'Minute-granular Pomodoro row replaces per-second tile',
        ]))
        await gateway.send(HudPanel(tab='search', revision=3, rows=[
            'Even Hub SDK 0.0.14 · list containers',
            'LVGL font metrics · measured rows',
        ]))
        # Running focus block with 65 s left: the minute boundary passes
        # while the page is open, so the rebuilt row is observable.
        await gateway.send(HudPanel(tab='pomodoro', revision=5, rows=['Pause', 'Skip to break', 'Reset'],
                                    pomodoro=HudPanelPomodoro(phase='focus', running=True, remaining_ms=65_000,
                                                              length_ms=1_500_000, completed=1,
                                                              ends_at_unix_ms=now_ms + 65_000)))
        await asyncio.sleep(1.0)
        ids = last_id(console())
        await snap('0-overview', 0.5)

        # The rail: down moves to NEWS, a click expands it.
        await press('down', 0.6)
        await press('click', 0.5)
        await snap('1-news', 1.0)
        report['news_timing'] = timing_since(console(), ids); ids = last_id(console())

        t_read = time.monotonic()
        await press('click', 0.5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
                t.container_name == 'capdata' and t.selected_name == 'NEWS' for t in taps):
            await asyncio.sleep(0.05)
        await snap('2-article', 1.0)
        report['article_after_tap_s'] = round(time.monotonic() - t_read, 2)
        report['article_timing'] = timing_since(console(), ids); ids = last_id(console())
        await press('down', 0.8)
        await snap('3-article-down', 0.6)
        report['article_down_timing'] = timing_since(console(), ids); ids = last_id(console())
        await press('double_click', 0.8)
        await snap('4-news-again', 1.0)
        report['article_back_timing'] = timing_since(console(), ids); ids = last_id(console())
        await press('double_click', 1.0)
        report['news_home_timing'] = timing_since(console(), ids); ids = last_id(console())

        # POMODORO: expand, then wait past the minute boundary.
        await press('down', 0.6)
        await press('click', 0.6)
        await snap('5-pomodoro', 1.0)
        report['pomodoro_timing'] = timing_since(console(), ids); ids = last_id(console())
        await asyncio.sleep(7.0)
        await snap('6-pomodoro-minute-later', 0.5)
        report['pomodoro_tick_timing'] = timing_since(console(), ids); ids = last_id(console())
        await press('double_click', 1.0)
        report['pomodoro_home_timing'] = timing_since(console(), ids); ids = last_id(console())

        # SEARCH.
        await press('down', 0.6)
        await press('click', 0.6)
        await snap('7-search', 1.0)
        report['search_timing'] = timing_since(console(), ids); ids = last_id(console())
        await press('double_click', 1.0)
        await snap('8-after-back', 0.8)
        report['taps'] = [(t.container_name, t.selected_name, t.selected_index) for t in taps]
    finally:
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
