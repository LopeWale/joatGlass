#!/usr/bin/env python3
"""A whole Brainstorm room with the REAL Codex and Claude, on the simulator.

Every part is the shipping one: the gateway, HudWriter, CapabilityRouter,
SessionManager, DebateSession, HudSink, VerifierGate and the two CLI seats
(`codex exec`, `claude -p`) on whatever login this Mac has. Only the wearer
is played by this script: it asks, reads the glasses through the simulator's
screenshots, scrolls the reader with the simulated swipes, speaks its next
move to `SessionManager.handle_transcript` (with no wake word, as the wearer
does while the room waits), and ends the room with the cancel words.

    NODE_ENV=development VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses \\
        npm --prefix apps/hub-plugin run dev -- --port 5190 --host 127.0.0.1
    evenhub-simulator http://127.0.0.1:5190/ --automation-port 9898
    PYTHONPATH=apps/mac .venv/bin/python scripts/simulator_brainstorm_room_probe.py .collab/room

It uses its own gateway port (18791) and a throwaway state directory, so the
live JOAT engine and the wearer's real glasses are never touched. It does
spend real CLI turns: two seats per round, two rounds.
"""
from __future__ import annotations

import asyncio, json, logging, os, sys, tempfile, time, urllib.request
from pathlib import Path

from joat.agents.cli_accounts import AccountRegistry
from joat.agents.subprocess_agent import critic, proposer
from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import BrainstormAction, BrainstormHudView, VOICE_REQUEST_PREFIX as VOICE_PREFIX
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.models import ModelRegistry, assistant_slot, default_slots
from joat.services.session import DebateSession, SessionManager, seat_participants
from joat.services.sinks import Broadcast, DebateStateSink, HudSink
from joat.services.status import StatusPublisher
from joat.verifier.fetcher import fetch_page_text, wayback_page_text
from joat.verifier.gate import VerifierGate

log = logging.getLogger('room-probe')
BASE = 'http://127.0.0.1:9898'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'joat-room-probe'
OUT.mkdir(parents=True, exist_ok=True)

QUESTION = ('How should I decide whether to move my small business\'s customer '
            'records from spreadsheets into a real database? Break it down for me in detail.')
MOVE = 'Okay, now give me a step by step plan for the first two weeks.'
ROUND_TIMEOUT_S = 780


def console() -> list[dict]:
    with urllib.request.urlopen(f'{BASE}/api/console', timeout=5) as r:
        return json.load(r)['entries']


async def snap(name: str, settle: float = 0.8) -> bytes:
    await asyncio.sleep(settle)
    with urllib.request.urlopen(f'{BASE}/api/screenshot/glasses', timeout=10) as r:
        data = r.read()
    (OUT / f'{name}.png').write_bytes(data)
    return data


async def press(action: str, settle: float = 0.5) -> None:
    req = urllib.request.Request(f'{BASE}/api/input', data=json.dumps({'action': action}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5):
        pass
    await asyncio.sleep(settle)


async def until(predicate, timeout: float, what: str, every: float = 0.25) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f'timed out waiting for {what}')
        await asyncio.sleep(every)


async def read_reader(prefix: str, report: dict, key: str, steps: int = 40) -> None:
    """Swipe down through the reader, a screenshot per step (a wearer skimming)."""
    shots = [await snap(f'{prefix}-p0', 1.0)]
    for step in range(1, steps):
        await press('down', 0.7)
        shot = await snap(f'{prefix}-p{step}', 0.2)
        if shot == shots[-1]:
            break
        shots.append(shot)
    report[key] = {'screens': len(shots)}


async def scroll_to_end(prefix: str, report: dict, key: str, limit: int = 1500) -> None:
    """Keep swiping down until the page stops changing: the end must be reachable."""
    swipes = 0
    last = await snap(f'{prefix}-mid', 0.2)
    still = 0
    while swipes < limit and still < 3:
        for _ in range(8):
            await press('down', 0.12)
            swipes += 1
        await asyncio.sleep(0.5)
        shot = await snap(f'{prefix}-tail', 0.0)
        still = still + 1 if shot == last else 0
        last = shot
    await snap(f'{prefix}-end', 0.5)
    report[key] = {'swipes': swipes, 'reached_end': still >= 3}


async def main() -> None:
    connected = asyncio.Event()
    actions: list[str] = []
    report: dict[str, object] = {}
    sizes: list[int] = []
    with tempfile.TemporaryDirectory(prefix='joat-room-probe-') as root:
        rootp = Path(root)
        gateway = HubGatewayTransport(host='127.0.0.1', port=18791)
        gateway.on_hello(lambda _h: connected.set())
        gateway.on_brainstorm_action(lambda action: actions.append(action.action))
        real_send = gateway.send

        async def measured_send(message):  # type: ignore[no-untyped-def]
            if isinstance(message, BrainstormHudView):
                sizes.append(len(json.dumps(message.to_wire())))
            return await real_send(message)

        gateway.send = measured_send  # type: ignore[method-assign]
        hud = HudWriter(gateway)
        status = StatusPublisher(rootp / 'status.json')
        # A throwaway account registry: the default profile, so the CLIs use
        # whatever login this Mac already has, and nothing of the engine's
        # own state is read or written.
        accounts = AccountRegistry(rootp / 'accounts')
        models = ModelRegistry([*default_slots(), assistant_slot('claude')])
        seats = [
            ('codex', 'proposer', proposer(models.provider('codex'), accounts.env_provider('codex'))),
            ('claude', 'critic', critic(models.provider('claude'), accounts.env_provider('claude'))),
        ]
        if os.environ.get('ROOM_PROBE_SECOND_CLAUDE'):
            # Codex has one login on this Mac and it can be out of usage for
            # days; a second Claude seat in its place still exercises every
            # real part (CLI streaming, the prompt, the room, the reader)
            # except Codex's own output. Say so in the report.
            seats[0] = ('claude-b', 'proposer',
                        critic(models.provider('claude'), accounts.env_provider('claude')))
            report['seats'] = 'claude-b (proposer) + claude (critic); Codex not used'
        seated = [(n, r, a) for n, r, a in seats if a]
        if len(seated) < 2:
            raise SystemExit(f'need both CLIs, found {[n for n, _r, _a in seated]}')
        gate = VerifierGate(fetch_page_text, wayback_page_text)
        state = DebateStateSink()
        holder: dict[str, DebateSession] = {}
        phases: list[str] = []

        def make_debate(topic: str) -> DebateSession:
            session = DebateSession(
                topic, seat_participants(seated),
                Broadcast([HudSink(
                    hud,
                    open_terminal=lambda: router.open_terminal_from_brainstorm(),
                    brainstorm_view_current=lambda: router.brainstorm_view_current,
                    show_room=router.show_brainstorm_room,
                    hide_room=router.hide_brainstorm_room,
                ), state]),
                gate=gate, on_phase=phases.append,
            )
            holder['session'] = session
            return session

        def on_state(s: str) -> None:
            if s == 'running':
                router.claim_brainstorm_view()

        sessions = SessionManager(
            hud, make_debate,
            brainstorm_available=lambda: True,
            brainstorm_view_current=lambda: router.brainstorm_view_current,
            surface_revision=lambda: router.view_revision,
            on_state=on_state,
        )
        router = CapabilityRouter(
            hud=hud, transport=gateway, status=status, messages=None,
            cards=CardLog(rootp / 'cards.jsonl'),
            brainstorm_sessions=lambda: [],
            brainstorm_continue=lambda _id: asyncio.sleep(0, result=True),  # type: ignore[arg-type]
            brainstorm_replay=sessions.replay_active_hud,
        )

        def on_brainstorm_action(action: BrainstormAction) -> None:
            actions.append(action.action)
            if action.action == 'show_brainstorm' and isinstance(sessions.active_session, DebateSession):
                router.claim_brainstorm_view()
            sessions.handle_brainstorm_action(action)

        gateway.on_brainstorm_action(on_brainstorm_action)
        gateway.on_input(router.on_input)
        await gateway.start()
        try:
            log.info('waiting for the simulator plugin on 18791')
            await asyncio.wait_for(connected.wait(), timeout=240)
            await asyncio.sleep(2.0)

            # Into Brainstorm the way the wearer goes: the picker, then NEW.
            await router.open('Brainstorm', request_id=VOICE_PREFIX + 'room-probe')
            await snap('1-picker', 1.2)
            await press('click', 0.3)
            await snap('2-ready', 1.0)

            t0 = time.monotonic()
            result = sessions.start_brainstorm(QUESTION)
            report['start'] = result.value
            assert result.value == 'started', result
            session = holder['session']

            # ---- round 1: watch it live, then it must STOP and wait ----
            tick = 0
            while not session.awaiting_wearer:
                if not sessions.active:
                    raise RuntimeError('the room ended before it waited on the wearer')
                if time.monotonic() - t0 > ROUND_TIMEOUT_S:
                    raise TimeoutError('round 1 did not finish')
                if tick % 12 == 0:
                    await snap(f'3-live-{tick // 12}', 0.0)
                tick += 1
                await asyncio.sleep(1.0)
            report['round1_s'] = round(time.monotonic() - t0, 1)
            report['round1_texts'] = {label: {'chars': len(text), 'words': len(text.split())}
                                      for label, text in session._transcript}
            calls_at_wait = session._calls
            await asyncio.sleep(3.0)
            await read_reader('4-room1', report, 'room1_reader')
            report['reader_open_after_round1'] = router._note_reader is not None and router._note_reader.title

            # ---- nothing may carry on by itself ----
            await asyncio.sleep(45)
            report['no_auto_continue'] = bool(session.awaiting_wearer and session._calls == calls_at_wait)
            assert report['no_auto_continue'], 'the room carried on without the wearer'

            # ---- the wearer speaks (no wake word): round 2 ----
            t1 = time.monotonic()
            consumed = sessions.handle_transcript(MOVE, addressed=None)
            report['move_consumed'] = consumed
            await until(lambda: not session.awaiting_wearer, 20, 'round 2 to start')
            await snap('5-round2-start', 1.5)
            tick = 0
            while not session.awaiting_wearer:
                if not sessions.active:
                    raise RuntimeError('the room ended during round 2')
                if time.monotonic() - t1 > ROUND_TIMEOUT_S:
                    raise TimeoutError('round 2 did not finish')
                if tick % 12 == 0:
                    await snap(f'5-round2-live-{tick // 12}', 0.0)
                tick += 1
                await asyncio.sleep(1.0)
            report['round2_s'] = round(time.monotonic() - t1, 1)
            report['transcript'] = {f'{i}:{label}': {'chars': len(text), 'words': len(text.split())}
                                    for i, (label, text) in enumerate(session._transcript)}
            await asyncio.sleep(3.0)
            await read_reader('6-room2', report, 'room2_reader', steps=6)
            await scroll_to_end('6-room2', report, 'room2_scroll')

            # ---- Back, then the cancel words ----
            before = len(actions)
            await press('double_click', 1.0)
            await snap('7-after-back', 1.2)
            report['back_actions'] = actions[before:]
            report['active_after_back'] = sessions.active
            if sessions.active:
                sessions.handle_transcript('stop', addressed=None)
                await until(lambda: not sessions.active, 10, 'stop to end the room')
                await snap('8-after-stop', 1.2)
            report['ended'] = not sessions.active
            report['phases'] = phases
            report['brainstorm_frames'] = len(sizes)
            report['max_frame_bytes'] = max(sizes, default=0)
            report['total_frame_bytes'] = sum(sizes)
            report['full_texts'] = {f'{i}:{label}': text for i, (label, text) in enumerate(session._transcript)}
            report['console_errors'] = [e['message'][:300] for e in console() if e['level'] in ('error',)]
        finally:
            (OUT / 'report.json').write_text(json.dumps(report, indent=2))
            await sessions.close()
            await gateway.stop()
    summary = {k: v for k, v in report.items() if k != 'full_texts'}
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    asyncio.run(main())
