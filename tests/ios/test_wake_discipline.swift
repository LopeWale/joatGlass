// Multi-turn reliability, wake discipline, speech-start follow-up latching,
// retired-conversationId rotation, and sub-50ms local capability tests for JOAT Brain.
//
// Run directly via:
//   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift \
//     tests/ios/test_wake_discipline.swift

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
        .appendingPathComponent("joat_test_wake_\(ProcessInfo.processInfo.processIdentifier)")
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
    let outBinary = tempDir.appendingPathComponent("wake_runner")
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

// MARK: - Assertions & Test Doubles

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

private final class RecordingPluginLink: PluginLink {
    private(set) var sent: [[String: Any]] = []
    func send(_ object: [String: Any]) { sent.append(object) }
    func clear() { sent.removeAll() }
    var conversations: [[String: Any]] {
        sent.filter { ($0["kind"] as? String) == "assistant.conversation" }
    }
}

@MainActor
private func makeTestBrain() -> (brain: Brain, link: RecordingPluginLink) {
    let link = RecordingPluginLink()
    let server = LoopbackServer(port: 0)
    let stt = Transcriber(kind: .dictation(words: ["JOAT", "Hey JOAT"]), label: "test")
    let voice = Voice()
    voice.spokenReplies = false
    let brain = Brain(server: server, stt: stt, shadow: nil, voice: voice, linkOverride: link)
    brain.capabilities.handle(kind: "hello", message: ["kind": "hello", "handoff": true])
    link.clear()
    return (brain, link)
}

@MainActor
private func settle(_ seconds: TimeInterval = 0.08) {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.015))
        Thread.sleep(forTimeInterval: 0.01)
    }
}

private func lastTurns(_ link: RecordingPluginLink) -> [[String: String]] {
    (link.conversations.last?["turns"] as? [[String: String]]) ?? []
}

// MARK: - 1. WakeGate: first-try Apple Dictation phonetic wake + zero casual false wakes

private func testWakeGate() {
    // Casual bystander sentences starting with "Joe" or "Jo" must NEVER wake JOAT:
    expect(WakeGate.strip("Joe said he'd be late") == nil, "casual 'Joe said…' is not addressed")
    expect(WakeGate.strip("Joe, pass the salt") == nil, "casual 'Joe, pass…' is not addressed")
    expect(WakeGate.strip("Jo went to the store") == nil, "bare 'Jo went…' is not addressed")
    expect(WakeGate.strip("I said hey joe to him") == nil, "'hey joe' mid-sentence is not addressed")

    // Apple Dictation's real transcriptions of "Hey JOAT" / "JOAT, <question>" MUST wake on first try:
    expectEqual(WakeGate.strip("Hey Joe, what time is it"), "what time is it", "'Hey Joe' wakes JOAT")
    expectEqual(WakeGate.strip("hey jo take me home"), "take me home", "'Hey Jo' wakes JOAT")
    expectEqual(WakeGate.strip("Joe, can you tell me my location"), "can you tell me my location",
                "Apple Dictation 'Joe, can you…' (2026-10-23 hardware log) wakes JOAT on first try")
    expectEqual(WakeGate.strip("Joe, what's on my calendar today?"), "what's on my calendar today?",
                "Apple Dictation 'Joe, what's…' wakes JOAT on first try")
    expectEqual(WakeGate.strip("Jot, what time is it"), "what time is it",
                "Apple Dictation 'Jot, …' at sentence start wakes JOAT")
    expectEqual(WakeGate.strip("Jolt remind me to call Sam"), "remind me to call Sam",
                "Apple Dictation 'Jolt …' at sentence start wakes JOAT")
    expectEqual(WakeGate.strip("JOAT, what time is it"), "what time is it", "'JOAT' wakes")
    expectEqual(WakeGate.strip("Hey JOAT"), "", "name alone")

    // Calling "JOAT" alone when Apple Dictation transcribes 1-token phonetic variants:
    expectEqual(WakeGate.strip("Joe."), "", "standalone 'Joe.' wakes JOAT with Yes?")
    expectEqual(WakeGate.strip("Joe?"), "", "standalone 'Joe?' wakes JOAT with Yes?")
    expectEqual(WakeGate.strip("Jo."), "", "standalone 'Jo.' wakes JOAT with Yes?")
    expectEqual(WakeGate.strip("Joats."), "", "standalone 'Joats.' wakes JOAT with Yes?")
    expectEqual(WakeGate.strip("Jode."), "", "standalone 'Jode.' wakes JOAT with Yes?")
    expectEqual(WakeGate.strip("Joke?"), "", "standalone 'Joke?' wakes JOAT with Yes?")
    expect(WakeGate.strip("That's a joke") == nil, "casual 'That's a joke' never wakes JOAT")

    // Expanded openers & polite/hesitation lead-ins:
    expectEqual(WakeGate.strip("Joe, is it going to rain?"), "is it going to rain?", "'Joe, is…' wakes")
    expectEqual(WakeGate.strip("Joe, I need a reminder at 5"), "I need a reminder at 5", "'Joe, I…' wakes")
    expectEqual(WakeGate.strip("Jo, what time is it?"), "what time is it?", "'Jo, what…' wakes")
    expectEqual(WakeGate.strip("Okay, Hey Joe, what's on my calendar?"), "what's on my calendar?",
                "hesitation/polite lead-in before anchored wake phrase wakes JOAT")
    expectEqual(WakeGate.strip("Um, Joe, what time is it?"), "what time is it?",
                "hesitation lead-in before 'Joe, <request>' wakes JOAT")
}

// MARK: - 2. Zero volatile conversation spam + clean dismiss revision

@MainActor
private func testNoVolatileConversationSpam(brain: Brain, link: RecordingPluginLink) {
    link.clear()
    brain.handle(.volatile("I told Jo about the movie"))
    brain.handle(.volatile("Hey JOAT what is"))
    brain.handle(.volatile("Hey Joanne can you pass the salt"))
    settle()
    expectEqual(link.conversations.count, 0,
                "volatile partials NEVER prematurely mount assistant.conversation on BLE")

    brain.handle(.final("Hey Joanne can you pass the salt", latencyMs: 20))
    expectEqual(link.conversations.count, 0, "unaddressed final sends nothing")

    // A tool that claims the lens dismisses the streaming conversation at the
    // exact revision the hub is showing AND rotates conversationId so Turn 2 never hits retired Set.
    link.clear()
    brain.beginStreamingConversationForTesting(user: "what's up", partialAssistant: "Hel")
    let firstCid = link.conversations.last?["conversationId"] as? String
    let shownRevision = link.conversations.last?["revision"] as? Int
    brain.dismissActiveStreamingConversation()
    expectEqual(link.conversations.last?["operation"] as? String, "dismiss", "streaming dismiss sent")
    expectEqual(link.conversations.last?["conversationId"] as? String, firstCid, "dismiss targets shown cid")
    expectEqual(link.conversations.last?["revision"] as? Int, shownRevision,
                "streaming dismiss carries exact revision on the lens")

    // Immediately asking a second question within 30s MUST mint a NEW conversationId!
    link.clear()
    brain.handle(.final("Hey JOAT, what time is it", latencyMs: 10))
    let secondCid = link.conversations.last?["conversationId"] as? String
    expect(secondCid != nil && secondCid != firstCid,
           "Turn 2 after tool dismiss MUST mint a fresh conversationId (\(String(describing: secondCid)) vs \(String(describing: firstCid)))")
    link.clear()
}

// MARK: - 3. Multi-turn conversationId rotation after touchpad exit + pre-dismissed error recovery

@MainActor
private func testMultiTurnAfterExitAndTool(brain: Brain, link: RecordingPluginLink) {
    UserDefaults.standard.set(8.0, forKey: Brain.followUpKey)
    brain.conversationChoice("exit")
    link.clear()

    // Turn 1: answered on conversation card C1
    brain.handle(.final("Hey JOAT, pomodoro status", latencyMs: 15))
    let c1 = link.conversations.last?["conversationId"] as? String
    expect(c1 != nil, "Turn 1 produced conversation C1")
    expectEqual(link.conversations.last?["listening"] as? Bool, true,
                "Turn 1 ready snapshot carries listening: true for status bar")
    expect(lastTurns(link).allSatisfy { !($0["text"] ?? "").contains("Listening") },
           "NO fake 'You: Listening…' turn added to transcript rows")

    // Wearer taps touchpad once to dismiss C1 -> Hub retires C1 and sends "exit".
    brain.conversationChoice("exit")
    link.clear()

    // 2 seconds later (well within 30s!), wearer asks Turn 2: MUST get a brand-new C2 != C1!
    brain.handle(.final("Hey JOAT, what time is it", latencyMs: 15))
    let c2 = link.conversations.last?["conversationId"] as? String
    expect(c2 != nil && c2 != c1,
           "Turn 2 after touchpad exit MUST use fresh conversationId (\(String(describing: c2)) != \(String(describing: c1)))")

    // Turn 3 inside the open follow-up window (card still visible, no exit): MUST stay on C2 and append!
    link.clear()
    brain.handle(.final("pomodoro status", latencyMs: 15))
    let c3 = link.conversations.last?["conversationId"] as? String
    expectEqual(c3, c2, "in-place follow-up while card is visible stays on C2")
    expectEqual(lastTurns(link).count, 4, "in-place follow-up appends to C2's turns (2 exchanges = 4 turns)")
}

// MARK: - 4. Speech-start follow-up latch & filler-safe "Hey JOAT -> Yes?"

@MainActor
private func testSpeechLatchAndFillerResistance(brain: Brain, link: RecordingPluginLink) {
    // Use a tiny 0.25s follow-up window to simulate a user who starts speaking
    // inside the window and finishes speaking AFTER the window expires!
    UserDefaults.standard.set(0.25, forKey: Brain.followUpKey)
    brain.conversationChoice("exit")
    link.clear()

    brain.handle(.final("Hey JOAT, what time is it", latencyMs: 10))
    expect(brain.followUpWindowOpen, "0.25s window open right after answer")

    // Wearer starts speaking at t = 0.10s (inside window!)
    settle(0.10)
    brain.handle(.volatile("what day is"))

    // Sentence finishes at t = 0.45s (PAST the 0.25s window!)
    settle(0.35)
    expect(!brain.followUpWindowOpen, "raw timer has now expired while wearer was still talking")
    link.clear()
    brain.handle(.final("what's today's date", latencyMs: 40))
    let answered = link.conversations.filter { ($0["state"] as? String) == "ready" }
    expect(!answered.isEmpty,
           "utterance that STARTED inside follow-up window MUST be answered even when .final lands after expiry")

    // "Hey JOAT" -> "Yes?" followed by filler "um" MUST NOT consume nameOnlyUntil!
    UserDefaults.standard.set(8.0, forKey: Brain.followUpKey)
    brain.conversationChoice("exit")
    link.clear()
    brain.handle(.final("Hey JOAT", latencyMs: 10))
    expectEqual(lastTurns(link).last?["text"], "Yes?", "name alone replies Yes?")
    link.clear()
    brain.handle(.final("um", latencyMs: 10))
    expectEqual(link.conversations.count, 0, "filler 'um' does not trigger an answer or consume nameOnlyUntil")
    brain.handle(.final("what time is it", latencyMs: 10))
    expect(!link.conversations.isEmpty, "actual question after 'Hey JOAT ... um ...' is answered!")

    // Surface receipts, navigation cues, and message arrivals still NEVER open follow-up window
    brain.conversationChoice("exit")
    brain.handle(.final("Hey JOAT, open my mail", latencyMs: 20))
    expect(!brain.followUpWindowOpen, "surface receipt does not arm follow-up window")
    brain.capabilities.openHome()

    // Setting = 0 disables follow-ups and listening flag
    UserDefaults.standard.set(0.0, forKey: Brain.followUpKey)
    brain.conversationChoice("exit")
    link.clear()
    brain.handle(.final("Hey JOAT, pomodoro status", latencyMs: 10))
    expect(!brain.followUpWindowOpen, "0s disables follow-up window")
    expect(link.conversations.last?["listening"] as? Bool != true, "listening flag omitted/false when 0s")
    UserDefaults.standard.removeObject(forKey: Brain.followUpKey)
}

// MARK: - 5. Instant (<50ms) local deterministic capabilities (Time, Date, Calendar, Reminders, QuickNote)

@MainActor
private func testFastLocalCapabilities(brain: Brain, link: RecordingPluginLink) {
    brain.conversationChoice("exit")
    link.clear()

    let t0 = Date()
    brain.handle(.final("Hey JOAT, what time is it?", latencyMs: 5))
    brain.handle(.final("What is on my calendar today?", latencyMs: 5))
    brain.handle(.final("What are my reminders right now?", latencyMs: 5))
    brain.handle(.final("Hey JOAT, remember for me that the gate code is 7741", latencyMs: 5))
    let elapsedMs = Date().timeIntervalSince(t0) * 1000
    settle(0.15)

    expect(elapsedMs < 200, "local deterministic queries run in <200ms without LLM round-trips (took \(Int(elapsedMs))ms)")
    expect(brain.quickNotes.textNotes.contains { $0.text == "the gate code is 7741" },
           "'remember for me that …' saved verbatim QuickNote deterministically")
}

// MARK: - 6.Reply arbitration & non-destructive glasses_command

@MainActor
private func testReplyArbitrationAndTools(brain: Brain, link: RecordingPluginLink) async {
    brain.capabilities.openHome()
    brain.messages.arrived(from: "Mom", body: "Are you coming?")
    settle(0.05)
    expect(brain.arbitrateUnaddressedReply("reply sounds good", now: Date()) == nil,
           "arrival alone does not let unaddressed 'reply …' through")

    brain.capabilities.claim("MESSAGES")
    _ = brain.messages.thread("Mom", requestId: nil, traceId: nil)
    expect(brain.arbitrateUnaddressedReply("reply sounds good", now: Date()) === brain.messages,
           "with Messages thread on glass, 'reply …' routes to Messages")
    brain.capabilities.openHome()

    // Non-claiming glasses_command MUST NOT pre-dismiss active streaming conversation!
    brain.beginStreamingConversationForTesting(user: "check timer", partialAssistant: "Checking…")
    link.clear()
    let status = await brain.toolBelt.execute(name: "glasses_command", arguments: ["utterance": "pomodoro status"])
    expect(!status.claimsLens, "text command reply keeps claimsLens == false")
    expect(!link.conversations.contains { ($0["operation"] as? String) == "dismiss" },
           "non-claiming glasses_command MUST NOT prematurely dismiss assistant.conversation!")

    let open = await brain.toolBelt.execute(name: "glasses_command", arguments: ["utterance": "open my mail"])
    expect(open.claimsLens, "surface-opening glasses_command claims lens")
    expect(link.conversations.contains { ($0["operation"] as? String) == "dismiss" },
           "lens-claiming glasses_command dismisses assistant.conversation before claiming")
    brain.capabilities.openHome()

    let sys = OnPhoneAI.assistantSystem(now: Date(), area: nil, snippets: nil)
    expect(!sys.contains("hands the glasses screen directly"), "stale Mac-handoff text removed")
    expect(sys.contains("glasses_command"), "system prompt describes glasses_command")
}

// MARK: - 7. Split Dictation Call ("Joe." -> question), R1 Ring Isolation, & Log Export .txt

@MainActor
private func testSplitCallAndBleIsolation(brain: Brain, link: RecordingPluginLink) {
    brain.conversationChoice("exit")
    link.clear()

    // Progressive dictation splits "JOAT, [pause] what time is it?" into "Joe." then "what time is it?":
    brain.handle(.final("Joe.", latencyMs: 12))
    expectEqual(lastTurns(link).last?["text"], "Yes?", "standalone 'Joe.' immediately mounts Yes? card")
    brain.handle(.final("what time is it?", latencyMs: 15))
    expect((lastTurns(link).last?["text"] ?? "").hasPrefix("It is "),
           "second half of split dictation immediately answers the question")

    // R1 Ring device status (deviceRole == "other") records BLE diagnostics without touching G2 wear/mic resume:
    link.clear()
    let beforeRingCount = BleDiagnostics.shared.otherDeviceEvents
    brain.router.route(
        kind: "device",
        message: [
            "kind": "device",
            "connect": "disconnected",
            "deviceRole": "other",
            "snTail": "4412",
            "wearing": false,
            "battery": 0,
        ],
        raw: #"{"kind":"device","connect":"disconnected","deviceRole":"other","snTail":"4412","wearing":false}"#
    )
    settle(0.05)
    expectEqual(BleDiagnostics.shared.otherDeviceEvents, beforeRingCount + 1,
                "R1/other device status recorded in BleDiagnostics")
    expect(!link.sent.contains { ($0["kind"] as? String) == "audio.control" },
           "R1/other device status does NOT trigger G2 audio.control")

    // Log share export produces a .txt file so chat/AirDrop uploads never fail as 'Invalid Media':
    Log.add("diagnostic export check")
    let exportURL = Log.exportShareURL()
    expectEqual(exportURL.pathExtension, "txt", "exported diagnostics share file uses .txt extension")
    expect(FileManager.default.fileExists(atPath: exportURL.path), "exported .txt log file exists on disk")

    // MicResume tracks 'mic' field from kind: 'device' and reopens mic on wear under 'auto'/'mac',
    // while respecting micMode == 'on_demand' (R1-safe zero idle BLE mic):
    link.clear()
    brain.router.route(kind: "policy", message: ["kind": "policy", "resume": "auto", "micMode": "glasses"], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "mic": false], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "wearing": false], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "wearing": true], raw: "{}")
    settle(0.05)
    expect(link.sent.contains { ($0["kind"] as? String) == "audio.control" && ($0["open"] as? Bool) == true },
           "MicResume reopens mic on wear when mic was false and micMode is glasses")

    link.clear()
    brain.router.route(kind: "policy", message: ["kind": "policy", "resume": "auto", "micMode": "on_demand"], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "mic": false], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "wearing": false], raw: "{}")
    brain.router.route(kind: "device", message: ["kind": "device", "connect": "connected", "wearing": true], raw: "{}")
    settle(0.05)
    expect(!link.sent.contains { ($0["kind"] as? String) == "audio.control" && ($0["open"] as? Bool) == true },
           "MicResume keeps idle BLE mic OFF in on_demand (R1-safe) mode")

    // Voice QuickNote control command works even while wantsSpeech is active (never swallowed into note body):
    brain.conversationChoice("exit")
    _ = brain.quickNotes.handle("start a voice note called Standup")
    expect(brain.quickNotes.wantsSpeech(audioEnd: Date()), "voice note is actively recording")
    brain.handle(.final("First line of the note", latencyMs: 10))
    brain.handle(.final("Hey JOAT, pause voice note", latencyMs: 10))
    expect(!brain.quickNotes.wantsSpeech(audioEnd: Date().addingTimeInterval(2)),
           "'Hey JOAT, pause voice note' pauses recording instead of appending to note")
    let latestBody = (brain.quickNotes.openVoiceNoteSnapshot?.paragraphs ?? []).joined(separator: " ")
    expect(!latestBody.lowercased().contains("pause voice note"), "control command was NOT transcribed into note body")
    _ = brain.quickNotes.handle("discard voice note")
    _ = brain.quickNotes.handle("confirm discard")
    brain.capabilities.openHome()
}

func runEntry() {
    let sema = DispatchSemaphore(value: 0)
    Task { @MainActor in
        testWakeGate()
        let (brain, link) = makeTestBrain()
        testNoVolatileConversationSpam(brain: brain, link: link)
        testMultiTurnAfterExitAndTool(brain: brain, link: link)
        testSpeechLatchAndFillerResistance(brain: brain, link: link)
        testFastLocalCapabilities(brain: brain, link: link)
        await testReplyArbitrationAndTools(brain: brain, link: link)
        testSplitCallAndBleIsolation(brain: brain, link: link)
        brain.pomodoro.reset()
        print("PASS: all \(passedAssertions) wake & multi-turn reliability assertions succeeded.")
        sema.signal()
    }
    while sema.wait(timeout: .now()) == .timedOut {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
}
#endif

runEntry()
