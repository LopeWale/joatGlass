#!/usr/bin/env python3
"""Multi-turn reliability & wake discipline on REAL evenhub-simulator + compiled Swift JOAT Brain.

Reuses the phone-first E2E harness (scripts/simulator_phone_first_e2e.py): every real
`apps/ios-companion/Sources/*.swift` file compiled into an iOS-Simulator runner
(`xcrun simctl spawn`), the REAL `apps/hub-plugin` Vite server and the REAL
`evenhub-simulator`. Speech is injected as `Transcriber.Event`s straight into
`Brain.handle` (the exact entry point Apple's recognizer feeds), and touchpad
gestures (`click`, `up`) are driven through `evenhub-simulator`'s `/api/input`.

Scenarios (PNG + OCR + wire downlinks + latency):
  a) Unaddressed & misheard volatile speech -> 0 `assistant.conversation` frames,
     zero premature volatile card flicker, lens stays on Home.
  b) First-try phonetic wake ("Joe, what time is it?") -> sub-100ms local answer,
     single `ready` frame with `listening: true`, `JOAT · LISTENING` status bar
     (zero fake `"Listening…"` user turn), filler-word ("um") + unaddressed
     in-place follow-up ("and what's the date today?"), clean header expiry, and
     post-expiry unaddressed speech ignored.
  c) Touchpad `click` exit (`conversation.choice: "exit"`, retiring `cid_1` on Hub) ->
     immediate live Gemini Turn 1 on fresh `cid_2` -> touchpad `up` scroll on Turn 1 ->
     immediate Turn 2 follow-up resets viewport scroll to bottom so `"PHOTON"` is
     visibly rendered on the G2 lens (never hidden below Turn 1).
  d) Receipt command ("JOAT start a pomodoro") opens NO follow-up window; ambient
     speech after it produces 0 `assistant.conversation` frames.
  e) Deterministic note capture ("JOAT remember that the lab gate code is 4912") ->
     immediate live Gemini `read_notes` tool call reading back `"4912"` on fresh `cid_3`.

Isolation: binds only loopback ports 18981 (glasses WS), 18982 (control),
9981 (simulator automation) + an ephemeral Vite port.

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_wake_discipline_e2e.py \
        --output-dir dist/wake-e2e --screenshots DIR
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any

from PIL import Image

from scripts.simulator_agent_terminal_flow import BRIDGE_MARKER, OCR_SOURCE, http
from scripts.simulator_phone_first_e2e import (
    norm_ws,
    prepare_plugin_root,
    resolve_booted_simulator_udid,
    wait_http_ready,
)

ROOT = Path(__file__).resolve().parents[1]
PHONE_BRAIN_PORT = 18981
SWIFT_CONTROL_PORT = 18982
SIM_PORT = 9981
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"
FOLLOW_UP_SECONDS = 6.0

SWIFT_HARNESS_MAIN = r"""
import Foundation
import Network

private final class TapPluginLink: PluginLink {
    private let lock = NSLock()
    private let forward: LoopbackServer
    private var recorded: [[String: Any]] = []
    init(forward: LoopbackServer) { self.forward = forward }
    func send(_ object: [String: Any]) {
        lock.lock()
        var rec = Self.sanitize(object) as? [String: Any] ?? [:]
        rec["_t"] = Date().timeIntervalSince1970
        recorded.append(rec)
        lock.unlock()
        forward.send(object)
    }
    func snapshot() -> [[String: Any]] { lock.lock(); defer { lock.unlock() }; return recorded }
    static func sanitize(_ value: Any) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict where k != "image" && k != "png" { out[k] = sanitize(v) }
            return out
        }
        if let arr = value as? [Any] { return arr.map(sanitize) }
        if value is String || value is Int || value is Double || value is Bool || value is NSNumber || value is NSNull { return value }
        return String(describing: value)
    }
}

@MainActor
private final class WakeHarness {
    let server: LoopbackServer
    let tapLink: TapPluginLink
    let brain: Brain
    private var uplinks: [[String: Any]] = []
    private var controlListener: NWListener?
    private let controlQueue = DispatchQueue(label: "joat.wake.control")

    init(wsPort: UInt16, controlPort: UInt16, geminiJSON: String?) throws {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)
        let geminiDst = docs.appendingPathComponent("gemini.json")
        try? FileManager.default.removeItem(at: geminiDst)
        if let geminiJSON, !geminiJSON.isEmpty {
            try FileManager.default.copyItem(at: URL(fileURLWithPath: geminiJSON), to: geminiDst)
        }
        UserDefaults.standard.set(geminiJSON == nil ? "auto" : "gemini", forKey: OnPhoneAI.engineDefaultsKey)
        UserDefaults.standard.set(Double(env["JOAT_E2E_FOLLOWUP"] ?? "6") ?? 6, forKey: Brain.followUpKey)

        let defaults = UserDefaults(suiteName: "joat.e2e.wake.\(UUID().uuidString)")!
        let srv = LoopbackServer(port: wsPort)
        let tap = TapPluginLink(forward: srv)
        let stt = Transcriber(kind: .dictation(words: ["JOAT"]), label: "e2e")
        let voice = Voice()
        voice.spokenReplies = false
        let br = Brain(server: srv, stt: stt, shadow: nil, voice: voice, cardDefaults: defaults, linkOverride: tap)
        br.pomodoro.reset()
        server = srv; tapLink = tap; brain = br
        srv.onUplink = { [weak self, weak br] kind, obj, raw in
            if let self { self.uplinks.append(TapPluginLink.sanitize(obj) as? [String: Any] ?? ["kind": kind]) }
            br?.router.route(kind: kind, message: obj, raw: raw)
        }
        srv.start()
        try startControl(port: controlPort)
    }

    private func startControl(port: UInt16) throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        let listener = try NWListener(using: params)
        listener.newConnectionHandler = { [weak self] conn in self?.handle(conn) }
        listener.start(queue: controlQueue)
        controlListener = listener
    }

    private nonisolated func handle(_ conn: NWConnection) {
        conn.start(queue: controlQueue)
        var buffer = Data()
        func readLoop() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] chunk, _, done, error in
                if let chunk { buffer.append(chunk) }
                if let parsed = Self.parse(buffer) {
                    Task { @MainActor in
                        let r = self?.dispatch(parsed.0, parsed.1, parsed.2) ?? (500, ["error": "gone"])
                        Self.write(conn, r.0, r.1)
                    }
                    return
                }
                if done || error != nil { conn.cancel(); return }
                readLoop()
            }
        }
        readLoop()
    }

    private nonisolated static func parse(_ data: Data) -> (String, String, Data)? {
        guard let sep = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let lines = String(decoding: data[..<sep.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
        let parts = (lines.first ?? "").split(separator: " ")
        guard parts.count >= 2 else { return nil }
        var len = 0
        for l in lines.dropFirst() {
            let kv = l.split(separator: ":", maxSplits: 1)
            if kv.count == 2, kv[0].trimmingCharacters(in: .whitespaces).lowercased() == "content-length" {
                len = Int(kv[1].trimmingCharacters(in: .whitespaces)) ?? 0
            }
        }
        guard data.count - sep.upperBound >= len else { return nil }
        return (String(parts[0]), String(parts[1]), data.subdata(in: sep.upperBound..<(sep.upperBound + len)))
    }

    private nonisolated static func write(_ conn: NWConnection, _ status: Int, _ json: [String: Any]) {
        let payload = (try? JSONSerialization.data(withJSONObject: json)) ?? Data("{}".utf8)
        var out = Data("HTTP/1.1 \(status) OK\r\nContent-Type: application/json\r\nContent-Length: \(payload.count)\r\nConnection: close\r\n\r\n".utf8)
        out.append(payload)
        conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func dispatch(_ method: String, _ path: String, _ body: Data) -> (Int, [String: Any]) {
        if path == "/health" { return (200, ["ok": true]) }
        if path == "/state" {
            return (200, [
                "authenticated": server.authenticated,
                "pluginSessions": server.pluginSessions,
                "followUpWindowOpen": brain.followUpWindowOpen,
                "followUpSeconds": Brain.conversationWindow,
                "activeCapability": brain.capabilities.activeCapability ?? NSNull(),
                "uplinks": uplinks,
                "downlinks": tapLink.snapshot(),
            ])
        }
        let p = (try? JSONSerialization.jsonObject(with: body) as? [String: Any]) ?? [:]
        if path == "/stt" {
            let text = p["text"] as? String ?? ""
            if (p["mode"] as? String) == "volatile" {
                brain.handle(.volatile(text))
            } else {
                brain.handle(.final(text, latencyMs: 0))
            }
            return (200, ["ok": true, "followUpWindowOpen": brain.followUpWindowOpen])
        }
        if path == "/tool" {
            let name = p["name"] as? String ?? ""
            let args = p["arguments"] as? [String: Any] ?? [:]
            Task { @MainActor in
                let out = await brain.toolBelt.execute(name: name, arguments: args)
                Log.add("e2e tool \(name) output: \(String(describing: out).prefix(200))")
            }
            return (200, ["ok": true])
        }
        return (400, ["error": "unknown \(method) \(path)"])
    }
}

let env = ProcessInfo.processInfo.environment
var retained: AnyObject?
Task { @MainActor in
    do {
        retained = try WakeHarness(
            wsPort: UInt16(env["JOAT_E2E_WS_PORT"] ?? "18981")!,
            controlPort: UInt16(env["JOAT_E2E_CONTROL_PORT"] ?? "18982")!,
            geminiJSON: env["JOAT_E2E_GEMINI_JSON"]
        )
    } catch {
        fputs("harness boot failed: \(error)\n", stderr)
        exit(1)
    }
}
RunLoop.main.run()
"""


def compile_runner(build_dir: Path, env: dict[str, str]) -> Path:
    inputs: list[str] = []
    for path in sorted((ROOT / "apps/ios-companion/Sources").glob("*.swift")):
        if path.name == "JOATBrainApp.swift":
            staged = build_dir / "JOATBrainApp_NoMain.swift"
            staged.write_text(path.read_text(encoding="utf-8").replace("\n@main\n", "\n// @main\n"), encoding="utf-8")
            inputs.append(str(staged))
        else:
            inputs.append(str(path))
    main_swift = build_dir / "main.swift"
    main_swift.write_text(SWIFT_HARNESS_MAIN, encoding="utf-8")
    out = build_dir / "joat_wake_e2e_runner"
    res = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", "iphonesimulator", "swiftc", "-target", "arm64-apple-ios26.0-simulator",
         "-swift-version", "5", "-suppress-warnings", "-DDEBUG", *inputs, str(main_swift), "-o", str(out)],
        env=env, capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"swiftc failed:\n{res.stderr[-6000:]}")
    return out


def conv_frames(downlinks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in downlinks if d.get("kind") == "assistant.conversation"]


def turn_texts(frame: dict[str, Any]) -> list[str]:
    return [str(t.get("text", "")) for t in frame.get("turns", []) or []]


async def run(output_dir: Path, shots: Path, gemini_json: Path | None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    shots.mkdir(parents=True, exist_ok=True)
    for port in (PHONE_BRAIN_PORT, SWIFT_CONTROL_PORT, SIM_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    with socket.socket() as ephem:
        ephem.bind(("127.0.0.1", 0))
        plugin_port = int(ephem.getsockname()[1])

    xenv = dict(os.environ)
    xenv.setdefault("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    scratch = tempfile.TemporaryDirectory(prefix="joat-wake-e2e-")
    work = Path(scratch.name)
    logs = output_dir
    procs: list[subprocess.Popen[bytes]] = []
    screens: list[dict[str, Any]] = []
    checks: list[str] = []
    failures: list[str] = []
    bridge: list[dict[str, Any]] = []
    console_last: int | None = None

    def start_proc(name: str, args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        with (logs / f"{name}.log").open("wb") as fh:
            p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(p)
        return p

    async def ctl(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = await asyncio.to_thread(http, f"http://127.0.0.1:{SWIFT_CONTROL_PORT}{path}", payload)
        return dict(json.loads(raw))

    async def state() -> dict[str, Any]:
        return await ctl("/state")

    async def say(text: str, mode: str = "final") -> None:
        print(f"--> stt {mode}: {text!r}", flush=True)
        await ctl("/stt", {"mode": mode, "text": text})

    async def press(action: str, delay: float = 0.65) -> None:
        print(f"--> gesture: {action}", flush=True)
        await asyncio.to_thread(http, f"{AUTOMATION}/api/input", {"action": action})
        await asyncio.sleep(delay)

    async def wait_for(pred: Callable[[dict[str, Any]], bool], label: str, timeout: float = 25.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await state()
            if pred(last):
                return last
            await asyncio.sleep(0.15)
        raise AssertionError(f"timed out: {label}")

    async def poll_bridge() -> None:
        nonlocal console_last
        for _ in range(30):
            url = f"{AUTOMATION}/api/console" + (f"?since_id={console_last}" if console_last is not None else "")
            entries = json.loads(await asyncio.to_thread(http, url)).get("entries") or []
            if not entries:
                return
            ids = [e["id"] for e in entries if isinstance(e.get("id"), int)]
            prev = console_last
            if ids:
                console_last = max(ids)
            for e in entries:
                msg = e.get("message")
                if isinstance(msg, str) and BRIDGE_MARKER in msg:
                    try:
                        bridge.append(json.loads(msg[msg.find(BRIDGE_MARKER) + len(BRIDGE_MARKER):].strip()))
                    except json.JSONDecodeError:
                        pass
            if console_last == prev or len(entries) < 50:
                return

    ocr_bin = work / "ocr"
    (work / "ocr.swift").write_text(OCR_SOURCE, encoding="utf-8")
    subprocess.run(["/usr/bin/xcrun", "swiftc", str(work / "ocr.swift"), "-o", str(ocr_bin)], env=xenv, check=True, capture_output=True)

    async def shot(name: str, settle: float = 0.9, save: bool = True) -> str:
        await asyncio.sleep(settle)
        prev = None
        raw = b""
        for _ in range(18):
            raw = await asyncio.to_thread(http, f"{AUTOMATION}/api/screenshot/glasses")
            vis = Image.open(io.BytesIO(raw)).crop((0, 24, 576, 288)).tobytes()
            if vis == prev:
                break
            prev = vis
            await asyncio.sleep(0.25)
        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        opt = Image.new("RGBA", src.size, "black")
        opt.alpha_composite(src)
        path = (shots if save else work) / f"{name}.png"
        opt.convert("RGB").save(path)
        src.getchannel("A").point(lambda p: 255 if p > 8 else 0).resize((2304, 1152)).save(work / "ocr-in.png")
        res = await asyncio.to_thread(subprocess.run, [str(ocr_bin), str(work / "ocr-in.png")], env=xenv, check=True, capture_output=True, text=True)
        text = "\n".join(json.loads(res.stdout))
        await poll_bridge()
        screens.append({"name": name, "path": str(path) if save else None,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "ocr": text})
        print(f"=== SCREEN {name} ===\n{text}\n", flush=True)
        return text

    def ok(msg: str) -> None:
        checks.append(msg)
        print(f"PASS {msg}", flush=True)

    def home_like(ocr: str) -> bool:
        low = norm_ws(ocr)
        return ("swipe: tabs" in low or ("notifications" in low and "quicknotes" in low)) and not any(
            w in low for w in ("listening", "thinking", "answering", "no name needed"))

    try:
        print("Compiling real Swift JOAT Brain harness...", flush=True)
        bdir = work / "build"
        bdir.mkdir()
        runner = await asyncio.to_thread(compile_runner, bdir, xenv)
        udid = await asyncio.to_thread(resolve_booted_simulator_udid, xenv)
        sim_home = work / "sim_home"
        sim_home.mkdir()
        senv = dict(xenv)
        senv.update({
            "SIMCTL_CHILD_HOME": str(sim_home),
            "SIMCTL_CHILD_JOAT_E2E_WS_PORT": str(PHONE_BRAIN_PORT),
            "SIMCTL_CHILD_JOAT_E2E_CONTROL_PORT": str(SWIFT_CONTROL_PORT),
            "SIMCTL_CHILD_JOAT_E2E_FOLLOWUP": str(FOLLOW_UP_SECONDS),
        })
        if gemini_json is not None:
            staged_key = work / "gemini.json"
            shutil.copy2(gemini_json, staged_key)
            senv["SIMCTL_CHILD_JOAT_E2E_GEMINI_JSON"] = str(staged_key)
        swift = start_proc("swift-phone-brain", ["/usr/bin/xcrun", "simctl", "spawn", udid, str(runner)], env=senv)
        await wait_http_ready(f"http://127.0.0.1:{SWIFT_CONTROL_PORT}/health", swift, timeout_s=40)

        ws_url = f"ws://127.0.0.1:{PHONE_BRAIN_PORT}/glasses"
        plugin_root = prepare_plugin_root(ROOT, work, [ws_url])
        vite = start_proc("vite", [str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"), str(plugin_root),
                                   "--config", str(ROOT / "apps/hub-plugin/vite.config.ts"),
                                   "--host", "127.0.0.1", "--port", str(plugin_port), "--strictPort"],
                          env=dict(os.environ, VITE_MAC_WS_URL=ws_url))
        await wait_http_ready(f"http://127.0.0.1:{plugin_port}/", vite)
        sim = start_proc("evenhub-simulator", [str(Path.home() / ".local/bin/evenhub-simulator"), "--no-aid",
                                               f"http://127.0.0.1:{plugin_port}/", "--automation-port", str(SIM_PORT)])
        await wait_http_ready(f"{AUTOMATION}/api/screenshot/glasses", sim, timeout_s=35)
        await wait_for(lambda s: s.get("authenticated") is True and any(u.get("kind") == "hello" for u in s["uplinks"]),
                       "plugin auth + hello", timeout=30)
        await shot("wake-00-boot-home", settle=1.6)
        st = await state()
        assert st["followUpSeconds"] == FOLLOW_UP_SECONDS, st["followUpSeconds"]
        ok(f"joat.followUpSeconds={st['followUpSeconds']} read by Brain.conversationWindow")

        # ---------------- a) unaddressed & misheard volatile speech ----------------
        unaddressed = ["Joe said he'd be late", "I told her to reply later",
                       "The blue folder is on the kitchen counter next to the keys"]
        for i, sentence in enumerate(unaddressed, start=1):
            mark = len((await state())["downlinks"])
            words = sentence.split()
            for n in range(2, len(words) + 1, 2):
                await say(" ".join(words[:n]), "volatile")
                await asyncio.sleep(0.10)
            await say(sentence)
            await asyncio.sleep(0.6)
            st = await state()
            new = conv_frames(st["downlinks"][mark:])
            assert not new, f"unaddressed {sentence!r} produced assistant.conversation {new}"
            assert st["followUpWindowOpen"] is False
            if i == 1:
                ocr = await shot("wake-01-unaddressed-1", settle=0.45)
                assert home_like(ocr) and "late" not in norm_ws(ocr), ocr
            ok(f"a) unaddressed {sentence!r}: zero assistant.conversation frames, lens on Home")

        # Volatile partial that starts addressed ("hey joat...") then finalises unaddressed:
        # MUST NOT mount a premature volatile card on BLE!
        mark = len((await state())["downlinks"])
        await say("hey joat can", "volatile")
        await asyncio.sleep(0.15)
        await say("hey joat can you pass", "volatile")
        await asyncio.sleep(0.20)
        await say("Hey Joanne can you pass the salt")
        await asyncio.sleep(0.6)
        st = await state()
        misheard_frames = conv_frames(st["downlinks"][mark:])
        assert not misheard_frames, f"volatile misheard partial spammed BLE: {misheard_frames}"
        ocr = await shot("wake-02-misheard-volatile-zero-spam", settle=0.45)
        assert home_like(ocr) and "salt" not in norm_ws(ocr), ocr
        ok("a) volatile 'hey joat can you pass' -> final 'Hey Joanne...' sent 0 assistant.conversation frames (zero flicker)")

        # ---------------- b) 1st-try phonetic wake ("Joe, what time is it?"), header cue & follow-up ----------------
        def answered_ready(s: dict[str, Any], since: int, min_assistant: int, listening: bool | None = None) -> bool:
            frames = [f for f in conv_frames(s["downlinks"][since:]) if f.get("operation") != "dismiss"]
            if not frames:
                return False
            f = frames[-1]
            ts = f.get("turns") or []
            if f.get("state") != "ready":
                return False
            if listening is not None and bool(f.get("listening")) != listening:
                return False
            return sum(1 for t in ts if t.get("role") == "assistant") >= min_assistant

        mark = len((await state())["downlinks"])
        t_local_start = time.monotonic()
        await say("Joe, what time", "volatile")
        await say("Joe, what time is it?")
        st = await wait_for(lambda s: answered_ready(s, mark, 1, listening=True), "1st-try phonetic 'Joe, what time is it?'", timeout=10)
        local_ms = (time.monotonic() - t_local_start) * 1000
        ready_frames_b1 = [f for f in conv_frames(st["downlinks"][mark:]) if f.get("state") == "ready"]
        assert len(ready_frames_b1) == 1, f"expected single ready frame (no double-rev cue spam), got {ready_frames_b1}"
        f1 = ready_frames_b1[-1]
        cid_1 = f1["conversationId"]
        assert not any("no name needed" in t.lower() for t in turn_texts(f1)), f"fake listening user turn still present: {f1}"
        answer1 = [t["text"] for t in f1["turns"] if t.get("role") == "assistant"][-1]
        assert st["followUpWindowOpen"] is True
        ocr = await shot("wake-03-first-try-joe-listening-header", settle=0.5)
        assert "joat" in norm_ws(ocr) and "listening" in norm_ws(ocr), f"expected JOAT · LISTENING header in:\n{ocr}"
        assert "no name needed" not in norm_ws(ocr), ocr
        ok(f"b) 1st-try 'Joe, what time is it?' answered locally in {local_ms:.0f}ms ({answer1!r}); single ready rev, JOAT · LISTENING header")

        # Filler word ("um") + follow-up without wake name -> stays on same cid_1 and updates in-place!
        mark = len((await state())["downlinks"])
        await say("um", "volatile")
        await say("um")
        await asyncio.sleep(0.15)
        await say("and what's the date", "volatile")
        await say("and what's the date today?")
        st = await wait_for(lambda s: answered_ready(s, mark, 2, listening=True), "in-place follow-up date answer", timeout=10)
        f2 = [f for f in conv_frames(st["downlinks"][mark:]) if f.get("state") == "ready"][-1]
        assert f2["conversationId"] == cid_1, f"expected same cid {cid_1}, got {f2['conversationId']}"
        answer2 = [t["text"] for t in f2["turns"] if t.get("role") == "assistant"][-1]
        t_armed = time.monotonic()
        ocr = await shot("wake-04-in-place-followup-answered", settle=0.5)
        assert any(day in norm_ws(ocr) for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")), ocr
        ok(f"b) filler 'um' + unaddressed follow-up 'and what's the date today?' answered in-place on {cid_1}: {answer2!r}")

        # Window expiry flips listening -> false in status bar without wiping answer
        mark = len((await state())["downlinks"])
        st = await wait_for(
            lambda s: s["followUpWindowOpen"] is False and answered_ready(s, mark, 2, listening=False),
            "window expiry clears listening header",
            timeout=FOLLOW_UP_SECONDS + 8,
        )
        closed_after = time.monotonic() - t_armed
        ocr = await shot("wake-05-window-expired-header-clean", settle=0.5)
        assert "listening" not in norm_ws(ocr), f"LISTENING stayed in header after expiry:\n{ocr}"
        ok(f"b) follow-up window closed cleanly after ~{closed_after:.1f}s; status bar returned to JOAT")

        mark = len((await state())["downlinks"])
        await say("We should grab lunch", "volatile")
        await say("We should grab lunch at noon tomorrow")
        await asyncio.sleep(0.8)
        st = await state()
        assert not conv_frames(st["downlinks"][mark:]), conv_frames(st["downlinks"][mark:])
        ocr = await shot("wake-06-after-expiry-ignored", settle=0.4)
        assert "lunch" not in norm_ws(ocr), ocr
        ok("b) after expiry, 'We should grab lunch at noon tomorrow' produced 0 frames")

        # ---------------- c) touchpad exit retires cid_1 -> immediate live Gemini Turn 1 + scroll-up -> Turn 2 visible ----------------
        uplink_mark = len((await state())["uplinks"])
        await press("click", delay=0.7)
        st = await wait_for(
            lambda s: any(
                u.get("kind") == "input"
                and (u.get("event") or {}).get("containerName") == "assistant-conversation"
                and (u.get("event") or {}).get("selectedName") == "exit"
                for u in s["uplinks"][uplink_mark:]
            ),
            "touchpad click -> input(assistant-conversation, exit)",
            timeout=8,
        )
        ocr = await shot("wake-07-touchpad-exit-home", settle=0.5)
        assert home_like(ocr), f"expected Home after touchpad exit, got:\n{ocr}"
        ok(f"c) touchpad tap exited conversation {cid_1} cleanly back to Home")

        # Immediately (<2s after exit — previously dead for 30s due to retired cid reuse!) ask live Gemini a multi-line question:
        mark = len((await state())["downlinks"])
        t_gem1 = time.monotonic()
        await say("JOAT, explain in five short bullet points how waveguide microdisplays work on smart glasses")
        st = await wait_for(lambda s: answered_ready(s, mark, 1, listening=True), "post-exit live Gemini Turn 1", timeout=25)
        gem1_ms = (time.monotonic() - t_gem1) * 1000
        f_gem1 = [f for f in conv_frames(st["downlinks"][mark:]) if f.get("state") == "ready"][-1]
        cid_2 = f_gem1["conversationId"]
        assert cid_2 != cid_1, f"Brain reused retired conversationId {cid_1} after exit!"
        gem1_text = [t["text"] for t in f_gem1["turns"] if t.get("role") == "assistant"][-1]
        ocr_gem1 = await shot("wake-08-post-exit-live-gemini-turn1", settle=0.6)
        assert not home_like(ocr_gem1), f"post-exit live Gemini response failed to render on lens:\n{ocr_gem1}"
        ok(f"c) immediate post-exit Turn 1 minted fresh {cid_2} and answered via live Gemini in {gem1_ms:.0f}ms ({len(gem1_text)} chars)")

        # Scroll UP on Turn 1 so committedScroll > 0, then ask Turn 2 without wake word!
        await press("up", delay=0.45)
        await press("up", delay=0.45)
        await shot("wake-09-turn1-scrolled-up", settle=0.4)

        mark = len((await state())["downlinks"])
        t_gem2 = time.monotonic()
        await say("Now reply with a five-word motto that includes the exact word PHOTON")
        st = await wait_for(lambda s: answered_ready(s, mark, 2, listening=True), "scrolled Turn 2 live Gemini follow-up", timeout=25)
        gem2_ms = (time.monotonic() - t_gem2) * 1000
        f_gem2 = [f for f in conv_frames(st["downlinks"][mark:]) if f.get("state") == "ready"][-1]
        assert f_gem2["conversationId"] == cid_2
        gem2_text = [t["text"] for t in f_gem2["turns"] if t.get("role") == "assistant"][-1]
        ocr_gem2 = await shot("wake-10-turn2-after-scroll-visible", settle=0.6)
        assert "photon" in norm_ws(ocr_gem2), (
            f"Turn 2 answer ({gem2_text!r}) was hidden off-screen by Turn 1 scroll lock! OCR:\n{ocr_gem2}"
        )
        ok(f"c) Turn 2 after scrolling up reset viewport to bottom and rendered 'PHOTON' visibly in {gem2_ms:.0f}ms: {gem2_text!r}")

        # Exit conversation before receipt test
        await press("click", delay=0.65)

        # ---------------- d) receipt command opens NO follow-up window ----------------
        mark = len((await state())["downlinks"])
        await say("JOAT start a pomodoro")
        st = await wait_for(lambda s: any(d.get("kind") in ("hud.receipt", "hud.text", "panel.update", "hud.panel")
                                          for d in s["downlinks"][mark:]), "pomodoro receipt", timeout=10)
        await asyncio.sleep(0.6)
        st = await state()
        kinds_c = [d.get("kind") for d in st["downlinks"][mark:]]
        assert st["followUpWindowOpen"] is False, "receipt armed the follow-up window"
        await shot("wake-11-pomodoro-receipt-no-window", settle=0.4)
        mark2 = len(st["downlinks"])
        await say("I told her to reply later")
        await asyncio.sleep(0.25)
        await say("Can you believe how cold it is outside")
        await asyncio.sleep(0.8)
        st = await state()
        after = conv_frames(st["downlinks"][mark2:])
        assert not after, after
        await say("JOAT reset pomodoro")
        await asyncio.sleep(0.5)
        ok(f"d) 'JOAT start a pomodoro' -> receipt ({sorted(set(map(str, kinds_c)))}), window closed, ambient speech ignored")

        # ---------------- e) deterministic note capture -> immediate live Gemini tool readback ----------------
        mark = len((await state())["downlinks"])
        await say("JOAT remember that the lab gate code is 4912")
        await asyncio.sleep(0.8)
        await shot("wake-12-quicknote-saved", settle=0.4)

        mark = len((await state())["downlinks"])
        log_mark = (logs / "swift-phone-brain.log").read_text(errors="replace").count("\n")
        t_tool = time.monotonic()
        await say("Hey JOAT, check my notes and tell me the lab gate code")
        st = await wait_for(lambda s: answered_ready(s, mark, 1, listening=True), "post-note live Gemini read_notes answer", timeout=25)
        tool_ms = (time.monotonic() - t_tool) * 1000
        f_note = [f for f in conv_frames(st["downlinks"][mark:]) if f.get("state") == "ready"][-1]
        cid_3 = f_note["conversationId"]
        assert cid_3 not in (cid_1, cid_2), f"expected fresh conversationId after command, got {cid_3}"
        note_answer = [t["text"] for t in f_note["turns"] if t.get("role") == "assistant"][-1]
        ocr_note = await shot("wake-13-post-note-live-gemini-readback", settle=0.6)
        assert "4912" in note_answer and "4912" in norm_ws(ocr_note), (
            f"expected '4912' in model answer ({note_answer!r}) and lens OCR:\n{ocr_note}"
        )
        lines = (logs / "swift-phone-brain.log").read_text(errors="replace").splitlines()[log_mark:]
        tool_hits = [l for l in lines if "read_notes" in l]
        ok(f"e) 'JOAT remember... 4912' -> immediate live Gemini read_notes ({len(tool_hits)} tool log hits) answered on {cid_3} in {tool_ms:.0f}ms: {note_answer!r}")

        receipt = {"result": "failed" if failures else "passed", "failures": failures, "model": "gemini (real)" if gemini_json else "none (failure answer)",
                   "ports": {"ws": PHONE_BRAIN_PORT, "control": SWIFT_CONTROL_PORT, "sim": SIM_PORT, "vite": plugin_port},
                   "checks": checks, "screens": screens}
        (output_dir / "wake-e2e-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (output_dir / "wake-e2e-downlinks.json").write_text(json.dumps((await state())["downlinks"], indent=1) + "\n")
        print(f"{'FAILED' if failures else 'SUCCESS'} {len(checks)} checks, {len(failures)} failures", flush=True)
        if failures:
            raise SystemExit(1)
        return receipt
    finally:
        try:
            (output_dir / "wake-e2e-downlinks.json").write_text(json.dumps((await state())["downlinks"], indent=1) + "\n")
        except Exception:
            pass
        for p in reversed(procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, 15)
                    p.wait(timeout=4)
                except Exception:
                    try:
                        os.killpg(p.pid, 9)
                    except Exception:
                        pass
        scratch.cleanup()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", type=Path, default=ROOT / "dist/wake-e2e")
    ap.add_argument("--screenshots", type=Path, default=ROOT / "dist/wake-e2e/screenshots")
    ap.add_argument("--gemini-json", type=Path, default=ROOT / "apps/ios-companion/Generated/gemini.json")
    ap.add_argument("--no-model", action="store_true", help="run without any model key")
    a = ap.parse_args()
    key = None if a.no_model or not a.gemini_json.exists() else a.gemini_json.resolve()
    asyncio.run(run(a.output_dir.resolve(), a.screenshots.resolve(), key))


if __name__ == "__main__":
    main()
