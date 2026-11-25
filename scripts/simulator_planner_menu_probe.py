#!/usr/bin/env python3
"""Probe the overview's PLANNER tab and the Notifications menu row in the simulator.

Plays the Mac's side of the overview with the real gateway: pushes the
`hud.panel` frame the PlannerPanel would send (open reminders, labelled by
due day), plus two unread notification cards, and records what the glasses
paint and which requests they send back.

Two modes, one URL each (the simulator cannot deliver a firmware menu click,
so the second mode opens the very method EventRouter calls for the overview's
first menu row, through the DEV-only `?joatTestOpenMenu=Notifications` hook):

    overview   the resting overview. PLANNER is the first rail tab and lists
               reminders; Notifications is NOT a rail tab; a swipe moves the
               rail; a tap on PLANNER asks the Mac to open Planning.
    menu       Notifications opened from the menu: the newest unread card
               paints over the page, a tap shows the next card, a second
               tap past the last returns.

    # NODE_ENV=development matters: a shell exporting NODE_ENV=production makes
    # vite serve PROD, which strips the ?joatTestOpenMenu= hook (silently).
    NODE_ENV=development VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses npm --prefix apps/hub-plugin run dev
    PYTHONPATH=apps/mac python3 scripts/simulator_planner_menu_probe.py /tmp/probe overview &
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    # then, for the menu mode, restart both with the hook in the URL:
    PYTHONPATH=apps/mac python3 scripts/simulator_planner_menu_probe.py /tmp/probe menu &
    evenhub-simulator 'http://127.0.0.1:5190/?joatTestOpenMenu=Notifications' --automation-port 9898
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import (
    HudAmbient, HudNotificationCard, HudNotifications, HudPanel, InputEvent,
)

log = logging.getLogger('planner-menu-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-planner-menu-probe'
MODE = sys.argv[2] if len(sys.argv) > 2 else 'overview'
OUT.mkdir(parents=True, exist_ok=True)

# What PlannerPanel.planner_rows produces for a mixed week.
PLANNER_ROWS = (
    '[Late] Call the bank',
    '[09:00] Standup prep',
    '[Today] Pay rent',
    '[Tmrw] 09:00 Send invoice',
    '[Sat] Gym bag',
)


def console() -> list[dict]:
    with urllib.request.urlopen(f'{BASE}/api/console', timeout=5) as r:
        return json.load(r)['entries']


def last_id(entries: list[dict]) -> int:
    return max((e['id'] for e in entries), default=0)


def logs_since(entries: list[dict], since_id: int) -> list[str]:
    out = []
    for e in entries:
        if e['id'] <= since_id:
            continue
        m = e['message']
        if m.startswith('[hud-timing] ') or '[joat]' in m or '[uncaught]' in m or '[unhandledrejection]' in m:
            out.append(m[:200])
    return out


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


def cards() -> HudNotifications:
    now_ms = int(time.time() * 1000)
    return HudNotifications(cards=(
        HudNotificationCard(card_id='a' * 32, app='Messages', sender='Ada',
                            body='Lunch at one? I booked the corner table by the window.',
                            at_unix_ms=now_ms - 60_000),
        HudNotificationCard(card_id='b' * 32, app='Mail', sender='Quinn',
                            body='The invoice is attached. Please confirm receipt today.',
                            at_unix_ms=now_ms - 600_000, urgent=True),
    ))


async def push_overview(gateway: HubGatewayTransport) -> None:
    await gateway.send(HudAmbient(temperature='24°', condition='Clear', unread=2))
    await gateway.send(cards())
    await gateway.send(HudPanel(tab='planner', revision=11, rows=PLANNER_ROWS))
    await gateway.send(HudPanel(tab='news', revision=7, rows=(
        'Glasses firmware list adopted by JOAT panels',
        'Simulator sweep finds zero tiles on readers',
    )))


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {'mode': MODE}
    taps: list[InputEvent] = []
    gateway = HubGatewayTransport(host='127.0.0.1', port=18791)

    def on_hello(_h: object) -> None:
        connected.set()
        if MODE == 'menu':
            # The DEV hook opens the menu row ~750 ms after the page settles;
            # the cards must already be in the plugin's cache by then.
            asyncio.get_running_loop().create_task(push_overview(gateway))

    gateway.on_hello(on_hello)
    gateway.on_input(lambda event: taps.append(event))
    await gateway.start()
    try:
        log.info('waiting for plugin hello on 18791 (mode=%s)', MODE)
        await asyncio.wait_for(connected.wait(), timeout=180)
        if MODE == 'overview':
            await asyncio.sleep(2.0)
            await push_overview(gateway)
            await asyncio.sleep(1.2)
            ids = last_id(console())
            await snap('1-overview-planner', 0.8)

            # The rail: down moves to the next tab (NEWS), up returns.
            await press('down', 0.8)
            await snap('2-overview-news', 0.8)
            report['after_down'] = logs_since(console(), ids); ids = last_id(console())
            await press('up', 0.8)
            await snap('3-overview-planner-again', 0.8)

            # A tap on the lit PLANNER tab opens Planning on the Mac.
            before = len(taps)
            await press('click', 1.5)
            await snap('4-after-planner-tap', 0.8)
            report['planner_tap_requests'] = [
                (t.container_name, t.selected_name, t.selected_index, t.request_id) for t in taps[before:]]
            report['planner_tap_logs'] = logs_since(console(), ids)
            # Planner rows refresh when the Mac pushes a newer revision.
            await gateway.send(HudPanel(tab='planner', revision=12, rows=PLANNER_ROWS[:2] + ('+3 more',)))
            await asyncio.sleep(0.2)
            await press('double_click', 1.0)
            await snap('5-after-back', 0.8)
        else:
            # Frames from the moment the plugin said hello: the hook opens the
            # row ~750 ms after the page settles, so the run shows what came
            # up, how long it stayed, and what (if anything) closed it.
            for i in range(14):
                await snap(f'0-frame-{i:02d}', 0.35)
            ids = last_id(console())
            await snap('1-menu-notification-newest', 0.2)
            report['opened_logs'] = logs_since(console(), 0)
            # A tap reads this card and shows the next.
            await press('click', 1.0)
            await snap('2-menu-notification-next', 0.8)
            report['after_tap_logs'] = logs_since(console(), ids); ids = last_id(console())
            await press('click', 1.0)
            await snap('3-menu-notification-after-last', 0.8)
            report['after_second_tap_logs'] = logs_since(console(), ids)
        report['taps'] = [(t.container_name, t.selected_name, t.selected_index, t.request_id) for t in taps]
    finally:
        (OUT / f'report-{MODE}.json').write_text(json.dumps(report, indent=2))
        await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
