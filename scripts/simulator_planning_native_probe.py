#!/usr/bin/env python3
"""Probe the Planning week and day pages in the official simulator and count tiles.

Plays the Mac's side with the real gateway and router over a fake personal
data bridge (one event and one reminder on a fixed day, the shape the unit
tests use). Opens Planning by voice (the month dashboard stays raster by
design), taps a week row, then a day row, then an entry, then walks Back,
and records every [hud-timing] console entry per step.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_planning_native_probe.py /tmp/probe
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import InputEvent, VOICE_REQUEST_PREFIX as VOICE_PREFIX
from joat.services.capabilities import CapabilityRouter, PlanningScope
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher

log = logging.getLogger('planning-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-planning-probe'
OUT.mkdir(parents=True, exist_ok=True)


def bridge_response(*, result=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar='full_access', reminders='full_access'),
        result=result, store_instance_id='eventkit-store', store_generation=1)


class FakePersonalData:
    """The native bridge, answered from a dict the way the unit tests do."""

    def __init__(self, responses):  # type: ignore[no-untyped-def]
        self._responses = responses

    async def call(self, method: str, body=None):  # type: ignore[no-untyped-def]
        return self._responses[method]


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


async def _wait_until(predicate, timeout: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError('condition did not become true')
        await asyncio.sleep(0.05)


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {}
    starts_at = datetime(2026, 9, 1, 9, 30).astimezone()
    personal = FakePersonalData({
        'authorization.status': bridge_response(),
        'calendar.listContainers': bridge_response(result=[{'calendar_identifier': 'cal-work', 'title': 'Work'}]),
        'calendar.query': bridge_response(result=[{
            'title': 'Design review', 'calendar_item_identifier': 'event-1',
            'start_unix_ms': int(starts_at.timestamp() * 1_000),
            'end_unix_ms': int(starts_at.timestamp() * 1_000) + 3_600_000}]),
        'reminders.listContainers': bridge_response(result=[{'calendar_identifier': 'list-home', 'title': 'Home'}]),
        'reminders.query': bridge_response(result=[{
            'title': 'Buy filters', 'calendar_item_identifier': 'reminder-1',
            'due': {'year': 2026, 'month': 9, 'day': 1, 'hour': 18, 'minute': 0}, 'is_completed': False}]),
    })
    with tempfile.TemporaryDirectory(prefix='joat-planning-') as root:
        rootp = Path(root)
        gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
        gateway.on_hello(lambda _h: connected.set())
        hud = HudWriter(gateway)
        router = CapabilityRouter(hud=hud, transport=gateway,
                                  status=StatusPublisher(rootp / 'status.json'),
                                  messages=None, cards=CardLog(), personal_data=personal)  # type: ignore[arg-type]
        router._planning_selected_day = date(2026, 9, 1)
        gateway.on_input(router.on_input)
        await gateway.start()
        try:
            log.info('waiting for plugin hello on 18791')
            await asyncio.wait_for(connected.wait(), timeout=120)
            await asyncio.sleep(2.0)
            ids = last_id(console())

            await router.open('Planning', request_id=VOICE_PREFIX + 'drive-planning')
            await _wait_until(lambda: router._planning_scope is PlanningScope.MONTH and bool(router._planning_rows), 30)
            await snap('1-month', 1.5)
            report['month_timing'] = timing_since(console(), ids); ids = last_id(console())

            # The month dashboard stays raster by design; its week row 0 is
            # asked for the way the dashboard's own tap asks: by index.
            t_week = time.monotonic()
            router.on_input(InputEvent(container_name='capdata', selected_name='PLANNING', selected_index=0,
                                       request_id=router._active_request_id))
            await _wait_until(lambda: router._planning_scope is PlanningScope.WEEK, 30)
            report['week_after_tap_s'] = round(time.monotonic() - t_week, 2)
            await snap('2-week', 1.2)
            report['week_timing'] = timing_since(console(), ids); ids = last_id(console())

            # TUE SEP 1 is row 1 of the week page: scroll the firmware
            # highlight there and tap.
            await press('down', 0.3)
            t_day = time.monotonic()
            await press('click', 0.3)
            await _wait_until(lambda: router._planning_scope is PlanningScope.DAY, 30)
            report['day_after_tap_s'] = round(time.monotonic() - t_day, 2)
            await snap('3-day', 1.2)
            report['day_timing'] = timing_since(console(), ids); ids = last_id(console())

            # The event is row 0 of the day page.
            await press('click', 0.3)
            await _wait_until(lambda: not router._planning_rows, 30)
            await snap('4-event', 1.2)
            report['event_timing'] = timing_since(console(), ids); ids = last_id(console())

            await press('double_click', 0.8)
            await _wait_until(lambda: router._planning_scope is PlanningScope.DAY and bool(router._planning_rows), 30)
            await snap('5-day-again', 1.2)
            report['event_back_timing'] = timing_since(console(), ids); ids = last_id(console())
            await press('double_click', 0.8)
            await _wait_until(lambda: router._planning_scope is PlanningScope.WEEK, 30)
            await snap('6-week-again', 1.2)
            report['day_back_timing'] = timing_since(console(), ids); ids = last_id(console())
            await press('double_click', 1.0)
            await snap('7-after-back', 1.5)
            report['week_back_timing'] = timing_since(console(), ids)
        finally:
            (OUT / 'report.json').write_text(json.dumps(report, indent=2))
            await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
