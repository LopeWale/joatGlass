#!/usr/bin/env python3
"""End-to-end Phone-First JOAT Brain + Mac Relay verification in evenhub-simulator.

Boots the 100% REAL tri-stack end-to-end with zero mocks on the glasses path and
zero collision with live host ports (8787-8790, 50060):
  1. REAL Python `MessagesEndpoint` (`127.0.0.1:18997`) + `TerminalEndpoint`
     (`127.0.0.1:18998`) backed by `AgentTerminalRoster` (`claude`, `codex`,
     `antigravity`).
  2. REAL compiled Swift `JOAT Brain` binary (`LoopbackServer.swift` +
     `Brain.swift` + `FeatureRouter` + `CapabilityMenu.swift` + `Cards.swift` +
     `Prompts.swift` + `Messages.swift` + `Mail.swift` + `MacMessages.swift` +
     `MacTerminal.swift` + `GlassesTerminal.swift` + `ConfirmGate`) running
     inside `xcrun simctl spawn` on `ws://127.0.0.1:18996/glasses` (with HTTP
     test harness control on `127.0.0.1:18999`).
  3. Canary Mac WebSocket server (`18995`) included in `VITE_MAC_WS_URL` so
     `BrainHandoff` has an eligible Mac target, proving `0` handoff connections
     ever occur when opening `Messages` or `Agent Terminal`.
  4. REAL `apps/hub-plugin` Vite dev server (`127.0.0.1:5298`) + REAL
     `evenhub-simulator` (`127.0.0.1:9996`) driven via HTTP `/api/input` and
     verified via PNG screenshots + Apple Vision OCR + SDK bridge container logs.

Usage:
    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_phone_first_e2e.py \
        --output-dir /tmp/joat-phone-first-e2e
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from PIL import Image
import websockets

from joat.agents.antigravity_terminal import store_gemini_api_key
from joat.agents.claude_code_terminal import _RosterPolicy
from joat.agents.cli_accounts import AccountProfile, AccountRegistry
from joat.agents.cli_signin import AccountStatus, LoginSnapshot
from joat.channels.imessage import (
    IncomingText,
    MessageEntry,
    MessageHistoryPage,
    MessageThread,
)
from joat.core.interfaces import MessageSender
from joat.services.agent_terminal import AgentTerminalRoster
from joat.services.messages_endpoint import MessagesEndpoint
from joat.services.models import ModelRegistry, assistant_slot, default_slots
from joat.services.terminal_endpoint import TerminalEndpoint
from scripts.simulator_agent_terminal_flow import (
    BRIDGE_MARKER,
    OCR_SOURCE,
    http,
    inject_bridge_probe,
    until,
)

ROOT = Path(__file__).resolve().parents[1]

CANARY_MAC_WS_PORT = 18995
PHONE_BRAIN_PORT = 18996
MESSAGES_RELAY_PORT = 18997
TERMINAL_RELAY_PORT = 18998
SWIFT_CONTROL_PORT = 18999
PLUGIN_PORT = 5298
SIM_PORT = 9996
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"

TEST_MESSAGES_TOKEN = "e2e-ephemeral-messages-relay-token"
TEST_TERMINAL_TOKEN = "e2e-ephemeral-terminal-relay-token"
TEST_GEMINI_SENTINEL = "e2e-ephemeral-gemini-placeholder-not-a-key"


def discover_non_loopback_ipv4() -> str:
    """Discover active local IPv4 without hardcoding any host address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 80))
        addr = str(probe.getsockname()[0])
    if not addr or addr.startswith("127."):
        raise RuntimeError("Could not resolve a non-loopback local IPv4 interface")
    return addr


def prepare_plugin_root(repo: Path, output: Path, websocket_urls: list[str]) -> Path:
    """Snapshot Hub plugin source with local test WebSocket origins allowlisted."""
    origins: list[str] = []
    for ws_url in websocket_urls:
        parsed = urlsplit(ws_url)
        if parsed.scheme != "ws" or not parsed.hostname or parsed.path != "/glasses":
            raise RuntimeError(f"Invalid WebSocket URL: {ws_url}")
        origins.append(f"{parsed.scheme}://{parsed.netloc}")

    source_root = repo / "apps/hub-plugin"
    manifest = json.loads((source_root / "app.json").read_text(encoding="utf-8"))
    permissions = manifest.get("permissions")
    if not isinstance(permissions, list):
        raise RuntimeError("app.json missing permissions list")
    network = next(
        (item for item in permissions if isinstance(item, dict) and item.get("name") == "network"),
        None,
    )
    if not isinstance(network, dict) or not isinstance(network.get("whitelist"), list):
        raise RuntimeError("app.json missing network whitelist")
    network["whitelist"] = origins

    root = output / "simulator-plugin-root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700, parents=True)
    for name in ("index.html", "src", "tsconfig.json"):
        src = source_root / name
        dst = root / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    os.symlink(source_root / "node_modules", root / "node_modules", target_is_directory=True)
    manifest_path = root / "app.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    inject_bridge_probe(root)
    return root


class EphemeralMessagesStore:
    """In-memory IMessageStore backing real `MessagesEndpoint`."""

    available = True

    def __init__(self) -> None:
        now = time.time()
        self.texts: list[IncomingText] = []
        self.threads: list[MessageThread] = [
            MessageThread(
                sender="Maya Lin",
                handle="+14055550142",
                text="Are we still meeting at Blue Bottle at 6?",
                at=now - 300.0,
                chat_id=11,
                chat_guid="iMessage;-;+14055550142",
                unread=True,
            ),
            MessageThread(
                sender="Jordan Park",
                handle="+14055550188",
                text="Uploaded the optical calibration curves.",
                at=now - 1800.0,
                chat_id=12,
                chat_guid="iMessage;-;+14055550188",
                unread=False,
            ),
        ]
        self.histories: dict[int, tuple[MessageEntry, ...]] = {
            11: (
                MessageEntry(
                    text="On my way shortly!",
                    at=now - 600.0,
                    from_me=True,
                    row_id=201,
                    sender="You",
                ),
                MessageEntry(
                    text="Are we still meeting at Blue Bottle at 6?",
                    at=now - 300.0,
                    from_me=False,
                    row_id=202,
                    sender="Maya Lin",
                    unread=True,
                ),
            ),
            12: (
                MessageEntry(
                    text="Uploaded the optical calibration curves.",
                    at=now - 1800.0,
                    from_me=False,
                    row_id=203,
                    sender="Jordan Park",
                ),
            ),
        }

    def baseline(self) -> int:
        return 300

    def read_new_texts(self, after: int, limit: int = 20) -> tuple[list[IncomingText], int, bool]:
        fresh = [t for t in self.texts if t.row_id > after][:limit]
        return fresh, (fresh[-1].row_id if fresh else after), False

    def recent_threads(self, limit: int = 10) -> list[MessageThread]:
        return list(self.threads[:limit])

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 16,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        entries = self.histories.get(thread.chat_id, ())
        if before_row_id is not None:
            entries = tuple(e for e in entries if e.row_id < before_row_id)
        sliced = entries[-limit:]
        return MessageHistoryPage(entries=sliced, has_older=len(entries) > len(sliced))


class RecordingMessageSender(MessageSender):
    """Records outbound texts dispatched through `MessagesEndpoint` `/v1/send`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, recipient: str, text: str) -> None:
        self.sent.append(("participant", recipient, text))

    async def send_chat(self, chat_guid: str, text: str) -> None:
        self.sent.append(("chat", chat_guid, text))


class StreamingTerminalLaneAgent:
    """Controllable lane agent backing real `AgentTerminalRoster` + `TerminalEndpoint`."""

    policy = _RosterPolicy()

    def __init__(self, provider: str, cwd: str) -> None:
        self.provider = provider
        self.cwd = cwd
        self.model: str | None = None
        self.prompts: list[str] = []
        self.resets = 0
        self.sessions: list[dict[str, str]] = []
        self.session_histories: dict[str, list[tuple[str, str]]] = {}
        self.resumed_sessions: list[str] = []
        self._approval_cb: Any = None

    def set_approval_callback(self, callback: Any) -> None:
        self._approval_cb = callback

    async def reset_conversation(self) -> None:
        self.resets += 1

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        return list(self.sessions[:limit])

    async def resume_session(self, session_key: str) -> None:
        valid = {s["key"] for s in self.sessions}
        if session_key not in valid:
            raise RuntimeError("session not found")
        self.resumed_sessions.append(session_key)

    async def history(self, limit: int = 10) -> list[tuple[str, str]]:
        if not self.resumed_sessions:
            return []
        return list(self.session_histories.get(self.resumed_sessions[-1], []))[-limit:]

    async def resolve_approval(self, approval_id: str, kind: str, decision: str) -> str | None:
        return decision

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        yield "Inspecting BLE coalescer frame metrics...\n"
        await asyncio.sleep(0.45)
        yield "BLE frame coalescer verified: 76 suites green and zero dropped tiles.\n"


class EphemeralSignIn:
    """Sign-in status provider reporting CLI lanes logged in for `/terminal`."""

    def __init__(self, registry: AccountRegistry) -> None:
        self._registry = registry
        self.current: LoginSnapshot | None = None

    def binary(self, provider: str) -> str | None:
        return f"/usr/local/bin/{provider}"

    async def status(self, profile: AccountProfile) -> AccountStatus:
        return AccountStatus("logged_in", email=f"{profile.profile_id}@example.com", plan="Pro")

    async def sign_out(self, profile: AccountProfile) -> bool:
        return True

    def cancel(self, *, publish: bool = True) -> None:
        self.current = None

    def dismiss(self) -> None:
        self.current = None


class CanaryMacBrainServer:
    """Canary WebSocket server proving `apps/hub-plugin` NEVER hands off away from iPhone."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.connection_count = 0
        self._server: Any = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self.host, self.port, max_size=2**20)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, ws: Any) -> None:
        self.connection_count += 1
        await ws.close()


SWIFT_HARNESS_MAIN = r"""
import Foundation
import Network

private final class TapPluginLink: PluginLink {
    private let lock = NSLock()
    private let forward: LoopbackServer
    private var recorded: [[String: Any]] = []

    init(forward: LoopbackServer) {
        self.forward = forward
    }

    func send(_ object: [String: Any]) {
        lock.lock()
        recorded.append(Self.sanitize(object) as? [String: Any] ?? [:])
        lock.unlock()
        forward.send(object)
    }

    func snapshot() -> [[String: Any]] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    static func sanitize(_ value: Any) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict { out[k] = sanitize(v) }
            return out
        }
        if let arr = value as? [Any] {
            return arr.map(sanitize)
        }
        if value is String || value is Int || value is Double || value is Bool || value is NSNumber || value is NSNull {
            return value
        }
        return String(describing: value)
    }
}

@MainActor
private final class PhoneBrainHarness {
    let server: LoopbackServer
    let tapLink: TapPluginLink
    let brain: Brain
    let cardDefaults: UserDefaults
    private let uplinkLock = NSLock()
    private var recordedUplinks: [[String: Any]] = []
    private var controlListener: NWListener?
    private let controlQueue = DispatchQueue(label: "joat.e2e.control")

    init(
        wsPort: UInt16,
        controlPort: UInt16,
        messagesURL: String,
        messagesToken: String,
        terminalURL: String,
        terminalToken: String
    ) throws {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)

        let msgCfg: [String: String] = ["url": messagesURL, "token": messagesToken]
        try JSONSerialization.data(withJSONObject: msgCfg, options: [.prettyPrinted])
            .write(to: docs.appendingPathComponent("mac-messages.json"))

        let termCfg: [String: String] = ["url": terminalURL, "token": terminalToken]
        try JSONSerialization.data(withJSONObject: termCfg, options: [.prettyPrinted])
            .write(to: docs.appendingPathComponent("mac-terminal.json"))

        // Isolate harness CLI domain keys in simulator cfprefsd so repeated E2E runs start clean
        for key in ["joat.messages.v1", "joat.messages.macthreads.v1", "joat.macmessages.after.v1"] {
            UserDefaults.standard.removeObject(forKey: key)
        }

        let suiteName = "joat.e2e.cards.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        self.cardDefaults = defaults

        let srv = LoopbackServer(port: wsPort)
        let tap = TapPluginLink(forward: srv)
        let stt = Transcriber(kind: .dictation(words: ["JOAT"]), label: "e2e")
        let voice = Voice()
        voice.spokenReplies = false

        let br = Brain(
            server: srv,
            stt: stt,
            shadow: nil,
            voice: voice,
            cardDefaults: defaults,
            linkOverride: tap
        )
        br.glassesTerminal.busyPollSeconds = 0.30
        br.glassesTerminal.idlePollSeconds = 0.90

        self.server = srv
        self.tapLink = tap
        self.brain = br

        srv.onUplink = { [weak self, weak br] kind, obj, raw in
            if let self {
                self.uplinkLock.lock()
                self.recordedUplinks.append(TapPluginLink.sanitize(obj) as? [String: Any] ?? ["kind": kind])
                self.uplinkLock.unlock()
            }
            br?.router.route(kind: kind, message: obj, raw: raw)
        }

        srv.start()
        try startControlServer(port: controlPort)
    }

    private func snapshotUplinks() -> [[String: Any]] {
        uplinkLock.lock()
        defer { uplinkLock.unlock() }
        return recordedUplinks
    }

    private func startControlServer(port: UInt16) throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        let listener = try NWListener(using: params)
        listener.newConnectionHandler = { [weak self] conn in
            self?.handleControlConnection(conn)
        }
        listener.start(queue: controlQueue)
        self.controlListener = listener
    }

    private nonisolated func handleControlConnection(_ conn: NWConnection) {
        conn.start(queue: controlQueue)
        var buffer = Data()

        func readLoop() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] chunk, _, isComplete, error in
                if let chunk { buffer.append(chunk) }
                if let parsed = Self.parseHTTP(buffer) {
                    Task { @MainActor in
                        let response = await self?.dispatchHTTP(method: parsed.method, path: parsed.path, body: parsed.body)
                            ?? (500, ["error": "harness gone"])
                        Self.writeHTTP(conn: conn, status: response.0, json: response.1)
                    }
                    return
                }
                if isComplete || error != nil {
                    conn.cancel()
                    return
                }
                readLoop()
            }
        }
        readLoop()
    }

    private nonisolated static func parseHTTP(_ data: Data) -> (method: String, path: String, body: Data)? {
        guard let sep = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let headerText = String(decoding: data[..<sep.lowerBound], as: UTF8.self)
        let lines = headerText.components(separatedBy: "\r\n")
        guard let reqLine = lines.first else { return nil }
        let parts = reqLine.split(separator: " ")
        guard parts.count >= 2 else { return nil }
        var contentLength = 0
        for line in lines.dropFirst() {
            let kv = line.split(separator: ":", maxSplits: 1)
            if kv.count == 2, kv[0].trimmingCharacters(in: .whitespaces).lowercased() == "content-length" {
                contentLength = Int(kv[1].trimmingCharacters(in: .whitespaces)) ?? 0
            }
        }
        let bodyStart = sep.upperBound
        guard data.count - bodyStart >= contentLength else { return nil }
        let body = data.subdata(in: bodyStart..<(bodyStart + contentLength))
        return (String(parts[0]), String(parts[1]), body)
    }

    private nonisolated static func writeHTTP(conn: NWConnection, status: Int, json: [String: Any]) {
        let payload = (try? JSONSerialization.data(withJSONObject: json)) ?? Data("{}".utf8)
        let reason = status == 200 ? "OK" : "Bad Request"
        let head = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(payload.count)\r\nConnection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(payload)
        conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func dispatchHTTP(method: String, path: String, body: Data) async -> (Int, [String: Any]) {
        if method == "GET" && path == "/health" {
            return (200, ["ok": true, "state": server.state])
        }
        if method == "GET" && path == "/state" {
            return (200, [
                "ok": true,
                "serverState": server.state,
                "clients": server.clients,
                "authenticated": server.authenticated,
                "pluginSessions": server.pluginSessions,
                "activeCardCount": brain.cards.active.count,
                "totalCardEntries": brain.cards.entries.count,
                "dismissedOriginIds": brain.cards.dismissedOriginIds,
                "promptActive": brain.prompts.active,
                "uplinks": snapshotUplinks(),
                "downlinks": tapLink.snapshot(),
            ])
        }
        let payload = (try? JSONSerialization.jsonObject(with: body) as? [String: Any]) ?? [:]
        if method == "POST" && path == "/inject_notifications" {
            let items = payload["items"] as? [[String: Any]] ?? []
            let now = Date()
            for item in items {
                let kind = (item["kind"] as? String) ?? "card"
                let ageSeconds = (item["ageSeconds"] as? Double)
                    ?? Double((item["ageSeconds"] as? Int) ?? 0)
                let sentAt = now.addingTimeInterval(-ageSeconds)
                if kind == "mac_message" {
                    let rowId = (item["rowId"] as? Int) ?? Int.random(in: 1000...9999)
                    let label = (item["source"] as? String) ?? "Contact"
                    let text = (item["summary"] as? String) ?? ""
                    let thread = (item["thread"] as? String) ?? "0123456789abcdef0123"
                    let incoming = MacMessages.Incoming(
                        id: rowId,
                        thread: thread,
                        label: label,
                        person: label,
                        group: false,
                        replyable: true,
                        text: text,
                        at: sentAt,
                        attachment: false
                    )
                    await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                        brain.messages.arrivedFromMac(incoming, quiet: false) {
                            cont.resume()
                        }
                    }
                } else {
                    let source = (item["source"] as? String) ?? "Mail"
                    let channel = (item["channel"] as? String) ?? "Mail"
                    let summary = (item["summary"] as? String) ?? ""
                    let fullBody = (item["body"] as? String) ?? summary
                    let originId = item["originId"] as? String
                    brain.cards.post(
                        source: source,
                        channel: channel,
                        summary: summary,
                        body: fullBody,
                        at: sentAt,
                        originId: originId
                    )
                }
            }
            return (200, [
                "ok": true,
                "activeCardCount": brain.cards.active.count,
                "dismissedOriginIds": brain.cards.dismissedOriginIds,
            ])
        }
        if method == "POST" && path == "/bounce_ws" {
            server.stop()
            try? await Task.sleep(nanoseconds: 250_000_000)
            server.start()
            return (200, ["ok": true, "pluginSessions": server.pluginSessions])
        }
        if method == "POST" && path == "/stt" {
            let mode = (payload["mode"] as? String) ?? "final"
            let text = (payload["text"] as? String) ?? ""
            if mode == "volatile" {
                brain.prompts.hearing(text)
            } else {
                let offset = (payload["spokeOffsetSeconds"] as? Double) ?? 0.0
                let spokeAt = Date().addingTimeInterval(offset)
                _ = brain.prompts.heard(text, spokeUntil: spokeAt)
            }
            return (200, ["ok": true, "promptActive": brain.prompts.active])
        }
        return (400, ["error": "unknown route \(method) \(path)"])
    }
}

let env = ProcessInfo.processInfo.environment
let wsPort = UInt16(env["JOAT_E2E_WS_PORT"] ?? "18996")!
let controlPort = UInt16(env["JOAT_E2E_CONTROL_PORT"] ?? "18999")!
let msgURL = env["JOAT_E2E_MESSAGES_URL"] ?? "http://127.0.0.1:18997"
let msgTok = env["JOAT_E2E_MESSAGES_TOKEN"] ?? ""
let termURL = env["JOAT_E2E_TERMINAL_URL"] ?? "http://127.0.0.1:18998"
let termTok = env["JOAT_E2E_TERMINAL_TOKEN"] ?? ""

var retainedHarness: AnyObject?
Task { @MainActor in
    do {
        retainedHarness = try PhoneBrainHarness(
            wsPort: wsPort,
            controlPort: controlPort,
            messagesURL: msgURL,
            messagesToken: msgTok,
            terminalURL: termURL,
            terminalToken: termTok
        )
    } catch {
        fputs("Failed to boot PhoneBrainHarness: \(error)\n", stderr)
        exit(1)
    }
}
RunLoop.main.run()
"""


def resolve_booted_simulator_udid(env: dict[str, str]) -> str:
    """Find a booted iOS Simulator UDID (or boot an available iPhone Simulator)."""
    out = subprocess.check_output(
        ["/usr/bin/xcrun", "simctl", "list", "devices", "available", "-j"],
        env=env,
    )
    data = json.loads(out)
    devices_by_runtime = data.get("devices", {})
    fallback: str | None = None
    for runtime, devices in devices_by_runtime.items():
        if "iOS" not in runtime:
            continue
        for dev in devices:
            if not dev.get("isAvailable"):
                continue
            udid = dev.get("udid") or ""
            if dev.get("state") == "Booted" and udid:
                return str(udid)
            if fallback is None and "iPhone" in (dev.get("name") or "") and udid:
                fallback = str(udid)
    if fallback:
        subprocess.run(
            ["/usr/bin/xcrun", "simctl", "boot", fallback],
            env=env,
            check=False,
            capture_output=True,
        )
        return fallback
    raise RuntimeError("No available iPhone Simulator found")


def compile_swift_brain_runner(repo: Path, build_dir: Path, env: dict[str, str]) -> Path:
    """Compile all real `apps/ios-companion/Sources/*.swift` into an arm64 simulator runner."""
    sources_dir = repo / "apps/ios-companion/Sources"
    swift_inputs: list[str] = []
    for path in sorted(sources_dir.glob("*.swift")):
        if path.name == "JOATBrainApp.swift":
            raw = path.read_text(encoding="utf-8")
            staged = build_dir / "JOATBrainApp_NoMain.swift"
            staged.write_text(raw.replace("\n@main\n", "\n// @main\n"), encoding="utf-8")
            swift_inputs.append(str(staged))
        else:
            swift_inputs.append(str(path))

    main_swift = build_dir / "main.swift"
    main_swift.write_text(SWIFT_HARNESS_MAIN, encoding="utf-8")
    out_bin = build_dir / "joat_phone_brain_e2e_runner"

    cmd = [
        "/usr/bin/xcrun",
        "--sdk",
        "iphonesimulator",
        "swiftc",
        "-target",
        "arm64-apple-ios26.0-simulator",
        "-swift-version",
        "5",
        "-suppress-warnings",
        "-DDEBUG",
        *swift_inputs,
        str(main_swift),
        "-o",
        str(out_bin),
    ]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    return out_bin


async def wait_http_ready(url: str, proc: subprocess.Popen[bytes] | None = None, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"Process exited early with code {proc.returncode} while waiting for {url}")
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=2.0).read())
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}")


async def swift_ctl(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"http://127.0.0.1:{SWIFT_CONTROL_PORT}{path}"
    raw = await asyncio.to_thread(http, url, payload)
    return dict(json.loads(raw))


def norm_ws(text: str) -> str:
    """Collapse newlines and repeated spaces so G2 line-wrapping never breaks multi-word OCR checks."""
    return " ".join(text.lower().split())


async def run_e2e(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    canary_ip = discover_non_loopback_ipv4()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ephem:
        ephem.bind(("127.0.0.1", 0))
        plugin_port = int(ephem.getsockname()[1])

    for host, port in (
        (canary_ip, CANARY_MAC_WS_PORT),
        ("127.0.0.1", PHONE_BRAIN_PORT),
        ("127.0.0.1", MESSAGES_RELAY_PORT),
        ("127.0.0.1", TERMINAL_RELAY_PORT),
        ("127.0.0.1", SWIFT_CONTROL_PORT),
        ("127.0.0.1", SIM_PORT),
    ):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))

    xcode_env = dict(os.environ)
    xcode_env.setdefault("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")

    scratch = tempfile.TemporaryDirectory(prefix="joat-phone-first-e2e-")
    work_dir = Path(scratch.name)
    processes: list[subprocess.Popen[bytes]] = []
    canary_mac = CanaryMacBrainServer(canary_ip, CANARY_MAC_WS_PORT)
    messages_endpoint: MessagesEndpoint | None = None
    terminal_endpoint: TerminalEndpoint | None = None
    roster: AgentTerminalRoster | None = None

    console_last_id: int | None = None
    bridge_ledger: list[dict[str, Any]] = []
    screens: list[dict[str, Any]] = []

    def start_proc(
        name: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        log_path = work_dir / f"{name}.log"
        with log_path.open("wb") as log_file:
            proc = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(proc)
            return proc

    async def poll_bridge_console() -> list[dict[str, Any]]:
        nonlocal console_last_id
        for _ in range(30):
            url = f"{AUTOMATION}/api/console"
            if console_last_id is not None:
                url += f"?since_id={console_last_id}"
            raw = await asyncio.to_thread(http, url)
            decoded = json.loads(raw)
            entries = decoded.get("entries") or []
            if not entries:
                break
            ids = [e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), int)]
            prev_last = console_last_id
            if ids:
                console_last_id = max(ids)
            for entry in entries:
                msg = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, str):
                    continue
                idx = msg.find(BRIDGE_MARKER)
                if idx >= 0:
                    parsed = json.loads(msg[idx + len(BRIDGE_MARKER) :].strip())
                    if isinstance(parsed, dict):
                        bridge_ledger.append(parsed)
            if console_last_id == prev_last or len(entries) < 50:
                break
        return list(bridge_ledger)

    async def press(action: str, delay: float = 0.65) -> None:
        await asyncio.to_thread(http, f"{AUTOMATION}/api/input", {"action": action})
        await asyncio.sleep(delay)

    ocr_bin = work_dir / "ocr"
    (work_dir / "ocr.swift").write_text(OCR_SOURCE, encoding="utf-8")
    subprocess.run(
        ["/usr/bin/xcrun", "swiftc", str(work_dir / "ocr.swift"), "-o", str(ocr_bin)],
        env=xcode_env,
        check=True,
        capture_output=True,
    )

    async def capture_screen(
        name: str,
        *,
        settle: float = 0.9,
        save_to_output: bool = False,
    ) -> str:
        await asyncio.sleep(settle)
        previous: bytes | None = None
        raw = b""
        for _ in range(18):
            raw = await asyncio.to_thread(http, f"{AUTOMATION}/api/screenshot/glasses")
            visible = Image.open(io.BytesIO(raw)).crop((0, 24, 576, 288)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(0.25)

        source = Image.open(io.BytesIO(raw)).convert("RGBA")
        optical = Image.new("RGBA", source.size, "black")
        optical.alpha_composite(source)
        rgb = optical.convert("RGB")
        target_path = (output_dir if save_to_output else work_dir) / f"{name}.png"
        rgb.save(target_path)

        ocr_channel = source.getchannel("A").point(lambda p: 255 if p > 8 else 0)
        ocr_input = work_dir / "ocr-input.png"
        ocr_channel.resize((2304, 1152)).save(ocr_input)
        ocr = await asyncio.to_thread(
            subprocess.run,
            [str(ocr_bin), str(ocr_input)],
            env=xcode_env,
            check=True,
            capture_output=True,
            text=True,
        )
        text = "\n".join(json.loads(ocr.stdout))
        await poll_bridge_console()
        screens.append(
            {
                "name": name,
                "saved_to_output": save_to_output,
                "path": str(target_path) if save_to_output else None,
                "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                "ocr": text,
            }
        )
        print(f"=== SCREEN {name} ===\n{text}\n", flush=True)
        return text

    async def wait_brain_state(
        predicate: Callable[[dict[str, Any]], bool],
        label: str,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await swift_ctl("/state")
            if predicate(last):
                return last
            await asyncio.sleep(0.12)
        raise AssertionError(f"Timed out waiting for {label}; last state keys={list(last)}")

    try:
        # 1. Start Canary Mac WS + REAL Python MessagesEndpoint & TerminalEndpoint
        await canary_mac.start()

        msg_store = EphemeralMessagesStore()
        msg_sender = RecordingMessageSender()
        messages_endpoint = MessagesEndpoint(
            msg_store,  # type: ignore[arg-type]
            msg_sender,
            TEST_MESSAGES_TOKEN,
            host="127.0.0.1",
            port=MESSAGES_RELAY_PORT,
            poll_s=0.1,
        )
        await messages_endpoint.start()

        joat_dir = work_dir / "JOAT"
        joat_dir.mkdir(mode=0o700)
        workspace_dir = work_dir / "workspace"
        workspace_dir.mkdir()
        gemini_key_path = joat_dir / "gemini-api-key"
        store_gemini_api_key(TEST_GEMINI_SENTINEL, gemini_key_path)

        claude_agent = StreamingTerminalLaneAgent("claude-code", str(workspace_dir))
        claude_agent.sessions = [
            {"key": "sess-ble-coalescer", "label": "Refactor BLE Frame Coalescer"},
            {"key": "sess-relay-auth", "label": "Audit Relay Bearer Auth"},
        ]
        claude_agent.session_histories["sess-ble-coalescer"] = [
            ("user", "Inspect BLE tile coalescing"),
            ("assistant", "Coalescer batches dirty regions into one frame."),
        ]
        codex_agent = StreamingTerminalLaneAgent("codex", str(workspace_dir))
        ag_agent = StreamingTerminalLaneAgent("antigravity", str(workspace_dir))

        roster = AgentTerminalRoster(
            [("claude", claude_agent), ("codex", codex_agent), ("antigravity", ag_agent)],
            visible_lanes=("claude", "codex", "antigravity"),
        )
        registry = AccountRegistry(root=joat_dir / "accounts")
        signin = EphemeralSignIn(registry)
        models = ModelRegistry([*default_slots(), assistant_slot("claude")])
        terminal_endpoint = TerminalEndpoint(
            roster=roster,
            models=models,
            registry=registry,
            signin=signin,  # type: ignore[arg-type]
            token=TEST_TERMINAL_TOKEN,
            host="127.0.0.1",
            port=TERMINAL_RELAY_PORT,
            config_dir=joat_dir,
            gemini_key_path=gemini_key_path,
            antigravity_installed=lambda: True,
        )
        await terminal_endpoint.probe_all()
        await terminal_endpoint.start()

        # 2. Compile & spawn REAL Swift `JOAT Brain` inside iOS Simulator
        print("Compiling real Swift JOAT Brain harness...", flush=True)
        swift_build_dir = work_dir / "swift_build"
        swift_build_dir.mkdir()
        runner_bin = await asyncio.to_thread(
            compile_swift_brain_runner, ROOT, swift_build_dir, xcode_env
        )
        sim_udid = await asyncio.to_thread(resolve_booted_simulator_udid, xcode_env)
        sim_home = work_dir / "sim_home"
        sim_home.mkdir(mode=0o700)

        sim_env = dict(xcode_env)
        sim_env.update(
            {
                "SIMCTL_CHILD_HOME": str(sim_home),
                "SIMCTL_CHILD_JOAT_E2E_WS_PORT": str(PHONE_BRAIN_PORT),
                "SIMCTL_CHILD_JOAT_E2E_CONTROL_PORT": str(SWIFT_CONTROL_PORT),
                "SIMCTL_CHILD_JOAT_E2E_MESSAGES_URL": f"http://127.0.0.1:{MESSAGES_RELAY_PORT}",
                "SIMCTL_CHILD_JOAT_E2E_MESSAGES_TOKEN": TEST_MESSAGES_TOKEN,
                "SIMCTL_CHILD_JOAT_E2E_TERMINAL_URL": f"http://127.0.0.1:{TERMINAL_RELAY_PORT}",
                "SIMCTL_CHILD_JOAT_E2E_TERMINAL_TOKEN": TEST_TERMINAL_TOKEN,
            }
        )
        swift_proc = start_proc(
            "swift-phone-brain",
            ["/usr/bin/xcrun", "simctl", "spawn", sim_udid, str(runner_bin)],
            env=sim_env,
        )
        await wait_http_ready(f"http://127.0.0.1:{SWIFT_CONTROL_PORT}/health", swift_proc)

        # 3. Boot REAL `apps/hub-plugin` Vite server + REAL `evenhub-simulator`
        phone_ws_url = f"ws://127.0.0.1:{PHONE_BRAIN_PORT}/glasses"
        canary_mac_ws_url = f"ws://{canary_ip}:{CANARY_MAC_WS_PORT}/glasses"
        plugin_root = prepare_plugin_root(ROOT, work_dir, [phone_ws_url, canary_mac_ws_url])

        vite_env = dict(os.environ, VITE_MAC_WS_URL=f"{phone_ws_url},{canary_mac_ws_url}")
        vite_proc = start_proc(
            "vite",
            [
                str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"),
                str(plugin_root),
                "--config",
                str(ROOT / "apps/hub-plugin/vite.config.ts"),
                "--host",
                "127.0.0.1",
                "--port",
                str(plugin_port),
                "--strictPort",
            ],
            env=vite_env,
        )
        await wait_http_ready(f"http://127.0.0.1:{plugin_port}/", vite_proc)

        sim_proc = start_proc(
            "evenhub-simulator",
            [
                str(Path.home() / ".local/bin/evenhub-simulator"),
                "--no-aid",
                f"http://127.0.0.1:{plugin_port}/?joatTestPromptMic=1",
                "--automation-port",
                str(SIM_PORT),
            ],
        )
        await wait_http_ready(f"{AUTOMATION}/api/screenshot/glasses", sim_proc, timeout_s=35.0)

        await wait_brain_state(
            lambda st: st.get("authenticated") is True and any(
                u.get("kind") == "hello" for u in st.get("uplinks", [])
            ),
            "initial plugin WebSocket auth + hello on Swift JOAT Brain",
            timeout_s=30.0,
        )
        await capture_screen("diag-00-boot-home", settle=1.5)

        # =====================================================================
        # SCENARIO 1: Notifications True Age (`15m ago`, `2h ago`, `Now`) & 1/3 Count
        # =====================================================================
        print("Running Scenario 1: Notification true elapsed age & 1/3 count...", flush=True)
        bridge_mark_s1 = len(await poll_bridge_console())
        injected = await swift_ctl(
            "/inject_notifications",
            {
                "items": [
                    {
                        "kind": "mac_message",
                        "rowId": 701,
                        "source": "Alex Rivera",
                        "summary": "Lobby doors are open!",
                        "ageSeconds": 5,
                    },
                    {
                        "kind": "card",
                        "source": "Sarah Chen",
                        "channel": "Mail",
                        "summary": "Q4 Launch — Sign-off ready",
                        "body": "Q4 Launch — Sign-off ready",
                        "ageSeconds": 2 * 3600 + 120,
                        "originId": "mail:acct1:702",
                    },
                    {
                        "kind": "mac_message",
                        "rowId": 703,
                        "source": "Dele",
                        "summary": "Prototype PCB arrived on bench 2.",
                        "ageSeconds": 15 * 60 + 20,
                    },
                ]
            },
        )
        assert injected["activeCardCount"] == 3, f"Expected 3 active cards, got {injected}"

        s1_ocr = await capture_screen(
            "sim-e2e-01-notifications-age-and-count",
            settle=0.6,
            save_to_output=True,
        )
        s1_lower = norm_ws(s1_ocr)
        assert "15m ago" in s1_lower, f"Expected '15m ago' on popup card, got:\n{s1_ocr}"
        assert "1/3" in s1_lower, f"Expected '1/3' unread position on popup card, got:\n{s1_ocr}"
        assert "dele" in s1_lower, f"Expected sender 'Dele' on popup card, got:\n{s1_ocr}"
        assert "now · 1/3" not in s1_lower, f"Old notification wrongly showed 'Now':\n{s1_ocr}"

        s1_bridge = [
            ev for ev in (await poll_bridge_console())[bridge_mark_s1:]
            if ev.get("operation") == "rebuild"
            and any(t.get("containerName") == "note-meta" for t in ev.get("text", []))
        ]
        assert s1_bridge, "Expected native notification card rebuild in SDK bridge ledger"
        latest_meta = next(
            t["content"] for t in s1_bridge[-1]["text"] if t.get("containerName") == "note-meta"
        )
        assert latest_meta == "15m ago · 1/3", f"Expected exact note-meta '15m ago · 1/3', got {latest_meta!r}"

        state_s1 = await swift_ctl("/state")
        ambient_frames = [d for d in state_s1["downlinks"] if d.get("kind") == "hud.ambient"]
        notif_frames = [d for d in state_s1["downlinks"] if d.get("kind") == "hud.notifications"]
        assert ambient_frames and ambient_frames[-1].get("unread") == 3, (
            f"Expected hud.ambient unread==3, got {ambient_frames[-1] if ambient_frames else None}"
        )
        assert notif_frames and len(notif_frames[-1].get("cards", [])) == 3, (
            f"Expected 3 cards in hud.notifications, got {notif_frames[-1] if notif_frames else None}"
        )

        # Verify wire ordering: `hud.notifications` published BEFORE `hud.text` popup!
        downlink_kinds = [
            (idx, d.get("kind"), d.get("contentKind"))
            for idx, d in enumerate(state_s1["downlinks"])
        ]
        last_popup_idx = max(
            idx for idx, kind, ckind in downlink_kinds if kind == "hud.text" and ckind == "notification"
        )
        preceding_notifs = [
            idx for idx, kind, _ in downlink_kinds if kind == "hud.notifications" and idx < last_popup_idx
        ]
        assert preceding_notifs, "Expected hud.notifications before hud.text notification popup"

        # =====================================================================
        # SCENARIO 2: Tap (`2h ago · 1/2`), `2x tap` Dismiss -> 0 Unread -> Reconnect
        # =====================================================================
        print("Running Scenario 2: Tap + 2x tap dismiss + WebSocket reconnect...", flush=True)
        # Tap once on popup card -> marks `15m ago` read and advances to `Sarah Chen` (`2h ago · 1/2`)
        await press("click", delay=0.55)
        s2_second_ocr = await capture_screen("diag-01-second-card-2h-ago", settle=0.45)
        s2_second_norm = norm_ws(s2_second_ocr)
        assert "2h ago" in s2_second_norm and "1/2" in s2_second_norm, (
            f"Expected '2h ago' and '1/2' after single tap, got:\n{s2_second_ocr}"
        )
        assert "sarah chen" in s2_second_norm, (
            f"Expected 'Sarah Chen' on second card, got:\n{s2_second_ocr}"
        )

        # Double-tap (`2x tap`) on popup card -> dismisses remaining unread cards & closes popup!
        await press("double_click", delay=0.75)
        state_after_dismiss = await wait_brain_state(
            lambda st: st.get("activeCardCount") == 0 and len(st.get("dismissedOriginIds", [])) == 3,
            "all 3 notification cards dismissed on Swift JOAT Brain",
            timeout_s=10.0,
        )
        assert set(state_after_dismiss["dismissedOriginIds"]) == {
            "msg:mac-701",
            "mail:acct1:702",
            "msg:mac-703",
        }, f"Expected all 3 origin tombstones recorded, got {state_after_dismiss['dismissedOriginIds']}"

        # Disconnect & reconnect WebSocket (`bounce_ws` stops & restarts LoopbackServer)
        sessions_before = int(state_after_dismiss["pluginSessions"])
        bridge_mark_reconnect = len(await poll_bridge_console())
        await swift_ctl("/bounce_ws", {"bounce": True})
        state_after_reconnect = await wait_brain_state(
            lambda st: st.get("authenticated") is True and int(st.get("pluginSessions", 0)) > sessions_before,
            "plugin WebSocket reconnected to Swift JOAT Brain",
            timeout_s=20.0,
        )

        # Also attempt to re-inject the same 3 origins after reconnect to prove tombstone dedup!
        re_injected = await swift_ctl(
            "/inject_notifications",
            {
                "items": [
                    {
                        "kind": "mac_message",
                        "rowId": 701,
                        "source": "Alex Rivera",
                        "summary": "Lobby doors are open!",
                        "ageSeconds": 5,
                    },
                    {
                        "kind": "card",
                        "source": "Sarah Chen",
                        "channel": "Mail",
                        "summary": "Q4 Launch — Sign-off ready",
                        "ageSeconds": 2 * 3600 + 120,
                        "originId": "mail:acct1:702",
                    },
                    {
                        "kind": "mac_message",
                        "rowId": 703,
                        "source": "Dele",
                        "summary": "Prototype PCB arrived on bench 2.",
                        "ageSeconds": 15 * 60 + 20,
                    },
                ]
            },
        )
        assert re_injected["activeCardCount"] == 0, (
            f"Dismissed notifications resurrected on replay: {re_injected}"
        )

        s2_ocr = await capture_screen(
            "sim-e2e-02-notifications-dismiss-reconnect",
            settle=1.2,
            save_to_output=True,
        )
        s2_lower = norm_ws(s2_ocr)
        assert "prototype pcb" not in s2_lower and "sign-off ready" not in s2_lower, (
            f"Dismissed notification reappeared visually after reconnect:\n{s2_ocr}"
        )
        reconnect_popups = [
            ev for ev in (await poll_bridge_console())[bridge_mark_reconnect:]
            if ev.get("operation") == "rebuild"
            and any(t.get("containerName") == "note-meta" for t in ev.get("text", []))
        ]
        assert not reconnect_popups, f"Unexpected notification popup after reconnect: {reconnect_popups}"

        # =====================================================================
        # SCENARIO 3: Messages via Mac Relay + Multi-Clause Dictation (`Prompts.swift`)
        # =====================================================================
        print("Running Scenario 3: Messages via Mac relay + multi-clause dictation...", flush=True)
        canary_before_capabilities = canary_mac.connection_count
        sessions_before_capabilities = int(state_after_reconnect["pluginSessions"])

        # Open contextual menu -> row 0 is `Messages` -> click
        await press("context_menu", delay=0.6)
        await press("click", delay=0.9)

        # Wait for `MESSAGES` list populated from `MessagesEndpoint` (`Maya Lin`)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "hud.list"
                and d.get("title") == "MESSAGES"
                and any("Maya Lin" in str(item) for item in d.get("items", []))
                for d in st.get("downlinks", [])
            ),
            "MESSAGES list loaded from real Python MessagesEndpoint",
            timeout_s=15.0,
        )
        assert canary_mac.connection_count == canary_before_capabilities, (
            f"BrainHandoff hijacked WebSocket to canary Mac ({canary_mac.connection_count - canary_before_capabilities} connections)!"
        )
        s3_list_ocr = await capture_screen("diag-02-messages-list", settle=0.8)
        assert "maya lin" in norm_ws(s3_list_ocr), (
            f"Expected Mac thread 'Maya Lin' in MESSAGES list, got:\n{s3_list_ocr}"
        )

        # Highlight row 1 (`Maya Lin`) and click -> `MESSAGES · CONVERSATION` with Mac history
        await press("down", delay=0.45)
        await press("click", delay=0.9)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "hud.list"
                and d.get("title") == "MESSAGES · CONVERSATION"
                and "Blue Bottle" in str(d.get("body", ""))
                for d in st.get("downlinks", [])
            ),
            "MESSAGES · CONVERSATION history loaded from real Python MessagesEndpoint",
            timeout_s=15.0,
        )
        s3_thread_ocr = await capture_screen("diag-03-messages-thread", settle=0.8)
        assert "maya lin" in norm_ws(s3_thread_ocr) and "blue bottle" in norm_ws(s3_thread_ocr), (
            f"Expected Maya Lin history ('Blue Bottle') on thread reader, got:\n{s3_thread_ocr}"
        )

        # Tap thread to reply -> `Prompts.ask` (`field: "body"`) -> wait for `prompt.ack`
        downlink_mark_reply = len((await swift_ctl("/state"))["downlinks"])
        await press("click", delay=0.6)
        await wait_brain_state(
            lambda st: st.get("promptActive") is True and any(
                u.get("kind") == "prompt.ack" and u.get("success") is True
                for u in st.get("uplinks", [])
            ),
            "Prompts.swift active with prompt.ack (success=true) from simulator",
            timeout_s=12.0,
        )

        # Feed consecutive clause `.final` #1 -> wait 0.6s -> must NOT cut off at first period!
        clause_1 = "Hey I'm five minutes late."
        clause_2 = "Grab us a booth inside."
        combined_expected = f"{clause_1} {clause_2}"

        r1 = await swift_ctl("/stt", {"mode": "final", "text": clause_1})
        assert r1["promptActive"] is True, "Prompts.swift prematurely closed on first clause!"
        await asyncio.sleep(0.6)
        mid_state = await swift_ctl("/state")
        assert mid_state["promptActive"] is True, (
            "Prompts.swift cut off after 0.6s gap before second clause!"
        )

        # Feed consecutive clause `.final` #2 -> both sentences appear together in `prompt.partial`!
        r2 = await swift_ctl("/stt", {"mode": "final", "text": clause_2})
        assert r2["promptActive"] is True, "Prompts.swift should hold 1.5s settle window after clause 2"
        after_c2_state = await wait_brain_state(
            lambda st: any(
                d.get("kind") == "prompt.partial" and d.get("text") == combined_expected
                for d in st.get("downlinks", [])[downlink_mark_reply:]
            ),
            "combined two-sentence prompt.partial emitted by Prompts.swift",
            timeout_s=5.0,
        )
        s3_partial_ocr = await capture_screen("diag-04-messages-dictation-partial", settle=0.35)
        s3_partial_norm = norm_ws(s3_partial_ocr)
        assert "five minutes late" in s3_partial_norm and "booth inside" in s3_partial_norm, (
            f"Expected both clauses visible live in prompt.partial card, got:\n{s3_partial_ocr}"
        )

        # Wait for 1.5s `settleWorkItem` -> `ConfirmGate` emits `Send to Maya Lin...?` with BOTH sentences!
        confirm_state = await wait_brain_state(
            lambda st: any(
                d.get("kind") == "confirm.request"
                and clause_1 in str(d.get("prompt", ""))
                and clause_2 in str(d.get("prompt", ""))
                for d in st.get("downlinks", [])[downlink_mark_reply:]
            ),
            "confirm.request containing both sentences from Prompts.swift",
            timeout_s=8.0,
        )
        assert canary_mac.connection_count == canary_before_capabilities
        assert not any(d.get("kind") == "brain.handoff" for d in confirm_state["downlinks"]), (
            "Unexpected brain.handoff frame emitted!"
        )

        s3_confirm_ocr = await capture_screen(
            "sim-e2e-03-messages-via-mac-relay-dictation",
            settle=0.8,
            save_to_output=True,
        )
        s3_confirm_lower = norm_ws(s3_confirm_ocr)
        assert "send to maya lin" in s3_confirm_lower or "say to maya lin" in s3_confirm_lower, (
            f"Expected Maya Lin confirmation card on lens, got:\n{s3_confirm_ocr}"
        )
        assert "(...)" not in s3_confirm_lower and "(…)" not in s3_confirm_lower, (
            f"Empty handle placeholder leaked into Mac-thread confirmation card:\n{s3_confirm_ocr}"
        )
        assert "five minutes late" in s3_confirm_lower and "booth inside" in s3_confirm_lower, (
            f"Expected BOTH sentences in confirmation card without cutoff, got:\n{s3_confirm_ocr}"
        )

        # Tap (`click`) to confirm -> verify real `MessagesEndpoint` `/v1/send` receives both sentences!
        await press("click", delay=0.9)
        await until(lambda: len(msg_sender.sent) == 1, timeout=10)
        assert msg_sender.sent[0] == (
            "chat",
            "iMessage;-;+14055550142",
            combined_expected,
        ), f"Expected exact Mac relay send of combined sentences, got {msg_sender.sent}"

        # =====================================================================
        # SCENARIO 4: 3-Level Agent Terminal via Mac Relay (`/terminal`) + Streaming
        # =====================================================================
        print("Running Scenario 4: 3-Level Agent Terminal via Mac relay...", flush=True)
        downlink_mark_term = len((await swift_ctl("/state"))["downlinks"])

        # Open contextual menu -> `Agent Terminal` is index 5 (`Messages`, `Mail`, `Reminders`, `Navigation`, `Search`, `Agent Terminal`)
        await press("context_menu", delay=0.55)
        for _ in range(5):
            await press("down", delay=0.28)
        await press("click", delay=0.9)

        # Verify Level 1 (`AGENT TERMINAL`: `Claude Code`, `Codex`, `Antigravity`)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "hud.list"
                and d.get("title") == "AGENT TERMINAL"
                and d.get("selectable") is True
                and len(d.get("items", [])) == 3
                for d in st.get("downlinks", [])[downlink_mark_term:]
            ),
            "Level 1 AGENT TERMINAL picker from real TerminalEndpoint",
            timeout_s=15.0,
        )
        s4_l1_ocr = await capture_screen("diag-05-terminal-level1-agents", settle=0.8)
        for expected_agent in ("claude code", "codex", "antigravity"):
            assert expected_agent in norm_ws(s4_l1_ocr), (
                f"Expected {expected_agent!r} in Level 1 AGENT TERMINAL picker, got:\n{s4_l1_ocr}"
            )

        # Tap `Claude Code` (row 0) -> Level 2 (`AGENT TERMINAL · CLAUDE`: `+ New session` + sessions from `/terminal`)
        await press("click", delay=0.8)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "hud.list"
                and d.get("title") == "AGENT TERMINAL · CLAUDE"
                and "+ New session" in d.get("items", [])
                and "Refactor BLE Frame Coalescer" in d.get("items", [])
                for d in st.get("downlinks", [])[downlink_mark_term:]
            ),
            "Level 2 AGENT TERMINAL · CLAUDE session picker with saved /terminal sessions",
            timeout_s=15.0,
        )
        s4_l2_ocr = await capture_screen("diag-06-terminal-level2-sessions", settle=0.8)
        assert "new session" in norm_ws(s4_l2_ocr) and "coalescer" in norm_ws(s4_l2_ocr), (
            f"Expected '+ New session' and 'Refactor BLE Frame Coalescer' on Level 2, got:\n{s4_l2_ocr}"
        )

        # Move down to row 1 (`Refactor BLE Frame Coalescer`) and tap -> resumes session & opens Level 3 5-container `agent_terminal.view`!
        bridge_mark_l3 = len(await poll_bridge_console())
        await press("down", delay=0.4)
        await press("click", delay=0.9)

        await until(lambda: claude_agent.resumed_sessions == ["sess-ble-coalescer"], timeout=10)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "agent_terminal.view"
                for d in st.get("downlinks", [])[downlink_mark_term:]
            ),
            "Level 3 agent_terminal.view emitted by GlassesTerminal.swift",
            timeout_s=15.0,
        )
        await capture_screen("diag-07-terminal-level3-resumed", settle=0.8)

        l3_events = (await poll_bridge_console())[bridge_mark_l3:]
        l3_rebuilds = [
            ev for ev in l3_events
            if ev.get("operation") in ("rebuild", "rebuild-start")
            and (
                ev.get("containerTotalNum") == 5
                or len(ev.get("text", [])) == 5
                or any("term" in str(t.get("containerName", "")) for t in ev.get("text", []))
            )
        ]
        assert l3_rebuilds or "tap: speak" in norm_ws(
            screens[-1]["ocr"]
        ), f"Expected 5-container native terminal surface on Level 3, events={l3_events}"

        # Tap on Level 3 card -> `terminal.action` `begin_terminal_input` -> `Prompts.swift` (`field: "instruction"`)
        prompt_ack_count_before = sum(
            1 for u in (await swift_ctl("/state"))["uplinks"]
            if u.get("kind") == "prompt.ack" and u.get("success") is True
        )
        await press("click", delay=0.65)
        await wait_brain_state(
            lambda st: st.get("promptActive") is True
            and sum(
                1 for u in st.get("uplinks", [])
                if u.get("kind") == "prompt.ack" and u.get("success") is True
            ) > prompt_ack_count_before,
            "Prompts.swift active for terminal instruction",
            timeout_s=12.0,
        )

        # Speak instruction via `Prompts.swift` and tap (`prompt-commit`) for 0ms commit!
        spoken_instruction = "Verify the BLE frame coalescer suite and report status."
        await swift_ctl("/stt", {"mode": "final", "text": spoken_instruction})
        await asyncio.sleep(0.25)
        await press("click", delay=0.4)  # 0ms tap-to-commit (`prompt-commit`)!

        await until(lambda: claude_agent.prompts == [spoken_instruction], timeout=12)
        await wait_brain_state(
            lambda st: any(
                d.get("kind") == "agent_terminal.view"
                and d.get("terminal", {}).get("state") == "ready"
                and any(
                    "76 suites green" in str(m.get("text", ""))
                    for m in d.get("terminal", {}).get("messages", [])
                )
                for d in st.get("downlinks", [])[downlink_mark_term:]
            ),
            "streamed TerminalEndpoint response rendered onto Level 3 agent_terminal.view",
            timeout_s=15.0,
        )

        s4_final_ocr = await capture_screen(
            "sim-e2e-04-agent-terminal-via-mac-relay",
            settle=0.9,
            save_to_output=True,
        )
        s4_lower = norm_ws(s4_final_ocr)
        assert "76 suites green" in s4_lower or "zero dropped tiles" in s4_lower, (
            f"Expected streamed Claude Code response on 5-container terminal card, got:\n{s4_final_ocr}"
        )

        final_state = await swift_ctl("/state")
        assert canary_mac.connection_count == canary_before_capabilities, (
            "Expected 0 canary Mac WebSocket connections across Messages & Agent Terminal, "
            f"got {canary_mac.connection_count - canary_before_capabilities}"
        )
        assert int(final_state["pluginSessions"]) == sessions_before_capabilities, (
            "Expected phone brain WebSocket session to remain 100% unbroken across Messages & Agent Terminal, "
            f"got {sessions_before_capabilities} -> {final_state['pluginSessions']}"
        )
        assert not any(d.get("kind") == "brain.handoff" for d in final_state["downlinks"]), (
            "Expected ZERO brain.handoff frames across entire run!"
        )

        receipt = {
            "result": "passed",
            "ports": {
                "canary_mac_ws": CANARY_MAC_WS_PORT,
                "phone_brain_ws": PHONE_BRAIN_PORT,
                "messages_relay_http": MESSAGES_RELAY_PORT,
                "terminal_relay_http": TERMINAL_RELAY_PORT,
                "swift_control_http": SWIFT_CONTROL_PORT,
                "vite_plugin_http": plugin_port,
                "evenhub_simulator_http": SIM_PORT,
            },
            "assertions": {
                "canary_mac_ws_handoff_connections": canary_mac.connection_count - canary_before_capabilities,
                "unbroken_phone_ws_session_across_capabilities": int(final_state["pluginSessions"]) == sessions_before_capabilities,
                "brain_handoff_frames": 0,
                "notification_popup_meta": latest_meta,
                "reconnect_plugin_sessions": state_after_reconnect["pluginSessions"],
                "dismissed_origin_tombstones": state_after_dismiss["dismissedOriginIds"],
                "mac_messages_sent": msg_sender.sent,
                "terminal_resumed_sessions": claude_agent.resumed_sessions,
                "terminal_prompts_received": claude_agent.prompts,
            },
            "screenshots": [s for s in screens if s["saved_to_output"]],
        }
        receipt_path = output_dir / "phone-first-e2e-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(f"SUCCESS: All 4 real evenhub-simulator E2E scenarios passed! Receipt: {receipt_path}", flush=True)
        return receipt
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 15)
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        os.killpg(proc.pid, 9)
                    except Exception:
                        pass
        if roster is not None:
            for name in roster.names():
                lane = roster.lane(name)
                if lane is not None and lane.task is not None and not lane.task.done():
                    lane.task.cancel()
                    await asyncio.gather(lane.task, return_exceptions=True)
        if terminal_endpoint is not None:
            await terminal_endpoint.stop()
        if messages_endpoint is not None:
            await messages_endpoint.stop()
        await canary_mac.stop()
        scratch.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "joat-phone-first-e2e",
        help="Directory where the 4 E2E verification PNG screenshots and receipt JSON are written.",
    )
    args = parser.parse_args()
    asyncio.run(run_e2e(args.output_dir.resolve()))


if __name__ == "__main__":
    main()
