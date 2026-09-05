#!/usr/bin/env python3
"""Synthetic voice-loss recovery on the official Even Realities Simulator.

Run from the repository: PYTHONPATH=apps/mac:. .venv/bin/python
scripts/simulator_voice_gap_acceptance.py --out /tmp/joat-voice-gap-acceptance

No native mic PCM, external STT/model, pairing store, package, or live brain is used.
Only the disposable main.ts substitutes native audioControl with a no-op;
production AudioStreamer, MacLink, renderer, gateway, router and orchestrator
execute. Drop exactly one numbered batch before gateway dispatch. Hold STT feed
to prove ingress loss cannot erase a completed turn, then render its reply and
the next contiguous recovery turn. This is an interface/routing fixture, not a
reproduction of the installed Whisper engine or a physical-G2 acceptance test.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

from joat.audio.router import AudioRouter
from joat.audio.vad import RmsVad
from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.interfaces import SttEngine, Transcript
from joat.core.protocol import AssistantOverlay, Audio, DownlinkMessage
from joat.services.orchestrator import VoiceOrchestrator
from scripts.simulator_brainstorm_action_flow_acceptance import (
    ManagedProcess,
    _capture,
    _http_bytes,
    _port_busy,
    _prepare_simulator_plugin_root,
    _start_process,
    _wait_for,
    _wait_http,
)


def pcm(amplitude: int) -> bytes:
    return struct.pack("<3200h", *([amplitude, -amplitude] * 1600))


FIRST = pcm(8000) * 2 + pcm(0) * 3
RECOVERED = pcm(9000) * 2 + pcm(0) * 3
FIXTURE = FIRST + pcm(4000) * 2 + RECOVERED


class FixtureStt(SttEngine):
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.feeds = 0
        self.completed = 0
        self.first = asyncio.Event()
        self.second = asyncio.Event()

    async def feed(self, data: bytes) -> Transcript | None:
        self.feeds += 1
        assert data == (FIRST if self.feeds == 1 else RECOVERED), "PCM crossed a gap"
        self.buffer.extend(data)
        await (self.first if self.feeds == 1 else self.second).wait()
        return None

    async def flush(self) -> Transcript | None:
        assert self.buffer == (FIRST if self.completed == 0 else RECOVERED), "STT turn erased"
        self.buffer.clear()
        self.completed += 1
        return Transcript(
            text="JOAT, first proof" if self.completed == 1 else "JOAT, recovery proof",
            is_final=True,
        )

    def reset(self) -> None:
        self.buffer.clear()


class FaultGateway(HubGatewayTransport):
    """One explicit after-numbering loss; all other dispatch is production."""

    def __init__(self, port: int) -> None:
        super().__init__(host="127.0.0.1", port=port)
        self.sequences: list[int] = []
        self.replies: list[str] = []

    def _handle_audio(self, message: Audio, session_id: str) -> None:
        assert message.source == "glasses"
        assert len(message.pcm) == 3200
        self.sequences.append(message.seq)
        if message.seq != 6:
            super()._handle_audio(message, session_id)

    async def send(self, message: DownlinkMessage) -> None:
        await super().send(message)
        if isinstance(message, AssistantOverlay) and message.text in (
            "First turn preserved", "Voice recovered",
        ):
            self.replies.append(message.text)


async def run(args: argparse.Namespace) -> dict[str, object]:
    for port in (args.ws_port, args.vite_port, args.automation_port):
        if _port_busy(port):
            raise RuntimeError(f"isolated acceptance port {port} is already occupied")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    processes: list[ManagedProcess] = []
    stt = FixtureStt()
    gateway = FaultGateway(args.ws_port)

    async def command(query: str) -> str | None:
        return {"first proof": "First turn preserved", "recovery proof": "Voice recovered"}[query]

    orchestrator = VoiceOrchestrator(
        gateway, stt, frames=AudioRouter(gateway),
        vad=RmsVad(adaptive=False, start_batches=1, hangover_batches=3),
        wake_words=("joat",), commands=command,
    )
    orchestrator.attach()
    hashes: dict[str, str] = {}
    for name in (
        "apps/hub-plugin/src/AudioStreamer.ts", "apps/hub-plugin/src/MacLink.ts",
        "apps/hub-plugin/src/HudVisualSurface.ts", "apps/hub-plugin/src/HudPageBootstrap.ts",
        "apps/hub-plugin/src/HudPresenter.ts",
        "apps/hub-plugin/src/main.ts", "apps/mac/joat/ble/hub_gateway.py",
        "apps/mac/joat/services/orchestrator.py", "apps/mac/joat/audio/router.py",
        "apps/mac/joat/audio/vad.py", "scripts/simulator_voice_gap_acceptance.py",
    ):
        hashes[name] = hashlib.sha256((args.repo / name).read_bytes()).hexdigest()
    await gateway.start()
    try:
        # The source snapshot and synthetic audio are temporary. Receipt/frame
        # hashes are durable evidence; only synthetic PCM enters this lane.
        with tempfile.TemporaryDirectory(prefix="joat-voice-gap-source-") as scratch:
            root = _prepare_simulator_plugin_root(
                args.repo, Path(scratch), f"ws://127.0.0.1:{args.ws_port}/glasses",
            )
            main = root / "src/main.ts"
            source = main.read_text()
            if args.stall_spine:
                # Fault injection lives only in the temporary source snapshot.
                # Other native calls stay real, including the fallback text HUD.
                init = "await client.init();"
                assert source.count(init) == 1
                source = source.replace(init, init + """
  const fixtureBridge = client.require();
  const nativeText = fixtureBridge.textContainerUpgrade.bind(fixtureBridge);
  fixtureBridge.textContainerUpgrade = (update) =>
    update.containerID === ContainerId.SpineLabel
      ? new Promise<boolean>(() => {})
      : nativeText(update);
""")
            needle = "const audio = new AudioStreamer(client, link);"
            assert source.count(needle) == 1
            source = source.replace(needle, """
  // Disposable acceptance only: suppress the app's native microphone enable.
  const fixtureClient = {
    assertStarted: () => { client.assertStarted(); return { audioControl: async () => true }; },
    require: () => ({ audioControl: async () => true }),
  } as unknown as BridgeClient;
  const audio = new AudioStreamer(fixtureClient, link);
""")
            main.write_text(source)
            fixture = root / "public/__joat-audio-fixture/gap-recovery.pcm"
            fixture.parent.mkdir(mode=0o700, parents=True)
            fixture.write_bytes(FIXTURE)
            env = os.environ.copy()
            env["VITE_MAC_WS_URL"] = f"ws://127.0.0.1:{args.ws_port}/glasses"
            vite = _start_process(
                "voice-gap-vite",
                ["npx", "vite", str(root), "--host", "127.0.0.1", "--port", str(args.vite_port), "--strictPort"],
                cwd=args.repo / "apps/hub-plugin", env=env, log_path=args.out / "vite.log",
            )
            processes.append(vite)
            url = f"http://127.0.0.1:{args.vite_port}"
            await _wait_http(url, vite)
            simulator = _start_process(
                "official-evenhub-simulator",
                [str(args.simulator), f"{url}/?joatAudioFixture=gap-recovery",
                 "--automation-port", str(args.automation_port),
                 "--aid", "coreaudio:BuiltInMicrophoneDevice"],
                cwd=args.repo, env=env, log_path=args.out / "simulator.log",
            )
            processes.append(simulator)
            base = f"http://127.0.0.1:{args.automation_port}"
            await _wait_http(f"{base}/api/ping", simulator)
            await _wait_for(
                lambda: True if len(gateway.sequences) == 12 else None,
                label="twelve synthetic numbered batches",
                timeout_s=60 if args.stall_spine else 20,
            )
            if args.stall_spine:
                console = json.loads(await asyncio.to_thread(_http_bytes, f"{base}/api/console"))
                assert any(
                    "visual HUD unavailable; using text compatibility surface" in entry.get("message", "")
                    for entry in console["entries"]
                ), "native status stall did not reach the production text fallback"
            assert gateway.sequences == list(range(12))
            assert stt.feeds == 1 and stt.completed == 0
            assert orchestrator._pending_utterance == (RECOVERED, "glasses")
            assert not gateway.physical_g2_connected
            ready, _, _ = await _capture(base, args.out, "01-waiting", None)
            stt.first.set()
            await _wait_for(
                lambda: True if gateway.replies == ["First turn preserved"] else None,
                label="first completed turn reply",
            )
            first, _, _ = await _capture(base, args.out, "02-first-preserved", ready)
            stt.second.set()
            await _wait_for(
                lambda: True if gateway.replies == ["First turn preserved", "Voice recovered"] else None,
                label="recovered contiguous turn reply",
            )
            recovered, _, _ = await _capture(base, args.out, "03-recovered", first)
            assert stt.completed == 2
            assert orchestrator._pending_utterance is None
            return {
                "schemaVersion": 1,
                "result": "simulator-plugin-startup-recovery-pass" if args.stall_spine else "simulator-voice-gap-routing-pass",
                "evidence": {
                    "input": "synthetic PCM through production AudioStreamer and MacLink",
                    "fault": "one numbered batch omitted before real gateway audio dispatch",
                    "startupFault": "nonsettling native spine text; production text fallback observed" if args.stall_spine else "not injected",
                    "nativeAudioControl": "replaced only in disposable main.ts; app mic enable suppressed; only synthetic PCM admitted",
                    "stt": "held asynchronous fixture; no Whisper/model recognition claim",
                    "hud": "official Even Realities Simulator settled framebuffer",
                    "physicalG2": "not run",
                    "visualInspection": "pending human or agent review of optical PNGs",
                    "packageUpload": "not run",
                },
                "checks": {
                    "numberedBatches": 12, "injectedMissingBatches": 1,
                    "exactPcmTurns": 2, "cloudCalls": 0,
                    "activeTurnPreserved": True, "noCrossGapPcm": True,
                },
                "frames": {"waiting": ready, "firstPreserved": first, "recovered": recovered},
                "sourceSha256": hashes,
            }
    finally:
        stt.first.set()
        stt.second.set()
        for process in reversed(processes):
            process.stop()
        if orchestrator._utterance_worker is not None:
            await asyncio.wait_for(orchestrator._utterance_worker, 2)
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ws-port", type=int, default=8799)
    parser.add_argument("--vite-port", type=int, default=5299)
    parser.add_argument("--automation-port", type=int, default=9999)
    parser.add_argument("--simulator", type=Path, default=Path.home() / ".local/bin/evenhub-simulator")
    parser.add_argument("--stall-spine", action="store_true", help="Inject an unresolved native spine status write before first light")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    (args.out / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    # Native simulator/vite logs are temporary and not acceptance evidence.
    for name in ("vite.log", "simulator.log"):
        (args.out / name).unlink(missing_ok=True)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
