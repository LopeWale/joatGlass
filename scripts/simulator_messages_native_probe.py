#!/usr/bin/env python3
"""Probe the Messages reply flow in the official simulator and count tiles.

A thinner companion to simulator_messages_flow_acceptance.py for one
question: which Messages screens are firmware pages and which are still
four-tile image pages. It reuses that script's fixtures (disposable chat.db,
recording sender, production gateway/router/reply controller), opens Messages
from the Mac side the way a voice command does, drives picker → conversation
→ reply → confirmation → tap → result → Back through the simulator's
automation API, and records every [hud-timing] console entry per step.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_messages_native_probe.py /tmp/probe

Writes 1-picker.png … 6-after-back.png and report.json to the output
directory. A step with no [hud-timing] entry painted no image tile. No real
message is sent: the final Messages.app boundary is the recording sender.
"""
from __future__ import annotations

import asyncio, importlib.util, json, logging, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import simulator_messages_flow_acceptance as accept  # noqa: E402

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.status import StatusPublisher
from joat.core.protocol import VOICE_REQUEST_PREFIX as VOICE_PREFIX

log = logging.getLogger('drive')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-messages-probe'
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


async def main() -> None:
    connected = asyncio.Event()
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix='joat-drive-') as root:
        rootp = Path(root)
        store = accept.build_fixture_store(rootp / 'chat.db')
        gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
        gateway.on_hello(lambda _h: connected.set())
        hud = HudWriter(gateway)
        sender = accept.RecordingSender(None)
        replies = accept.ObservedReplies(MessageReplyController(
            sender, gateway.request_confirmation,
            contact_search=lambda q: []))
        router = CapabilityRouter(hud=hud, transport=gateway,
                                  status=StatusPublisher(rootp / 'status.json'),
                                  messages=store, cards=CardLog(), replies=replies)  # type: ignore[arg-type]
        gateway.on_input(router.on_input)
        await gateway.start()
        try:
            log.info('waiting for plugin hello on 18791')
            await asyncio.wait_for(connected.wait(), timeout=120)
            await asyncio.sleep(2.0)  # let boot chrome settle before the voice-style open
            t0 = time.monotonic()
            await router.open('Messages', request_id=VOICE_PREFIX + 'drive-messages')
            await asyncio.wait_for(accept._wait_for_messages_picker(router), timeout=60)
            report['picker_after_hello_s'] = round(time.monotonic() - t0, 2)
            ids = last_id(console())
            await snap('1-picker', 1.0)
            report['picker_timing'] = timing_since(console(), 0)[-6:]
            ids = last_id(console())

            await press('down', 0.4)
            await snap('2-picker-down', 0.5)
            report['picker_down_timing'] = timing_since(console(), ids); ids = last_id(console())

            t1 = time.monotonic()
            await press('click', 0.2)
            await asyncio.wait_for(replies.selected.wait(), timeout=60)
            report['conversation_selected_after_click_s'] = round(time.monotonic() - t1, 2)
            await snap('3-conversation', 1.2)
            report['conversation_timing'] = timing_since(console(), ids); ids = last_id(console())
            report['selected_thread'] = getattr(replies.selected_thread, 'sender', None)

            # voice draft injected exactly as the acceptance script does it
            t2 = time.monotonic()
            task = asyncio.create_task(replies.handle_command(
                'reply saying Simulator trace only. No message will be sent.'))
            deadline = time.monotonic() + 20
            visible = None
            while time.monotonic() < deadline:
                ents = console()
                if any('confirmation-visible' in e['message'] for e in ents if e['id'] > ids):
                    visible = time.monotonic(); break
                await asyncio.sleep(0.05)
            report['confirmation_visible_after_command_s'] = None if visible is None else round(visible - t2, 2)
            await snap('4-confirmation', 0.2)
            report['confirmation_timing'] = timing_since(console(), ids); ids = last_id(console())

            t3 = time.monotonic()
            await press('click', 0.1)
            result = await asyncio.wait_for(task, timeout=60)
            report['command_result_after_tap_s'] = round(time.monotonic() - t3, 2)
            report['result'] = result
            report['sender_called'] = sender.called.is_set()
            report['sender_sent'] = sender.sent.is_set()
            await hud.assistant(result or '')
            await hud.settle_assistant(10_000)
            await snap('5-result', 1.5)
            report['result_timing'] = timing_since(console(), ids); ids = last_id(console())

            await press('double_click', 0.5)
            await snap('6-after-back', 1.5)
            report['back_timing'] = timing_since(console(), ids)
        finally:
            (OUT / 'report.json').write_text(json.dumps(report, indent=2))
            await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    asyncio.run(main())
