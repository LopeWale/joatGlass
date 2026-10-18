#!/usr/bin/env python3
"""Probe the reminders list a spoken request opens, in the official simulator.

"Create reminders from the last note", said while some other page is up, has no
note page to answer. The Mac puts the list up as a spoken request of its own
(a fresh `voice-` request id) and the glasses app takes it once. Plays the Mac's
side with the real gateway and router and fixture data only: opens Notifications
(a different page), asks for the reminders list, scrolls it, taps (the Mac gets
the tap), asks again and leaves it with Back (the Mac gets the exit).

    VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses npm --prefix apps/hub-plugin run dev
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_spoken_reminders_probe.py /tmp/probe
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

log = logging.getLogger('spoken-reminders-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-spoken-reminders-probe'
OUT.mkdir(parents=True, exist_ok=True)

TITLE = 'Fixture plumber call'
LINES = (
    '□ Call the plumber about the leak — Oct 19 at 09:00',
    '□ Send Dele the signed contract — Oct 23 at 09:00',
    '□ Pay the tile shop invoice — Oct 18 at 09:00',
    '□ Book the electrician for the kitchen — Oct 18 at 09:00',
)


def console() -> list[dict]:
    with urllib.request.urlopen(f'{BASE}/api/console', timeout=5) as r:
        return json.load(r)['entries']


def last_id(entries: list[dict]) -> int:
    return max((e['id'] for e in entries), default=0)


def joat_log_since(since_id: int) -> list[str]:
    return [e['message'][:200] for e in console()
            if e['id'] > since_id and ('[joat]' in e['message'] or 'uncaught' in e['message'])]


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
    with tempfile.TemporaryDirectory(prefix='joat-spoken-reminders-') as root:
        rootp = Path(root)
        with DurableStore(rootp / 'notes.sqlite3') as durable:
            notes = NoteStore(durable)
            notes.create(
                note_id='note-01', idempotency_key='key-01',
                text='Fixture note · call the plumber about the leak tomorrow',
                source=NoteSource(kind='dictation'), now_us=int(time.time() * 1_000_000))
            cards = CardLog()
            cards.add(Card(source='Dele', channel='Messages', summary='Call me when you arrive.'))
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

                # Some other page is up: the wearer is nowhere near a note.
                await router.open('Notifications', request_id=VOICE_PREFIX + 'drive-notifications')
                await _wait_until(lambda: bool(router._notification_rows), 30)
                await snap('1-other-page', 1.0)
                report['other_page_reader_open'] = router._note_reader is not None

                # Said from there: the list opens as a request of its own.
                before = router._active_request_id
                asked = asyncio.create_task(router.review_note_reminders(TITLE, LINES, timeout_s=60))
                await _wait_until(lambda: router._note_reader is not None, 30)
                t_open = time.monotonic()
                report['list_request_is_new_voice_id'] = bool(
                    router._active_request_id and router._active_request_id != before
                    and router._active_request_id.startswith(VOICE_PREFIX))
                reader = router._note_reader
                report['list_title'] = reader.title if reader else None
                report['list_text_lines'] = len(reader.text.splitlines()) if reader and reader.text else 0
                await snap('2-spoken-list', 1.5)
                report['list_painted_after_s'] = round(time.monotonic() - t_open, 2)
                report['list_console'] = joat_log_since(ids)

                for _ in range(3):
                    await press('down', 0.25)
                await snap('3-spoken-list-scrolled', 0.6)

                # The tap reaches the Mac: the list is answered, the confirmation follows.
                await press('click', 0.4)
                report['tap_answered'] = await asyncio.wait_for(asked, 20)

                # Asked again, then left with Back: the Mac is told, nothing goes on.
                asked_again = asyncio.create_task(router.review_note_reminders(TITLE, LINES, timeout_s=60))
                await _wait_until(lambda: router._note_reader is not None, 30)
                await snap('4-spoken-list-again', 1.5)
                await press('double_click', 0.6)
                report['back_answered'] = await asyncio.wait_for(asked_again, 20)
                await snap('5-after-back', 1.0)
            finally:
                (OUT / 'report.json').write_text(json.dumps(report, indent=2))
                await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
