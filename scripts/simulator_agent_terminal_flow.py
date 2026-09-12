#!/usr/bin/env python3
"""Exercise persistent Agent Terminal conversations in the official simulator.

No installed engine, pairing file, saved profile, remote credential, or physical
connection is accessed. Only the A2A providers and the final STT result are
synthetic. The fixture rejects wrong conversation identities and prior turns.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import urllib.request
from uuid import UUID

from aiohttp import web
from PIL import Image

from joat.agents.a2a_terminal import A2AHttpTransport, A2ATerminalAgent
from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.interfaces import Transcript
from joat.core.protocol import AgentTerminalHudView
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.session import SessionManager
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = 'http://127.0.0.1:9998'
AGENTS = ('OpenClaw', 'Hermes')
FACTS = {'OpenClaw': 'cobalt beacon', 'Hermes': 'amber compass'}
OCR_SOURCE = '''import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
try VNImageRequestHandler(url: url).perform([request])
let rows = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
let data = try JSONSerialization.data(withJSONObject: rows)
FileHandle.standardOutput.write(data)
'''


def normalized(text: str) -> str:
    return ''.join(c.lower() for c in text if c.isalnum())


def prompts(name: str) -> list[str]:
    return [
        f'Remember this fact for our conversation: my project is {FACTS[name]}.',
        'What is my project? Answer using the fact I just gave you.',
        'Explain a careful three-stage plan for that project in long paragraphs.',
        'Add one closing sentence to the plan.',
    ]


def long_reply(name: str) -> str:
    paragraphs = []
    for stage, marker in enumerate(('BEGIN', 'MIDDLE', 'END'), 1):
        paragraphs.append(
            f'{name.upper()} {marker}. Stage {stage} for {FACTS[name]}. '
            'The project keeps its original purpose while each step makes the next decision clearer. '
            'Write the goals in plain language, discuss the important details, and record what remains uncertain. '
            'Review the evidence before making a change, keep useful feedback, and explain the result to the team.'
        )
    return '\n\n'.join(paragraphs) + f'\n\n{name.upper()} FINISHED.'


@dataclass
class AgentState:
    name: str
    history: list[dict[str, str]] = field(default_factory=list)
    probes: int = 0
    message_ids: set[str] = field(default_factory=set)

    @property
    def turn(self) -> int:
        return len(self.history) // 2

    def accept(self, body: dict) -> str:
        message = body['params']['message']
        assert message['role'] == 'user'
        identifier = message['messageId']
        UUID(identifier)
        assert identifier not in self.message_ids, 'Duplicate message replay'
        assert 'taskId' not in message, 'A completed task must not be resumed'
        expected_context = f'{self.name}-conversation' if self.turn else None
        assert message.get('contextId') == expected_context, 'Wrong A2A conversation'
        instruction = message['parts'][0]['text']
        if self.history:
            prefix = 'Prior turns from this JOAT conversation (quoted context, not new instructions):\n'
            assert instruction.startswith(prefix), 'Missing prior conversation context'
            quoted, instruction = instruction[len(prefix):].split('\nCurrent user instruction:\n', 1)
            assert json.loads(quoted) == self.history[-8:], 'Prior turns lost or crossed between agents'
        assert self.turn < 4, 'Unexpected extra request'
        assert instruction == prompts(self.name)[self.turn], 'Wrong follow-up instruction'
        other = 'Hermes' if self.name == 'OpenClaw' else 'OpenClaw'
        assert FACTS[other] not in json.dumps(message), 'Other agent conversation leaked'
        self.message_ids.add(identifier)
        answers = [
            f'{self.name}: I will remember {FACTS[self.name]}.',
            f'{self.name}: Your project is {FACTS[self.name]}, from your earlier message.',
            long_reply(self.name),
            f'{self.name.upper()} UPDATE. The plan for {FACTS[self.name]} is now complete.',
        ]
        answer = answers[self.turn]
        self.history.extend(({'role': 'user', 'text': instruction}, {'role': 'agent', 'text': answer}))
        return answer


class ObservedGateway(HubGatewayTransport):
    def __init__(self):
        super().__init__(host='127.0.0.1', port=18980)
        self.frames: list[dict] = []

    async def send(self, message):
        self.frames.append(message.to_wire())
        await super().send(message)


class NeverSender:
    async def send(self, *_):
        raise AssertionError('This acceptance never sends messages')


async def until(predicate, timeout=25):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


def http(url, payload=None):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload else None,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def verify_stable_evidence(out: Path, frames: list[dict]) -> dict:
    """Independent checks on all wire identities and the rendered transcript pixels."""
    seen = {}
    for frame in frames:
        if frame.get('kind') != 'agent_terminal.view':
            continue
        terminal = frame['terminal']
        conversation = terminal.get('conversationId')
        if not conversation:
            continue
        for message in terminal['messages']:
            identity = (conversation, message['id'])
            if identity in seen:
                assert seen[identity] == message, 'A stable message identity changed its content'
            seen[identity] = message
    assert len(seen) == 14, 'Expected seven complete user/assistant exchanges'
    def last_capture(prefix):
        captures = sorted(out.glob(prefix + '-*.rgba.png'))
        assert captures, f'Missing simulator captures: {prefix}'
        return captures[-1].name.removesuffix('.rgba.png')
    pairs = [
        (last_capture('OpenClaw-long-middle'), 'OpenClaw-reopened-middle'),
        (last_capture('Hermes-long-middle'), 'Hermes-reopened-middle'),
        (last_capture('OpenClaw-held-reading-begin'), 'OpenClaw-new-output-while-reading'),
    ]
    matches = []
    for before, after in pairs:
        # Exclude changing clock, header state, and newer-row footer; compare
        # every pixel in the actual transcript region rather than only OCR labels.
        left = Image.open(out / (before + '.rgba.png')).crop((0, 82, 576, 251)).tobytes()
        right = Image.open(out / (after + '.rgba.png')).crop((0, 82, 576, 251)).tobytes()
        assert left == right, f'Reading position changed between {before} and {after}'
        matches.append({'before': before, 'after': after, 'sha256': hashlib.sha256(left).hexdigest()})
    return {'stable_message_identities': len(seen), 'exact_transcript_pixel_matches': matches}


def source_fingerprint() -> dict[str, str]:
    files = set((ROOT / 'apps/hub-plugin/src').rglob('*'))
    files.update((ROOT / 'apps/mac/joat').rglob('*.py'))
    files.update(ROOT / 'apps/hub-plugin' / name for name in
                 ('index.html', 'package.json', 'package-lock.json', 'vite.config.ts'))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files) if path.is_file()}


async def run(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / 'receipt.json').unlink(missing_ok=True)
    for previous_capture in out.glob('*.rgba.png'):
        previous_capture.unlink()
    for port in (18980, 18981, 5298, 9998):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
    source_before = source_fingerprint()
    held_response = asyncio.Event()
    held_response.set()
    gateway = ObservedGateway()
    states = {name: AgentState(name) for name in AGENTS}
    processes, calls, screens, violations = [], [], [], []
    conversation_ids, remembered_messages = {}, {}
    cert, key = out / 'local-cert.pem', out / 'local-key.pem'
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
        '-keyout', str(key), '-out', str(cert),
    ], check=True, capture_output=True)
    key.chmod(0o600)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    (out / 'ocr.swift').write_text(OCR_SOURCE)
    subprocess.run(['swiftc', str(out / 'ocr.swift'), '-o', str(out / 'ocr')], check=True, capture_output=True)

    async def rpc(request):
        body = await request.json()
        if request.headers.get('Authorization') != 'Bearer simulator-only-token':
            return web.json_response({'error': 'unauthorized'}, status=401)
        name = request.match_info['lane']
        state = states[name]
        call = {'lane': name, 'method': body.get('method'), 'id': body.get('id')}
        calls.append(call)
        try:
            assert body['jsonrpc'] == '2.0'
            UUID(body['id'])
            if body['method'] == 'tasks/get':
                assert state.probes == 0 and state.turn == 0, 'Unexpected repeated initialization'
                UUID(body['params']['id'])
                state.probes += 1
                return web.json_response({'jsonrpc': '2.0', 'id': body['id'],
                                          'error': {'code': -32001, 'message': 'Task not found'}})
            assert body['method'] == 'message/send'
            assert state.probes == 1, 'Send before authenticated initialization'
            answer = state.accept(body)
            call.update(turn=state.turn, contextId=body['params']['message'].get('contextId'),
                        messageId=body['params']['message']['messageId'])
        except (AssertionError, KeyError, ValueError) as error:
            violations.append(str(error))
            return web.json_response({'jsonrpc': '2.0', 'id': body.get('id'),
                                      'error': {'code': -32602, 'message': str(error)}})
        await asyncio.sleep(.5)
        await held_response.wait()
        return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': {
            'id': f'{name}-task-{state.turn}', 'contextId': f'{name}-conversation',
            'status': {'state': 'TASK_STATE_COMPLETED'},
            'artifacts': [{'parts': [{'kind': 'text', 'text': answer}]}],
        }})

    app = web.Application()
    app.router.add_post('/{lane}', rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18981, ssl_context=server_tls).start()

    async def token():
        return 'simulator-only-token'

    agents = {name: A2ATerminalAgent(
        f'https://127.0.0.1:18981/{name}', token, name,
        transport=A2AHttpTransport(ssl_context=client_tls),
    ) for name in AGENTS}
    hud = HudWriter(gateway)

    async def relay(frame):
        await opening.paint(frame)

    async def paint(frame, request_id, selection_id):
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state, focus=frame.topic, lanes=frame.lanes,
            activity='\n'.join(frame.lines), approvals=frame.approvals, reason=frame.reason,
            opening_request_id=request_id, opening_selection_id=selection_id,
            conversation_id=frame.conversation_id, messages=frame.messages,
            history_truncated=frame.history_truncated, input_required=frame.input_required,
        ))

    roster = AgentTerminalRoster(
        list(agents.items()) + [(name, agents['OpenClaw']) for name in ('work', 'calendar', 'hermes session')],
        paint=relay, visible_lanes=AGENTS,
    )
    opening = TerminalOpeningController(roster, paint=paint,
                                        connection_generation=lambda: gateway.audio_connection_generation)
    router = CapabilityRouter(hud, gateway, StatusPublisher(out / 'status.json'), None, CardLog(),
                              terminal_rows=roster.picker_rows, terminal_busy=lambda: roster.busy,
                              terminal_current=lambda: roster.focused, terminal_open=opening.open)
    commands = AgentTerminalCommands(roster)

    async def capture():
        current = commands.begin_capture()
        assert current is not None
        assert await runtime.publish_terminal_capture(current)

    sessions = SessionManager(hud, lambda _: None, terminal_capture=capture,
                              terminal_view_current=lambda: opening.current)
    replies = MessageReplyController(NeverSender(), gateway.request_confirmation, contact_search=lambda _: [])
    runtime = PromptedSpeechRuntime(hud, replies, sessions=lambda: sessions,
                                    connection_generation=lambda: gateway.audio_connection_generation,
                                    fence_audio=lambda: 10, capturing=lambda: False, terminal=lambda: commands)
    gateway.on_input(router.on_input)
    gateway.on_device(runtime.handle_device)
    gateway.on_brainstorm_action(sessions.handle_brainstorm_action)
    connected = asyncio.Event()
    gateway.on_hello(lambda _: connected.set())

    async def input_action(action):
        await asyncio.to_thread(http, AUTOMATION + '/api/input', {'action': action})
        await asyncio.sleep(.7)

    def current_terminal():
        assert gateway.frames[-1]['kind'] == 'agent_terminal.view', gateway.frames[-1]
        return gateway.frames[-1]['terminal']

    async def screen(name, *, settle=2.5):
        await asyncio.sleep(settle)
        previous = None
        for _ in range(20):
            raw = await asyncio.to_thread(http, AUTOMATION + '/api/screenshot/glasses')
            visible = Image.open(io.BytesIO(raw)).crop((0, 30, 576, 288)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(.35)
        else:
            raise AssertionError('Simulator framebuffer did not settle')
        (out / f'{name}.rgba.png').write_bytes(raw)
        source = Image.open(io.BytesIO(raw)).convert('RGBA')
        optical = Image.new('RGBA', source.size, 'black')
        optical.alpha_composite(source)
        optical.convert('RGB').save(out / f'{name}.png')
        source.getchannel('A').resize((2304, 1152)).save(out / 'ocr-input.png')
        ocr = subprocess.run([str(out / 'ocr'), str(out / 'ocr-input.png')],
                             check=True, capture_output=True, text=True)
        text = '\n'.join(json.loads(ocr.stdout))
        receipt = {'name': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                   'text': text, 'last_frame': gateway.frames[-1] if gateway.frames else None}
        screens.append(receipt)
        print('SCREEN', name, repr(text), flush=True)
        return receipt

    def assert_picker():
        assert gateway.frames[-1]['kind'] == 'hud.list', gateway.frames[-1]
        assert [row.split(' · ')[0] for row in gateway.frames[-1]['items']] == list(AGENTS)

    def assert_conversation(name, turn):
        terminal = current_terminal()
        UUID(terminal['conversationId'])
        prior = conversation_ids.setdefault(name, terminal['conversationId'])
        assert prior == terminal['conversationId'], 'Conversation identity changed within visit'
        assert terminal['historyTruncated'] is False
        messages = terminal['messages']
        assert len({m['id'] for m in messages}) == len(messages), 'Duplicate conversation message identity'
        user = [m['text'] for m in messages if m['role'] == 'user']
        assistant = [m['text'] for m in messages if m['role'] == 'assistant']
        assert user == prompts(name)[:turn], 'Missing, duplicated, or reordered user turns'
        assert assistant == [m['text'] for m in states[name].history if m['role'] == 'agent'], 'Assistant reply was truncated or misplaced'
        other = 'Hermes' if name == 'OpenClaw' else 'OpenClaw'
        assert FACTS[other] not in json.dumps(messages), 'Other conversation is visible'
        remembered_messages[name] = messages

    async def begin_question(name, label):
        await input_action('click')
        await until(lambda: runtime.dispatcher.is_armed)
        ticket = runtime.dispatcher.current_ticket
        assert ticket is not None and ticket.connection_generation == gateway.audio_connection_generation
        frame = await screen(label)
        assert 'QUESTION' not in frame['text'].splitlines(), 'Input replaced the terminal with a standalone question'
        return ticket, frame

    async def submit(ticket, text):
        assert runtime.dispatcher.current_ticket == ticket, 'Question acknowledgment expired or changed'
        assert await runtime.dispatcher.dispatch(
            Transcript(text=text, is_final=True),
            UtteranceProvenance(source=ticket.source, connection_generation=ticket.connection_generation,
                                audio_epoch=ticket.prompt_id, first_ingress_ordinal=11,
                                last_ingress_ordinal=12, speaker_roles=frozenset()),
        )

    async def seek(name, marker, direction, label):
        needle = normalized(f'{name} {marker}')
        for step in range(26):
            frame = await screen(f'{label}-{step:02}', settle=.6)
            if needle in normalized(frame['text']):
                return frame
            await input_action(direction)
        raise AssertionError(f'{name} {marker} never became visible through simulator scrolling')

    def start(name, args):
        with (out / f'{name}.log').open('wb') as log:
            process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        processes.append(process)

    try:
        await gateway.start()
        env = dict(os.environ, VITE_MAC_WS_URL='ws://127.0.0.1:18980/glasses')
        with (out / 'build.log').open('w') as log:
            subprocess.run([str(ROOT / 'apps/hub-plugin/node_modules/.bin/vite'), 'build',
                            '--outDir', str(out / 'plugin'), '--emptyOutDir'],
                           cwd=ROOT / 'apps/hub-plugin', env=env, check=True,
                           stdout=log, stderr=subprocess.STDOUT)
        start('server', [sys.executable, '-m', 'http.server', '5298', '--bind', '127.0.0.1',
                         '--directory', str(out / 'plugin')])
        start('simulator', [str(Path.home() / '.local/bin/evenhub-simulator'),
                            'http://127.0.0.1:5298', '--automation-port', '9998',
                            '--aid', 'coreaudio:BuiltInMicrophoneDevice'])
        await asyncio.wait_for(connected.wait(), 45)
        await asyncio.sleep(3)
        await screen('00-home')
        await input_action('context_menu')
        await screen('01-feature-menu')
        for _ in range(5):
            await input_action('down')
        await screen('02-agent-terminal-selected')
        await input_action('click')
        await screen('03-agent-picker')
        assert_picker()
        for index, name in enumerate(AGENTS):
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name and agents[name].capture_context_generation is not None)
            await screen(f'{name}-conversation')
            for turn in range(3):
                ticket, question = await begin_question(name, f'{name}-turn-{turn+1}-input')
                if turn == 1:
                    assert normalized(f'{name}: I will remember') in normalized(question['text']), 'Follow-up input hid the prior assistant reply'
                    assert normalized(FACTS[name]) in normalized(question['text']), 'Follow-up input lost the remembered fact'
                await submit(ticket, prompts(name)[turn])
                await until(lambda: not roster.busy and gateway.frames[-1].get('kind') == 'agent_terminal.view')
                assert_conversation(name, turn + 1)
                await screen(f'{name}-turn-{turn+1}-reply')
            await seek(name, 'BEGIN', 'up', f'{name}-long-begin')
            anchor = await seek(name, 'MIDDLE', 'down', f'{name}-long-middle')
            await input_action('double_click')
            await screen(f'{name}-back-picker')
            assert_picker()
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name and gateway.frames[-1].get('kind') == 'agent_terminal.view')
            reopened = await screen(f'{name}-reopened-middle')
            assert_conversation(name, 3)
            assert normalized(f'{name} MIDDLE') in normalized(reopened['text']), 'Reopening lost the reading position'
            await seek(name, 'END', 'down', f'{name}-long-end')
            ticket, _ = await begin_question(name, f'{name}-cancel-input')
            await input_action('double_click')
            await screen(f'{name}-cancelled')
            assert not runtime.dispatcher.is_armed
            assert_conversation(name, 3)
            await input_action('double_click')
            await screen(f'{name}-final-picker')
            assert_picker()
        assert conversation_ids['OpenClaw'] != conversation_ids['Hermes']
        # Hold a fourth OpenClaw response: busy entry and updates while reading older text.
        await input_action('click')
        await until(lambda: roster.focused == 'OpenClaw' and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        await screen('OpenClaw-before-held-turn')
        ticket, _ = await begin_question('OpenClaw', 'OpenClaw-held-input')
        held_response.clear()
        pending = asyncio.create_task(submit(ticket, prompts('OpenClaw')[3]))
        await until(lambda: roster.busy)
        await input_action('context_menu')
        for _ in range(5):
            await input_action('down')
        await input_action('click')
        await screen('busy-agent-picker')
        assert_picker()
        await input_action('click')
        await until(lambda: roster.focused == 'OpenClaw' and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        held_frame = await seek('OpenClaw', 'BEGIN', 'up', 'OpenClaw-held-reading-begin')
        assert 'WORKING' in held_frame['text'], 'Running conversation must say WORKING'
        assert 'LIVE' not in held_frame['text'].splitlines(), 'Legacy LIVE state label remains'
        held_response.set()
        await pending
        await until(lambda: not roster.busy)
        update = await screen('OpenClaw-new-output-while-reading')
        assert_conversation('OpenClaw', 4)
        assert normalized('OpenClaw BEGIN') in normalized(update['text']), 'New output displaced the reading position'
        await seek('OpenClaw', 'UPDATE', 'down', 'OpenClaw-read-new-output')
        assert not violations, violations
        assert [states[name].turn for name in AGENTS] == [4, 3]
        assert [states[name].probes for name in AGENTS] == [1, 1]
        assert len(calls) == 9, calls
        assert source_fingerprint() == source_before, 'Production sources changed during simulator acceptance'
        assert len({call['id'] for call in calls}) == 9, 'JSON-RPC request identity repeated'
        (out / 'receipt.json').write_text(json.dumps({
            'result': 'passed', 'screens': screens, 'rpc_calls': calls,
            'conversation_ids': conversation_ids,
            'source_fingerprint': source_before,
            'stable_evidence': verify_stable_evidence(out, gateway.frames),
            'boundaries': ['Stateful local TLS providers replace remote agents',
                           'Final STT text supplied after simulator prompt acknowledgment'],
            'isolation': {'engine_port': 18980, 'agent_port': 18981,
                          'plugin_port': 5298, 'simulator_port': 9998},
        }, indent=2))
    finally:
        held_response.set()
        (out / 'observed.json').write_text(json.dumps({
            'screens': screens, 'rpc_calls': calls, 'frames': gateway.frames,
            'provider_violations': violations,
        }, indent=2))
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, 15)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, 9)
                    process.wait()
        await runtime.aclose()
        await sessions.close()
        await gateway.stop()
        for agent in agents.values():
            await agent.close()
        await runner.cleanup()
        key.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'dist/mac-workspace/control-audit/agent-terminal-persistent')
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
