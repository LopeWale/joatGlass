"""Official-simulator prompt runtime acceptance with isolated synthetic providers.

Copies current Mac/Hub sources and composes the production runtime, owners,
authenticated gateway, AudioRouter, VAD and voice orchestrator as __main__ does.
Contacts, completed STT text and the held Brainstorm session are synthetic.
The disposable SDK audio-control response succeeds without calling the host;
all live SDK audio events are suppressed. Synthetic PCM enters the ORIGINAL
AudioStreamer.ingest with explicit Glasses source only after a real prompt ack.
No model, microphone, Messages database, sender, installed package or portal is
used. Entry is injected through the message tool and Brainstorm action. Later
decline, Back and mute are official simulator gestures. Simulator right-double
is normalized to left-double only in the disposable event subscription.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request


REPO = Path(__file__).resolve().parents[3]
SIM = Path.home() / ".local/lib/node_modules/@evenrealities/evenhub-simulator/node_modules/@evenrealities/sim-darwin-arm64/bin/evenhub-simulator"
BASE = Path(__file__).resolve().parent / "evidence/remediation"
PREFIX = "[joat prompt fixture] "
BODY = "  Stop, open News?!\nKeep these exact words.  "
TOPIC = "  Stop?! Should JOAT preserve a spoken question exactly?  "
PCM = b"".join(struct.pack("<3200h", *([amplitude, -amplitude] * 1600))
               for amplitude in (8000, 8000, 0, 0))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--diagnose-handoff", action="store_true",
                        help="Stop after recipient PCM reaches the body question; not full journey acceptance")
    parser.add_argument("--vite-port", type=int, default=5396)
    parser.add_argument("--gateway-port", type=int, default=18897)
    parser.add_argument("--automation-port", type=int, default=9796)
    result = parser.parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", result.run_label) is None:
        parser.error("run label must be a short lowercase identifier")
    ports = (result.vite_port, result.gateway_port, result.automation_port)
    if len(set(ports)) != 3 or any(not 1024 <= port <= 65535 for port in ports):
        parser.error("three distinct unprivileged ports are required")
    return result


def check_free_ports(ports: tuple[int, ...]) -> None:
    for port in ports:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def http(base: str, path: str, payload: dict | None = None) -> bytes:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()


def stop_owned(process: subprocess.Popen) -> None:
    # Each child starts a fresh session, so this group contains only that
    # owned simulator/Vite workload and any helpers it created.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
    # Reap/stop helpers even when their primary process exited first.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    raise RuntimeError("owned process group did not exit")


async def wait_for(predicate, *, timeout: float = 12) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def console_events(base: str) -> list[dict]:
    console = json.loads(await asyncio.to_thread(http, base, "/api/console"))
    events = []
    for entry in console.get("entries", []):
        message = entry.get("message", "")
        if message.startswith(PREFIX):
            events.append({**json.loads(message[len(PREFIX):]), "console_id": entry["id"]})
        elif message == "[joat] acceptance confirmation-visible":
            events.append({"event": "confirmation-visible", "console_id": entry["id"]})
    return events


async def wait_console(base: str, predicate, *, after: int = -1, timeout: float = 12) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            for event in reversed(await console_events(base)):
                if event["console_id"] > after and predicate(event):
                    return event
            await asyncio.sleep(.1)


async def capture(base: str, out: Path, name: str) -> dict:
    previous = None
    stable_since = time.monotonic()
    async with asyncio.timeout(6):
        while True:
            raw = await asyncio.to_thread(http, base, "/api/screenshot/glasses")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != previous:
                previous, stable_since = digest, time.monotonic()
            if time.monotonic() - stable_since >= .5:
                rgba = out / f"{name}.rgba.png"
                rgba.write_bytes(raw)
                await asyncio.to_thread(subprocess.run, [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(rgba),
                    "-vf", "alphaextract", str(out / f"{name}.png"),
                ], check=True, timeout=3)
                return {"name": name, "rgba_sha256": digest, "file": f"{name}.png"}
            await asyncio.sleep(.15)


class Driver:
    """A loopback-only command endpoint; public fixture files never change."""

    def __init__(self) -> None:
        self.command = {"sequence": 0}
        self.history: deque[dict] = deque(maxlen=512)
        driver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                command = driver.command
                driver.history.append({"event": "control-get", "at": time.monotonic(),
                                       "sequence": command["sequence"]})
                data = json.dumps(command).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                driver.history.append({"event": "control-written", "at": time.monotonic(),
                                       "sequence": command["sequence"]})

            def log_message(self, _format: str, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def replay(self, prompt_id: str, fixture: str) -> int:
        sequence = self.command["sequence"] + 1
        self.command = {"sequence": sequence, "promptId": prompt_id, "fixture": fixture}
        self.history.append({"event": "command-issued", "at": time.monotonic(), **self.command})
        return sequence

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("owned fixture control thread did not stop")


def replace_once(source: str, anchor: str, replacement: str) -> str:
    if source.count(anchor) != 1:
        raise RuntimeError(f"fixture anchor drift: {anchor[:80]}")
    return source.replace(anchor, replacement, 1)


def instrument_plugin(root: Path, token: str, driver_port: int) -> dict:
    main_file = root / "src/main.ts"
    source = main_file.read_text()
    source = replace_once(source, "import { AudioInputSource }",
                          "import { AudioInputSource, OsEventTypeList, EventSourceType, Sys_ItemEvent }")
    anchor = "  await client.init();"
    source = replace_once(source, anchor, anchor + r'''
  const fixtureLog = (detail: Record<string, unknown>) =>
    console.info('[joat prompt fixture] ' + JSON.stringify({
      at: performance.now(), wall: Date.now(), visibility: document.visibilityState,
      focused: document.hasFocus(), ...detail,
    }));
  const fixtureLifecycle = (reason: string) => fixtureLog({event: 'lifecycle', reason});
  fixtureLifecycle('initial');
  document.addEventListener('visibilitychange', () => fixtureLifecycle('visibilitychange'));
  for (const name of ['pagehide', 'pageshow', 'focus', 'blur']) {
    window.addEventListener(name, (event) => fixtureLog({event: 'lifecycle', reason: name,
      ...('persisted' in event ? {persisted: event.persisted} : {})}));
  }
  // Disposable simulator input provider. Never invoke host audioControl.
  const fixtureBridge = client.require();
  fixtureBridge.audioControl = async () => true;
  let sdkSequence = 0;
  const observeSdk = <Args extends unknown[], Result>(name: string, operation: (...args: Args) => Promise<Result>) =>
    async (...args: Args): Promise<Result> => {
      const sequence = ++sdkSequence;
      fixtureLog({event: 'sdk-start', name, sequence});
      try {
        const result = await operation(...args);
        fixtureLog({event: 'sdk-settled', name, sequence,
          result: typeof result === 'boolean' || typeof result === 'number' ? result : typeof result});
        return result;
      } catch (error) {
        fixtureLog({event: 'sdk-rejected', name, sequence,
          error: error instanceof Error ? error.name : 'unknown'});
        throw error;
      }
    };
  fixtureBridge.rebuildPageContainer = observeSdk('rebuild', fixtureBridge.rebuildPageContainer.bind(fixtureBridge));
  fixtureBridge.updateImageRawData = observeSdk('image', fixtureBridge.updateImageRawData.bind(fixtureBridge));
  fixtureBridge.textContainerUpgrade = observeSdk('text', fixtureBridge.textContainerUpgrade.bind(fixtureBridge));
  const fixtureSubscribe = fixtureBridge.onEvenHubEvent.bind(fixtureBridge);
  fixtureBridge.onEvenHubEvent = (callback) => fixtureSubscribe((event) => {
    const { audioEvent: ignoredLiveAudio, ...withoutAudio } = event;
    if (ignoredLiveAudio !== undefined) {
      console.info('[joat prompt fixture] ' + JSON.stringify({event: 'live-audio-suppressed'}));
    }
    if (withoutAudio.sysEvent?.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      console.info('[joat prompt fixture] ' + JSON.stringify({
        event: 'back-source-normalized', original: withoutAudio.sysEvent.eventSource,
        fixture: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      }));
      callback({...withoutAudio, sysEvent: new Sys_ItemEvent({...withoutAudio.sysEvent,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L})});
    } else if (Object.keys(withoutAudio).length > 0) callback(withoutAudio);
  });
''')
    anchor = "  const link = new MacLink({ urls: MAC_WS_URLS, tokens, preferredUrl });"
    source = replace_once(source, anchor,
        "  tokens = {...tokens, [new URL(MAC_WS_URLS[0]!).origin]: " + json.dumps(token) + "};\n" + anchor)
    anchor = "  const menu = new CapabilityMenu(hud, settings, link);"
    source = replace_once(source, anchor, anchor + r'''
  let fixtureAck: string | undefined;
  const fixtureSend = link.sendForGeneration.bind(link);
  link.sendForGeneration = (message, generation) => {
    const sent = fixtureSend(message, generation);
    if (sent && message.kind === 'prompt.ack' && message.success) fixtureAck = message.promptId;
    if (sent && message.kind === 'prompt.cancel' && fixtureAck === message.promptId) fixtureAck = undefined;
    if (message.kind !== 'audio') console.info('[joat prompt fixture] ' + JSON.stringify({
      event: 'generation-send', kind: message.kind, sent, promptId: message.promptId,
    }));
    return sent;
  };
  link.onMessageIngress((message, context) => {
    fixtureLog({event: 'ingress', kind: message.kind, generation: context.generation,
      ...('promptId' in message ? {promptId: message.promptId} : {})});
  });
  const fixtureOnMessage = link.onMessage.bind(link);
  link.onMessage = (handler) => fixtureOnMessage(async (message, context) => {
    fixtureLog({event: 'handler-start', kind: message.kind,
      ...('promptId' in message ? {promptId: message.promptId} : {})});
    try {
      await handler(message, context);
    } catch (error) {
      fixtureLog({event: 'handler-rejected', kind: message.kind,
        error: error instanceof Error ? error.name : 'unknown'});
      throw error;
    } finally {
      fixtureLog({event: 'handler-settled', kind: message.kind, aborted: context.signal.aborted});
    }
    console.info('[joat prompt fixture] ' + JSON.stringify({
      event: 'handled', kind: message.kind,
      ...('promptId' in message ? {promptId: message.promptId} : {}),
      promptActive: promptedInput.isActive(),
    }));
  });
  let fixtureSequence = 0;
  let fixtureBusy = false;
  let fixturePoll = 0;
  let fetchSequence = 0;
  const fixtureFetch: typeof fetch = async (input, init) => {
    const sequence = ++fetchSequence;
    const abort = new AbortController();
    fixtureLog({event: 'fetch-start', sequence});
    const timer = window.setTimeout(() => {
      fixtureLog({event: 'fetch-timeout', sequence});
      abort.abort();
    }, 2500);
    try {
      const response = await fetch(input, {...init, signal: abort.signal});
      fixtureLog({event: 'fetch-response', sequence, status: response.status});
      const body = await response.arrayBuffer();
      fixtureLog({event: 'fetch-body', sequence, bytes: body.byteLength});
      return new Response(body, {status: response.status, headers: response.headers});
    }
    finally {
      window.clearTimeout(timer);
      fixtureLog({event: 'fetch-settled', sequence});
    }
  };
  let heartbeatAt = -Infinity;
  const fixtureTimer = window.setInterval(() => {
    if (performance.now() - heartbeatAt >= 500) {
      heartbeatAt = performance.now();
      fixtureLog({event: 'heartbeat', busy: fixtureBusy, poll: fixturePoll, sequence: fixtureSequence});
    }
    if (fixtureBusy) return;
    fixtureBusy = true;
    void (async () => {
      const poll = ++fixturePoll;
      if (poll % 10 === 1) fixtureLog({event: 'control-start', poll, sequence: fixtureSequence});
      const response = await fixtureFetch('/__joat-prompt-control', {cache: 'no-store'});
      if (!response.ok) throw new Error('fixture control unavailable');
      const command = await response.json();
      if (!Number.isSafeInteger(command.sequence) || command.sequence <= fixtureSequence) return;
      fixtureLog({event: 'control-decoded', poll, ...command});
      fixtureSequence = command.sequence;
      if (command.promptId !== fixtureAck || !promptedInput.isActive() || !audio.isOpen
        || !['recipient', 'body', 'topic'].includes(command.fixture)) {
        throw new Error('synthetic PCM has no current acknowledged question');
      }
      const fixtureUrl = new URL(`/__joat-audio-fixture/${command.fixture}.pcm`, window.location.origin);
      fixtureLog({event: 'replay-start', sequence: command.sequence, promptId: command.promptId});
      const batches = await replaySyntheticAudioFixture({
        get isOpen() { return audio.isOpen; },
        // The existing DEV helper predates source provenance. This fixture
        // adapter labels its known synthetic source at actual sample ingress.
        ingest: (pcm, speaker) => {
          fixtureLog({event: 'replay-ingest', sequence: command.sequence, bytes: pcm.byteLength});
          audio.ingest(pcm, speaker, AudioInputSource.Glasses);
        },
      }, fixtureUrl, {fetch: async (input, init) => {
        fixtureLog({event: 'pcm-fetch-start', sequence: command.sequence});
        const response = await fixtureFetch(input, init);
        fixtureLog({event: 'pcm-fetch-response', sequence: command.sequence, status: response.status});
        return response;
      }});
      console.info('[joat prompt fixture] ' + JSON.stringify({
        event: 'replayed', sequence: command.sequence, promptId: command.promptId, batches,
      }));
    })().catch((error: unknown) => {
      console.error('[joat prompt fixture] ' + JSON.stringify({
        event: 'driver-error', sequence: fixtureSequence,
        error: error instanceof Error ? error.message : 'unknown',
      }));
    }).finally(() => { fixtureBusy = false; });
  }, 100);
  window.addEventListener('beforeunload', () => window.clearInterval(fixtureTimer));
''')
    anchor = "  window.addEventListener('beforeunload', () => { void teardown(); });"
    source = replace_once(source, anchor, anchor + "\n  console.info('[joat prompt fixture] ' + JSON.stringify({event: 'ready', audioOpen: audio.isOpen}));")
    main_file.write_text(source)
    (root / "vite.config.ts").write_text(
        "import { defineConfig } from 'vite';\nexport default defineConfig({server:{proxy:{"
        "'/__joat-prompt-control': " + json.dumps(f"http://127.0.0.1:{driver_port}") + "}}});\n")
    fixture_dir = root / "public/__joat-audio-fixture"
    fixture_dir.mkdir(parents=True)
    for name in ("recipient", "body", "topic"):
        (fixture_dir / f"{name}.pcm").write_bytes(PCM)
    return {"main_ts_sha256_after_instrumentation": hashlib.sha256(source.encode()).hexdigest(),
            "synthetic_pcm_sha256": hashlib.sha256(PCM).hexdigest(), "pcm_bytes_per_answer": len(PCM)}


async def run(args: argparse.Namespace) -> int:
    os.umask(0o077)
    out = BASE / args.run_label
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"scope": __doc__, "frames": [], "checks": {}, "gestures": [], "ui_receipts": [],
              "physical_g2_verified": False, "actual_stt_recognition_verified": False,
              "actual_delivery_verified": False, "complete_main_startup_verified": False,
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    processes = []
    logs = []
    driver = None
    gateway = None
    runtime = None
    sessions = None
    replies = None
    voice = None
    tasks = []
    base = f"http://127.0.0.1:{args.automation_port}"
    scratch = tempfile.TemporaryDirectory(prefix="joat-prompt-visual-")
    temp = Path(scratch.name)
    try:
        check_free_ports((args.vite_port, args.gateway_port, args.automation_port))
        if not SIM.is_file() or shutil.which("ffmpeg") is None:
            raise RuntimeError("official simulator and ffmpeg must already be installed")
        mac_root = temp / "mac"
        shutil.copytree(REPO / "apps/mac/joat", mac_root / "joat", ignore=shutil.ignore_patterns("__pycache__"))
        sys.path[:0] = [str(mac_root), str(REPO / "scripts")]
        from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root
        from joat.agents.message_tools import message_tools
        from joat.audio.router import AudioRouter
        from joat.audio.vad import RmsVad
        from joat.ble.hub_gateway import HubGatewayTransport
        from joat.core.interfaces import Transcript
        from joat.core.pairing import PairingStore
        from joat.core.protocol import BrainstormAction, BrainstormHudView
        from joat.services.hud import HudWriter
        from joat.services.message_reply import MessageReplyController
        from joat.services.orchestrator import VoiceOrchestrator
        from joat.services.prompted_speech import PromptedSpeechRuntime
        from joat.services.session import Session, SessionManager

        ws_url = f"ws://127.0.0.1:{args.gateway_port}/glasses"
        root = _prepare_simulator_plugin_root(REPO, temp, ws_url)
        hashes = {}
        for folder, prefix in ((root / "src", "apps/hub-plugin/src"), (mac_root / "joat", "apps/mac/joat")):
            for source_file in sorted(folder.rglob("*")):
                if source_file.is_file() and source_file.suffix in (".ts", ".py"):
                    hashes[prefix + "/" + source_file.relative_to(folder).as_posix()] = hashlib.sha256(source_file.read_bytes()).hexdigest()
        result["copied_source_sha256_before_instrumentation"] = hashes
        result["simulator_binary_sha256"] = hashlib.sha256(SIM.read_bytes()).hexdigest()
        result["git_head_at_capture"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, timeout=3).strip()
        pairing = PairingStore(temp / "pairing.json")
        token = PairingStore.mint()
        pairing.confirm(token)
        driver = Driver()
        result["instrumentation"] = instrument_plugin(root, token, driver.server.server_port)
        result["simulator_entry_url"] = f"http://127.0.0.1:{args.vite_port}/"
        questions = []
        mac_frames = []
        audio_receipts = []
        confirms = []
        result.update(questions=questions, mac_frames=mac_frames,
                      audio_receipts=audio_receipts, synthetic_confirmation=confirms)
        connected = asyncio.Event()

        class ObservedGateway(HubGatewayTransport):
            async def request_prompt(self, request, **kwargs):
                questions.append(request.to_wire())
                return await super().request_prompt(request, **kwargs)

            async def send(self, message):
                wire = message.to_wire()
                if wire["kind"] in ("brainstorm.view", "confirm.request"):
                    mac_frames.append(wire)
                await super().send(message)

            async def request_confirmation(self, action_id, prompt, *args, **kwargs):
                receipt = {"action_id": action_id, "prompt": prompt}
                confirms.append(receipt)
                decision = await super().request_confirmation(action_id, prompt, *args, **kwargs)
                receipt["authenticated_response_received"] = decision is not None
                receipt["accepted"] = decision.accepted if decision is not None else None
                return decision

        gateway = ObservedGateway(host="127.0.0.1", port=args.gateway_port, pairing=pairing, trust_loopback=False)
        hud = HudWriter(gateway)
        topics = []
        session_release = asyncio.Event()
        sender_calls = []
        result.update(synthetic_topics=topics, sender_attempts=sender_calls)

        class Sender:
            async def send(self, recipient, body):
                sender_calls.append(True)
                raise AssertionError("synthetic sender must never be invoked")

        class SyntheticStt:
            def __init__(self):
                self.answers = deque(["Sam", BODY, TOPIC])
                self.completed = []

            async def feed(self, pcm):
                pass

            async def flush(self):
                if not self.answers:
                    raise AssertionError("unexpected extra synthetic utterance")
                text = self.answers.popleft()
                self.completed.append(text)
                return Transcript(text, True)

            def reset(self):
                pass

        class HeldSession(Session):
            async def run(self):
                await hud.show_brainstorm(BrainstormHudView(
                    state="running", topic=topics[-1], collaborators=["Fixture A", "Fixture B"],
                    activity="Synthetic topic accepted. No model request is running.",
                ))
                await session_release.wait()

            def cancel(self):
                session_release.set()

        def factory(topic):
            topics.append(topic)
            return HeldSession()

        def session_state(state):
            if state in ("settled", "cancelled", "failed"):
                tasks.append(asyncio.create_task(hud.show_brainstorm(BrainstormHudView(
                    state="ready", topic="Synthetic Brainstorm", collaborators=["Fixture A", "Fixture B"],
                    activity="Tap for a topic. Previous synthetic session ended.",
                ))))

        sessions = SessionManager(
            hud, factory, brainstorm_available=lambda: True, on_state=session_state,
            publish_topic_capture=lambda capture: runtime.publish_topic_capture(capture),
        )
        replies = MessageReplyController(Sender(), gateway.request_confirmation,
            contact_search=lambda name: [("Sam", "fixture-recipient")] if name == "Sam" else [])
        audio_router = AudioRouter(gateway, on_capture_started=lambda: runtime.revoke_current())
        runtime = PromptedSpeechRuntime(
            hud, replies, sessions=lambda: sessions,
            connection_generation=lambda: gateway.audio_connection_generation,
            fence_audio=lambda: voice.fence_prompt_audio(), capturing=lambda: audio_router.capturing,
        )
        stt = SyntheticStt()
        result["synthetic_stt"] = stt.completed
        command_calls = []

        async def commands(text):
            command_calls.append(text)
            return None

        voice = VoiceOrchestrator(
            gateway, stt, sessions=sessions, frames=audio_router, prompted_input=runtime.dispatcher,
            vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
            wake_words=("joat",), commands=commands,
        )
        voice.attach()
        gateway.on_audio_frame(lambda frame: audio_receipts.append({
            "source": frame.source, "epoch": frame.audio_epoch, "seq": frame.seq,
            "bytes": len(frame.pcm), "sha256": hashlib.sha256(frame.pcm).hexdigest(),
        }))

        def hello(_hello):
            runtime.revoke_current()
            sessions.clear_topic_capture()
            connected.set()
            tasks.append(asyncio.create_task(voice.on_connect()))

        gateway.on_hello(hello)
        gateway.on_disconnect(runtime.revoke_current)
        gateway.on_device(lambda device: runtime.revoke_current()
                          if device.mic is False or device.wearing is False else None)
        gateway.on_brainstorm_action(sessions.handle_brainstorm_action)
        tool_map = {tool.name: tool for tool in message_tools(replies, after_input=runtime.after_message_input)}
        await gateway.start()
        env = {**os.environ, "VITE_MAC_WS_URL": ws_url}
        for name, command, cwd in (
            ("vite", [str(REPO / "apps/hub-plugin/node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", str(args.vite_port), "--strictPort"], root),
            ("native", [str(SIM), result["simulator_entry_url"], "--automation-port", str(args.automation_port), "--no-glow"], REPO),
        ):
            log = (out / f"{name}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                              stderr=subprocess.STDOUT, start_new_session=True))
            if name == "vite":
                async with asyncio.timeout(8):
                    while True:
                        if processes[-1].poll() is not None:
                            raise RuntimeError("owned Vite process exited before readiness")
                        try:
                            await asyncio.to_thread(http, result["simulator_entry_url"].rstrip("/"), "/")
                            break
                        except OSError:
                            await asyncio.sleep(.1)
        await asyncio.wait_for(connected.wait(), 20)
        result["ui_receipts"].append(await wait_console(base, lambda event: event.get("event") == "ready" and event.get("audioOpen"), timeout=20))
        await asyncio.sleep(.8)  # Drain the simulator's known late startup press.

        async def question_frame(owner, field, name):
            result["stage"] = f"question:{name}"
            def armed():
                current = runtime.dispatcher.current_ticket
                return (runtime.dispatcher.is_armed and current is not None
                        and current.owner_kind == owner and current.field == field)

            await wait_for(armed)
            ticket = runtime.dispatcher.current_ticket
            result["ui_receipts"].append(await wait_console(base, lambda event:
                event.get("event") == "handled" and event.get("kind") == "prompt.present"
                and event.get("promptId") == ticket.prompt_id and event.get("promptActive")))
            result["frames"].append(await capture(base, out, name))
            return ticket

        async def answer(ticket, fixture):
            result["stage"] = f"answer:{fixture}"
            sequence = driver.replay(ticket.prompt_id, fixture)
            result["ui_receipts"].append(await wait_console(base, lambda event:
                event.get("event") == "replayed" and event.get("sequence") == sequence))

        async def gesture(action):
            response = await asyncio.to_thread(http, base, "/api/input", {"action": action})
            result["gestures"].append({"action": action, "response_sha256": hashlib.sha256(response).hexdigest()})

        preparation = asyncio.create_task(tool_map["prepare_message"].run({
            "mode": "new", "action_id": "visual-message", "recipient_name": "my wife",
        }))
        tasks.append(preparation)
        recipient = await question_frame("messages", "recipient", "recipient-question")
        await answer(recipient, "recipient")
        body = await question_frame("messages", "body", "body-question")
        if args.diagnose_handoff:
            result["checks"]["recipient_pcm_reached_body_question"] = (
                stt.completed == ["Sam"] and not sender_calls
                and len(audio_receipts) == 4
                and all(frame["source"] == "glasses" and frame["epoch"] == recipient.prompt_id
                        for frame in audio_receipts)
            )
            result["scope"] = "Diagnostic recipient-to-body handoff with synthetic PCM; no full journey, send, STT accuracy or physical-G2 acceptance"
            result["passed"] = all(result["checks"].values())
        else:
            await answer(body, "body")
            await asyncio.wait_for(preparation, 6)
            result["ui_receipts"].append(await wait_console(base, lambda event: event.get("event") == "confirmation-visible"))
            assert confirms[-1]["prompt"] == f"Send to Sam? “{BODY}”"
            result["frames"].append(await capture(base, out, "message-confirmation"))
            await gesture("down")
            await wait_for(lambda: replies.message_action_status("visual-message").state == "cancelled")
            assert confirms[-1]["authenticated_response_received"] and confirms[-1]["accepted"] is False
            result["frames"].append(await capture(base, out, "message-declined"))
            result["checks"]["exact_message_review_then_simulator_decline"] = not sender_calls

            sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
            topic = await question_frame("brainstorm", "topic", "brainstorm-question")
            await answer(topic, "topic")
            await wait_for(lambda: topics == [TOPIC] and sessions.active)
            result["ui_receipts"].append(await wait_console(base, lambda event:
                event.get("event") == "handled" and event.get("kind") == "brainstorm.view"))
            result["frames"].append(await capture(base, out, "brainstorm-started"))
            result["checks"]["exact_topic_started_held_session"] = topics == [TOPIC]
            await gesture("double_click")
            await wait_for(lambda: not sessions.active)
            # The fixture's completion callback owns an asynchronous ready frame.
            # Drain that publication before starting the next independent question.
            await asyncio.wait_for(asyncio.gather(*tasks), 6)

            sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
            await question_frame("brainstorm", "topic", "brainstorm-before-back")
            await gesture("double_click")
            await wait_for(lambda: runtime.dispatcher.current_ticket is None and not sessions.topic_capture_armed)
            result["frames"].append(await capture(base, out, "brainstorm-after-back"))
            result["checks"]["simulator_back_revoked_question"] = True

            sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
            await question_frame("brainstorm", "topic", "brainstorm-before-mute")
            await gesture("long_press")
            await gesture("long_press_release")
            await wait_for(lambda: runtime.dispatcher.current_ticket is None and gateway.device is not None and gateway.device.mic is False)
            result["frames"].append(await capture(base, out, "brainstorm-after-mute"))
            result["checks"]["simulator_mute_revoked_question"] = not sessions.topic_capture_armed
            expected_ids = {recipient.prompt_id, body.prompt_id, topic.prompt_id}
            result["checks"]["only_scoped_synthetic_audio_received"] = (
                len(audio_receipts) > 0
                and all(frame["source"] == "glasses" and frame["epoch"] in expected_ids for frame in audio_receipts)
            )
            result["checks"]["no_command_redispatch_or_extra_stt"] = command_calls == [] and stt.completed == ["Sam", BODY, TOPIC]
            result["questions"] = questions
            result["synthetic_confirmation"] = confirms
            result["synthetic_stt"] = stt.completed
            result["synthetic_topics"] = topics
            result["audio_receipts"] = audio_receipts
            result["mac_frames"] = mac_frames
            result["sender_calls"] = len(sender_calls)
            result["passed"] = all(result["checks"].values())
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        result["passed"] = False
    finally:
        try:
            (out / "console.json").write_bytes(await asyncio.to_thread(http, base, "/api/console"))
        except Exception:
            pass
        cleanup_errors = []

        async def cleanup(stage, operation, timeout=6):
            try:
                await asyncio.wait_for(operation, timeout)
            except Exception as error:
                cleanup_errors.append({"stage": stage, "error": type(error).__name__})

        try:
            if runtime is not None:
                runtime.begin_shutdown()
            if voice is not None and voice._utterance_worker is not None:
                worker = voice._utterance_worker
                worker.cancel()
                await cleanup("voice", asyncio.gather(worker, return_exceptions=True))
            if sessions is not None:
                await cleanup("sessions", sessions.close())
            if replies is not None:
                await cleanup("messages", replies.aclose())
            if runtime is not None:
                await cleanup("runtime", runtime.aclose())
            for task in tasks:
                if not task.done():
                    task.cancel()
            await cleanup("fixture-tasks", asyncio.gather(*tasks, return_exceptions=True))
            if gateway is not None:
                await cleanup("gateway", gateway.stop())
        finally:
            # Process cleanup is unconditional even if an async owner fails.
            for process in reversed(processes):
                try:
                    stop_owned(process)
                except Exception as error:
                    cleanup_errors.append({"stage": "owned-process", "error": type(error).__name__})
            result["owned_process_exit_codes"] = [process.poll() for process in processes]
            for log in logs:
                log.close()
            if driver is not None:
                result["driver_history"] = list(driver.history)
                try:
                    driver.close()
                except Exception as error:
                    cleanup_errors.append({"stage": "fixture-control", "error": type(error).__name__})
            scratch.cleanup()
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                result["passed"] = False
            (out / "receipt.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(out), "passed": result.get("passed"), "checks": result["checks"], "error": result.get("error")}, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(arguments())))
