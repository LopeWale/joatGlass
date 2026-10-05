#!/usr/bin/env python3
"""Probe the modals a tap lands on, in the official simulator, and count tiles.

The wearer's complaint: fixing a page is not enough when the page a tap
redirects to is still four image tiles. This plays the Mac's side with the
real gateway and drives, from a firmware list, every surface that used to
come back as pixels: a receipt and an outcome strip over the list, a
productivity page, the Messages result and review pages, the QuickNotes
recorder, the Brainstorm saved-session reader, the Retry page, and the
navigation frames before the live map. It records every [hud-timing]
console entry per step: a step with no entry painted no image tile.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_transitions_native_probe.py /tmp/probe
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import (
    AssistantOverlay, HudList, NavigationHudView, NavigationView, ProductivityHudView,
    VOICE_REQUEST_PREFIX as VOICE_PREFIX,
)

log = logging.getLogger('transitions-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-transitions-probe'
OUT.mkdir(parents=True, exist_ok=True)


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


def tiles(timing: list[dict]) -> int:
    return sum(1 for t in timing if 'log' not in t)


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {}
    gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
    gateway.on_hello(lambda _h: connected.set())
    await gateway.start()
    steps: list[tuple[str, int]] = []

    async def step(name: str, send, settle: float = 1.0) -> None:  # type: ignore[no-untyped-def]
        ids = last_id(console())
        await send()
        await snap(name, settle)
        timing = timing_since(console(), ids)
        report[name] = timing
        steps.append((name, tiles(timing)))

    try:
        log.info('waiting for plugin hello on 18791')
        await asyncio.wait_for(connected.wait(), timeout=120)
        await asyncio.sleep(2.0)
        # A voice-prefixed request is adopted by the plugin without a menu tap.
        rid = VOICE_PREFIX + 'probe-1'

        # 1. A firmware list, then a receipt and an outcome strip over it.
        await step('1-activity-list', lambda: gateway.send(HudList(
            items=['[2m] Replied to Dele', '[1h] Saved a note'], title='ACTIVITY',
            hint='tap: read · left x2: back', request_id=rid, selectable=True)))
        await step('2-receipt-over-list', lambda: gateway.send(AssistantOverlay(
            text='Noted.', presentation='receipt', operation='replace')))
        await step('3-receipt-settles', lambda: gateway.send(AssistantOverlay(
            operation='settle', dismiss_after_ms=1000)), settle=2.0)
        await step('4-saved-strip-over-list', lambda: gateway.send(ProductivityHudView(
            domain='reminders', phase='saved', title='Buy filters', primary='Saved',
            secondary='Reminders read-back matched', hint='left x2: Planning')), settle=1.0)
        await step('5-strip-settles', lambda: asyncio.sleep(0), settle=6.0)

        # 2. A productivity page (due) over the list, then Back.
        await step('6-due-page', lambda: gateway.send(ProductivityHudView(
            domain='reminders', phase='due', title='Buy filters', primary='Due now',
            secondary='This reminder is still incomplete.', hint='left x2: Planning')))
        await step('7-back-from-due', lambda: press('double_click', 0.8), settle=1.5)

        # 3. The Messages pages that used to be pixels.
        await step('8-messages-compose', lambda: gateway.send(HudList(
            items=['Say the recipient name first.', 'Then say the message.'], title='MESSAGES · COMPOSE',
            hint='left x2: back', request_id=rid)))
        await step('9-messages-review', lambda: gateway.send(HudList(
            items=['1/1 · To Dele', 'On my way, see you at the gate in ten minutes.'],
            title='MESSAGES · REVIEW', hint='tap: confirm · left x2: back', request_id=rid,
            selectable=True, reader_page=('review-1', 0, 1))))
        await step('10-messages-retry', lambda: gateway.send(HudList(
            items=['Mac did not answer. Tap to retry Messages.'], title='MESSAGES · RETRY',
            hint='tap: retry · left x2: back', request_id=rid, selectable=True)))

        # 4. The QuickNotes recorder and the Brainstorm saved reader.
        await step('11-quicknotes-recording', lambda: gateway.send(HudList(
            items=['Save and finish', 'Pause recording', 'Discard recording'],
            title='QUICKNOTES · RECORDING', hint='Standup · tap: choose', request_id=rid, selectable=True)))
        await step('12-brainstorm-saved', lambda: gateway.send(HudList(
            items=['1/2 · Pricing', '[Codex]', 'Start with the smallest plan.', '[Claude]', 'Agreed.'],
            title='BRAINSTORM · SAVED', hint='down more · tap: continue · left x2: back', request_id=rid,
            selectable=True, reader_page=('saved-1', 0, 2))))

        # 5. Navigation frames before the live map, then the map.
        # The plugin rejects a frame whose generation does not advance.
        generation = [0]

        def nav() -> dict:
            generation[0] += 1
            return dict(session_id='nav-probe', request_id='nav-req-1', generation=generation[0])
        await step('13-nav-destination', lambda: gateway.send(NavigationView(**nav(), navigation=NavigationHudView(
            view='destination', primary='Central Library', secondary='1.2 km away',
            hint='tap/say confirm · swipe retry', interaction='stationary'))))
        await step('14-nav-mode', lambda: gateway.send(NavigationView(**nav(), navigation=NavigationHudView(
            view='mode', primary='How are you travelling?', secondary='Central Library · 1.2 km',
            hint='tap choose · swipe select', interaction='stationary', options=['Walk', 'Cycle']))))
        await step('15-nav-mode-cursor', lambda: press('down', 0.4), settle=0.8)
        await step('16-nav-preview', lambda: gateway.send(NavigationView(**nav(), navigation=NavigationHudView(
            view='preview', primary='Walk · 14 min', secondary='Central Library',
            hint='tap: start · up: mode · down: place', interaction='stationary', mode='walking'))))
        await step('17-nav-guidance-map', lambda: gateway.send(NavigationView(**nav(), navigation=NavigationHudView(
            view='guidance', primary='Turn left', secondary='onto Main St', hint='double-click: menu',
            interaction='none', mode='walking', maneuver='turn_left', maneuver_distance_m=80,
            remaining_distance_m=900, eta_minutes=13))))
        await step('18-nav-arrived', lambda: gateway.send(NavigationView(**nav(), navigation=NavigationHudView(
            view='arrived', primary='You have arrived', secondary='Central Library',
            hint='tap: finish', interaction='stationary'))))
    finally:
        report['_tiles_per_step'] = steps
        (OUT / 'report.json').write_text(json.dumps(report, indent=2))
        await gateway.stop()
    for name, count in steps:
        print(f'{count:>3} tiles  {name}')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
