#!/usr/bin/env python3
"""Probe the Brainstorm surface in the official simulator and count tiles.

The companion of simulator_messages_native_probe.py for the other live
surface. It plays the Mac's side of a Brainstorm with the real gateway and
the real HudWriter: the idle picker (a firmware list), NEW, the ready frame,
the tap that arms topic capture, the listening frame, a streaming run with
several revisions, the settled synthesis, and Back. Every step records the
[hud-timing] console entries so a step that painted an image tile is visible
in the report.

    npm --prefix apps/hub-plugin run dev        # VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac python3 scripts/simulator_brainstorm_native_probe.py /tmp/probe

No CLI seat runs: the frames are the ones a DebateStateSink paints, sent by
this script, so the probe measures the lenses and the gesture wire only.
"""
from __future__ import annotations

import asyncio, json, logging, sys, tempfile, time, urllib.request
from dataclasses import dataclass
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import BrainstormAction, BrainstormHudView, VOICE_REQUEST_PREFIX as VOICE_PREFIX
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher

log = logging.getLogger('brainstorm-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-brainstorm-probe'
OUT.mkdir(parents=True, exist_ok=True)


@dataclass
class PastSession:
    """The two fields the picker row reads off a stored session, plus its age."""

    session_id: str
    title: str
    turns: tuple[str, ...]
    ended_at: float

    def age(self) -> str:
        minutes = int((time.time() - self.ended_at) // 60)
        return f'{minutes}m ago' if minutes < 60 else f'{minutes // 60}h ago'


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
    actions: list[str] = []
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix='joat-brainstorm-') as root:
        rootp = Path(root)
        gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
        gateway.on_hello(lambda _h: connected.set())
        gateway.on_brainstorm_action(lambda action: actions.append(action.action))
        hud = HudWriter(gateway)
        past = [PastSession('s-1', 'Cache the picker on the glasses?', ('you', 'codex', 'claude'), time.time() - 600),
                PastSession('s-2', 'Name for the reply verb', ('you', 'codex'), time.time() - 7200)]
        router = CapabilityRouter(hud=hud, transport=gateway,
                                  status=StatusPublisher(rootp / 'status.json'),
                                  messages=None, cards=CardLog(),
                                  brainstorm_sessions=lambda: past,
                                  brainstorm_continue=lambda _id: asyncio.sleep(0, result=True))  # type: ignore[arg-type]
        gateway.on_input(router.on_input)
        await gateway.start()
        try:
            log.info('waiting for plugin hello on 18791')
            await asyncio.wait_for(connected.wait(), timeout=120)
            await asyncio.sleep(2.0)
            ids = last_id(console())

            # The idle picker: a firmware list of past sessions plus NEW.
            await router.open('Brainstorm', request_id=VOICE_PREFIX + 'drive-brainstorm')
            await snap('1-picker', 1.2)
            report['picker_timing'] = timing_since(console(), ids); ids = last_id(console())

            # NEW is row 0: the ready frame, after the Mac claims the surface
            # exactly as a spoken start does.
            await press('click', 0.3)
            await snap('2-ready', 1.0)
            report['ready_timing'] = timing_since(console(), ids); ids = last_id(console())

            # The tap on a ready frame arms topic capture on the Mac.
            before = len(actions)
            await press('click', 0.4)
            report['tap_action'] = actions[before:]
            await hud.show_brainstorm(BrainstormHudView(
                state='topic_capture', topic='Speak your question', collaborators=['Codex', 'Claude'],
                activity='Your next spoken phrase starts this Brainstorm.'))
            await snap('3-listening', 0.8)
            report['listening_timing'] = timing_since(console(), ids); ids = last_id(console())

            # A run: the frames a DebateStateSink paints, several revisions.
            topic = 'Should the glasses cache the picker rows?'
            await hud.begin_brainstorm()
            transcript = [f'YOU > {topic}']
            await hud.show_brainstorm(BrainstormHudView(
                state='running', topic=topic, collaborators=['Codex', 'Claude'],
                active_speaker='Codex · round 1/3 · 1 calls', activity='\n'.join(transcript)))
            t_run = time.monotonic()
            chunks = ['CODEX > A cache saves one round trip per open,',
                      ' but the Mac already answers in 0.2 s and stale rows',
                      ' would tap the wrong thread. Keep the Mac authoritative.',
                      '\nCLAUDE > Agreed on authority. The real cost is the',
                      ' first paint; prefetch the rows while the menu opens.',
                      '\nJOAT > Keep rows Mac-owned; prefetch on menu open.']
            transcript.append('')
            for chunk in chunks:
                transcript[-1] += chunk if not chunk.startswith('\n') else ''
                if chunk.startswith('\n'):
                    transcript.append(chunk[1:])
                await hud.show_brainstorm(BrainstormHudView(
                    state='running', topic=topic, collaborators=['Codex', 'Claude'],
                    active_speaker='Claude · round 1/3 · 2 calls' if 'CLAUDE' in transcript[-1] else 'Codex · round 1/3 · 1 calls',
                    activity='\n'.join(transcript)))
                await asyncio.sleep(0.35)
            report['run_revisions_s'] = round(time.monotonic() - t_run, 2)
            await snap('4-running', 0.8)
            report['running_timing'] = timing_since(console(), ids); ids = last_id(console())

            # A swipe up pages the transcript on the glasses, no Mac round trip.
            await press('up', 0.6)
            await snap('5-scrolled', 0.6)
            report['scroll_timing'] = timing_since(console(), ids); ids = last_id(console())
            await press('down', 0.6)

            await hud.show_brainstorm(BrainstormHudView(
                state='settled', topic=topic, collaborators=['Codex', 'Claude'],
                activity='\n'.join(transcript)))
            await snap('6-settled', 1.0)
            report['settled_timing'] = timing_since(console(), ids); ids = last_id(console())

            await press('double_click', 0.6)
            await snap('7-after-back', 1.2)
            report['back_timing'] = timing_since(console(), ids)
            report['actions'] = actions
        finally:
            (OUT / 'report.json').write_text(json.dumps(report, indent=2))
            await gateway.stop()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
