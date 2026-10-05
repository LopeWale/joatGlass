#!/usr/bin/env python3
"""Probe QuickNotes and Notifications in the official simulator and count tiles.

Plays the Mac's side with the real gateway, router and stores: a note store
with more notes than one list page, and a card log with two notifications.
Drives list → older page by tap → newer page → note reader → swipe → Back,
then the Notifications list → a card's detail → Back, and records every
[hud-timing] console entry per step. A step with no entry painted no image
tile.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_quicknotes_native_probe.py /tmp/probe
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import Card, CardLog
from joat.core.protocol import VOICE_REQUEST_PREFIX as VOICE_PREFIX
from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher

log = logging.getLogger('quicknotes-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-quicknotes-probe'
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


async def _wait_until(predicate, timeout: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError('condition did not become true')
        await asyncio.sleep(0.05)


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix='joat-quicknotes-') as root:
        rootp = Path(root)
        with DurableStore(rootp / 'notes.sqlite3') as durable:
            notes = NoteStore(durable)
            now_us = int(time.time() * 1_000_000)
            long_text = ' '.join(
                f'Sentence {index} of a dictated note long enough to need more than one firmware page.'
                for index in range(1, 9))
            for index in range(1, 24):
                notes.create(
                    note_id=f'note-{index:02d}', idempotency_key=f'key-{index:02d}',
                    text=long_text if index == 23 else f'Fixture note {index} · buy batteries for the ring',
                    source=NoteSource(kind='dictation'), now_us=now_us - (24 - index) * 60_000_000)
            cards = CardLog()
            cards.add(Card(source='Dele', channel='Messages', summary='Call me when you arrive.',
                           body='Call me when you arrive. The gate code is the same as last time, '
                                'and park on the left side of the building.'))
            cards.add(Card(source='Calendar', channel='Calendar', summary='Design review in 15 minutes'))
            gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
            gateway.on_hello(lambda _h: connected.set())
            hud = HudWriter(gateway)
            router = CapabilityRouter(hud=hud, transport=gateway,
                                      status=StatusPublisher(rootp / 'status.json'),
                                      messages=None, cards=cards, notes=notes)  # type: ignore[arg-type]
            gateway.on_input(router.on_input)
            await gateway.start()
            try:
                log.info('waiting for plugin hello on 18791')
                await asyncio.wait_for(connected.wait(), timeout=120)
                await asyncio.sleep(2.0)
                ids = last_id(console())

                await router.open('QuickNotes', request_id=VOICE_PREFIX + 'drive-quicknotes')
                await _wait_until(lambda: router._note_list is not None, 30)
                await snap('1-notes', 1.2)
                report['notes_timing'] = timing_since(console(), ids); ids = last_id(console())

                # The newest note is row 1 of this page (20 rows); "▼ Older
                # notes" is the row after the last note.
                rows_on_page = len(router._note_rows) + 1
                for _ in range(rows_on_page):
                    await press('down', 0.12)
                t_page = time.monotonic()
                await press('click', 0.3)
                await _wait_until(lambda: router._note_list is not None and router._note_list.index == 1, 30)
                report['older_page_after_tap_s'] = round(time.monotonic() - t_page, 2)
                await snap('2-older-notes', 1.0)
                report['older_timing'] = timing_since(console(), ids); ids = last_id(console())

                # Back to the newest page by its row, then open the long note
                # (the newest row on that page).
                older_rows = len(router._note_rows) + 1
                for _ in range(older_rows):
                    await press('down', 0.12)
                await press('click', 0.3)
                await _wait_until(lambda: router._note_list is not None and router._note_list.index == 0, 30)
                await snap('3-newest-again', 1.0)
                report['newer_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('down', 0.2)
                t_read = time.monotonic()
                await press('click', 0.3)
                await _wait_until(lambda: router._note_reader is not None, 30)
                report['reader_after_tap_s'] = round(time.monotonic() - t_read, 2)
                await snap('4-note', 1.0)
                report['reader_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('down', 0.8)
                await snap('5-note-down', 0.6)
                report['reader_down_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('double_click', 0.6)
                await _wait_until(lambda: router._note_reader is None, 30)
                await snap('6-notes-again', 1.0)
                report['reader_back_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('double_click', 0.8)

                await router.open('Notifications', request_id=VOICE_PREFIX + 'drive-notifications')
                await _wait_until(lambda: bool(router._notification_rows), 30)
                await snap('7-notifications', 1.2)
                report['notifications_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('click', 0.4)
                await snap('8-notification-detail', 1.0)
                report['detail_timing'] = timing_since(console(), ids); ids = last_id(console())
                await press('double_click', 0.6)
                await snap('9-after-back', 1.2)
                report['back_timing'] = timing_since(console(), ids)
            finally:
                (OUT / 'report.json').write_text(json.dumps(report, indent=2))
                await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
