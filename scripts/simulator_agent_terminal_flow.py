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
import tempfile
import urllib.request
from uuid import UUID, uuid4

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
from joat.persistence.store import DurableStore
from joat.persistence.terminal import TerminalCheckpointStore
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController
from scripts.simulator_harness import (
    SimulatorFlowError,
    _prepare_simulator_plugin_root,
)

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION = 'http://127.0.0.1:9998'
BRIDGE_MARKER = '[agent-terminal-native-bridge]'
AGENTS = ('OpenClaw', 'Hermes')
FACTS = {'OpenClaw': 'cobalt beacon', 'Hermes': 'amber compass'}
_CONSOLE_LAST_ID: int | None = None
_BRIDGE_LEDGER: list[dict] = []
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
class StreamingRestartState:
    """Reject replay; expose a controllable fragmented v1 SSE task and polling recovery."""
    name: str
    probes: int = 0
    sends: int = 0
    polls: int = 0
    phase: int = 0
    task_id: str = ''
    identifiers: set[str] = field(default_factory=set)
    final_release: asyncio.Event = field(default_factory=asyncio.Event)
    replacement_release: asyncio.Event = field(default_factory=asyncio.Event)
    append_release: asyncio.Event = field(default_factory=asyncio.Event)
    stream_closed: asyncio.Event = field(default_factory=asyncio.Event)
    auth_complete: bool = False

    def snapshot(self):
        state = 'working'
        text = f'{self.name.upper()} PARTIAL. Accepted work survives an engine restart.'
        if self.sends == 3:
            state = 'completed' if self.auth_complete else 'auth-required'
            text = f'{self.name.upper()} OPERATOR COMPLETED.' if self.auth_complete else ''
        elif self.phase >= 3:
            state, text = 'completed', long_reply(self.name)
        return {'id': self.task_id, 'contextId': f'{self.name}-stream-context',
                'status': {'state': state, 'message': {'role': 'agent', 'parts': [{'kind': 'text', 'text': 'Provider authorization needed.' if state == 'auth-required' else 'Working on the plan.'}]}},
                'artifacts': [{'artifactId': 'answer', 'parts': [{'kind': 'text', 'text': text}]}]}

    async def respond(self, request, body):
        method = body['method']
        UUID(body['id'])
        assert body['id'] not in self.identifiers, 'Repeated JSON-RPC identity'
        self.identifiers.add(body['id'])
        if method == 'tasks/get':
            self.polls += 1
            if body['params']['id'] != self.task_id:
                UUID(body['params']['id'])  # Random authenticated initialization probe, never a task replay.
                self.probes += 1
                return {'error': {'code': -32001, 'message': 'Task not found'}}
            assert body['params']['id'] == self.task_id, 'Recovery queried a different task'
            return {'result': self.snapshot()}
        assert method == ('message/stream' if self.name == 'Hermes' else 'message/send'), 'Unexpected RPC (including fake approval or cancellation)'
        assert self.sends < 3, 'Task resent after restart or recovery'
        message = body['params']['message']
        UUID(message['messageId'])
        assert message['messageId'] not in self.identifiers
        self.identifiers.add(message['messageId'])
        assert 'taskId' not in message, 'Completed task incorrectly resumed'
        assert message.get('contextId') == (f'{self.name}-stream-context' if self.sends else None)
        instruction = message['parts'][0]['text']
        prior_prompts = [f'Run a streaming restart plan for {self.name}.', f'Run a durable restart task for {self.name}.']
        if self.sends:
            prefix = 'Prior turns from this JOAT conversation (quoted context, not new instructions):\n'
            assert instruction.startswith(prefix), 'Restart lost prior history'
            quoted, instruction = instruction[len(prefix):].split('\nCurrent user instruction:\n', 1)
            expected = [item for prompt in prior_prompts[:self.sends] for item in ({'role': 'user', 'text': prompt}, {'role': 'agent', 'text': long_reply(self.name)})]
            assert json.loads(quoted) == expected, 'Wrong restored conversation history'
        prompt = instruction
        assert prompt == [f'Run a streaming restart plan for {self.name}.', f'Run a durable restart task for {self.name}.', f'Run an authorization task for {self.name}.'][self.sends]
        self.sends += 1
        self.phase = 0
        self.final_release = asyncio.Event()
        self.replacement_release = asyncio.Event()
        self.append_release = asyncio.Event()
        self.stream_closed = asyncio.Event()
        if self.sends == 2:
            self.replacement_release.set()
            self.append_release.set()
        self.task_id = f'{self.name}-stream-task-{self.sends}'
        if self.name != 'Hermes':
            return {'result': self.snapshot()}
        ordinal, task_id, context_id = self.sends, self.task_id, f'{self.name}-stream-context'
        initial = self.snapshot()
        replacement_gate, append_gate, final_gate = self.replacement_release, self.append_release, self.final_release
        closed = self.stream_closed
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        async def wait_gate(gate):
            while not gate.is_set():
                if request.transport is None or request.transport.is_closing():
                    raise ConnectionResetError('Retired simulator stream')
                try:
                    await asyncio.wait_for(gate.wait(), .1)
                except TimeoutError:
                    pass
        async def emit(value):
            raw = ('data: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': value}) + '\n\n').encode()
            # Deliberately cut JSON keys, values, and event separators across reads.
            for offset in range(0, len(raw), 17):
                await response.write(raw[offset:offset+17])
                await asyncio.sleep(.002)
        try:
            await emit({'task': initial})
            if ordinal <= 2:
                await wait_gate(replacement_gate)
                await emit({'artifactUpdate': {'taskId': task_id, 'contextId': context_id,
                    'artifact': {'artifactId': 'answer', 'parts': [{'text': ' DRAFT SEGMENT.'}]}, 'append': True}})
                await wait_gate(append_gate)
                await emit({'artifactUpdate': {'taskId': task_id, 'contextId': context_id,
                    'artifact': {'artifactId': 'answer', 'parts': [{'text': long_reply(self.name)}]}, 'append': False}})
                if self.task_id == task_id:
                    self.phase = 1
                await wait_gate(final_gate)
                if self.task_id == task_id:
                    self.phase = 3
                await emit({'statusUpdate': {'taskId': task_id, 'contextId': context_id,
                    'status': {'state': 'TASK_STATE_COMPLETED'}}})
            await response.write(b': done\n\n')
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            closed.set()
        return response


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


@dataclass
class LifecycleState:
    """Stateful provider for polling, same-task input, and stop outcomes."""
    name: str
    probes: int = 0
    accepted: list[str] = field(default_factory=list)
    message_ids: set[str] = field(default_factory=set)
    scenario: str = ""
    phase: int = 0
    task_id: str = ""
    resumed: bool = False
    cancelled: bool = False
    polls: list[tuple[str, int]] = field(default_factory=list)
    cancel_count: int = 0
    cancel_started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_release: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def context(self):
        return f'{self.name}-lifecycle-conversation'

    @property
    def question(self):
        return f'{self.name.upper()} QUESTION: choose cobalt or amber.'

    def snapshot(self):
        status, text, artifacts = 'working', f'{self.name.upper()} RUNNING task.', []
        if self.scenario == 'progress':
            status = ('submitted', 'working', 'working', 'completed')[self.phase]
            text = (f'{self.name.upper()} QUEUED task.', f'{self.name.upper()} SCANNING sources.',
                    f'{self.name.upper()} CHECKING results.', f'{self.name.upper()} FINISHED task.')[self.phase]
            if self.phase == 3:
                artifacts = [f'{self.name.upper()} COMPLETED staged task.']
        elif self.scenario == 'input':
            status = 'completed' if self.resumed else 'input-required'
            text = f'{self.name.upper()} REPLY accepted.' if self.resumed else self.question
            artifacts = [f'{self.name.upper()} RESUMED SAME TASK with cobalt.'] if self.resumed else [f'{self.name.upper()} DRAFT proposal.']
        elif self.cancelled:
            status, text = 'canceled', f'{self.name.upper()} STOP CONFIRMED.'
        return {'id': self.task_id, 'contextId': self.context,
                'status': {'state': status, 'message': {'role': 'agent', 'parts': [{'kind': 'text', 'text': text}]}},
                'artifacts': [{'parts': [{'kind': 'text', 'text': text}]} for text in artifacts]}

    async def respond(self, body):
        method = body['method']
        if method == 'tasks/get':
            requested = body['params']['id']
            if not self.task_id:
                assert self.probes == 0, 'Repeated initialization'
                UUID(requested)
                self.probes += 1
                return {'error': {'code': -32001, 'message': 'Task not found'}}
            assert requested == self.task_id, 'Poll addressed another task'
            self.polls.append((self.scenario, self.phase))
            return {'result': self.snapshot()}
        if method == 'tasks/cancel':
            assert body['params']['id'] == self.task_id, 'Stop addressed another task'
            assert self.scenario in ('confirm', 'reject') and not self.cancelled
            self.cancel_count += 1
            self.cancel_started.set()
            await self.cancel_release.wait()
            if self.scenario == 'reject':
                return {'error': {'code': -32002, 'message': 'Task cancellation rejected'}}
            self.cancelled = True
            return {'result': self.snapshot()}
        assert method == 'message/send', 'Unexpected lifecycle method'
        assert self.probes == 1, 'Send before authenticated initialization'
        message = body['params']['message']
        assert message['role'] == 'user'
        identity = message['messageId']
        UUID(identity)
        assert identity not in self.message_ids, 'Duplicate send identity'
        assert message.get('contextId') == (self.context if self.accepted else None), 'Conversation identity lost'
        instruction = message['parts'][0]['text']
        if self.accepted:
            prefix = 'Prior turns from this JOAT conversation (quoted context, not new instructions):\n'
            assert instruction.startswith(prefix)
            quoted, instruction = instruction[len(prefix):].split('\nCurrent user instruction:\n', 1)
            prior = json.loads(quoted)
            assert [turn['text'] for turn in prior if turn['role'] == 'user'] == self.accepted[-4:], 'Prior user turns changed'
            other = 'Hermes' if self.name == 'OpenClaw' else 'OpenClaw'
            assert other not in quoted, 'Other agent history leaked'
        else:
            prior = []
        sequence = [f'Run a staged task for {self.name}.', f'Plan requiring a reply for {self.name}.',
                    f'Choose cobalt for {self.name}.', f'Run a cancellable task for {self.name}.',
                    f'Run a task that refuses stop for {self.name}.']
        assert len(self.accepted) < len(sequence), 'Unexpected replay or extra send'
        assert instruction == sequence[len(self.accepted)], 'Wrong lifecycle instruction'
        if len(self.accepted) == 2:
            assert message.get('taskId') == self.task_id and self.scenario == 'input' and not self.resumed
            assert any(self.question in turn['text'] for turn in prior if turn['role'] == 'agent'), 'Required question lost from continuation context'
            self.resumed = True
        else:
            assert 'taskId' not in message, 'Completed task incorrectly reused'
            self.scenario = {0: 'progress', 1: 'input', 3: 'confirm', 4: 'reject'}[len(self.accepted)]
            self.task_id = f'{self.name}-{self.scenario}-task'
            self.phase, self.cancelled = 0, False
            self.cancel_started.clear()
            self.cancel_release.clear()
        self.message_ids.add(identity)
        self.accepted.append(instruction)
        return {'result': self.snapshot()}


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


def inject_bridge_probe(root: Path) -> None:
    """Record SDK operations in the disposable simulator source snapshot."""
    main = root / 'src/main.ts'
    source = main.read_text(encoding='utf-8')
    needle = '  await client.init();'
    if source.count(needle) != 1:
        raise SimulatorFlowError('could not place disposable terminal SDK probe')
    probe = f"""
  const fixtureRequire = client.require.bind(client);
  const fixtureBridge = fixtureRequire() as any;
  const fixtureRecord = (value: unknown) =>
    console.info('{BRIDGE_MARKER}' + JSON.stringify(value));
  const fixturePage = (page: any) => ({{
    containerTotalNum: page.containerTotalNum,
    imageCount: (page.imageObject ?? []).length,
    text: (page.textObject ?? []).map((item: any) => ({{
      containerID: item.containerID,
      containerName: item.containerName,
      isEventCapture: item.isEventCapture,
      content: item.content,
    }})),
    list: (page.listObject ?? []).map((item: any) => ({{
      containerID: item.containerID,
      containerName: item.containerName,
      isEventCapture: item.isEventCapture,
      itemNames: item.itemContainer?.itemName ?? [],
    }})),
    menuCount: (page.menuObject?.menuItems ?? []).length,
  }});
  const fixtureStartup = async (page: any) => {{
    fixtureRecord({{ operation: 'startup-start', atUnixMs: Date.now(), ...fixturePage(page) }});
    const result = await fixtureBridge.createStartUpPageContainer(page);
    fixtureRecord({{ operation: 'startup', acceptedAtUnixMs: Date.now(), result: String(result), ...fixturePage(page) }});
    return result;
  }};
  const fixtureRebuild = async (page: any) => {{
    fixtureRecord({{ operation: 'rebuild-start', atUnixMs: Date.now(), ...fixturePage(page) }});
    const result = await fixtureBridge.rebuildPageContainer(page);
    fixtureRecord({{ operation: 'rebuild', acceptedAtUnixMs: Date.now(), result: String(result), ...fixturePage(page) }});
    return result;
  }};
  const fixtureImage = async (update: any) => {{
    fixtureRecord({{ operation: 'image-start', atUnixMs: Date.now(), containerID: update.containerID }});
    const result = await fixtureBridge.updateImageRawData(update);
    fixtureRecord({{ operation: 'image', acceptedAtUnixMs: Date.now(), result: String(result), containerID: update.containerID }});
    return result;
  }};
  const fixtureText = async (update: any) => {{
    fixtureRecord({{
      operation: 'text-start', atUnixMs: Date.now(),
      containerID: update.containerID, containerName: update.containerName,
      content: update.content, contentLength: update.contentLength,
    }});
    const result = await fixtureBridge.textContainerUpgrade(update);
    fixtureRecord({{
      operation: 'text', acceptedAtUnixMs: Date.now(), result: String(result),
      containerID: update.containerID, containerName: update.containerName,
      content: update.content, contentLength: update.contentLength,
    }});
    return result;
  }};
  const fixtureObservedBridge = new Proxy(fixtureBridge, {{
    get(target, property) {{
      if (property === 'createStartUpPageContainer') return fixtureStartup;
      if (property === 'rebuildPageContainer') return fixtureRebuild;
      if (property === 'updateImageRawData') return fixtureImage;
      if (property === 'textContainerUpgrade') return fixtureText;
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    }},
  }});
  (client as any).require = () => fixtureObservedBridge;
"""
    main.write_text(source.replace(needle, f'{needle}{probe}'), encoding='utf-8')


async def console_payloads() -> list[dict]:
    global _CONSOLE_LAST_ID
    url = AUTOMATION + '/api/console'
    if _CONSOLE_LAST_ID is not None:
        url += f'?since_id={_CONSOLE_LAST_ID}'
    raw = await asyncio.to_thread(http, url)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError('Simulator returned invalid console JSON') from exc
    entries = decoded.get('entries') if isinstance(decoded, dict) else None
    assert isinstance(entries, list), 'Simulator console has no entries'
    ids = [entry.get('id') for entry in entries if isinstance(entry, dict)
           and isinstance(entry.get('id'), int)]
    if ids:
        _CONSOLE_LAST_ID = max(ids)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('message'), str):
            continue
        message = entry['message']
        offset = message.find(BRIDGE_MARKER)
        if offset < 0:
            continue
        payload = json.loads(message[offset + len(BRIDGE_MARKER):].strip())
        if isinstance(payload, dict):
            _BRIDGE_LEDGER.append(payload)
    return list(_BRIDGE_LEDGER)


async def bridge_event(after: int, operations: set[str], predicate, timeout=12) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            for event in (await console_payloads())[after:]:
                if event.get('operation') in operations and predicate(event):
                    return event
            await asyncio.sleep(.04)


def verify_stable_evidence(out: Path, frames: list[dict]) -> dict:
    """Check stable wire identities and the rendered native terminal body."""
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
        # Exclude the changing header and interaction footer; compare every
        # transcript pixel inside the native body container.
        left = Image.open(out / (before + '.rgba.png')).crop((0, 82, 576, 251)).tobytes()
        right = Image.open(out / (after + '.rgba.png')).crop((0, 82, 576, 251)).tobytes()
        assert left == right, f'Reading position changed between {before} and {after}'
        matches.append({'before': before, 'after': after, 'sha256': hashlib.sha256(left).hexdigest()})
    return {'stable_message_identities': len(seen), 'exact_native_body_pixel_matches': matches}


def source_fingerprint() -> dict[str, str]:
    files = set((ROOT / 'apps/hub-plugin/src').rglob('*'))
    files.update((ROOT / 'apps/mac/joat').rglob('*.py'))
    files.update(ROOT / 'apps/hub-plugin' / name for name in
                 ('index.html', 'package.json', 'package-lock.json', 'vite.config.ts'))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files) if path.is_file()}


async def run(out: Path, *, lifecycle: bool = False, streaming_restart: bool = False):
    global _CONSOLE_LAST_ID
    _CONSOLE_LAST_ID = None
    _BRIDGE_LEDGER.clear()
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
    states = {name: (StreamingRestartState(name) if streaming_restart else LifecycleState(name) if lifecycle else AgentState(name)) for name in AGENTS}
    durable = None
    checkpoint_path = out / f'terminal-checkpoints-{uuid4()}.sqlite3'
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
            if streaming_restart:
                result = await state.respond(request, body)
                call.update(taskId=state.task_id, sends=state.sends)
                if isinstance(result, web.StreamResponse):
                    return result
                return web.json_response({'jsonrpc': '2.0', 'id': body['id'], **result})
            if lifecycle:
                result = await state.respond(body)
                call.update(taskId=state.task_id, scenario=state.scenario, phase=state.phase)
                return web.json_response({'jsonrpc': '2.0', 'id': body['id'], **result})
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

    agents = hud = roster = opening = router = commands = sessions = runtime = durable = None

    def create_engine():
        nonlocal agents, hud, roster, opening, router, commands, sessions, runtime, durable
        agents = {name: A2ATerminalAgent(
            f'https://127.0.0.1:18981/{name}', token, name,
            transport=A2AHttpTransport(ssl_context=client_tls), poll_interval_s=.35 if lifecycle or streaming_restart else 1,
            streaming=streaming_restart and name == "Hermes", provider=name.lower() if streaming_restart else "generic",
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
                history_truncated=frame.history_truncated, input_required=frame.input_required, task_recovery=frame.task_recovery,
            ))

        roster = AgentTerminalRoster(
            list(agents.items()) + [(name, agents['OpenClaw']) for name in ('work', 'calendar', 'hermes session')],
            paint=relay, visible_lanes=AGENTS,
        )
        if streaming_restart:
            durable = DurableStore(checkpoint_path)
            repository = TerminalCheckpointStore(durable)
            for name, agent in agents.items():
                assert roster.bind_persistence(name, repository, name.lower(), agent.endpoint, provider=name.lower())
        opening = TerminalOpeningController(roster, paint=paint,
                                            connection_generation=lambda: gateway.audio_connection_generation)
        router = CapabilityRouter(hud, gateway, StatusPublisher(out / 'status.json'), None, CardLog(),
                                  terminal_rows=roster.picker_rows, terminal_busy=lambda: roster.busy,
                                  terminal_current=lambda: roster.focused, terminal_open=opening.open)
        commands = AgentTerminalCommands(roster, on_explicit_view=lambda: opening.claim_legacy(router.claim_terminal_voice))

        async def capture():
            current = commands.begin_capture()
            assert current is not None
            assert await runtime.publish_terminal_capture(current)

        async def retry_terminal():
            await opening.retry()

        sessions = SessionManager(hud, lambda _: None, terminal_capture=capture,
                                  terminal_retry=retry_terminal, terminal_view_current=lambda: opening.current)
        replies = MessageReplyController(NeverSender(), gateway.request_confirmation, contact_search=lambda _: [])
        runtime = PromptedSpeechRuntime(hud, replies, sessions=lambda: sessions,
                                        connection_generation=lambda: gateway.audio_connection_generation,
                                        fence_audio=lambda: 10, capturing=lambda: False, terminal=lambda: commands)

    create_engine()
    # The fixture keeps its isolated socket alive across engine reconstruction.
    # Register once and route to the current runtime; retired callbacks would
    # otherwise keep writing to the closed store and handle each gesture twice.
    gateway.on_input(lambda event: router.on_input(event))
    gateway.on_device(lambda event: runtime.handle_device(event))
    gateway.on_brainstorm_action(lambda event: sessions.handle_brainstorm_action(event))
    connected = asyncio.Event()
    gateway.on_hello(lambda _: connected.set())

    async def input_action(action):
        await asyncio.to_thread(http, AUTOMATION + '/api/input', {'action': action})
        await asyncio.sleep(.7)

    async def input_now(action):
        await asyncio.to_thread(http, AUTOMATION + '/api/input', {'action': action})

    def current_terminal():
        assert gateway.frames[-1]['kind'] == 'agent_terminal.view', gateway.frames[-1]
        return gateway.frames[-1]['terminal']

    def store_screen(name, raw):
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

    async def immediate_screen(name):
        """Capture once at the action boundary, before stable-frame polling."""
        raw = await asyncio.to_thread(http, AUTOMATION + '/api/screenshot/glasses')
        return store_screen(name, raw)

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
        return store_screen(name, raw)

    def assert_picker():
        assert gateway.frames[-1]['kind'] == 'hud.list', gateway.frames[-1]
        assert [row.split(' · ')[0] for row in gateway.frames[-1]['items']] == list(AGENTS)

    def is_picker_page(event):
        text = event.get('text')
        lists = event.get('list')
        return (event.get('operation') == 'rebuild'
                and event.get('containerTotalNum') == 4
                and event.get('imageCount') == 0
                and isinstance(text, list) and len(text) == 3
                and isinstance(lists, list) and len(lists) == 1
                and lists[0].get('containerName') == 'list'
                and lists[0].get('isEventCapture') == 1
                and [str(item).split(' · ')[0] for item in lists[0].get('itemNames', [])] == list(AGENTS)
                and sum(item.get('isEventCapture') == 1 for item in text) == 0)

    def is_terminal_page(event):
        text = event.get('text')
        if not (event.get('operation') == 'rebuild' and event.get('list') == [] and isinstance(text, list)):
            return False
        texts = [(item.get('containerID'), item.get('containerName'), item.get('isEventCapture'))
                 for item in text]
        # The head is the StatusBar line; where the image spine can be drawn
        # (HeaderImage.ts) it is swapped for the two header halves.
        return ((texts == [(1, 'terminal-head', 0), (14, 'terminal-body', 1)]
                 and event.get('containerTotalNum') == 2 and event.get('imageCount') == 0)
                or (texts == [(14, 'terminal-body', 1)]
                    and event.get('containerTotalNum') == 3 and event.get('imageCount') == 2))

    def is_visual_page(event):
        return (event.get('operation') in {'startup', 'rebuild'}
                and event.get('containerTotalNum') == 5
                and event.get('imageCount') == 4
                and event.get('list') == [])

    async def assert_picker_bridge(after, label, *, exactly_one=False):
        page = await bridge_event(after, {'rebuild'}, is_picker_page)
        events = (await console_payloads())[after:]
        completed = [event for event in events if event.get('operation') in {'rebuild', 'text', 'image'}]
        picker_pages = [event for event in completed if is_picker_page(event)]
        if exactly_one:
            assert len(picker_pages) == 1, f'{label}: expected one picker rebuild, got {len(picker_pages)}'
        assert not [event for event in completed if event.get('operation') == 'image'], f'{label}: picker wrote raster images'
        return page

    async def assert_terminal_entry(after, label):
        page = await bridge_event(after, {'rebuild'}, is_terminal_page)
        events = (await console_payloads())[after:]
        pages = [event for event in events if is_terminal_page(event)]
        assert len(pages) == 1, f'{label}: expected one terminal entry rebuild, got {len(pages)}'
        # Only the header halves (HeaderImageId 16/17) may be images.
        assert not [event for event in events if event.get('operation') == 'image'
                    and event.get('containerID') not in (16, 17)], f'{label}: entry wrote raster images'
        head = next((item for item in page['text'] if item.get('containerName') == 'terminal-head'), None)
        assert head is None or 'AGENT TERMINAL' in head['content']
        return page

    async def assert_terminal_visit(after, label):
        events = (await console_payloads())[after:]
        completed = [event for event in events if event.get('operation') in {'rebuild', 'text', 'image'}]
        rebuilds = [event for event in completed if event.get('operation') == 'rebuild']
        images = [event for event in completed if event.get('operation') == 'image'
                  and event.get('containerID') not in (16, 17)]
        upgrades = [event for event in completed if event.get('operation') == 'text']
        assert len(rebuilds) == 1 and is_terminal_page(rebuilds[0]), \
            f'{label}: terminal visit performed {len(rebuilds)} rebuilds instead of one entry rebuild'
        assert not images, f'{label}: active terminal performed raster image writes'
        assert upgrades, f'{label}: active terminal performed no native text upgrades'
        assert all((event.get('containerID'), event.get('containerName')) == (14, 'terminal-body')
                   for event in upgrades), f'{label}: active terminal updated outside terminal-body'
        assert all(event.get('result') in {'true', 'undefined'} for event in upgrades), f'{label}: SDK rejected a body upgrade'
        return upgrades

    async def body_event(after, *, changed_from=None):
        return await bridge_event(
            after,
            {'text'},
            lambda event: (event.get('containerID'), event.get('containerName')) == (14, 'terminal-body')
            and (changed_from is None or event.get('content') != changed_from),
        )

    async def assert_native_swipe_round_trip(name, label):
        events = await console_payloads()
        prior = next((event for event in reversed(events)
                      if event.get('operation') in {'text', 'rebuild'}
                      and ((event.get('containerID'), event.get('containerName')) == (14, 'terminal-body')
                           or is_terminal_page(event))), None)
        assert prior is not None, f'{label}: terminal has no body receipt'
        before_text = (prior.get('content') if prior.get('operation') == 'text'
                       else next(item for item in prior['text'] if item.get('containerName') == 'terminal-body')['content'])
        before_footer = before_text.splitlines()[-1].lower()
        assert 'up: older' in before_footer, f'{label}: scrollable tail did not advertise older content'
        assert 'down: newer' not in before_footer, f'{label}: tail advertised a false newer direction'
        before = await screen(f'{label}-tail-before-swipe', settle=.2)
        boundary = len(events)
        await input_action('up')
        moved = await body_event(boundary, changed_from=before_text)
        middle = await screen(f'{label}-after-up', settle=.2)
        assert moved['content'] != before_text, f'{label}: up swipe did not move native content'
        assert 'down: newer' in moved['content'].splitlines()[-1].lower(), f'{label}: moved view omitted the valid newer direction'
        before_pixels = Image.open(out / f"{before['name']}.rgba.png").crop((0, 35, 576, 288)).tobytes()
        middle_pixels = Image.open(out / f"{middle['name']}.rgba.png").crop((0, 35, 576, 288)).tobytes()
        assert before_pixels != middle_pixels, f'{label}: up swipe changed the model but not the framebuffer'
        boundary = len(await console_payloads())
        await input_action('down')
        restored = await body_event(boundary, changed_from=moved['content'])
        after = await screen(f'{label}-after-down', settle=.2)
        assert restored['content'] == before_text, f'{label}: down swipe did not restore the terminal tail'
        after_pixels = Image.open(out / f"{after['name']}.rgba.png").crop((0, 35, 576, 288)).tobytes()
        assert after_pixels == before_pixels, f'{label}: swipe round trip did not restore terminal pixels'
        return {'agent': name, 'upBodyChanged': True, 'roundTripBodyRestored': True,
                'tailHint': before_footer, 'middleHint': moved['content'].splitlines()[-1].lower()}

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

    async def streaming_restart_flow():
        def visible(text):
            return (gateway.frames[-1].get('kind') == 'agent_terminal.view'
                    and any(text in m['text'] for m in current_terminal()['messages']))
        async def reopen(name):
            # Context menu entry drives the production chooser, even after engine replacement.
            await input_action('context_menu')
            await screen(f'{name}-restart-feature-menu')
            for _ in range(5):
                await input_action('down')
            await input_action('click')
            await until(lambda: gateway.frames[-1].get('kind') == 'hud.list'
                        and gateway.frames[-1].get('title') == 'AGENT TERMINAL')
            await screen(f'{name}-restart-picker')
            if name == 'Hermes':
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        for index, name in enumerate(AGENTS):
            state = states[name]
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: agents[name].capture_context_generation is not None)
            await until(lambda: gateway.frames[-1].get('kind') == 'agent_terminal.view'
                        and current_terminal().get('state') == 'ready')
            await screen(f'{name}-stream-opened')
            identity = current_terminal()['conversationId']
            ticket, _ = await begin_question(name, f'{name}-stream-input')
            await submit(ticket, f'Run a streaming restart plan for {name}.')
            if name == 'Hermes':
                await until(lambda: visible(f'{name.upper()} PARTIAL'))
                partial = await screen(f'{name}-partial-before-final')
                assert 'PARTIAL' in partial['text'] and roster.busy, 'No visible partial result while stream remains active'
            else:
                await until(lambda: visible('Working on the plan.'))
                partial = await screen(f'{name}-polling-working-before-final')
                assert 'WORKING' in partial['text'].upper() and roster.busy, 'Polling task has no visible working status'
            anchor = partial
            if name == 'Hermes':
                state.replacement_release.set()
                await until(lambda: visible('DRAFT SEGMENT'))
                appended = await screen(f'{name}-stream-appended')
                assert 'DRAFT SEGMENT' in appended['text'] and 'PARTIAL' in appended['text']
                state.append_release.set()
                await until(lambda: state.phase == 1)
                anchor = await seek(name, 'MIDDLE', 'up', f'{name}-stream-middle')
                assert 'DRAFT SEGMENT' not in json.dumps(current_terminal()['messages']), 'Replacement retained obsolete artifact content'
            before = current_terminal()
            await input_action('double_click')
            await until(lambda: gateway.frames[-1].get('kind') == 'hud.list')
            await screen(f'{name}-picker-{state.sends}')
            assert_picker()
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name)
            reopened = await screen(f'{name}-stream-back-reopen')
            assert current_terminal()['conversationId'] == identity
            assert current_terminal()['messages'] == before['messages'], 'Back lost streamed history'
            def transcript_pixels(capture):
                return Image.open(out / (capture['name'] + '.rgba.png')).crop((0, 82, 576, 251)).tobytes()
            assert transcript_pixels(anchor) == transcript_pixels(reopened), 'Back moved the streamed reading anchor'
            # Finish the first task through its original SSE stream (or polling).
            state.phase = 3
            state.final_release.set()
            await until(lambda: not roster.busy and visible(f'{name.upper()} FINISHED'))
            await seek(name, 'FINISHED', 'down', f'{name}-stream-final')
            ticket, _ = await begin_question(name, f'{name}-durable-input')
            await submit(ticket, f'Run a durable restart task for {name}.')
            await until(lambda: roster.busy and state.sends == 2
                        and agents[name].export_state().get('task') == state.task_id
                        and agents[name].export_state().get('dispatch_phase') == 'accepted')
            saved = TerminalCheckpointStore(durable).load(name.lower(), agents[name].endpoint)
            assert saved is not None and saved['adapter']['task'] == state.task_id
            assert saved['adapter']['dispatch_phase'] == 'accepted', 'Client has not durably accepted the task'
            await screen(f'{name}-durable-before-restart')
            # Discard engine ownership and recreate every runtime object from the same durable store.
            accepted = len([c for c in calls if c['method'].startswith('message/')])
            task_id = state.task_id
            await roster.detach_for_shutdown()
            await runtime.aclose()
            await sessions.close()
            for agent in agents.values():
                await agent.close()
            if name == 'Hermes':
                await asyncio.wait_for(state.stream_closed.wait(), 5)
            durable.close()
            create_engine()
            await reopen(name)
            restored = await screen(f'{name}-restart-restored')
            assert current_terminal()['conversationId'] == identity
            assert any(m['role'] == 'user' for m in current_terminal()['messages'])
            assert not runtime.dispatcher.is_armed
            assert len([c for c in calls if c['method'].startswith('message/')]) == accepted
            assert not any(c['method'] == 'tasks/cancel' for c in calls)
            previous_polls = state.polls
            state.phase = 3
            await input_action('click')
            await until(lambda: state.polls > previous_polls and visible(f'{name.upper()} FINISHED'))
            await seek(name, 'FINISHED', 'down', f'{name}-restart-recovered-final')
            latest_user = max(i for i, m in enumerate(current_terminal()['messages']) if m['role'] == 'user')
            assert all(m['text'] == 'Completed' for m in current_terminal()['messages'][latest_user + 1:] if m['role'] == 'status'), 'Recovery retained stale progress or authorization guidance'
            assert state.task_id == task_id and state.sends == 2
            # auth-required is an external provider gate, never a JOAT approval RPC.
            ticket, _ = await begin_question(name, f'{name}-authorization-input')
            await submit(ticket, f'Run an authorization task for {name}.')
            await until(lambda: not roster.busy and agents[name].needs_recovery)
            guidance = await screen(f'{name}-authorization-guidance')
            assert 'authoriz' in guidance['text'].lower() or 'dashboard' in guidance['text'].lower(), 'Missing provider authorization guidance'
            assert not runtime.dispatcher.is_armed
            prior = state.polls
            state.auth_complete = True  # Simulates an operator acting outside JOAT; no approval endpoint exists.
            await input_action('click')
            await until(lambda: state.polls > prior and visible(f'{name.upper()} OPERATOR COMPLETED'))
            await screen(f'{name}-operator-completed-query')
            assert state.sends == 3
            await input_action('double_click')
            await until(lambda: gateway.frames[-1].get('kind') == 'hud.list')
            await screen(f'{name}-picker-{state.sends}')
            assert_picker()
        assert not violations, violations
        assert sum(c['method'] == 'message/send' for c in calls) == 3
        assert sum(c['method'] == 'message/stream' for c in calls) == 3
        assert not any(c['method'] not in ('tasks/get', 'message/send', 'message/stream') for c in calls)
        assert source_fingerprint() == source_before, 'Production sources changed during streaming acceptance'
        (out / 'receipt.json').write_text(json.dumps({
            'result': 'passed', 'mode': 'streaming-restart', 'screens': screens, 'rpc_calls': calls,
            'durable_database': str(checkpoint_path),
            'source_fingerprint': source_before,
            'boundaries': ['Local TLS v1 SSE/polling services; recognized final speech text',
                'Fragmented append/replacement frames stress protocol handling; current Hermes normally emits its final artifact rather than token deltas',
                'Fixture remote task survives disconnect intentionally; real Hermes may report a failed task on later query',
                'Fresh full engine objects restored in the same process; separate subprocess persistence tests cover actual process death',
                'External authorization completion is a fixture state change, never a JOAT approval RPC'],
            'isolation': {'engine_port': 18980, 'agent_port': 18981, 'plugin_port': 5298, 'simulator_port': 9998},
        }, indent=2))

    async def lifecycle_flow():
        command_receipts = []
        def messages():
            return current_terminal()['messages']
        def has_text(text):
            return (gateway.frames and gateway.frames[-1].get('kind') == 'agent_terminal.view'
                    and any(text in message['text'] for message in messages()))
        async def spoken_input(name, text, label):
            ticket, _ = await begin_question(name, label)
            await submit(ticket, text)
        for index, name in enumerate(AGENTS):
            state = states[name]
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name and agents[name].capture_context_generation is not None)
            await screen(f'{name}-lifecycle-open')
            identity = current_terminal()['conversationId']
            await spoken_input(name, f'Run a staged task for {name}.', f'{name}-progress-input')
            await until(lambda: has_text(f'{name.upper()} QUEUED'))
            queued = await screen(f'{name}-queued')
            assert 'QUEUED' in queued['text']
            progress = [m for m in messages() if m['role'] == 'status' and f'{name.upper()} QUEUED' in m['text']]
            assert len(progress) == 1
            progress_id = progress[0]['id']
            state.phase = 1
            await until(lambda: has_text(f'{name.upper()} SCANNING'))
            await screen(f'{name}-working-scanning')
            await until(lambda: state.polls.count(('progress', 1)) >= 3)
            repeated = [m for m in messages() if m['role'] == 'status' and 'SCANNING' in m['text']]
            assert len(repeated) == 1 and repeated[0]['id'] == progress_id, 'Repeated polling duplicated progress rows'
            state.phase = 2
            await until(lambda: has_text(f'{name.upper()} CHECKING'))
            changed = await screen(f'{name}-working-checking')
            assert 'CHECKING' in changed['text'] and 'SCANNING' not in changed['text']
            assert any(m['id'] == progress_id and 'CHECKING' in m['text'] for m in messages())
            polls_before = len(state.polls)
            await input_action('double_click')
            await screen(f'{name}-running-back-picker')
            assert_picker()
            assert roster.busy and state.cancel_count == 0
            await until(lambda: len(state.polls) > polls_before)
            if index:
                await input_action('down')
            await input_action('click')
            await until(lambda: roster.focused == name and gateway.frames[-1].get('kind') == 'agent_terminal.view')
            await screen(f'{name}-running-reopened')
            assert current_terminal()['conversationId'] == identity
            state.phase = 3
            await until(lambda: not roster.busy and has_text(f'{name.upper()} COMPLETED'))
            await screen(f'{name}-polled-completed')
            # A status question must win even when the input-required Task also has an artifact.
            await spoken_input(name, f'Plan requiring a reply for {name}.', f'{name}-question-task-input')
            await until(lambda: not roster.busy and current_terminal().get('inputRequired'))
            required = await screen(f'{name}-reply-required')
            assert 'QUESTION' in required['text'] and 'cobalt' in required['text'].lower()
            assert any(state.question in m['text'] for m in messages())
            same_task = state.task_id
            await spoken_input(name, f'Choose cobalt for {name}.', f'{name}-same-task-reply-input')
            await until(lambda: not roster.busy and has_text(f'{name.upper()} RESUMED SAME TASK'))
            await screen(f'{name}-same-task-completed')
            assert state.task_id == same_task and state.resumed
            assert current_terminal()['conversationId'] == identity
            for scenario, instruction in [('confirm', f'Run a cancellable task for {name}.'),
                                          ('reject', f'Run a task that refuses stop for {name}.')]:
                await spoken_input(name, instruction, f'{name}-{scenario}-stop-input')
                await until(lambda: roster.busy and has_text(f'{name.upper()} RUNNING'))
                await screen(f'{name}-{scenario}-before-stop')
                # Recognized speech enters the same production command dispatcher as __main__.
                stop = asyncio.create_task(commands.handle_command('terminal stop'))
                await asyncio.wait_for(state.cancel_started.wait(), 10)
                pending_frame = await screen(f'{name}-{scenario}-stop-pending')
                assert normalized('Stop requested') in normalized(pending_frame['text']), 'Pending stop is not visibly distinguished from a confirmed stop'
                assert not stop.done(), 'The pending stop was reported as finished before its remote receipt'
                state.cancel_release.set()
                receipt = await asyncio.wait_for(stop, 10)
                command_receipts.append({'agent': name, 'scenario': scenario, 'text': receipt})
                await until(lambda: not roster.busy)
                outcome = await screen(f'{name}-{scenario}-stop-outcome')
                if scenario == 'confirm':
                    assert agents[name].cancellation_status == 'confirmed'
                    assert 'confirmed' in str(receipt).lower() and 'unconfirmed' not in str(receipt).lower()
                    assert 'confirmed' in outcome['text'].lower()
                else:
                    assert agents[name].cancellation_status != 'confirmed'
                    assert 'unconfirmed' in str(receipt).lower()
                    assert 'unconfirmed' in outcome['text'].lower()
                    assert 'CHECK TASK' in outcome['text'] and normalized('check task status') in normalized(outcome['text']), 'Unconfirmed stop must offer task-status recovery'
                    assert 'OFFLINE' not in outcome['text'] and 'CONNECTION UNAVAILABLE' not in outcome['text'], 'Unknown stop outcome must not claim the live provider is offline'
                    assert agents[name].capture_context_generation is None
                    before = len(state.accepted)
                    polls_before_recovery = len(state.polls)
                    await input_action('click')
                    await until(lambda: len(state.polls) > polls_before_recovery)
                    await screen(f'{name}-unconfirmed-retry-no-resend')
                    assert len(state.accepted) == before and not runtime.dispatcher.is_armed
            assert len(state.accepted) == 5 and state.cancel_count == 2 and state.probes == 1
            await input_action('double_click')
            final_picker = await screen(f'{name}-lifecycle-final-picker')
            assert_picker()
            assert 'Connection failed' not in final_picker['text'], 'Task uncertainty must not be mislabeled as a connection failure'
        assert not violations, violations
        assert sum(call['method'] == 'message/send' for call in calls) == 10
        assert sum(call['method'] == 'tasks/cancel' for call in calls) == 4
        assert len({call['id'] for call in calls}) == len(calls)
        assert source_fingerprint() == source_before, 'Production sources changed during lifecycle acceptance'
        (out / 'receipt.json').write_text(json.dumps({
            'result': 'passed', 'mode': 'lifecycle', 'screens': screens, 'rpc_calls': calls,
            'command_receipts': command_receipts, 'source_fingerprint': source_before,
            'boundaries': ['Stateful local TLS providers replace remote agents',
                           'Acknowledged final STT text and recognized stop command text are synthetic'],
            'isolation': {'engine_port': 18980, 'agent_port': 18981, 'plugin_port': 5298, 'simulator_port': 9998},
        }, indent=2))

    def start(name, args):
        with (out / f'{name}.log').open('wb') as log:
            process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        processes.append(process)

    scratch = tempfile.TemporaryDirectory(prefix='joat-terminal-source-')
    try:
        await gateway.start()
        env = dict(os.environ, VITE_MAC_WS_URL='ws://127.0.0.1:18980/glasses')
        fixture_root = _prepare_simulator_plugin_root(
            ROOT, Path(scratch.name), 'ws://127.0.0.1:18980/glasses',
        )
        inject_bridge_probe(fixture_root)
        with (out / 'build.log').open('w') as log:
            subprocess.run([str(ROOT / 'apps/hub-plugin/node_modules/.bin/vite'), 'build', str(fixture_root),
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
        boot_events = await console_payloads()
        assert any(is_visual_page(event) for event in boot_events), 'Overview has no four-image SDK page declaration'
        await input_action('context_menu')
        await screen('01-feature-menu')
        for _ in range(5):
            await input_action('down')
        await screen('02-agent-terminal-selected')
        picker_bridge_start = len(await console_payloads())
        await input_action('click')
        await screen('03-agent-picker')
        assert_picker()
        await assert_picker_bridge(picker_bridge_start, 'initial Agent Terminal picker')
        if streaming_restart:
            await streaming_restart_flow()
            return
        if lifecycle:
            await lifecycle_flow()
            return
        native_receipts = []
        swipe_receipts = []
        for index, name in enumerate(AGENTS):
            if index:
                await input_action('down')
            visit_start = len(await console_payloads())
            await input_now('click')
            await immediate_screen(f'{name}-entry-transition-immediate')
            await until(lambda: roster.focused == name and agents[name].capture_context_generation is not None)
            entry = await assert_terminal_entry(visit_start, f'{name} first entry')
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
            swipe_receipts.append(await assert_native_swipe_round_trip(
                name, f'{name}-native-swipe',
            ))
            await seek(name, 'BEGIN', 'up', f'{name}-long-begin')
            anchor = await seek(name, 'MIDDLE', 'down', f'{name}-long-middle')
            visit_upgrades = await assert_terminal_visit(visit_start, f'{name} first visit')
            native_receipts.append({
                'agent': name, 'visit': 'first',
                'entryTextContainers': [item['containerName'] for item in entry['text']],
                'bodyUpgrades': len(visit_upgrades), 'imageOperations': 0,
                'pageRebuilds': 1,
            })
            back_start = len(await console_payloads())
            await input_now('double_click')
            await immediate_screen(f'{name}-back-transition-immediate')
            await asyncio.sleep(.7)
            await screen(f'{name}-back-picker')
            assert_picker()
            await assert_picker_bridge(back_start, f'{name} Back picker', exactly_one=True)
            if index:
                await input_action('down')
            revisit_start = len(await console_payloads())
            await input_action('click')
            await until(lambda: roster.focused == name and gateway.frames[-1].get('kind') == 'agent_terminal.view')
            await assert_terminal_entry(revisit_start, f'{name} reopen')
            reopened = await screen(f'{name}-reopened-middle')
            assert_conversation(name, 3)
            assert normalized(f'{name} MIDDLE') in normalized(reopened['text']), 'Reopening lost the reading position'
            await seek(name, 'END', 'down', f'{name}-long-end')
            ticket, _ = await begin_question(name, f'{name}-cancel-input')
            await input_action('double_click')
            await screen(f'{name}-cancelled')
            assert not runtime.dispatcher.is_armed
            assert_conversation(name, 3)
            revisit_upgrades = await assert_terminal_visit(revisit_start, f'{name} reopened visit')
            native_receipts.append({
                'agent': name, 'visit': 'reopened', 'bodyUpgrades': len(revisit_upgrades),
                'imageOperations': 0, 'pageRebuilds': 1,
            })
            picker_start = len(await console_payloads())
            await input_action('double_click')
            await screen(f'{name}-final-picker')
            assert_picker()
            await assert_picker_bridge(picker_start, f'{name} final picker', exactly_one=True)
        assert conversation_ids['OpenClaw'] != conversation_ids['Hermes']
        # Hold a fourth OpenClaw response: busy entry and updates while reading older text.
        held_visit_start = len(await console_payloads())
        await input_action('click')
        await until(lambda: roster.focused == 'OpenClaw' and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        await assert_terminal_entry(held_visit_start, 'OpenClaw held-turn entry')
        await screen('OpenClaw-before-held-turn')
        ticket, _ = await begin_question('OpenClaw', 'OpenClaw-held-input')
        held_response.clear()
        pending = asyncio.create_task(submit(ticket, prompts('OpenClaw')[3]))
        await until(lambda: roster.busy)
        await assert_terminal_visit(held_visit_start, 'OpenClaw held turn before chooser')
        await input_action('context_menu')
        for _ in range(5):
            await input_action('down')
        busy_picker_start = len(await console_payloads())
        await input_action('click')
        await screen('busy-agent-picker')
        assert_picker()
        await assert_picker_bridge(busy_picker_start, 'busy Agent Terminal picker')
        busy_reopen_start = len(await console_payloads())
        await input_action('click')
        await until(lambda: roster.focused == 'OpenClaw' and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        await assert_terminal_entry(busy_reopen_start, 'busy OpenClaw reopen')
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
        held_upgrades = await assert_terminal_visit(busy_reopen_start, 'OpenClaw held-output visit')
        native_receipts.append({
            'agent': 'OpenClaw', 'visit': 'held-output', 'bodyUpgrades': len(held_upgrades),
            'imageOperations': 0, 'pageRebuilds': 1,
        })

        # Complete the required navigation contract: selected terminal → native
        # picker → visual Overview. Capture both transition boundaries and the
        # settled destinations instead of inferring them from Mac-side frames.
        final_picker_start = len(await console_payloads())
        await input_now('double_click')
        await immediate_screen('OpenClaw-final-back-transition-immediate')
        await asyncio.sleep(.7)
        final_picker = await screen('OpenClaw-final-back-picker-stable')
        assert_picker()
        await assert_picker_bridge(final_picker_start, 'final Back picker', exactly_one=True)
        overview_start = len(await console_payloads())
        await input_now('double_click')
        await immediate_screen('overview-transition-immediate')
        await asyncio.sleep(.7)
        overview = await screen('overview-after-terminal-stable')
        overview_rebuild = await bridge_event(overview_start, {'rebuild'}, is_visual_page)
        overview_events = (await console_payloads())[overview_start:]
        overview_images = [event for event in overview_events if event.get('operation') == 'image']
        assert [event.get('containerID') for event in overview_images] == [4, 8, 9, 10], \
            'Overview did not restore the four visual tiles once in serial order'
        assert overview_rebuild['imageCount'] == 4
        assert normalized('AGENT TERMINAL') not in normalized(overview['text']), 'Overview still renders terminal chrome'
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
            'native_surface_evidence': {
                'pickerRows': list(AGENTS),
                'terminalContainers': ['terminal-head', 'terminal-body'],
                'visits': native_receipts,
                'swipes': swipe_receipts,
                'backToPicker': final_picker['name'],
                'backToOverview': overview['name'],
                'overviewImageOperations': [event.get('containerID') for event in overview_images],
                'immediateTransitionFrames': [
                    screen['name'] for screen in screens if 'immediate' in screen['name']
                ],
            },
            'boundaries': ['Stateful local TLS providers replace remote agents',
                           'Final STT text supplied after simulator prompt acknowledgment'],
            'isolation': {'engine_port': 18980, 'agent_port': 18981,
                          'plugin_port': 5298, 'simulator_port': 9998},
        }, indent=2))
    finally:
        held_response.set()
        if lifecycle:
            for state in states.values():
                state.cancel_release.set()
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
        if streaming_restart:
            for state in states.values():
                state.final_release.set()
                state.replacement_release.set()
                state.append_release.set()
            await roster.detach_for_shutdown()
        await runtime.aclose()
        await sessions.close()
        await gateway.stop()
        for agent in agents.values():
            await agent.close()
        await runner.cleanup()
        if durable is not None:
            durable.close()
        key.unlink(missing_ok=True)
        scratch.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--lifecycle', action='store_true', help='Exercise polled progress, same-task reply, and stop outcomes')
    parser.add_argument('--streaming-restart', action='store_true', help='Exercise fragmented Hermes SSE and durable task recovery')
    args = parser.parse_args()
    if args.lifecycle and args.streaming_restart:
        parser.error('Choose one acceptance mode')
    output = args.output or ROOT / 'dist/mac-workspace/control-audit' / ('agent-terminal-streaming-restart' if args.streaming_restart else 'agent-terminal-lifecycle' if args.lifecycle else 'agent-terminal-persistent')
    asyncio.run(run(output.resolve(), lifecycle=args.lifecycle, streaming_restart=args.streaming_restart))
