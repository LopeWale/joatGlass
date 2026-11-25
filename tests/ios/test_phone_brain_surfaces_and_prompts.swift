// Comprehensive unit & integration test suite for Phone-First JOAT Brain:
// 1. `Cards` persistence (`joat.cards.store.v2`), `originId` deduplication,
//    200-ID FIFO dismissal tombstones across reconnects & process restarts,
//    `keep = 25` 1:1 unread count parity across `hud.notifications` / `hud.ambient` / `NOTIFICATIONS`,
//    exact elapsed `at` timestamps from `Messages` & `Mail`, and critical wire ordering
//    (`publish()` BEFORE `hud.text` notification popup!).
// 2. `Prompts` + `Brain` pre-prompt fence (`spokeUntil < listeningSince ?? askedAt`),
//    multi-clause dictation accumulation (`1.5s` `settleWorkItem` on `body` / `instruction`),
//    volatile speech sliding window, `0 ms` tap commit (`prompt-commit`), and `Messages`
//    `"Loading Messages from your Mac…"` sync state.
// 3. `CapabilityMenu` zero-handoff routing (`CapabilityMenu.macPages == []`, `Messages`,
//    `Agent Terminal`, `Activity` summary rows, and direct voice opens `"open Claude"`,
//    `"open Codex"`, `"open Antigravity"`).
// 4. `MacTerminal` (`SessionItem` + `resumeSession`) and 3-level `GlassesTerminal`
//    (`AGENT TERMINAL` -> `AGENT TERMINAL · <LANE>` -> 5-container `agent_terminal.view`),
//    live polling deduplication, `[!] Approval needed: ...` formatting, and all 5
//    `terminal.action` gestures (`begin_terminal_input`, `approve_terminal`, `deny_terminal`,
//    `cancel_terminal`, `retry_terminal`).
//
// Run directly via:
//   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift \
//     tests/ios/test_phone_brain_surfaces_and_prompts.swift

import Foundation

#if !JOAT_COMPILED_WITH_SOURCES
private func resolveSimulatorUDID(env: [String: String]) -> String {
    let listProc = Process()
    listProc.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    listProc.arguments = ["simctl", "list", "devices", "available", "-j"]
    listProc.environment = env
    let pipe = Pipe()
    listProc.standardOutput = pipe
    try? listProc.run()
    listProc.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    if let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let devicesByRuntime = root["devices"] as? [String: [[String: Any]]] {
        var fallbackIphone: String?
        for (runtime, devices) in devicesByRuntime where runtime.contains("iOS") {
            for dev in devices where (dev["isAvailable"] as? Bool) == true {
                let state = dev["state"] as? String ?? ""
                let name = dev["name"] as? String ?? ""
                let udid = dev["udid"] as? String ?? ""
                if state == "Booted", !udid.isEmpty {
                    return udid
                }
                if fallbackIphone == nil, name.contains("iPhone"), !udid.isEmpty {
                    fallbackIphone = udid
                }
            }
        }
        if let udid = fallbackIphone {
            let boot = Process()
            boot.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
            boot.arguments = ["simctl", "boot", udid]
            boot.environment = env
            try? boot.run()
            boot.waitUntilExit()
            return udid
        }
    }
    return "booted"
}

func runEntry() {
    let testFile = URL(fileURLWithPath: #filePath)
    let repoRoot = testFile
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let sourcesDir = repoRoot.appendingPathComponent("apps/ios-companion/Sources")

    let tempDir = FileManager.default.temporaryDirectory
        .appendingPathComponent("joat_test_phone_brain_\(ProcessInfo.processInfo.processIdentifier)")
    try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempDir) }

    var env = ProcessInfo.processInfo.environment
    if env["DEVELOPER_DIR"] == nil {
        let defaultXcode = "/Applications/Xcode.app/Contents/Developer"
        if FileManager.default.fileExists(atPath: defaultXcode) {
            env["DEVELOPER_DIR"] = defaultXcode
        }
    }

    guard let entries = try? FileManager.default.contentsOfDirectory(
        at: sourcesDir,
        includingPropertiesForKeys: nil
    ) else {
        fputs("Failed to list Sources directory at \(sourcesDir.path)\n", stderr)
        exit(1)
    }

    var swiftInputs: [String] = []
    for url in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
    where url.pathExtension == "swift" {
        if url.lastPathComponent == "JOATBrainApp.swift" {
            guard let raw = try? String(contentsOf: url, encoding: .utf8) else {
                fputs("Failed to read JOATBrainApp.swift\n", stderr)
                exit(1)
            }
            let stripped = raw.replacingOccurrences(of: "\n@main\n", with: "\n// @main\n")
            let staged = tempDir.appendingPathComponent("JOATBrainApp_NoMain.swift")
            try? stripped.write(to: staged, atomically: true, encoding: .utf8)
            swiftInputs.append(staged.path)
        } else {
            swiftInputs.append(url.path)
        }
    }

    let mainSwift = tempDir.appendingPathComponent("main.swift")
    let outBinary = tempDir.appendingPathComponent("phone_brain_runner")
    do {
        try FileManager.default.copyItem(at: testFile, to: mainSwift)
    } catch {
        fputs("Failed to stage main.swift: \(error)\n", stderr)
        exit(1)
    }

    let compile = Process()
    compile.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    compile.arguments = [
        "--sdk", "iphonesimulator",
        "swiftc",
        "-target", "arm64-apple-ios26.0-simulator",
        "-swift-version", "5",
        "-suppress-warnings",
        "-DDEBUG",
        "-DJOAT_COMPILED_WITH_SOURCES",
    ] + swiftInputs + [mainSwift.path, "-o", outBinary.path]
    compile.environment = env
    do {
        try compile.run()
        compile.waitUntilExit()
    } catch {
        fputs("Failed to launch swiftc: \(error)\n", stderr)
        exit(1)
    }
    if compile.terminationStatus != 0 {
        exit(compile.terminationStatus)
    }

    let simUDID = resolveSimulatorUDID(env: env)
    let runProc = Process()
    runProc.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    runProc.arguments = ["simctl", "spawn", simUDID, outBinary.path]
    runProc.environment = env
    do {
        try runProc.run()
        runProc.waitUntilExit()
        exit(runProc.terminationStatus)
    } catch {
        fputs("Failed to spawn simulator test runner: \(error)\n", stderr)
        exit(1)
    }
}
#else
import UIKit
import PencilKit

// MARK: - Assertions & Mock Infrastructure

private var passedAssertions = 0

private func expect(
    _ condition: @autoclosure () -> Bool,
    _ message: String,
    file: StaticString = #file,
    line: UInt = #line
) {
    if !condition() {
        fputs("FAIL [\(file):\(line)]: \(message)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

private func expectEqual<T: Equatable>(
    _ actual: T,
    _ expected: T,
    _ message: String,
    file: StaticString = #file,
    line: UInt = #line
) {
    if actual != expected {
        fputs("FAIL [\(file):\(line)]: \(message)\n  actual:   \(actual)\n  expected: \(expected)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

private final class RecordingLink: PluginLink {
    private let lock = NSLock()
    private var storage: [[String: Any]] = []

    var sent: [[String: Any]] {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func send(_ object: [String: Any]) {
        lock.lock()
        storage.append(object)
        lock.unlock()
    }

    func clear() {
        lock.lock()
        storage.removeAll()
        lock.unlock()
    }
}

private final class MockRelayURLProtocol: URLProtocol {
    struct RecordedRequest {
        let method: String
        let url: String
        let authorization: String?
        let body: [String: Any]?
    }

    struct CannedResponse {
        let statusCode: Int
        let body: Data
    }

    private static let lock = NSLock()
    private static var handler: ((URLRequest, [String: Any]?) -> CannedResponse)?
    private static var requests: [RecordedRequest] = []

    static func install(_ h: @escaping (URLRequest, [String: Any]?) -> CannedResponse) {
        lock.lock()
        handler = h
        requests = []
        lock.unlock()
    }

    static func recordedRequests() -> [RecordedRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    static func clearRequests() {
        lock.lock()
        requests.removeAll()
        lock.unlock()
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open()
            var buf = [UInt8](repeating: 0, count: 4096)
            var out = Data()
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n > 0 { out.append(buf, count: n) } else { break }
            }
            stream.close()
            data = out
        }
        let parsedBody = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }

        Self.lock.lock()
        Self.requests.append(
            RecordedRequest(
                method: request.httpMethod ?? "GET",
                url: request.url?.absoluteString ?? "",
                authorization: request.value(forHTTPHeaderField: "Authorization"),
                body: parsedBody
            )
        )
        let h = Self.handler
        Self.lock.unlock()

        let canned = h?(request, parsedBody) ?? CannedResponse(statusCode: 500, body: Data("{}".utf8))
        let http = HTTPURLResponse(
            url: request.url ?? URL(string: "https://mac-mini.example.ts.net")!,
            statusCode: canned.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: canned.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

// MARK: - 1. Cards Persistence, Deduplication, Ages, Parity & Wire Ordering

@MainActor
private func testCardsPersistenceDedupTimestampsAndOrdering() async {
    let suiteName = "joat.test.cards.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suiteName)!
    defer { defaults.removePersistentDomain(forName: suiteName) }

    let link = RecordingLink()
    let cards = Cards(link: link, defaults: defaults)
    expectEqual(Cards.keep, 25, "Cards.keep is 25 for 1:1 parity with plugin notificationStack")

    let now = Date()
    let fifteenMinAgo = now.addingTimeInterval(-15 * 60)
    let twoHoursAgo = now.addingTimeInterval(-2 * 3600)

    // 1a. Post 3 cards with past/current timestamps and verify CRITICAL wire ordering:
    // `hud.notifications` + `hud.ambient` MUST arrive BEFORE `hud.text` (containerId 2)!
    link.clear()
    cards.post(
        source: "Alex Rivera",
        channel: "Messages",
        summary: "Prototype PCB arrived",
        body: "Prototype PCB arrived — ready for bench bring-up.",
        at: twoHoursAgo,
        originId: "msg:mac-501"
    )

    let frames1 = link.sent
    let notifIdx = frames1.firstIndex { $0.string("kind") == "hud.notifications" }
    let ambientIdx = frames1.firstIndex { $0.string("kind") == "hud.ambient" }
    let popupIdx = frames1.firstIndex {
        $0.string("kind") == "hud.text"
            && $0.int("containerId") == 2
            && $0.string("contentKind") == "notification"
    }
    expect(notifIdx != nil && ambientIdx != nil && popupIdx != nil,
           "Cards.post emitted hud.notifications, hud.ambient, and hud.text popup")
    if let n = notifIdx, let a = ambientIdx, let p = popupIdx {
        expect(n < p && a < p,
               "CRITICAL: hud.notifications (\(n)) and hud.ambient (\(a)) emitted BEFORE hud.text popup (\(p))")
    }

    let wireCards1 = (frames1[notifIdx ?? 0]["cards"] as? [[String: Any]]) ?? []
    expectEqual(wireCards1.count, 1, "1 unread card in hud.notifications")
    expectEqual(
        wireCards1.first?["at"] as? Int,
        Int(twoHoursAgo.timeIntervalSince1970 * 1000),
        "hud.notifications carries true past timestamp (2h ago, never Date() at post time)"
    )
    expectEqual(Cards.age(twoHoursAgo, now: now), "2h", "Cards.age formats 2h ago")
    expectEqual(Cards.age(fifteenMinAgo, now: now), "15m", "Cards.age formats 15m ago")

    // 1b. Duplicate `originId` while active is ignored completely
    link.clear()
    cards.post(
        source: "Alex Rivera",
        channel: "Messages",
        summary: "Prototype PCB arrived",
        body: "Prototype PCB arrived — ready for bench bring-up.",
        at: twoHoursAgo,
        originId: "msg:mac-501"
    )
    expectEqual(link.sent.count, 0, "Duplicate active originId produces 0 frames")
    expectEqual(cards.active.count, 1, "Active count stays 1 after duplicate originId")

    // Post 2 more cards (`15m ago` and `now`)
    cards.post(
        source: "Sarah Chen",
        channel: "Mail",
        summary: "Q4 Launch — Sign-off ready",
        body: "Q4 Launch\nAll firmware checks are green.",
        at: fifteenMinAgo,
        originId: "mail:acct1:900"
    )
    cards.post(
        source: "Mom",
        channel: "Messages",
        summary: "Dinner at 7!",
        body: "Dinner at 7!",
        at: now,
        originId: "msg:mac-502"
    )
    expectEqual(cards.active.count, 3, "3 active unread cards")
    let latestAmbient = link.sent.last { $0.string("kind") == "hud.ambient" }
    let latestNotifs = link.sent.last { $0.string("kind") == "hud.notifications" }
    expectEqual(latestAmbient?.int("unread"), 3, "hud.ambient unread == 3")
    expectEqual((latestNotifs?["cards"] as? [[String: Any]])?.count, 3, "hud.notifications count == 3")

    // 1c. Dismiss via `notification-dismiss` uplink + `dismiss(_:)`, then reconnect & restart!
    let firstCardId = cards.active[0].id
    cards.handle(
        kind: "input",
        message: ["kind": "input", "event": ["containerName": "notification-dismiss", "traceId": firstCardId]]
    )
    expectEqual(cards.active.count, 2, "Unread count decremented to 2 after notification-dismiss")
    expect(cards.dismissedOriginIds.contains("msg:mac-501"), "Recorded msg:mac-501 in dismissedOriginIds")

    // Dismiss remaining 2 cards
    cards.dismiss(cards.active.map(\.id))
    expectEqual(cards.active.count, 0, "All cards dismissed -> 0 active")

    // Reconnect WebSocket (`pluginConnected()`) -> ZERO dismissed cards reappear!
    link.clear()
    cards.pluginConnected()
    let reconnectNotifs = link.sent.first { $0.string("kind") == "hud.notifications" }
    let reconnectAmbient = link.sent.first { $0.string("kind") == "hud.ambient" }
    expectEqual((reconnectNotifs?["cards"] as? [[String: Any]])?.count, 0,
                "ZERO dismissed cards reappear on pluginConnected()")
    expectEqual(reconnectAmbient?.int("unread"), 0, "hud.ambient unread == 0 on pluginConnected()")

    // Simulate full JOAT Brain process restart with a fresh `Cards` instance reading `defaults`
    let linkAfterRestart = RecordingLink()
    let restartedCards = Cards(link: linkAfterRestart, defaults: defaults)
    expectEqual(restartedCards.active.count, 0, "Restarted Cards restores acknowledged state (0 unread)")
    restartedCards.pluginConnected()
    let restartNotifs = linkAfterRestart.sent.first { $0.string("kind") == "hud.notifications" }
    expectEqual((restartNotifs?["cards"] as? [[String: Any]])?.count, 0,
                "ZERO dismissed cards reappear after process restart + pluginConnected()")

    // Attempting to re-post any dismissed origin (`msg:mac-501`, `mail:acct1:900`, `msg:mac-502`) is blocked!
    linkAfterRestart.clear()
    restartedCards.post(
        source: "Alex Rivera",
        channel: "Messages",
        summary: "Prototype PCB arrived",
        at: twoHoursAgo,
        originId: "msg:mac-501"
    )
    expectEqual(restartedCards.active.count, 0, "Dismissed origin tombstone survives restart and blocks replay")
    expectEqual(linkAfterRestart.sent.count, 0, "Zero frames emitted for replayed dismissed origin")

    // 1d. Verify 25-card cap parity and 200-item `dismissedOriginIds` cap
    for i in 1...30 {
        restartedCards.post(
            source: "Sender \(i)",
            channel: "Messages",
            summary: "Burst \(i)",
            originId: "msg:burst-\(i)"
        )
    }
    expectEqual(restartedCards.active.count, 25, "Active unread cards capped at Cards.keep (25)")
    let capNotifs = linkAfterRestart.sent.last { $0.string("kind") == "hud.notifications" }
    let capAmbient = linkAfterRestart.sent.last { $0.string("kind") == "hud.ambient" }
    expectEqual((capNotifs?["cards"] as? [[String: Any]])?.count, 25, "hud.notifications sends all 25 active cards")
    expectEqual(capAmbient?.int("unread"), 25, "hud.ambient unread matches 25 1:1")

    for i in 1...230 {
        restartedCards.post(source: "S", channel: "Messages", summary: "T\(i)", originId: "origin-\(i)")
        if let lastId = restartedCards.active.last?.id {
            _ = restartedCards.acknowledge(lastId)
        }
    }
    expectEqual(restartedCards.dismissedOriginIds.count, 200, "dismissedOriginIds bounded to 200 items")
}

// MARK: - 2. Prompts Multi-Sentence Dictation, Pre-Prompt Fence, Tap Commit & Messages Mac Sync

@MainActor
private func testPromptsMultiClauseFenceAndMessagesSync() async {
    let link = RecordingLink()
    let prompts = Prompts(link: link)
    prompts.multiClauseSettleSeconds = 0.22 // fast deterministic test window (default verified separately)
    expectEqual(Prompts(link: link).multiClauseSettleSeconds, 1.5, "Default multiClauseSettleSeconds is 1.5s")

    // 2a. Pre-prompt fence (`spokeUntil < listeningSince ?? askedAt`)
    var committedAnswer: String? = "UNSET"
    prompts.ask("What should I say to Mom?", ownerKind: "messages", ownerId: "draft-1", field: "body") { said in
        committedAnswer = said
    }
    let promptFrame = link.sent.last { $0.string("kind") == "prompt.present" }
    let promptId = promptFrame?.string("promptId") ?? ""
    expect(!promptId.isEmpty, "prompt.present emitted promptId")

    // Utterance that ended 200ms BEFORE `ask()` (tail of "JOAT, text Mom") must be ignored!
    let preAskTime = Date().addingTimeInterval(-0.2)
    expectEqual(prompts.heard("text Mom", spokeUntil: preAskTime), true, "Prompts consumes pre-prompt speech without leaking")
    expectEqual(committedAnswer, "UNSET", "Pre-ask speech did NOT answer prompt")
    expectEqual(prompts.active, true, "Prompt remains active after pre-ask speech")

    // Simulate `prompt.ack` (`success: true`) arriving after a short delay, setting `listeningSince`
    let beforeAckTime = Date()
    try? await Task.sleep(nanoseconds: 25_000_000)
    prompts.handle(kind: "prompt.ack", message: ["kind": "prompt.ack", "promptId": promptId, "success": true])

    // Speech that ended before `listeningSince` (while mic was still opening) is also fenced!
    _ = prompts.heard("stale mic-open tail", spokeUntil: beforeAckTime)
    expectEqual(committedAnswer, "UNSET", "Pre-ack speech before listeningSince did NOT answer prompt")
    expectEqual(prompts.active, true, "Prompt remains active after pre-ack speech")

    // 2b. Multi-clause dictation (`field == "body"`) with volatile sliding window!
    link.clear()
    _ = prompts.heard("Hey, I'm running 5 minutes late.", spokeUntil: Date())
    expectEqual(committedAnswer, "UNSET", "First clause does NOT prematurely finish body prompt")
    expectEqual(prompts.active, true, "Prompt stays open waiting for next clause")
    let partial1 = link.sent.last { $0.string("kind") == "prompt.partial" }?.string("text")
    expectEqual(partial1, "Hey, I'm running 5 minutes late.", "Published Clause 1 via prompt.partial")

    // Wait 140ms (> half of 220ms settle window), then emit volatile speech for Clause 2!
    try? await Task.sleep(nanoseconds: 140_000_000)
    prompts.hearing("Start without")
    let partialVolatile = link.sent.last { $0.string("kind") == "prompt.partial" }?.string("text")
    expectEqual(
        partialVolatile,
        "Hey, I'm running 5 minutes late. Start without",
        "Volatile speech combines with finalized Clause 1 in prompt.partial"
    )

    // Wait another 130ms (270ms total since Clause 1 — would have expired if `hearing` hadn't pushed timer!)
    try? await Task.sleep(nanoseconds: 130_000_000)
    expectEqual(committedAnswer, "UNSET", "Volatile speech pushed settleWorkItem forward without cutting off wearer!")

    // Finalize Clause 2
    _ = prompts.heard("Start without me.", spokeUntil: Date())
    let partial2 = link.sent.last { $0.string("kind") == "prompt.partial" }?.string("text")
    expectEqual(
        partial2,
        "Hey, I'm running 5 minutes late. Start without me.",
        "Both finalized clauses combined in prompt.partial"
    )

    // Wait for settle timer to fire
    for _ in 0..<25 where committedAnswer == "UNSET" {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    expectEqual(
        committedAnswer,
        "Hey, I'm running 5 minutes late. Start without me.",
        "Multi-clause body settled cleanly with both sentences intact"
    )
    expectEqual(prompts.active, false, "Prompt closed after settling")

    // 2c. Immediate `0 ms` tap commit (`prompt-commit`) combining finalized segments + live partial!
    var tapCommitted: String? = "UNSET"
    prompts.multiClauseSettleSeconds = 5.0 // long settle window to prove tap commits immediately!
    prompts.ask("What should Claude Code do?", ownerKind: "terminal", ownerId: "term-1", field: "instruction") { said in
        tapCommitted = said
    }
    let tapPromptId = link.sent.last { $0.string("kind") == "prompt.present" }?.string("promptId") ?? ""
    _ = prompts.heard("Check the failing tests.", spokeUntil: Date())
    prompts.hearing("And fix the timeout")
    expectEqual(tapCommitted, "UNSET", "Still listening before tap")

    prompts.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": ["containerName": "prompt-commit", "selectedName": tapPromptId],
        ]
    )
    expectEqual(
        tapCommitted,
        "Check the failing tests. And fix the timeout",
        "prompt-commit immediately commits segments + partial in 0 ms"
    )
    expectEqual(prompts.active, false, "Prompt inactive immediately after tap commit")

    // 2d. Single-slot field (`recipient`) and `spokeUntil == nil` (`DebugSeed`) finish immediately!
    var recipientCommitted: String?
    prompts.ask("Who should I text?", ownerKind: "messages", ownerId: "draft-2", field: "recipient") { said in
        recipientCommitted = said
    }
    _ = prompts.heard("Priya Nair", spokeUntil: Date())
    expectEqual(recipientCommitted, "Priya Nair", "Single-slot recipient finishes immediately on first .final")

    var seedCommitted: String?
    prompts.ask("What should I say?", ownerKind: "messages", ownerId: "draft-3", field: "body") { said in
        seedCommitted = said
    }
    _ = prompts.heard("DebugSeed instant reply") // spokeUntil == nil
    expectEqual(seedCommitted, "DebugSeed instant reply", "spokeUntil == nil finishes immediately for DebugSeed/tests")

    // 2e. `Messages.pageList()` shows `"Loading Messages from your Mac…"` (`selectable: false`)
    // when Mac is configured and `macRows` / `texts` are empty, then repaints with threads!
    let sessionCfg = URLSessionConfiguration.ephemeral
    sessionCfg.protocolClasses = [MockRelayURLProtocol.self]
    let mockSession = URLSession(configuration: sessionCfg)
    let nowEpoch = Date().timeIntervalSince1970

    MockRelayURLProtocol.install { req, _ in
        if req.url?.path.hasSuffix("/v1/threads") == true {
            let doc: [String: Any] = [
                "threads": [
                    [
                        "id": "thr_mom",
                        "label": "Mom",
                        "group": false,
                        "preview": "See you tonight!",
                        "at": nowEpoch - 180,
                        "unread": true,
                    ]
                ]
            ]
            return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: doc))
        }
        return .init(statusCode: 200, body: Data("{}".utf8))
    }

    let cardSuite = "joat.test.msgcards.\(UUID().uuidString)"
    let cardDefaults = UserDefaults(suiteName: cardSuite)!
    defer { cardDefaults.removePersistentDomain(forName: cardSuite) }
    UserDefaults.standard.removeObject(forKey: Messages.storeKey)

    let msgLink = RecordingLink()
    let hud = Hud(link: msgLink)
    let confirm = ConfirmGate(link: msgLink)
    let msgCards = Cards(link: msgLink, defaults: cardDefaults)
    let messages = Messages(hud: hud, confirm: confirm, cards: msgCards)
    messages.mac = MacMessages(
        config: MacMessages.Config(url: "https://mac-mini.example.ts.net/messages", token: "tok"),
        session: mockSession
    )

    let initialPage = messages.pageList()
    expectEqual(initialPage.list["title"] as? String, "MESSAGES", "Initial page title is MESSAGES")
    expectEqual(
        initialPage.list["items"] as? [String],
        ["Loading Messages from your Mac…"],
        "Shows Loading Messages from your Mac… instead of 'connect your Mac' when Mac IS configured"
    )
    expectEqual(initialPage.list["selectable"] as? Bool, false, "Loading placeholder is non-selectable")

    var didRepaint = false
    messages.onChange = { didRepaint = true }
    messages.refreshFromMac()
    for _ in 0..<30 where !didRepaint {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expect(didRepaint, "refreshFromMac triggered onChange repaint")
    let loadedPage = messages.pageList()
    expectEqual(loadedPage.list["selectable"] as? Bool, true, "Loaded Messages page is selectable")
    expectEqual(loadedPage.senders, ["Mom"], "Loaded Mom thread from Mac")

    // Also verify `arrivedFromMac` passes true past `at` and deterministic `originId` (`msg:mac-<id>`)!
    let tenMinAgo = Date().addingTimeInterval(-600)
    var delivered = false
    messages.arrivedFromMac(
        MacMessages.Incoming(
            id: 777,
            thread: "thr_mom",
            label: "Mom",
            person: "Mom",
            group: false,
            replyable: true,
            text: "Picking up groceries",
            at: tenMinAgo,
            attachment: false
        ),
        done: { delivered = true }
    )
    for _ in 0..<30 where !delivered {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expectEqual(msgCards.active.count, 1, "Mac incoming message created 1 card")
    expectEqual(msgCards.active.first?.originId, "msg:mac-777", "Mac message card carries deterministic msg:mac-777 originId")
    expect(abs((msgCards.active.first?.at.timeIntervalSince1970 ?? 0) - tenMinAgo.timeIntervalSince1970) < 1.0,
           "Mac message card preserves exact sender timestamp")

    // Replaying the exact same `MacMessages.Incoming(id: 777)` never creates a second card!
    var deliveredDup = false
    messages.arrivedFromMac(
        MacMessages.Incoming(
            id: 777,
            thread: "thr_mom",
            label: "Mom",
            person: "Mom",
            group: false,
            replyable: true,
            text: "Picking up groceries",
            at: tenMinAgo,
            attachment: false
        ),
        done: { deliveredDup = true }
    )
    for _ in 0..<20 where !deliveredDup {
        try? await Task.sleep(nanoseconds: 15_000_000)
    }
    expectEqual(msgCards.active.count, 1, "Replayed Mac message row ID 777 did not create a duplicate card")

    // Verify `confirmHandleSuffix` & Mac thread reply confirmation prompt omit `" (…)"`
    // when `recipient` is an opaque Mac thread contact name (`"Maya Lin"`, 0 digits, no `@`)!
    expectEqual(
        Messages.confirmHandleSuffix(label: "Maya Lin", recipient: "Maya Lin", isGroup: false),
        "",
        "Opaque Mac thread label omits '(…)' suffix"
    )
    expectEqual(
        Messages.confirmHandleSuffix(label: "Maya Lin", recipient: "+13125551234", isGroup: false),
        " (…1234)",
        "Phone number recipient renders last 4 digits"
    )
    expectEqual(
        Messages.confirmHandleSuffix(label: "Maya Lin", recipient: "maya@example.com", isGroup: false),
        " (maya@example.com)",
        "Distinct email recipient renders email suffix"
    )
    expectEqual(
        Messages.confirmHandleSuffix(label: "maya@example.com", recipient: "maya@example.com", isGroup: false),
        "",
        "Email identical to label omits redundant suffix"
    )

    let msgPrompts = Prompts(link: msgLink)
    messages.link = msgLink
    messages.prompts = msgPrompts
    _ = messages.thread("Mom", requestId: "req-msg-reply", traceId: "trace-mom-1")
    msgLink.clear()
    messages.beginReply()
    _ = msgPrompts.heard("Grab us a booth inside.") // spokeUntil == nil finishes immediately
    let confirmFrame = msgLink.sent.last { $0.string("kind") == "confirm.request" }
    let confirmPromptText = confirmFrame?.string("prompt") ?? ""
    expect(
        confirmPromptText.hasPrefix("Send to Mom?"),
        "Mac thread reply confirmation renders 'Send to Mom?' without '(…)' (got: \(confirmPromptText))"
    )
    expect(
        !confirmPromptText.contains("(…)"),
        "Mac thread reply confirmation never contains '(…)'"
    )
    confirm.declineActive(reason: "test")
}

// MARK: - 3 & 4. Full 3-Level GlassesTerminal + CapabilityMenu Activity + Gestures

@MainActor
private func testGlassesTerminalThreeLevelsGesturesAndActivity() async {
    let sessionCfg = URLSessionConfiguration.ephemeral
    sessionCfg.protocolClasses = [MockRelayURLProtocol.self]
    let mockSession = URLSession(configuration: sessionCfg)

    var claudeBusy = false
    var claudeMessages: [[String: String]] = [
        ["role": "user", "text": "Audit notification counts"],
        ["role": "assistant", "text": "All 25 unread slots now sync 1:1."],
    ]
    var codexApprovals = 1
    var codexApprovalItems: [[String: String]] = [
        ["id": "appr_1", "kind": "bash", "summary": "pytest -q"],
    ]

    func makeTerminalState() -> [String: Any] {
        [
            "assistantSeat": "claude",
            "agents": [
                [
                    "name": "claude",
                    "provider": "claude",
                    "title": "Claude", // Real Python TerminalEndpoint PROVIDER_TITLES["claude"] == "Claude"
                    "installed": true,
                    "loggedIn": true,
                    "statusDetail": claudeBusy ? "Running unit suite" : "Ready in workspace",
                    "cwd": "~/Developer/workspace",
                    "currentModel": "claude-sonnet-4-6",
                    "modelOptions": ["claude-sonnet-4-6"],
                    "busy": claudeBusy,
                    "activeSessionTitle": "Notification audit",
                    "pendingApprovals": 0,
                    "recentMessages": claudeMessages,
                    "pendingApprovalItems": [],
                    "sessions": [
                        ["key": "sess_alpha", "label": "Fix notification ages"],
                        ["key": "sess_beta", "label": "Terminal picker parity"],
                    ],
                ],
                [
                    "name": "codex",
                    "provider": "codex",
                    "title": "Codex",
                    "installed": true,
                    "loggedIn": true,
                    "statusDetail": codexApprovals > 0 ? "Awaiting approval" : "Ready",
                    "cwd": "~/Developer/workspace",
                    "currentModel": "gpt-5.4",
                    "modelOptions": ["gpt-5.4"],
                    "busy": codexApprovals == 0 && claudeBusy,
                    "activeSessionTitle": "Relay endpoint tests",
                    "pendingApprovals": codexApprovals,
                    "recentMessages": [
                        ["role": "assistant", "text": "Prepared terminal_endpoint tests."],
                    ],
                    "pendingApprovalItems": codexApprovalItems,
                    "sessions": [
                        ["key": "codex_s1", "label": "Earlier Codex session"],
                    ],
                ],
                [
                    "name": "antigravity",
                    "provider": "antigravity",
                    "title": "Antigravity",
                    "installed": true,
                    "loggedIn": false,
                    "statusDetail": "API key needed",
                    "cwd": "~/Developer/workspace",
                    "currentModel": "gemini-3.5-flash-lite",
                    "modelOptions": ["gemini-3.5-flash-lite"],
                    "busy": false,
                    "pendingApprovals": 0,
                    "recentMessages": [],
                    "pendingApprovalItems": [],
                    "sessions": [],
                ],
            ],
        ]
    }

    MockRelayURLProtocol.install { req, body in
        let path = req.url?.path ?? ""
        if path.hasSuffix("/v1/state") {
            let state = makeTerminalState()
            return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: state))
        }
        if path.hasSuffix("/v1/config"), let action = body?["action"] as? String {
            switch action {
            case "resume_session":
                let key = body?["sessionKey"] as? String ?? ""
                claudeMessages = [
                    ["role": "user", "text": "Resumed \(key)"],
                    ["role": "assistant", "text": "Loaded session \(key) from Mac."],
                ]
            case "new_session":
                claudeMessages = []
            case "send_instruction":
                let instr = body?["instruction"] as? String ?? ""
                claudeBusy = true
                claudeMessages.append(["role": "user", "text": instr])
            case "decide_approval":
                codexApprovals = 0
                codexApprovalItems = []
            case "cancel_run":
                claudeBusy = false
            default:
                break
            }
            var resp = makeTerminalState()
            resp["notice"] = "ok:\(action)"
            return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: resp))
        }
        return .init(statusCode: 200, body: Data("{}".utf8))
    }

    let cardSuite = "joat.test.termcards.\(UUID().uuidString)"
    let cardDefaults = UserDefaults(suiteName: cardSuite)!
    defer { cardDefaults.removePersistentDomain(forName: cardSuite) }

    let link = RecordingLink()
    let server = LoopbackServer(port: 0)
    let stt = Transcriber(kind: .dictation(words: ["JOAT"]), label: "test")
    let voice = Voice()
    voice.spokenReplies = false
    let macTerminal = MacTerminal(
        config: MacTerminal.Config(url: "https://mac-mini.example.ts.net/terminal", token: "term-tok"),
        session: mockSession
    )
    let brain = Brain(
        server: server,
        stt: stt,
        shadow: nil,
        voice: voice,
        macTerminal: macTerminal,
        cardDefaults: cardDefaults,
        linkOverride: link
    )
    brain.glassesTerminal.busyPollSeconds = 0.08
    brain.glassesTerminal.idlePollSeconds = 0.12
    link.clear()

    // 4a. Level 1 (`AGENT TERMINAL`): shows `"Loading agents from your Mac…"` immediately on cold cache,
    // then fetches `/v1/state` (including `sessions`) and repaints selectable agent rows!
    brain.capabilities.open("Agent Terminal", requestId: "req-term-1", anchorMs: nil)
    let loadingFrame = link.sent.first { $0.string("kind") == "hud.list" }
    expectEqual(loadingFrame?.string("title"), "AGENT TERMINAL", "Cold open sends AGENT TERMINAL immediately")
    expectEqual(
        loadingFrame?["items"] as? [String],
        ["Loading agents from your Mac…"],
        "Cold open shows Loading agents from your Mac…"
    )
    expectEqual(loadingFrame?.bool("selectable"), false, "Loading frame is non-selectable")

    for _ in 0..<30 where link.sent.filter({ $0.string("kind") == "hud.list" }).count < 2 {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    let agentPickerFrame = link.sent.last { $0.string("kind") == "hud.list" }
    expectEqual(agentPickerFrame?.string("title"), "AGENT TERMINAL", "Repainted AGENT TERMINAL after fetch")
    expectEqual(agentPickerFrame?.bool("selectable"), true, "Loaded AGENT TERMINAL is selectable")
    expectEqual(
        agentPickerFrame?["items"] as? [String],
        [
            "Claude Code · ready",
            "Codex · approval needed",
            "Antigravity · setup needed",
        ],
        "Level 1 formats ready, approval needed, and setup needed rows accurately"
    )
    expectEqual(macTerminal.state.agents.first?.sessions.count, 2, "MacTerminal decoded AgentItem.sessions")
    expect(!link.sent.contains { $0.string("kind") == "brain.handoff" }, "Zero brain.handoff frames")

    // 4b. Level 2 (`AGENT TERMINAL · CLAUDE`): tap row 0 (`Claude Code`)
    link.clear()
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": [
                "containerName": "capdata",
                "selectedName": "AGENT TERMINAL",
                "selectedIndex": 0,
                "requestId": "req-term-1",
                "traceId": "hud-agent-0",
            ],
        ]
    )
    for _ in 0..<20 where !link.sent.contains(where: { $0.string("title") == "AGENT TERMINAL · CLAUDE" }) {
        try? await Task.sleep(nanoseconds: 15_000_000)
    }
    let sessionPickerFrame = link.sent.first { $0.string("title") == "AGENT TERMINAL · CLAUDE" }
    expectEqual(sessionPickerFrame?.bool("selectable"), true, "Level 2 session picker is selectable")
    expectEqual(sessionPickerFrame?.string("hint"), "tap: open · 2x tap: agents", "Level 2 hint matches CapabilityMenu.ts")
    expectEqual(
        sessionPickerFrame?["items"] as? [String],
        [
            "Active · Notification audit",
            "+ New session",
            "Fix notification ages",
            "Terminal picker parity",
        ],
        "Level 2 lists Active, + New session, and saved sessions from /terminal"
    )

    // 4c. Level 3 (`agent_terminal.view`): tap row 2 (`Fix notification ages` -> `resume_session`)
    link.clear()
    MockRelayURLProtocol.clearRequests()
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": [
                "containerName": "capdata",
                "selectedName": "AGENT TERMINAL",
                "selectedIndex": 2,
                "requestId": "req-term-1",
                "traceId": "hud-sess-2",
            ],
        ]
    )
    for _ in 0..<30 where !link.sent.contains(where: { $0.string("kind") == "agent_terminal.view" }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    let resumeReq = MockRelayURLProtocol.recordedRequests().first { $0.url.hasSuffix("/v1/config") }
    expectEqual(resumeReq?.body?["action"] as? String, "resume_session", "Tapped saved session POSTed resume_session")
    expectEqual(resumeReq?.body?["provider"] as? String, "claude", "resume_session sent provider=claude")
    expectEqual(resumeReq?.body?["sessionKey"] as? String, "sess_alpha", "resume_session sent sessionKey=sess_alpha")

    let termFrame = link.sent.last { $0.string("kind") == "agent_terminal.view" }?.object("terminal")
    expectEqual(termFrame?.string("openingRequestId"), "req-term-1", "Bound openingRequestId")
    expectEqual(termFrame?.string("openingSelectionId"), "hud-sess-2", "Bound openingSelectionId to traceId")
    expectEqual(termFrame?.string("state"), "ready", "Resumed session state is ready")
    expectEqual(termFrame?.string("focus"), "claude · ready", "Focus is 'claude · ready'")
    expectEqual(termFrame?.string("conversationId"), "claude-session", "conversationId is claude-session")
    let renderedMsgs = termFrame?["messages"] as? [[String: String]] ?? []
    expectEqual(renderedMsgs.count, 2, "Rendered 2 resumed messages")
    expectEqual(renderedMsgs.last?["text"], "Loaded session sess_alpha from Mac.", "Resumed assistant text matches")

    // 4d. Live polling deduplication: unchanged `/v1/state` emits ZERO duplicate `agent_terminal.view` frames!
    link.clear()
    try? await Task.sleep(nanoseconds: 260_000_000) // ~2 idle poll cycles
    let redundantFrames = link.sent.filter { $0.string("kind") == "agent_terminal.view" }
    expectEqual(redundantFrames.count, 0, "Zero redundant agent_terminal.view frames while state is unchanged")

    // 4e. `begin_terminal_input` -> `Prompts` (`ownerKind: "terminal"`, `field: "instruction"`) -> `send_instruction` + streaming poll!
    link.clear()
    MockRelayURLProtocol.clearRequests()
    brain.glassesTerminal.handle(
        kind: "terminal.action",
        message: ["kind": "terminal.action", "action": "begin_terminal_input"]
    )
    for _ in 0..<20 where !link.sent.contains(where: { $0.string("kind") == "prompt.present" }) {
        try? await Task.sleep(nanoseconds: 15_000_000)
    }
    let termPrompt = link.sent.last { $0.string("kind") == "prompt.present" }
    expectEqual(termPrompt?.string("ownerKind"), "terminal", "Terminal voice prompt uses ownerKind='terminal'")
    expectEqual(termPrompt?.string("field"), "instruction", "Terminal voice prompt uses field='instruction'")
    expectEqual(termPrompt?.string("question"), "What should Claude Code do?", "Prompt asks What should Claude Code do?")
    let termPromptId = termPrompt?.string("promptId") ?? ""

    // Speak two clauses and tap to commit!
    _ = brain.prompts.heard("Run the simulator harness.", spokeUntil: Date())
    brain.prompts.hearing("Report any diffs")
    brain.prompts.handle(
        kind: "input",
        message: ["kind": "input", "event": ["containerName": "prompt-commit", "selectedName": termPromptId]]
    )

    for _ in 0..<30 where !MockRelayURLProtocol.recordedRequests().contains(where: {
        ($0.body?["action"] as? String) == "send_instruction"
    }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    let instrReq = MockRelayURLProtocol.recordedRequests().first { ($0.body?["action"] as? String) == "send_instruction" }
    expectEqual(
        instrReq?.body?["instruction"] as? String,
        "Run the simulator harness. Report any diffs",
        "Sent combined multi-clause instruction to /terminal/v1/config"
    )
    let runningView = link.sent.last { $0.string("kind") == "agent_terminal.view" }?.object("terminal")
    expectEqual(runningView?.string("state"), "running", "Terminal transitioned to running state")

    // Simulate Mac finishing the turn with streamed assistant reply -> live poller picks it up automatically!
    link.clear()
    claudeBusy = false
    claudeMessages.append(["role": "assistant", "text": "Simulator harness passed with 0 diffs."])
    for _ in 0..<30 where !link.sent.contains(where: { $0.string("kind") == "agent_terminal.view" }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    let settledView = link.sent.last { $0.string("kind") == "agent_terminal.view" }?.object("terminal")
    expectEqual(settledView?.string("state"), "ready", "Poller transitioned state back to ready on completion")
    let settledMsgs = settledView?["messages"] as? [[String: String]] ?? []
    expectEqual(settledMsgs.last?["text"], "Simulator harness passed with 0 diffs.",
                "Poller streamed completed assistant message onto 5-container terminal")

    // 4f. Navigate `2x tap` (`terminal-sessions` -> `terminal-agents`) to `Codex` (approval needed!)
    // and test `[!] Approval needed: ...` + `approve_terminal` / `deny_terminal` / `cancel_terminal` / `retry_terminal`
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": ["containerName": "capability-lifecycle", "selectedName": "terminal-sessions", "requestId": "req-term-1"],
        ]
    )
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": ["containerName": "capability-lifecycle", "selectedName": "terminal-agents", "requestId": "req-term-1"],
        ]
    )
    try? await Task.sleep(nanoseconds: 30_000_000)
    link.clear()

    // Tap row 1 (`Codex`)
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": [
                "containerName": "capdata",
                "selectedName": "AGENT TERMINAL",
                "selectedIndex": 1,
                "requestId": "req-term-1",
                "traceId": "hud-agent-codex",
            ],
        ]
    )
    for _ in 0..<20 where !link.sent.contains(where: { $0.string("title") == "AGENT TERMINAL · CODEX" }) {
        try? await Task.sleep(nanoseconds: 15_000_000)
    }

    // Tap row 0 (`Active · Relay endpoint tests`)
    link.clear()
    brain.glassesTerminal.handle(
        kind: "input",
        message: [
            "kind": "input",
            "event": [
                "containerName": "capdata",
                "selectedName": "AGENT TERMINAL",
                "selectedIndex": 0,
                "requestId": "req-term-1",
                "traceId": "hud-codex-active",
            ],
        ]
    )
    for _ in 0..<20 where !link.sent.contains(where: { $0.string("kind") == "agent_terminal.view" }) {
        try? await Task.sleep(nanoseconds: 15_000_000)
    }
    let codexView = link.sent.last { $0.string("kind") == "agent_terminal.view" }?.object("terminal")
    expectEqual(codexView?.int("approvals"), 1, "Codex view reports approvals == 1")
    let codexActivity = codexView?.string("activity") ?? ""
    expect(
        codexActivity.hasSuffix("[!] Approval needed: bash: pytest -q"),
        "Activity ends with '[!] Approval needed: bash: pytest -q' (got: \(codexActivity))"
    )

    // Gesture `approve_terminal`
    MockRelayURLProtocol.clearRequests()
    link.clear()
    brain.glassesTerminal.handle(
        kind: "terminal.action",
        message: ["kind": "terminal.action", "action": "approve_terminal"]
    )
    for _ in 0..<25 where !MockRelayURLProtocol.recordedRequests().contains(where: {
        ($0.body?["action"] as? String) == "decide_approval"
    }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    let approveReq = MockRelayURLProtocol.recordedRequests().first { ($0.body?["action"] as? String) == "decide_approval" }
    expectEqual(approveReq?.body?["decision"] as? String, "approve", "approve_terminal sent decision=approve")
    expectEqual(approveReq?.body?["provider"] as? String, "codex", "approve_terminal sent provider=codex")
    let afterApproveView = link.sent.last { $0.string("kind") == "agent_terminal.view" }?.object("terminal")
    expectEqual(afterApproveView?.int("approvals"), 0, "Approvals cleared to 0 after approve_terminal")

    // Gesture `deny_terminal`, `cancel_terminal`, and `retry_terminal`
    codexApprovals = 1
    codexApprovalItems = [["id": "appr_2", "kind": "bash", "summary": "rm -rf build"]]
    MockRelayURLProtocol.clearRequests()
    brain.glassesTerminal.handle(
        kind: "terminal.action",
        message: ["kind": "terminal.action", "action": "deny_terminal"]
    )
    for _ in 0..<25 where !MockRelayURLProtocol.recordedRequests().contains(where: {
        ($0.body?["decision"] as? String) == "deny"
    }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expect(
        MockRelayURLProtocol.recordedRequests().contains { ($0.body?["decision"] as? String) == "deny" },
        "deny_terminal sent decision=deny"
    )

    MockRelayURLProtocol.clearRequests()
    brain.glassesTerminal.handle(
        kind: "terminal.action",
        message: ["kind": "terminal.action", "action": "cancel_terminal"]
    )
    for _ in 0..<25 where !MockRelayURLProtocol.recordedRequests().contains(where: {
        ($0.body?["action"] as? String) == "cancel_run"
    }) {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expect(
        MockRelayURLProtocol.recordedRequests().contains { ($0.body?["action"] as? String) == "cancel_run" },
        "cancel_terminal sent action=cancel_run"
    )

    MockRelayURLProtocol.clearRequests()
    brain.glassesTerminal.handle(
        kind: "terminal.action",
        message: ["kind": "terminal.action", "action": "retry_terminal"]
    )
    for _ in 0..<25 where MockRelayURLProtocol.recordedRequests().isEmpty {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expect(
        MockRelayURLProtocol.recordedRequests().contains { $0.url.hasSuffix("/v1/state") },
        "retry_terminal refreshed /v1/state"
    )

    // 4g. `Activity` capability (`ACTIVITY` phone-local summary) + direct voice agent open (`"open Codex"`)
    _ = brain.pomodoro.startTimer(minutes: 15)
    defer { brain.pomodoro.reset() }
    link.clear()
    brain.capabilities.open("Activity", requestId: "req-activity-1", anchorMs: nil)
    let activityFrame = link.sent.last { $0.string("kind") == "hud.list" && $0.string("title") == "ACTIVITY" }
    expectEqual(activityFrame?.bool("selectable"), false, "ACTIVITY list is read-only (selectable: false)")
    expectEqual(activityFrame?.string("hint"), CapabilityMenu.backHint, "ACTIVITY uses backHint")
    let activityItems = activityFrame?["items"] as? [String] ?? []
    expect(activityItems.contains { $0.hasPrefix("Claude Code · ") }, "ACTIVITY summarizes Claude Code lane")
    expect(activityItems.contains { $0.hasPrefix("Pomodoro · Timer running") }, "ACTIVITY summarizes active Pomodoro timer")
    expect(activityItems.contains { $0.hasPrefix("Reminders · ") }, "ACTIVITY summarizes Reminders")
    expect(activityItems.contains { $0.hasPrefix("Messages · ") }, "ACTIVITY summarizes Messages")
    expect(activityItems.contains { $0.hasPrefix("Mail · ") }, "ACTIVITY summarizes Mail")

    // Direct voice open `"open Codex"` jumps straight to `AGENT TERMINAL · CODEX`!
    link.clear()
    let voiceCodex = brain.routeCommand("open Codex")
    expectEqual(voiceCodex?.text, "Opening Codex.", "Voice 'open Codex' spoken reply")
    expect(
        link.sent.contains { $0.string("kind") == "hud.list" && $0.string("title") == "AGENT TERMINAL · CODEX" },
        "Voice 'open Codex' jumps directly to AGENT TERMINAL · CODEX session picker"
    )
    expect(!link.sent.contains { $0.string("kind") == "brain.handoff" }, "Zero brain.handoff on voice open Codex")
    brain.capabilities.openHome()
}

// MARK: - 5. Post-Close-Only Note Summaries (All Note Types) & AI Cues Off Parity

@MainActor
private func testNotesCloseOnlySummaryAllNoteTypesAndCuesOff() async {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [MockRelayURLProtocol.self]
    let mockSession = URLSession(configuration: config)

    let prevEngineChoice = OnPhoneAI.shared.engineChoice
    OnPhoneAI.shared.engineChoice = .mac
    defer { OnPhoneAI.shared.engineChoice = prevEngineChoice }

    var summaryCallCount = 0
    var summaryVersion = 1
    MockRelayURLProtocol.install { req, body in
        let path = req.url?.path ?? ""
        if path.hasSuffix("/agent") {
            let messages = body?["messages"] as? [[String: Any]] ?? []
            let promptText = messages.compactMap { $0["content"] as? String }.joined(separator: "\n")
            if promptText.contains("TITLE:") && promptText.contains("OVERVIEW:") {
                summaryCallCount += 1
                let reply = """
                TITLE:
                Design Review v\(summaryVersion)
                OVERVIEW:
                Summary v\(summaryVersion) generated after closing the modified note.
                KEY POINTS:
                - Finalized launch checklist v\(summaryVersion)
                NEXT STEPS:
                - Send Liam the slide deck
                """
                let resp: [String: Any] = ["choices": [["message": ["content": reply]]]]
                return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: resp))
            }
            if promptText.contains("TASK:") && promptText.contains("QUOTE:") {
                let resp: [String: Any] = ["choices": [["message": ["content": "NONE"]]]]
                return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: resp))
            }
            // Any cue call
            let resp: [String: Any] = ["choices": [["message": ["content": "CONCEPT | Latency | Round-trip budget under 120 ms."]]]]
            return .init(statusCode: 200, body: try! JSONSerialization.data(withJSONObject: resp))
        }
        return .init(statusCode: 200, body: Data("{}".utf8))
    }

    let tempNotesDir = FileManager.default.temporaryDirectory
        .appendingPathComponent("joat_test_qn_\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: tempNotesDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempNotesDir) }

    let link = RecordingLink()
    let hud = Hud(link: link)
    let qn = QuickNotes(link: link, hud: hud, directory: tempNotesDir)
    let macLink = MacLink(
        config: MacLink.Config(url: "https://mac-mini.example.ts.net/agent", token: "agent-tok"),
        session: mockSession
    )
    qn.macLink = macLink
    let panels = PanelPublisher(link: link)
    let confirm = ConfirmGate(link: link)
    let reminders = Reminders(link: link, hud: hud, confirm: confirm)
    let cards = Cards(link: link, defaults: UserDefaults(suiteName: "joat.test.notecards.\(UUID().uuidString)")!)
    let menu = CapabilityMenu(link: link, panels: panels, reminders: reminders, cards: cards, quickNotes: qn)
    qn.menu = menu

    // 5a. Open a PageView, edit it, wait past the old 4.0 s idle window:
    // ZERO summary calls and ZERO store-reload spam while editing, even if retryPendingInsights() runs!
    var storeReloadsWhileEditing = 0
    qn.onStoreChange = { storeReloadsWhileEditing += 1 }

    let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
    let page = PageView(noteId: nil, typed: "", rich: nil, drawing: PKDrawing(), quickNotes: qn)
    page.frame = window.bounds
    window.addSubview(page)
    page.layoutIfNeeded()
    storeReloadsWhileEditing = 0

    expect(qn.isEditingPage(page.pageId), "QuickNotes tracks active open page while PageView is mounted")
    page.textView.text = "Review Q4 launch latency budget and QA sign-off owner."
    page.textViewDidChange(page.textView)

    // Wait 4.3 s (past both the 1.2 s/2.0 s auto-save debounce AND the old 4.0 s OCR/summary idle timer)
    try? await Task.sleep(nanoseconds: 4_300_000_000)
    qn.retryPendingInsights()
    try? await Task.sleep(nanoseconds: 150_000_000)

    expectEqual(summaryCallCount, 0, "Zero AI summary calls while the page is still open and being edited")
    expectEqual(storeReloadsWhileEditing, 0, "Quiet mid-edit auto-save does not trigger SwiftUI store reload spam")

    // Now close the modified page: summary runs ONCE on close!
    _ = page.closeAndSettle(quickNotes: qn)
    page.removeFromSuperview()
    expect(!qn.isEditingPage(page.pageId), "Page is no longer marked editing after closeAndSettle")

    for _ in 0..<40 where (qn.savedNotes.first { $0.id == page.pageId }?.summary) == nil {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    let settledPage = qn.savedNotes.first { $0.id == page.pageId }
    expectEqual(summaryCallCount, 1, "Closing a modified page triggers exactly one post-close AI summary")
    expectEqual(
        settledPage?.summary,
        "Summary v1 generated after closing the modified note.",
        "Closed modified page receives AI summary"
    )

    // 5b. Quick Text Note: post-save AI summary AND post-update AI summary!
    summaryVersion = 2
    _ = qn.handle("take a note confirm the battery calibration results before Friday afternoon")
    for _ in 0..<40 where qn.textNotes.first?.summary == nil {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    expectEqual(
        qn.textNotes.first?.summary,
        "Summary v2 generated after closing the modified note.",
        "Quick Text Note receives AI summary after creation"
    )
    if let textId = qn.textNotes.first?.id {
        summaryVersion = 3
        qn.updateTextNote(textId, text: "Updated text note: ship the final hardware test report on Monday.")
        for _ in 0..<40 where qn.textNotes.first?.summary != "Summary v3 generated after closing the modified note." {
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
        expectEqual(
            qn.textNotes.first?.summary,
            "Summary v3 generated after closing the modified note.",
            "Modified Quick Text Note updates its AI summary once saved"
        )
    }

    // 5c. `AI cues during QuickNotes` (`Cues.enabledKey`) OFF applies across Cues, live glasses menu, and reader!
    let prevCuesPref = UserDefaults.standard.object(forKey: Cues.enabledKey)
    defer {
        if let prevCuesPref {
            UserDefaults.standard.set(prevCuesPref, forKey: Cues.enabledKey)
        } else {
            UserDefaults.standard.removeObject(forKey: Cues.enabledKey)
        }
    }
    UserDefaults.standard.set(false, forKey: Cues.enabledKey)

    let cues = Cues(claude: ClaudeLink())
    qn.cues = cues
    MockRelayURLProtocol.clearRequests()
    link.clear()

    _ = qn.handle("start a voice note called Weekly Sync")
    let liveOffFrame = link.sent.last { $0.string("kind") == "hud.list" && $0.string("title") == QuickNotes.liveTitle }
    expectEqual(liveOffFrame?.bool("cuesEnabled"), false, "Live QuickNote carries cuesEnabled=false when AI cues are off")

    qn.heard("What is our target p95 latency for the March release and who owns sign-off?", audioEnd: Date())
    expectEqual(MockRelayURLProtocol.recordedRequests().count, 0, "Zero cue requests while AI cues are off")
    expect(cues.given.isEmpty, "Zero cues generated while AI cues are off")

    // Toggling AI cues ON mid-recording updates the live glasses page with cuesEnabled=true;
    // toggling back OFF updates cuesEnabled=false immediately!
    link.clear()
    UserDefaults.standard.set(true, forKey: Cues.enabledKey)
    NotificationCenter.default.post(name: UserDefaults.didChangeNotification, object: nil)
    for _ in 0..<20 where link.sent.last(where: { $0.string("title") == QuickNotes.liveTitle }) == nil {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expectEqual(
        link.sent.last { $0.string("title") == QuickNotes.liveTitle }?.bool("cuesEnabled"),
        true,
        "Toggling AI cues ON repaints live QuickNote with cuesEnabled=true"
    )

    link.clear()
    UserDefaults.standard.set(false, forKey: Cues.enabledKey)
    NotificationCenter.default.post(name: UserDefaults.didChangeNotification, object: nil)
    for _ in 0..<20 where link.sent.last(where: { $0.string("title") == QuickNotes.liveTitle }) == nil {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }
    expectEqual(
        link.sent.last { $0.string("title") == QuickNotes.liveTitle }?.bool("cuesEnabled"),
        false,
        "Toggling AI cues OFF repaints live QuickNote with cuesEnabled=false"
    )

    // Stop the voice note and verify it STILL gets its post-session AI summary even with AI cues OFF!
    summaryVersion = 4
    _ = qn.handle("stop voice note")
    for _ in 0..<180 where (qn.savedNotes.first { $0.title == "Weekly Sync" || $0.title == "Design Review v4" }?.summary) == nil {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    let savedVoice = qn.savedNotes.first { $0.page != true }
    expectEqual(
        savedVoice?.summary,
        "Summary v4 generated after closing the modified note.",
        "Voice QuickNote still gets its post-close AI summary when AI cues are off"
    )
}

func runEntry() {
    let sema = DispatchSemaphore(value: 0)
    Task { @MainActor in
        await testCardsPersistenceDedupTimestampsAndOrdering()
        await testPromptsMultiClauseFenceAndMessagesSync()
        await testGlassesTerminalThreeLevelsGesturesAndActivity()
        await testNotesCloseOnlySummaryAllNoteTypesAndCuesOff()
        print("PASS: all \(passedAssertions) phone-brain surfaces, notifications, and multi-clause prompt assertions succeeded.")
        sema.signal()
    }
    while sema.wait(timeout: .now()) == .timedOut {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
}
#endif

runEntry()
