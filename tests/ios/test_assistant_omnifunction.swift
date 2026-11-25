// Comprehensive unit & integration test suite for the Omnicapable JOAT Assistant:
// 1. `Brain.stripPoliteLeadIn` & deterministic command routing for polite natural
//    variants across Mail, Messages (including `"text Mom"` / recipient-only),
//    Reminders, Timers/Pomodoro, QuickNotes/Voice Notes, Navigation, and
//    Capabilities (`"open Antigravity"`, `"open Claude"`, `"open Codex"`, `"go home"`).
// 2. Schema validation across every tool in `PhoneToolBelt` (`>= 25` tools):
//    - `geminiDeclarations`: recursive check that NO unsupported JSON Schema keys
//      (`additionalProperties`, `minLength`, `maxLength`, `pattern`, `anyOf`,
//      `oneOf`, `allOf`) appear at any depth, and every `"type"` is uppercased.
//    - `claudeDefinitions`: valid `"name"`, `"description"`, and lowercase
//      `"type": "object"` `input_schema`.
// 3. Direct execution of `PhoneToolBelt` tools against an in-memory `Brain`:
//    - `create_quicknote` & `read_notes` (including Apple Pencil `Notebook` page
//      typed + handwriting OCR text and `Prep` notes)
//    - `control_timer` (12-minute countdown timer + `status` check)
//    - `create_reminder` arming `ConfirmGate` (`claimsLens == true`) & dismissing
//      `assistant.conversation` BEFORE `confirm.request`
//    - `prepare_email` & `prepare_message` arming `ConfirmGate` (`claimsLens == true`)
//      & dismissing `assistant.conversation` BEFORE `confirm.request`
//    - `start_navigation` (`"driving"`, `claimsLens == true`) & `stop_navigation`
//    - `open_surface` (`"agent_terminal"`, `"antigravity"`, `"claude"`, `"mail"`, `"home"`)
//    - `get_weather` & `list_saved_places`
// 4. `GeminiLink` SSE parser & `streamWithTools` preserving `"thoughtSignature"`
//    verbatim across `functionCall` -> `functionResponse` rounds (plus lens-claiming
//    short-circuit and `ClaudeLink.streamWithTools` `tool_use` + `input_json_delta`).
//
// Run directly via:
//   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift \
//     tests/ios/test_assistant_omnifunction.swift

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
        .appendingPathComponent("joat_test_omnifunction_\(ProcessInfo.processInfo.processIdentifier)")
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
    let outBinary = tempDir.appendingPathComponent("omnifunction_runner")
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

private func expectTurnResult(
    _ result: Result<AssistantTurnResult, MacLink.Failure>,
    _ expected: AssistantTurnResult,
    _ message: String,
    file: StaticString = #file,
    line: UInt = #line
) {
    switch result {
    case .success(let actual):
        expectEqual(actual, expected, message, file: file, line: line)
    case .failure(let err):
        fputs("FAIL [\(file):\(line)]: \(message) — got failure: \(MacLink.explain(err))\n", stderr)
        exit(1)
    }
}

private final class RecordingPluginLink: PluginLink {
    private(set) var sent: [[String: Any]] = []

    func send(_ object: [String: Any]) {
        sent.append(object)
    }

    func clear() {
        sent.removeAll()
    }
}

/// Intercepts `URLSession` requests for deterministic SSE streaming tests of
/// `GeminiLink.streamWithTools` and `ClaudeLink.streamWithTools`.
private final class MockSSEURLProtocol: URLProtocol {
    struct CannedResponse {
        let statusCode: Int
        let headers: [String: String]
        let body: Data
    }

    struct RecordedRequest {
        let url: String
        let method: String
        let authorization: String?
        let body: [String: Any]?
    }

    private static let lock = NSLock()
    private static var queue: [(URLRequest) -> CannedResponse] = []
    private static var capturedBodies: [[String: Any]] = []
    private static var capturedRequests: [RecordedRequest] = []

    static func reset(_ handlers: [(URLRequest) -> CannedResponse]) {
        lock.lock()
        queue = handlers
        capturedBodies = []
        capturedRequests = []
        lock.unlock()
    }

    static func recordedBodies() -> [[String: Any]] {
        lock.lock()
        defer { lock.unlock() }
        return capturedBodies
    }

    static func recordedRequests() -> [RecordedRequest] {
        lock.lock()
        defer { lock.unlock() }
        return capturedRequests
    }

    private static func readBody(from request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var out = Data()
        let bufSize = 4096
        let buf = UnsafeMutablePointer<UInt8>.allocate(capacity: bufSize)
        defer { buf.deallocate() }
        while stream.hasBytesAvailable {
            let n = stream.read(buf, maxLength: bufSize)
            if n <= 0 { break }
            out.append(buf, count: n)
        }
        return out
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        MockSSEURLProtocol.lock.lock()
        var parsedBody: [String: Any]?
        if let raw = Self.readBody(from: request),
           let json = try? JSONSerialization.jsonObject(with: raw) as? [String: Any] {
            parsedBody = json
            MockSSEURLProtocol.capturedBodies.append(json)
        }
        MockSSEURLProtocol.capturedRequests.append(
            RecordedRequest(
                url: request.url?.absoluteString ?? "",
                method: request.httpMethod ?? "GET",
                authorization: request.value(forHTTPHeaderField: "Authorization"),
                body: parsedBody
            )
        )
        let handler = MockSSEURLProtocol.queue.isEmpty ? nil : MockSSEURLProtocol.queue.removeFirst()
        MockSSEURLProtocol.lock.unlock()

        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let canned = handler(request)
        let response = HTTPURLResponse(
            url: request.url ?? URL(string: "https://example.com")!,
            statusCode: canned.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: canned.headers
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: canned.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
private func makeTestBrain() -> (brain: Brain, link: RecordingPluginLink) {
    let link = RecordingPluginLink()
    let server = LoopbackServer(port: 0)
    let stt = Transcriber(kind: .dictation(words: ["JOAT", "Hey JOAT"]), label: "test")
    let voice = Voice()
    voice.spokenReplies = false
    let brain = Brain(server: server, stt: stt, shadow: nil, voice: voice, linkOverride: link)
    // Simulate a connected Even Hub plugin that supports Mac handoff.
    brain.capabilities.handle(kind: "hello", message: ["kind": "hello", "handoff": true])
    link.clear()
    return (brain, link)
}

// MARK: - 1. Polite Lead-In Normalizer & Deterministic Voice Routing

@MainActor
private func testStripPoliteLeadInAndDeterministicRouting(brain: Brain, link: RecordingPluginLink) async {
    // Direct unit checks on `Brain.stripPoliteLeadIn`
    expectEqual(
        Brain.stripPoliteLeadIn("can you open my mail?"),
        "open my mail",
        "Strips 'can you' and trailing question mark"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("Hey, could you please set a timer for 10 minutes?"),
        "set a timer for 10 minutes",
        "Strips 2 iterative prefixes ('Hey,' + 'could you please') and trailing '?'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("please email Alex saying thanks"),
        "email Alex saying thanks",
        "Strips 'please' while preserving original capitalization ('Alex')"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("Open my notes, please!"),
        "Open my notes",
        "Strips trailing ', please!'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("set a timer for 5 minutes please"),
        "set a timer for 5 minutes",
        "Strips trailing ' please'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("Okay, go ahead and go home."),
        "go home",
        "Strips 'Okay,' + 'go ahead and'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("I'd like you to take a note Meet Dr. Patel"),
        "take a note Meet Dr. Patel",
        "Strips straight-apostrophe 'I'd like you to' preserving note casing"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("I’d like you to open Antigravity."),
        "open Antigravity",
        "Strips curly-apostrophe 'I’d like you to'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("I want you to open Claude"),
        "open Claude",
        "Strips 'I want you to'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("I need you to walk to Starbucks?"),
        "walk to Starbucks",
        "Strips 'I need you to'"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("Would you please text Mom?"),
        "text Mom",
        "Strips 'Would you please' leaving clean recipient-only command"
    )
    expectEqual(
        Brain.stripPoliteLeadIn("So, will you check my reminders, please?"),
        "check my reminders",
        "Strips 'So,' + 'will you' + ', please?'"
    )

    // Negative / word-boundary safety checks
    expectEqual(Brain.stripPoliteLeadIn("open my mail"), nil, "Unprefixed command returns nil")
    expectEqual(Brain.stripPoliteLeadIn("highlight the notes"), nil, "'hi' does not strip inside 'highlight'")
    expectEqual(Brain.stripPoliteLeadIn("Okinawa weather"), nil, "'ok' does not strip inside 'Okinawa'")
    expectEqual(Brain.stripPoliteLeadIn("soap on the shopping list"), nil, "'so' does not strip inside 'soap'")
    expectEqual(Brain.stripPoliteLeadIn("please"), nil, "Bare 'please' leaves empty string -> nil")
    expectEqual(Brain.stripPoliteLeadIn("hey, can you please?"), nil, "Only polite words -> nil")

    // Seed sample inbox, relationship, and saved place for deterministic routing checks
    brain.mail.debugSeedSampleInbox()
    let prevMom = Recipients.savedRelationship(for: "mom")
    _ = Recipients.rememberRelationship(
        relationship: "mom",
        contactName: "Mom",
        fallbackCandidates: [(label: "Mom", phone: "+13125550177", email: "mom@example.com")]
    )
    let savedHome = brain.navigation.upsertPlace(
        name: "Home",
        address: "100 Main St",
        point: NavPoint(41.8810, -87.6320)
    )
    defer {
        if let id = savedHome?.id { brain.navigation.removePlace(id: id) }
        brain.navigation.stopFromPhone()
        brain.pomodoro.reset()
        brain.confirm.declineActive(reason: "routing test cleanup")
        if let prevMom {
            _ = Recipients.rememberRelationship(
                relationship: "mom",
                contactName: prevMom.label,
                fallbackCandidates: [(label: prevMom.label, phone: prevMom.phone, email: prevMom.email)]
            )
        }
    }

    // 1a. Capabilities: polite "open Mail", "open Antigravity", "open Claude", "go home"
    link.clear()
    let openMailReply = brain.routeCommand("can you open my mail?")
    expectEqual(openMailReply?.text, "Opening Mail.", "Polite 'can you open my mail?' routes to CapabilityMenu")
    expectEqual(openMailReply?.activated, true, "Mail capability activates lens")
    expect(link.sent.contains { $0.string("kind") == "hud.list" && $0.string("title") == "MAIL" },
           "Polite open mail sent hud.list MAIL")

    link.clear()
    let openAntigravity = brain.routeCommand("Hey, could you please open Antigravity?")
    expectEqual(openAntigravity?.text, "Opening Antigravity.", "Polite 'open Antigravity' routes to Antigravity session picker")
    expect(link.sent.contains { $0.string("kind") == "hud.list" && ($0.string("title") ?? "").hasPrefix("AGENT TERMINAL") },
           "'open Antigravity' emits phone-local AGENT TERMINAL hud.list")
    expect(!link.sent.contains { $0.string("kind") == "brain.handoff" },
           "'open Antigravity' never emits brain.handoff")

    link.clear()
    let openClaude = brain.routeCommand("I'd like you to open Claude, please.")
    expectEqual(openClaude?.text, "Opening Claude Code.", "Polite 'open Claude' routes to Claude Code session picker")
    expect(link.sent.contains { $0.string("kind") == "hud.list" && ($0.string("title") ?? "").hasPrefix("AGENT TERMINAL") },
           "'open Claude' emits phone-local AGENT TERMINAL hud.list")
    expect(!link.sent.contains { $0.string("kind") == "brain.handoff" },
           "'open Claude' never emits brain.handoff")

    link.clear()
    let goHome = brain.routeCommand("Okay, go ahead and go home!")
    expectEqual(goHome?.text, "Opening Home.", "Polite 'go home' routes to CapabilityMenu.openHome()")
    expect(link.sent.contains { $0.string("kind") == "hud.rebuild" && $0.string("layout") == "default" },
           "'go home' emits hud.rebuild default")

    // 1b. Timers / Pomodoro: polite start, status, reset
    brain.pomodoro.reset()
    link.clear()
    let timerReply = brain.routeCommand("could you please set a timer for 10 minutes?")
    expect(timerReply != nil && (timerReply?.text.contains("10") == true),
           "Polite 'set a timer for 10 minutes' starts Pomodoro countdown")
    expectEqual(timerReply?.activated, true, "Timer start activates Pomodoro panel")

    let statusReply = brain.routeCommand("can you tell me timer status, please?")
        ?? brain.routeCommand("how long is left on the timer, please?")
    expect(statusReply != nil && !(statusReply?.text.isEmpty ?? true),
           "Polite timer status query matches PomodoroCommands")
    _ = brain.routeCommand("please stop the timer")

    // 1c. Mail: polite "please email <addr> saying ..." -> ConfirmGate armed!
    link.clear()
    let emailReply = brain.routeCommand("please email sarah@example.com saying thanks for the Q4 update")
    expectEqual(emailReply?.text, "", "Polite email compose matched Mail command")
    // Allow async recipient resolution hop if needed
    for _ in 0..<20 where !brain.confirm.hasPending {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    expect(brain.confirm.hasPending, "Polite email compose armed ConfirmGate")
    expect(link.sent.contains { $0.string("kind") == "confirm.request" }, "Mail sent confirm.request")
    brain.confirm.declineActive(reason: "test")

    // 1d. Messages: polite recipient-only "can you text Mom?" & full "please text Mom saying ..."
    link.clear()
    var followUpAsked: String?
    brain.messages.askFollowUp = { question, _ in followUpAsked = question }
    let textMomOnly = brain.routeCommand("Can you text Mom?")
    expectEqual(textMomOnly?.text, "", "Polite 'Can you text Mom?' matched Messages recipient-only grammar")
    for _ in 0..<20 where followUpAsked == nil {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    expect(followUpAsked?.contains("Mom") == true, "'text Mom' resolves saved Mom relationship and asks follow-up body")

    link.clear()
    let textMomWithBody = brain.routeCommand("Could you please text +13125550177 saying on my way home")
    expectEqual(textMomWithBody?.text, "", "Polite 'text <number> saying ...' matched Messages")
    for _ in 0..<20 where !brain.confirm.hasPending {
        try? await Task.sleep(nanoseconds: 25_000_000)
    }
    expect(brain.confirm.hasPending, "Polite text message armed ConfirmGate")
    brain.confirm.declineActive(reason: "test")

    // 1e. Reminders: polite "could you please remind me to check the valve in 20 minutes?"
    link.clear()
    let remReply = brain.routeCommand("Could you please remind me to check the valve in 20 minutes?")
    expectEqual(remReply?.text, "", "Polite reminder creation matched Reminders")
    expect(brain.confirm.hasPending, "Polite reminder creation armed ConfirmGate")
    brain.confirm.declineActive(reason: "test")

    // 1f. QuickNotes: polite "I'd like you to take a note ..."
    let beforeNotesIds = Set(brain.quickNotes.textNotes.map(\.id))
    let noteReply = brain.routeCommand("I'd like you to take a note Order spare G2 nose pads")
    expect(noteReply != nil, "Polite 'take a note ...' matched QuickNotes")
    let addedNote = brain.quickNotes.textNotes.first { !beforeNotesIds.contains($0.id) }
    expect(addedNote?.text == "Order spare G2 nose pads",
           "Polite QuickNote preserved exact capitalization without polite prefix")
    if let id = addedNote?.id { brain.quickNotes.deleteNote(id) }

    // 1g. Navigation: polite "can you please drive home?" (never stolen by 'go home')
    link.clear()
    let navReply = brain.routeCommand("Can you please drive home?")
    expectEqual(navReply?.activated, true, "Polite 'drive home' matched Navigation (not CapabilityMenu 'go home')")
    expectEqual(brain.navigation.active, true, "Navigation session started for Home")
    brain.navigation.stopFromPhone()

    // Also verify full `Brain.handle(.final(...))` wake-word + polite routing end-to-end
    link.clear()
    brain.handle(.final("Hey JOAT, could you please open my mail?", latencyMs: 40))
    expect(link.sent.contains { $0.string("kind") == "hud.list" && $0.string("title") == "MAIL" },
           "Full WakeGate + polite lead-in pipeline opened MAIL deterministically")
}

// MARK: - 2. PhoneToolBelt Declarations & Schema Invariants

private let allowedGeminiSchemaKeys: Set<String> = [
    "type", "description", "properties", "required", "enum", "items",
]

private let forbiddenGeminiSchemaKeys: Set<String> = [
    "additionalProperties", "minLength", "maxLength", "pattern", "anyOf", "oneOf", "allOf", "$schema",
]

private let validGeminiTypes: Set<String> = [
    "OBJECT", "STRING", "INTEGER", "NUMBER", "BOOLEAN", "ARRAY",
]

private func validateGeminiSchemaNode(_ schema: [String: Any], path: String) {
    for key in schema.keys {
        expect(!forbiddenGeminiSchemaKeys.contains(key),
               "Forbidden Gemini JSON Schema key '\(key)' at \(path)")
        expect(allowedGeminiSchemaKeys.contains(key),
               "Unexpected Gemini JSON Schema key '\(key)' at \(path)")
    }
    let typeVal = schema["type"] as? String ?? ""
    expect(validGeminiTypes.contains(typeVal),
           "Gemini schema type '\(typeVal)' at \(path) must be uppercased valid OpenAPI type")

    if let props = schema["properties"] as? [String: Any] {
        for (propName, sub) in props {
            guard let subDict = sub as? [String: Any] else {
                expect(false, "Property \(propName) at \(path) must be a dictionary")
                continue
            }
            validateGeminiSchemaNode(subDict, path: "\(path).\(propName)")
        }
        if let req = schema["required"] as? [String] {
            for r in req {
                expect(props[r] != nil, "Required property '\(r)' exists in properties at \(path)")
            }
        }
    }
    if let items = schema["items"] as? [String: Any] {
        validateGeminiSchemaNode(items, path: "\(path)[]")
    }
}

@MainActor
private func testToolBeltSchemas(brain: Brain) {
    guard let belt = brain.toolBelt else {
        expect(false, "Brain.toolBelt initialized in Brain.init")
        return
    }
    let tools = belt.tools
    expectEqual(tools.count, 30, "PhoneToolBelt exposes all 30 tools (incl. glasses_command)")

    let expectedNames: Set<String> = [
        "calendar_agenda", "open_reminders", "create_reminder", "complete_reminder",
        "get_recent_emails", "read_email_thread", "prepare_email",
        "get_recent_messages", "read_message_thread", "prepare_message",
        "contacts_lookup", "remember_relationship",
        "start_navigation", "stop_navigation", "find_nearby_places", "where_am_i", "list_saved_places",
        "read_notes", "create_quicknote", "control_voice_note",
        "control_timer", "get_weather", "news_headlines", "read_news_article", "web_search",
        "open_surface", "terminal_status", "notifications_and_voice", "agent_terminal_action",
    ]
    let actualNames = Set(tools.map(\.name))
    for required in expectedNames {
        expect(actualNames.contains(required), "PhoneToolBelt includes '\(required)'")
    }

    // Validate Gemini functionDeclarations
    let geminiDecls = belt.geminiDeclarations
    expectEqual(geminiDecls.count, tools.count, "geminiDeclarations count matches tools count")
    expect(JSONSerialization.isValidJSONObject(["tools": [["functionDeclarations": geminiDecls]]]),
           "geminiDeclarations serializes cleanly to JSON")

    for decl in geminiDecls {
        let name = decl["name"] as? String ?? ""
        let desc = decl["description"] as? String ?? ""
        expect(!name.isEmpty, "Gemini declaration has non-empty name")
        expect(!desc.isEmpty, "Gemini declaration '\(name)' has non-empty description")
        guard let params = decl["parameters"] as? [String: Any] else {
            expect(false, "Gemini declaration '\(name)' has parameters dict")
            continue
        }
        expectEqual(params["type"] as? String, "OBJECT", "Root parameters type is OBJECT for '\(name)'")
        validateGeminiSchemaNode(params, path: name)
    }

    // Validate Claude definitions
    let claudeDefs = belt.claudeDefinitions
    expectEqual(claudeDefs.count, tools.count, "claudeDefinitions count matches tools count")
    expect(JSONSerialization.isValidJSONObject(["tools": claudeDefs]),
           "claudeDefinitions serializes cleanly to JSON")

    for def in claudeDefs {
        let name = def["name"] as? String ?? ""
        let desc = def["description"] as? String ?? ""
        expect(!name.isEmpty, "Claude definition has non-empty name")
        expect(!desc.isEmpty, "Claude definition '\(name)' has non-empty description")
        guard let schema = def["input_schema"] as? [String: Any] else {
            expect(false, "Claude definition '\(name)' has input_schema")
            continue
        }
        expectEqual(schema["type"] as? String, "object", "Claude input_schema root type is lowercase 'object' for '\(name)'")
    }
}

// MARK: - 3. Direct Execution of PhoneToolBelt Tools & Lens-Claiming Handoff

@MainActor
private func testDirectToolExecutionAndHandoff(brain: Brain, link: RecordingPluginLink) async {
    let belt = brain.toolBelt!
    var createdNoteIds: [String] = []
    var createdPlaceIds: [String] = []
    let tempPrepDir = FileManager.default.temporaryDirectory
        .appendingPathComponent("joat_prep_test_\(UUID().uuidString)")
    let testPrep = Prep(claude: ClaudeLink(config: nil), directory: tempPrepDir)
    brain.quickNotes.prep = testPrep

    defer {
        for id in createdNoteIds { brain.quickNotes.deleteNote(id) }
        for id in createdPlaceIds { brain.navigation.removePlace(id: id) }
        try? FileManager.default.removeItem(at: tempPrepDir)
        brain.navigation.stopFromPhone()
        brain.pomodoro.reset()
        brain.confirm.declineActive(reason: "tool execution test cleanup")
    }

    // 3a. `create_quicknote` + `read_notes` (QuickNote + Apple Pencil Notebook page + Prep folder)
    let existingTextIds = Set(brain.quickNotes.textNotes.map(\.id))
    brain.beginStreamingConversationForTesting(user: "Save a note about firmware 0.1.153", partialAssistant: "Saving…")
    link.clear()

    let qnOutcome = await belt.execute(
        name: "create_quicknote",
        arguments: ["text": "Verify G2 binocular LUT calibration at 400 lux"]
    )
    expectEqual(qnOutcome.claimsLens, true, "create_quicknote claims lens")
    expect(qnOutcome.spokenReceipt?.isEmpty == false, "create_quicknote provides spokenReceipt")
    expect(brain.currentTurns.isEmpty, "dismissActiveStreamingConversation cleared active streaming turns")
    let qnDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    let qnListIdx = link.sent.firstIndex { $0.string("kind") == "hud.list" }
    expect(qnDismissIdx != nil, "create_quicknote dismissed assistant.conversation")
    if let d = qnDismissIdx, let l = qnListIdx {
        expect(d < l, "assistant.conversation dismissed BEFORE QuickNotes hud.list")
    }
    if let createdText = brain.quickNotes.textNotes.first(where: { !existingTextIds.contains($0.id) }) {
        createdNoteIds.append(createdText.id)
    }

    // Seed an Apple Pencil Notebook page (typed + handwriting OCR) and a Prep note
    let pageId = "test-pencil-page-\(UUID().uuidString.prefix(8).lowercased())"
    createdNoteIds.append(pageId)
    _ = brain.quickNotes.ensureBlankPage(id: pageId, paperTemplate: "ruled")
    _ = brain.quickNotes.savePage(
        id: pageId,
        typed: "Typed header: Optical waveguide inspection",
        inkChanged: false,
        paperTemplate: "ruled"
    )
    brain.quickNotes.renameNote(pageId, to: "Waveguide Lab Page")
    brain.quickNotes.pageRead(pageId, handwriting: "Handwritten ink: serial #G2-884 passes MTF check")

    let prepId = testPrep.create()
    testPrep.addText("Key spec: 284x220 4-bit greyscale HUD map viewport.", title: "HUD Spec", source: "Notes", to: prepId)

    let allNotesOutcome = await belt.execute(name: "read_notes", arguments: ["count": 10])
    expectEqual(allNotesOutcome.claimsLens, false, "read_notes does not claim lens")
    expect(allNotesOutcome.output.contains("Verify G2 binocular LUT calibration"),
           "read_notes includes verbatim QuickNote")
    expect(allNotesOutcome.output.contains("Waveguide Lab Page"),
           "read_notes includes Apple Pencil Notebook page title")
    expect(allNotesOutcome.output.contains("Typed header: Optical waveguide inspection"),
           "read_notes includes Pencil page typed text")
    expect(allNotesOutcome.output.contains("Handwritten ink: serial #G2-884 passes MTF check"),
           "read_notes includes Pencil page handwriting OCR text")
    expect(allNotesOutcome.output.contains("284x220 4-bit greyscale HUD map viewport"),
           "read_notes includes Prep folder content")

    let filteredNotes = await belt.execute(name: "read_notes", arguments: ["query": "G2-884"])
    expect(filteredNotes.output.contains("Handwritten ink: serial #G2-884"),
           "read_notes query filters across Pencil page handwriting OCR")

    // 3b. `control_timer`: set a 12-minute timer (`claimsLens == true`) & read `status` (`claimsLens == false`)
    brain.beginStreamingConversationForTesting(user: "Set a 12 minute timer")
    link.clear()
    let startTimerOutcome = await belt.execute(
        name: "control_timer",
        arguments: ["action": "start_timer", "minutes": 12]
    )
    expectEqual(startTimerOutcome.claimsLens, true, "control_timer start_timer claims lens")
    expect(startTimerOutcome.output.contains("12"), "control_timer output confirms 12-minute timer")
    expect(startTimerOutcome.spokenReceipt?.contains("12") == true, "control_timer spokenReceipt mentions 12")
    expect(link.sent.contains { $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss" },
           "control_timer start_timer dismissed assistant.conversation")

    let timerStatusOutcome = await belt.execute(
        name: "control_timer",
        arguments: ["action": "status"]
    )
    expectEqual(timerStatusOutcome.claimsLens, false, "control_timer status keeps claimsLens == false for LLM synthesis")
    expect(!timerStatusOutcome.output.isEmpty && !timerStatusOutcome.output.lowercased().contains("idle"),
           "control_timer status reports active 12-minute countdown (\(timerStatusOutcome.output))")
    _ = await belt.execute(name: "control_timer", arguments: ["action": "reset"])

    // 3c. `create_reminder`: arms `ConfirmGate` (`claimsLens == true`) & dismisses `assistant.conversation` first!
    brain.beginStreamingConversationForTesting(user: "Remind me to call Sam in 30 minutes", partialAssistant: "Thinking…")
    link.clear()
    let remOutcome = await belt.execute(
        name: "create_reminder",
        arguments: ["title": "Call Sam about PCB rev 3", "due": "in 30 minutes"]
    )
    expectEqual(remOutcome.claimsLens, true, "create_reminder claims lens")
    expect(brain.confirm.hasPending, "create_reminder armed ConfirmGate (never bypasses touchpad confirmation)")
    let remDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    let remConfirmIdx = link.sent.firstIndex { $0.string("kind") == "confirm.request" }
    expect(remDismissIdx != nil && remConfirmIdx != nil, "Both dismiss and confirm.request emitted for create_reminder")
    if let d = remDismissIdx, let c = remConfirmIdx {
        expect(d < c, "assistant.conversation dismissed BEFORE confirm.request on create_reminder")
    }
    brain.confirm.declineActive(reason: "test")

    // 3d. `prepare_email` + `get_recent_emails` + `read_email_thread`
    brain.mail.debugSeedSampleInbox()
    let recentMailOutcome = await belt.execute(name: "get_recent_emails", arguments: ["limit": 5])
    expectEqual(recentMailOutcome.claimsLens, false, "get_recent_emails is read-only")
    expect(recentMailOutcome.output.contains("Sarah Chen") && recentMailOutcome.output.contains("Q4 Launch Checklist"),
           "get_recent_emails reads seeded multi-account Gmail inbox")

    let threadMailOutcome = await belt.execute(name: "read_email_thread", arguments: ["query": "Q4 Launch"])
    expect(threadMailOutcome.output.contains("release notes before 3 PM"),
           "read_email_thread returns full chronological email body")

    brain.beginStreamingConversationForTesting(user: "Email Sarah saying looks great")
    link.clear()
    let prepEmailOutcome = await belt.execute(
        name: "prepare_email",
        arguments: [
            "mode": "new",
            "recipient": "sarah@example.com",
            "body": "Looks great, approved for Tuesday rollout.",
        ]
    )
    expectEqual(prepEmailOutcome.claimsLens, true, "prepare_email claims lens")
    expect(brain.confirm.hasPending, "prepare_email armed ConfirmGate awaiting touchpad tap")
    let mailDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    let mailConfirmIdx = link.sent.firstIndex { $0.string("kind") == "confirm.request" }
    expect(mailDismissIdx != nil && mailConfirmIdx != nil, "prepare_email sent dismiss + confirm.request")
    if let d = mailDismissIdx, let c = mailConfirmIdx {
        expect(d < c, "assistant.conversation dismissed BEFORE confirm.request on prepare_email")
    }
    brain.confirm.declineActive(reason: "test")

    // 3e. `prepare_message`
    brain.beginStreamingConversationForTesting(user: "Text +13125550199 saying running 5 minutes late")
    link.clear()
    let prepMsgOutcome = await belt.execute(
        name: "prepare_message",
        arguments: [
            "mode": "new",
            "recipient": "+13125550199",
            "body": "Running 5 minutes late!",
        ]
    )
    expectEqual(prepMsgOutcome.claimsLens, true, "prepare_message claims lens")
    expect(brain.confirm.hasPending, "prepare_message armed ConfirmGate awaiting touchpad tap")
    let msgDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    let msgConfirmIdx = link.sent.firstIndex { $0.string("kind") == "confirm.request" }
    expect(msgDismissIdx != nil && msgConfirmIdx != nil, "prepare_message sent dismiss + confirm.request")
    if let d = msgDismissIdx, let c = msgConfirmIdx {
        expect(d < c, "assistant.conversation dismissed BEFORE confirm.request on prepare_message")
    }
    brain.confirm.declineActive(reason: "test")

    // 3f. `list_saved_places` + `start_navigation` ("driving") + `stop_navigation`
    if let office = brain.navigation.upsertPlace(
        name: "Test HQ",
        address: "233 S Wacker Dr, Chicago, IL",
        point: NavPoint(41.8789, -87.6359)
    ) {
        createdPlaceIds.append(office.id)
    }
    let placesOutcome = await belt.execute(name: "list_saved_places", arguments: [:])
    expectEqual(placesOutcome.claimsLens, false, "list_saved_places is read-only")
    expect(placesOutcome.output.contains("Test HQ") && placesOutcome.output.contains("233 S Wacker Dr"),
           "list_saved_places lists saved places and addresses")

    brain.beginStreamingConversationForTesting(user: "Drive me to Test HQ")
    link.clear()
    let navOutcome = await belt.execute(
        name: "start_navigation",
        arguments: ["destination": "Test HQ", "mode": "driving"]
    )
    expectEqual(navOutcome.claimsLens, true, "start_navigation claims lens")
    expectEqual(brain.navigation.active, true, "start_navigation started active driving session")
    let navDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    expect(navDismissIdx != nil, "start_navigation dismissed assistant.conversation")

    let stopNavOutcome = await belt.execute(name: "stop_navigation", arguments: [:])
    expectEqual(stopNavOutcome.claimsLens, false, "stop_navigation does not claim lens")
    expectEqual(brain.navigation.active, false, "stop_navigation ended active navigation")

    // 3g. `open_surface` for `"agent_terminal"`, `"mail"`, and `"home"`
    for (surfaceArg, expectedReceipt, verifyFrame) in [
        ("agent_terminal", "Opening Agent Terminal.", { (frames: [[String: Any]]) in
            frames.contains { $0.string("kind") == "hud.list" && $0.string("title") == "AGENT TERMINAL" }
                && !frames.contains { $0.string("kind") == "brain.handoff" }
        }),
        ("mail", "Opening Mail.", { (frames: [[String: Any]]) in
            frames.contains { $0.string("kind") == "hud.list" && $0.string("title") == "MAIL" }
        }),
        ("home", "Opening Home.", { (frames: [[String: Any]]) in
            frames.contains { $0.string("kind") == "hud.rebuild" && $0.string("layout") == "default" }
        }),
    ] {
        brain.beginStreamingConversationForTesting(user: "Open \(surfaceArg)")
        link.clear()
        let outcome = await belt.execute(name: "open_surface", arguments: ["surface": surfaceArg])
        expectEqual(outcome.claimsLens, true, "open_surface('\(surfaceArg)') claims lens")
        expectEqual(outcome.spokenReceipt, expectedReceipt, "open_surface('\(surfaceArg)') spokenReceipt")
        let disIdx = link.sent.firstIndex {
            $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
        }
        expect(disIdx != nil, "open_surface('\(surfaceArg)') dismissed assistant.conversation")
        expect(verifyFrame(link.sent), "open_surface('\(surfaceArg)') emitted expected downlink frame")
    }

    // 3h. `get_weather`
    let weatherOutcome = await belt.execute(name: "get_weather", arguments: ["location": "Chicago, IL"])
    expectEqual(weatherOutcome.claimsLens, false, "get_weather returns data for LLM synthesis")
    expect(!weatherOutcome.output.isEmpty, "get_weather returned non-empty result")
    expect(
        weatherOutcome.output.contains("°F") || weatherOutcome.output.lowercased().contains("weather"),
        "get_weather formatted Open-Meteo response or clean offline message (\(weatherOutcome.output))"
    )
}

// MARK: - 4. Gemini 3.5 `thoughtSignature` Preservation & Multi-Tool SSE Streaming

@MainActor
private func testGeminiThoughtSignatureAndToolStreaming(brain: Brain, link: RecordingPluginLink) async {
    // 4a. System prompt verification (`OnPhoneAI.assistantSystem`)
    let sys = OnPhoneAI.assistantSystem(now: Date(), area: "Chicago, United States", snippets: nil)
    expect(sys.contains("Calendar") && sys.contains("Reminders") && sys.contains("Gmail") && sys.contains("Messages"),
           "assistantSystem enumerates personal data tools")
    expect(sys.contains("create_quicknote") && sys.contains("verbatim"),
           "assistantSystem enforces verbatim QuickNote capture")
    expect(sys.contains("touchpad"), "assistantSystem enforces touchpad confirmation safety")
    expectEqual(brain.voice.spokenReplies, false, "Brain.voice is directly accessible on Brain")

    // 4b. Direct unit test of `GeminiLink.SSETurnAccumulator`
    var acc = GeminiLink.SSETurnAccumulator()
    let chunk1 = #"data: {"candidates":[{"content":{"role":"model","parts":[{"thought":true,"text":"Checking timer status first."}]}}]}"#
    let sigValue = "EuQBCuEBAXLI2nyGemini35ThoughtSignature=="
    let chunk2 = #"data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"control_timer","args":{"action":"status"},"id":"call_01"},"thoughtSignature":"\#(sigValue)"}]}}]}"#
    let chunk3 = #"data: {"candidates":[{"content":{"role":"model","parts":[{"text":""}]},"finishReason":"STOP"}]}"#

    expectEqual(acc.ingestSSELine(chunk1), nil, "Thought part is never streamed to onDelta")
    expectEqual(acc.ingestSSELine(chunk2), nil, "functionCall chunk is never streamed to onDelta")
    expectEqual(acc.ingestSSELine(chunk3), nil, "Empty trailing stop part is ignored")

    expectEqual(acc.functionCalls.count, 1, "Parsed 1 functionCall")
    expectEqual(acc.functionCalls.first?.name, "control_timer", "Parsed functionCall name")
    expectEqual(acc.functionCalls.first?.id, "call_01", "Parsed functionCall id")
    expectEqual(acc.functionCalls.first?.args["action"] as? String, "status", "Parsed functionCall args")

    let modelTurn = acc.modelContentTurn()
    expectEqual(modelTurn["role"] as? String, "model", "Model turn role is 'model'")
    let rawParts = modelTurn["parts"] as? [[String: Any]] ?? []
    expectEqual(rawParts.count, 2, "Kept thought + functionCall parts and dropped empty stop part")
    expectEqual(rawParts[0]["thought"] as? Bool, true, "Preserved raw thought part")
    expectEqual(rawParts[1]["thoughtSignature"] as? String, sigValue,
                "CRITICAL: Preserved exact thoughtSignature on functionCall part")

    let fnRespTurn = GeminiLink.buildFunctionResponseTurn([
        (call: acc.functionCalls[0], outcome: PhoneToolOutcome("Pomodoro is idle."))
    ])
    expectEqual(fnRespTurn["role"] as? String, "user", "Function response turn role is 'user'")
    let respParts = fnRespTurn["parts"] as? [[String: Any]] ?? []
    let fnResp = respParts.first?["functionResponse"] as? [String: Any]
    expectEqual(fnResp?["name"] as? String, "control_timer", "functionResponse name matches")
    expectEqual(fnResp?["id"] as? String, "call_01", "functionResponse id preserved")
    expectEqual((fnResp?["response"] as? [String: Any])?["result"] as? String, "Pomodoro is idle.",
                "functionResponse result payload matches")

    // 4c. End-to-end `GeminiLink.streamWithTools` over `MockSSEURLProtocol`
    let sessionConfig = URLSessionConfiguration.ephemeral
    sessionConfig.protocolClasses = [MockSSEURLProtocol.self]
    let mockSession = URLSession(configuration: sessionConfig)
    let gemini = GeminiLink(
        config: GeminiLink.Config(key: "test-gemini-key", model: "gemini-3.5-flash-lite"),
        session: mockSession
    )

    _ = brain.pomodoro.startTimer(minutes: 7)
    defer { brain.pomodoro.reset() }

    let round1SSE = [
        #"data: {"candidates":[{"content":{"role":"model","parts":[{"thought":true,"text":"Let me check the timer."}]}}]}"#,
        #"data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"control_timer","args":{"action":"status"}},"thoughtSignature":"sig_e2e_round1_999"}]}}]}"#,
        "",
    ].joined(separator: "\n\n")

    let round2SSE = [
        #"data: {"candidates":[{"content":{"role":"model","parts":[{"text":"You have about seven minutes "}]}}]}"#,
        #"data: {"candidates":[{"content":{"role":"model","parts":[{"text":"left on your timer."}]}}]}"#,
        "",
    ].joined(separator: "\n\n")

    MockSSEURLProtocol.reset([
        { _ in .init(statusCode: 200, headers: ["Content-Type": "text/event-stream"], body: Data(round1SSE.utf8)) },
        { _ in .init(statusCode: 200, headers: ["Content-Type": "text/event-stream"], body: Data(round2SSE.utf8)) },
    ])

    var streamedDeltas: [String] = []
    let e2eResult = await gemini.streamWithTools(
        turns: [["role": "user", "text": "How much time is left?"]],
        system: sys,
        toolBelt: brain.toolBelt,
        onDelta: { streamedDeltas.append($0) }
    )
    expectTurnResult(
        e2eResult,
        .answer("You have about seven minutes left on your timer."),
        "GeminiLink.streamWithTools executed tool and streamed Round 2 synthesis"
    )
    expect(!streamedDeltas.isEmpty, "Round 2 streamed cumulative deltas to onDelta")

    let recorded = MockSSEURLProtocol.recordedBodies()
    expectEqual(recorded.count, 2, "Made exactly 2 Gemini HTTP requests")
    // Verify Free-Tier invariants (no googleSearch, no thinkingBudget) + thoughtSignature in request #2!
    for (idx, reqBody) in recorded.enumerated() {
        let toolsArr = reqBody["tools"] as? [[String: Any]] ?? []
        expect(!toolsArr.isEmpty, "Request #\(idx + 1) includes functionDeclarations")
        expect(!toolsArr.contains { $0["googleSearch"] != nil }, "Never sends googleSearch tool")
        let genCfg = reqBody["generationConfig"] as? [String: Any] ?? [:]
        let thinkingCfg = genCfg["thinkingConfig"] as? [String: Any]
        expect(genCfg["thinkingBudget"] == nil && (thinkingCfg?["thinkingBudget"] as? Int) != 0,
               "Never sends thinkingBudget: 0")
        expectEqual(thinkingCfg?["thinkingLevel"] as? String, "MINIMAL",
                    "Uses MINIMAL thinkingLevel on Gemini 3.5 Flash Lite for sub-second streaming")
    }
    let secondContents = recorded[1]["contents"] as? [[String: Any]] ?? []
    expectEqual(secondContents.count, 3, "Round 2 contents has [user, model(functionCall), user(functionResponse)]")
    let modelPartsSent = secondContents[1]["parts"] as? [[String: Any]] ?? []
    expectEqual(modelPartsSent.last?["thoughtSignature"] as? String, "sig_e2e_round1_999",
                "Round 2 HTTP POST preserved thoughtSignature intact on model functionCall part")
    let userRespParts = secondContents[2]["parts"] as? [[String: Any]] ?? []
    let timerResultSent = ((userRespParts.first?["functionResponse"] as? [String: Any])?["response"] as? [String: Any])?["result"] as? String ?? ""
    expect(timerResultSent.contains("7") || timerResultSent.lowercased().contains("timer") || timerResultSent.lowercased().contains("left") || !timerResultSent.isEmpty,
           "Round 2 HTTP POST carried real PhoneToolBelt output")

    // 4d. Gemini lens-claiming tool short-circuit (returns `.claimedLens` in 1 round, NO 2nd HTTP call!)
    let claimRoundSSE = [
        #"data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"open_surface","args":{"surface":"mail"}},"thoughtSignature":"sig_claim_1"}]}}]}"#,
        "",
    ].joined(separator: "\n\n")
    MockSSEURLProtocol.reset([
        { _ in .init(statusCode: 200, headers: ["Content-Type": "text/event-stream"], body: Data(claimRoundSSE.utf8)) },
    ])
    link.clear()
    let claimRes = await gemini.streamWithTools(
        turns: [["role": "user", "text": "Pull up my inbox"]],
        system: sys,
        toolBelt: brain.toolBelt,
        onDelta: { _ in expect(false, "Lens-claiming turn must not stream text delta") }
    )
    expectTurnResult(
        claimRes,
        .claimedLens(spokenReceipt: "Opening Mail."),
        "Lens-claiming tool immediately returns .claimedLens without second Gemini round"
    )
    expectEqual(MockSSEURLProtocol.recordedBodies().count, 1,
                "Only 1 Gemini HTTP request sent when tool claims lens")

    // 4e. End-to-end `ClaudeLink.streamWithTools` (`tool_use` + split `input_json_delta` -> `tool_result`)
    let claude = ClaudeLink(
        config: ClaudeLink.Config(key: "test-claude-key", chatModel: "claude-haiku-5-5", noteModel: nil),
        session: mockSession
    )
    let claudeRound1 = [
        #"data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_abc","name":"control_timer","input":{}}}"#,
        #"data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"act"}}"#,
        #"data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"ion\":\"status\"}"}}"#,
        #"data: {"type":"content_block_stop","index":0}"#,
        #"data: {"type":"message_stop"}"#,
        "",
    ].joined(separator: "\n\n")
    let claudeRound2 = [
        #"data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
        #"data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Your seven minute timer is running."}}"#,
        #"data: {"type":"message_stop"}"#,
        "",
    ].joined(separator: "\n\n")

    MockSSEURLProtocol.reset([
        { _ in .init(statusCode: 200, headers: ["Content-Type": "text/event-stream"], body: Data(claudeRound1.utf8)) },
        { _ in .init(statusCode: 200, headers: ["Content-Type": "text/event-stream"], body: Data(claudeRound2.utf8)) },
    ])

    var claudeDeltas: [String] = []
    let claudeRes = await claude.streamWithTools(
        turns: [["role": "user", "text": "Check my timer"]],
        system: sys,
        toolBelt: brain.toolBelt,
        onDelta: { claudeDeltas.append($0) }
    )
    expectTurnResult(
        claudeRes,
        .answer("Your seven minute timer is running."),
        "ClaudeLink.streamWithTools assembled partial_json, executed tool, and streamed synthesis"
    )
    expect(!claudeDeltas.isEmpty, "ClaudeLink streamed delta in synthesis round")
    let claudeBodies = MockSSEURLProtocol.recordedBodies()
    expectEqual(claudeBodies.count, 2, "ClaudeLink made 2 HTTP requests")
    let claudeMessages = claudeBodies[1]["messages"] as? [[String: Any]] ?? []
    expectEqual(claudeMessages.count, 3, "Claude Round 2 messages has [user, assistant(tool_use), user(tool_result)]")
}

// MARK: - 5. Phone-First Capability Routing (Zero Handoff!), MacMessages ?q= Search & Agent Terminal Actions

@MainActor
private func testAlwaysOnMacHandoffMessagesAndTerminal(brain: Brain, link: RecordingPluginLink) async {
    let belt = brain.toolBelt!

    // 5a. CapabilityMenu NEVER emits `brain.handoff` — `macPages` is empty and
    // `Messages`, `Agent Terminal`, and `Activity` are served locally on `JOAT Brain`!
    expectEqual(
        CapabilityMenu.macPages,
        Set<String>(),
        "CapabilityMenu.macPages is empty so JOAT Brain never hands off the WebSocket"
    )

    brain.capabilities.pluginConnected()
    for (localCap, expectedTitle) in [
        ("Messages", "MESSAGES"),
        ("Agent Terminal", "AGENT TERMINAL"),
        ("Activity", "ACTIVITY"),
    ] {
        link.clear()
        brain.capabilities.open(localCap, requestId: "local-\(localCap)", anchorMs: nil)
        expect(
            !link.sent.contains { $0.string("kind") == "brain.handoff" },
            "Opening '\(localCap)' never emits brain.handoff"
        )
        expect(
            link.sent.contains {
                $0.string("kind") == "hud.list"
                    && $0.string("title") == expectedTitle
                    && $0.string("requestId") == "local-\(localCap)"
            },
            "Opening '\(localCap)' immediately emits phone-local hud.list '\(expectedTitle)'"
        )
        expect(
            !link.sent.contains { ($0.string("title") ?? "").contains("NOT YET") },
            "Opening '\(localCap)' never emits a NOT YET stub"
        )
    }

    // `hello` with `handoff: true` or `handoff: false` still keeps all pages phone-local
    for handoffFlag in [true, false] {
        brain.capabilities.handle(kind: "hello", message: ["kind": "hello", "handoff": handoffFlag])
        link.clear()
        brain.capabilities.open("Messages", requestId: "hello-\(handoffFlag)", anchorMs: nil)
        expect(
            link.sent.contains { $0.string("kind") == "hud.list" && $0.string("title") == "MESSAGES" },
            "hello(handoff:\(handoffFlag)) serves Messages locally via hud.list"
        )
        expect(
            !link.sent.contains { $0.string("kind") == "brain.handoff" },
            "hello(handoff:\(handoffFlag)) never emits brain.handoff"
        )
    }

    // Voice checks: "open messages", "check my messages", "open Claude Code", "check Claude Code"
    // open phone-local `hud.list` surfaces with zero `brain.handoff` and zero trailing `assistant.overlay`.
    for (phrase, expectedSpoken, expectedTitlePrefix) in [
        ("open my messages", "Opening Messages.", "MESSAGES"),
        ("check my messages", "Opening Messages.", "MESSAGES"),
        ("open Claude Code", "Opening Claude Code.", "AGENT TERMINAL"),
        ("check Claude Code", "Opening Claude Code.", "AGENT TERMINAL"),
    ] {
        link.clear()
        let reply = brain.routeCommand(phrase)
        expectEqual(reply?.text, expectedSpoken, "Voice '\(phrase)' routes locally")
        expectEqual(reply?.suppressesHudReceipt, true, "Voice '\(phrase)' suppresses trailing hud.receipt")
        expect(
            link.sent.contains { $0.string("kind") == "hud.list" && ($0.string("title") ?? "").hasPrefix(expectedTitlePrefix) },
            "Voice '\(phrase)' emits phone-local \(expectedTitlePrefix) hud.list"
        )
        expect(
            !link.sent.contains { $0.string("kind") == "brain.handoff" },
            "Voice '\(phrase)' never emits brain.handoff"
        )
        link.clear()
        brain.handle(.final("Hey JOAT, \(phrase)", latencyMs: 25))
        expect(
            link.sent.contains { $0.string("kind") == "hud.list" && ($0.string("title") ?? "").hasPrefix(expectedTitlePrefix) },
            "Full Brain.handle(.final) for '\(phrase)' emits phone-local hud.list"
        )
        expect(
            !link.sent.contains { $0.string("kind") == "brain.handoff" },
            "Full Brain.handle(.final) for '\(phrase)' never emits brain.handoff"
        )
        expect(
            !link.sent.contains { $0.string("kind") == "assistant.overlay" },
            "Full Brain.handle(.final) for '\(phrase)' emits zero trailing assistant.overlay frames"
        )
    }
    expect(brain.routeCommand("what did Sarah text me?") == nil,
           "'what did Sarah text me?' falls through to PhoneToolBelt get_recent_messages")
    expect(brain.routeCommand("did I get any texts?") == nil,
           "'did I get any texts?' falls through to PhoneToolBelt get_recent_messages")

    // 5b. PhoneToolBelt `open_surface` opens phone-local `hud.list` with zero `brain.handoff`
    for (surfaceArg, expectedTitlePrefix, expectedReceipt) in [
        ("messages", "MESSAGES", "Opening Messages."),
        ("claude", "AGENT TERMINAL", "Opening Claude Code."),
        ("codex", "AGENT TERMINAL", "Opening Codex."),
        ("activity", "ACTIVITY", "Opening Activity."),
    ] {
        brain.beginStreamingConversationForTesting(user: "Open \(surfaceArg)")
        link.clear()
        let out = await belt.execute(name: "open_surface", arguments: ["surface": surfaceArg])
        expectEqual(out.claimsLens, true, "open_surface('\(surfaceArg)') claims lens")
        expectEqual(out.spokenReceipt, expectedReceipt, "open_surface('\(surfaceArg)') spokenReceipt")
        let dismissIdx = link.sent.firstIndex {
            $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
        }
        let listIdx = link.sent.firstIndex {
            $0.string("kind") == "hud.list" && ($0.string("title") ?? "").hasPrefix(expectedTitlePrefix)
        }
        expect(dismissIdx != nil && listIdx != nil,
               "open_surface('\(surfaceArg)') emitted dismiss + phone-local hud.list(\(expectedTitlePrefix))")
        if let d = dismissIdx, let l = listIdx {
            expect(d < l, "assistant.conversation dismissed BEFORE hud.list(\(expectedTitlePrefix))")
        }
        expect(
            !link.sent.contains { $0.string("kind") == "brain.handoff" },
            "open_surface('\(surfaceArg)') never emits brain.handoff"
        )
        expect(
            !link.sent.contains { $0.string("kind") == "assistant.overlay" },
            "open_surface('\(surfaceArg)') emits zero trailing assistant.overlay frames"
        )
    }

    // 5c. `MacMessages` `?q=` search & authoritative Mac mini `Messages` tools
    let sessionCfg = URLSessionConfiguration.ephemeral
    sessionCfg.protocolClasses = [MockSSEURLProtocol.self]
    let mockSession = URLSession(configuration: sessionCfg)

    let originalMacMessages = brain.messages.mac
    let testMacMessages = MacMessages(
        config: MacMessages.Config(url: "https://mac-mini.example.ts.net/messages", token: "test-msg-token"),
        session: mockSession
    )
    brain.messages.mac = testMacMessages
    defer {
        brain.messages.mac = originalMacMessages
    }

    // Seed an old stale phone-local text in `brain.messages` (2 hours old)
    brain.messages.arrived(
        from: "StalePhoneLocalOnly",
        body: "Old cached text from an earlier phone session",
        fromMac: true,
        quiet: true,
        sentAt: Date().addingTimeInterval(-7200)
    )

    // Direct `MacMessages.threads(limit:query:)` and `history(_:limit:)` URL checks
    let nowEpoch = Date().timeIntervalSince1970
    let directSearchJSON: [String: Any] = [
        "threads": [
            [
                "id": "thr_priya_01",
                "label": "Priya Nair",
                "group": false,
                "preview": "Prototype flex PCB arrived!",
                "at": nowEpoch - 120,
                "unread": true,
            ]
        ]
    ]
    let directHistoryJSON: [String: Any] = [
        "messages": [
            ["person": "Priya Nair", "from_me": false, "text": "Prototype flex PCB arrived!", "at": nowEpoch - 120],
            ["person": "", "from_me": true, "text": "Awesome, heading to the lab.", "at": nowEpoch - 60],
        ]
    ]
    MockSSEURLProtocol.reset([
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: directSearchJSON)
            )
        },
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: directHistoryJSON)
            )
        },
    ])

    let queriedRows = try? await testMacMessages.threads(limit: 20, query: "Priya Nair")
    expectEqual(queriedRows?.count, 1, "MacMessages.threads(limit:20, query:) decoded matching thread")
    expectEqual(queriedRows?.first?.label, "Priya Nair", "Decoded Priya Nair thread label")
    let queriedHistory = try? await testMacMessages.history("thr_priya_01", limit: 30)
    expectEqual(queriedHistory?.count, 2, "MacMessages.history(_:limit:30) decoded 2 messages")

    let msgReqs = MockSSEURLProtocol.recordedRequests()
    expectEqual(msgReqs.count, 2, "Issued threads(?q=) + history requests")
    expect(
        msgReqs[0].url.hasSuffix("/messages/v1/threads?limit=20&q=Priya%20Nair"),
        "MacMessages.threads encoded limit=20&q=Priya%20Nair (got \(msgReqs[0].url))"
    )
    expectEqual(msgReqs[0].authorization, "Bearer test-msg-token", "Sent Bearer token to /messages")
    expect(
        msgReqs[1].url.hasSuffix("/messages/v1/threads/thr_priya_01/messages?limit=30"),
        "MacMessages.history requested limit=30 (got \(msgReqs[1].url))"
    )

    // Tool `get_recent_messages`: Mac mini is authoritative (excludes `StalePhoneLocalOnly`)
    let topThreadsJSON: [String: Any] = [
        "threads": [
            [
                "id": "thr_alex_01",
                "label": "Alex Rivera",
                "group": false,
                "preview": "Lunch at 12:30?",
                "at": nowEpoch - 300,
                "unread": true,
            ]
        ]
    ]
    MockSSEURLProtocol.reset([
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: topThreadsJSON)
            )
        },
    ])
    let recentMsgTool = await belt.execute(name: "get_recent_messages", arguments: ["limit": 5])
    expect(recentMsgTool.output.contains("Alex Rivera") && recentMsgTool.output.contains("Lunch at 12:30?"),
           "get_recent_messages includes live Mac mini thread")
    expect(!recentMsgTool.output.contains("StalePhoneLocalOnly"),
           "get_recent_messages excludes stale phone-local UserDefaults texts when Mac is configured")
    expect(
        MockSSEURLProtocol.recordedRequests().first?.url.hasSuffix("/messages/v1/threads?limit=30") == true,
        "pollMacThreadsBounded requested limit=30"
    )

    // Tool `read_message_thread`: searches Mac `chat.db` via `?q=` when not in cached `macRows`!
    MockSSEURLProtocol.reset([
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: directSearchJSON)
            )
        },
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: directHistoryJSON)
            )
        },
    ])
    let readPriya = await belt.execute(
        name: "read_message_thread",
        arguments: ["sender": "Priya", "count": 8]
    )
    expect(
        readPriya.output.contains("Prototype flex PCB arrived!")
            && readPriya.output.contains("Awesome, heading to the lab."),
        "read_message_thread searched Mac chat.db via ?q=Priya and returned full thread history"
    )
    let searchThreadReqs = MockSSEURLProtocol.recordedRequests()
    expectEqual(searchThreadReqs.count, 2, "read_message_thread ran ?q= search + thread history fetch")
    expect(
        searchThreadReqs[0].url.hasSuffix("/messages/v1/threads?limit=20&q=Priya"),
        "readConversationForTool queried /v1/threads?limit=20&q=Priya (got \(searchThreadReqs[0].url))"
    )
    expect(
        searchThreadReqs[1].url.hasSuffix("/messages/v1/threads/thr_priya_01/messages?limit=30"),
        "readConversationForTool fetched /v1/threads/thr_priya_01/messages?limit=30"
    )

    // 5d. `MacTerminal` + `terminal_status` (`recentMessages` & `pendingApprovalItems`) + `agent_terminal_action`
    let prevTermHook = AssistantRuntimeHooks.sharedMacTerminal
    let testTerminal = MacTerminal(
        config: MacTerminal.Config(url: "https://mac-mini.example.ts.net/terminal", token: "test-term-token"),
        session: mockSession
    )
    AssistantRuntimeHooks.sharedMacTerminal = { testTerminal }
    defer {
        AssistantRuntimeHooks.sharedMacTerminal = prevTermHook
    }

    let statePayload: [String: Any] = [
        "assistantSeat": "claude",
        "agents": [
            [
                "name": "claude",
                "provider": "claude",
                "title": "Claude Code",
                "installed": true,
                "loggedIn": true,
                "statusDetail": "Idle in joatGlass",
                "cwd": "~/Developer/project",
                "currentModel": "claude-opus-4-6",
                "modelOptions": ["claude-opus-4-6", "claude-sonnet-4-6"],
                "busy": false,
                "activeSessionTitle": "Handoff audit",
                "pendingApprovals": 0,
                "recentMessages": [
                    ["role": "user", "text": "Check BrainHandoff rebuild count"],
                    ["role": "assistant", "text": "Eliminated the 3-rebuild BLE storm in BrainHandoff.ts."],
                ],
                "pendingApprovalItems": [],
            ],
            [
                "name": "codex",
                "provider": "codex",
                "title": "Codex",
                "installed": true,
                "loggedIn": true,
                "statusDetail": "Waiting on tool approval",
                "cwd": "~/Developer/project",
                "currentModel": "gpt-5.4",
                "modelOptions": ["gpt-5.4"],
                "busy": true,
                "pendingApprovals": 1,
                "recentMessages": [
                    ["role": "assistant", "text": "Ready to run unit suite once approved."],
                ],
                "pendingApprovalItems": [
                    ["id": "appr_42", "kind": "bash", "summary": "pytest tests/unit/test_terminal_endpoint.py"],
                ],
            ],
        ],
    ]

    MockSSEURLProtocol.reset([
        { _ in
            .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: statePayload)
            )
        },
    ])

    let termStatus = await belt.execute(name: "terminal_status", arguments: [:])
    expectEqual(termStatus.claimsLens, false, "terminal_status is read-only for LLM synthesis")
    expect(
        termStatus.output.contains("Eliminated the 3-rebuild BLE storm in BrainHandoff.ts."),
        "terminal_status includes Claude Code's latest recentMessages snippet"
    )
    expect(
        termStatus.output.contains("bash: pytest tests/unit/test_terminal_endpoint.py"),
        "terminal_status includes Codex's pendingApprovalItems summary"
    )

    // Helper returning a 200 `/v1/config` response carrying an updated state + notice
    func configAck(_ notice: String) -> (URLRequest) -> MockSSEURLProtocol.CannedResponse {
        return { _ in
            var body = statePayload
            body["notice"] = notice
            return .init(
                statusCode: 200,
                headers: ["Content-Type": "application/json"],
                body: try! JSONSerialization.data(withJSONObject: body)
            )
        }
    }

    // 1) `send_instruction` (default `open_on_glasses == true` -> dismisses conversation + opens phone-local `agent_terminal.view`!)
    MockSSEURLProtocol.reset([configAck("Sent instruction to Claude Code.")])
    brain.beginStreamingConversationForTesting(user: "Have Claude Code run the handoff tests", partialAssistant: "Sending…")
    link.clear()
    let sendInstr = await belt.execute(
        name: "agent_terminal_action",
        arguments: [
            "action": "send_instruction",
            "agent": "claude",
            "instruction": "Run the handoff unit tests and summarize results",
        ]
    )
    expectEqual(sendInstr.claimsLens, true, "send_instruction defaults open_on_glasses=true and claims lens")
    expectEqual(sendInstr.spokenReceipt, "Sent to Claude Code on your Mac.", "send_instruction spokenReceipt")
    let instrDismissIdx = link.sent.firstIndex {
        $0.string("kind") == "assistant.conversation" && $0.string("operation") == "dismiss"
    }
    let instrTermIdx = link.sent.firstIndex {
        $0.string("kind") == "agent_terminal.view"
    }
    expect(instrDismissIdx != nil && instrTermIdx != nil,
           "send_instruction emitted assistant.conversation dismiss + phone-local agent_terminal.view")
    if let d = instrDismissIdx, let t = instrTermIdx {
        expect(d < t, "dismissConversation() ran BEFORE agent_terminal.view in send_instruction")
    }
    expect(
        !link.sent.contains { $0.string("kind") == "brain.handoff" },
        "send_instruction never emits brain.handoff"
    )
    expect(
        !link.sent.contains { $0.string("kind") == "assistant.overlay" },
        "send_instruction with open_on_glasses emits zero trailing assistant.overlay frames"
    )
    brain.capabilities.openHome()
    let sendReq = MockSSEURLProtocol.recordedRequests().first
    expect(sendReq?.url.hasSuffix("/terminal/v1/config") == true && sendReq?.method == "POST",
           "send_instruction POSTed to /terminal/v1/config")
    expectEqual(sendReq?.body?["action"] as? String, "send_instruction", "POST action == send_instruction")
    expectEqual(sendReq?.body?["provider"] as? String, "claude", "POST provider == claude")
    expectEqual(
        sendReq?.body?["instruction"] as? String,
        "Run the handoff unit tests and summarize results",
        "POST instruction matches"
    )
    expectEqual(sendReq?.body?["newSession"] as? Bool, false, "POST newSession defaults to false")

    // 2) `send_instruction` with `new_session: true` and `open_on_glasses: false`
    MockSSEURLProtocol.reset([configAck("Sent instruction to Antigravity.")])
    link.clear()
    let quietInstr = await belt.execute(
        name: "agent_terminal_action",
        arguments: [
            "action": "send_instruction",
            "agent": "antigravity",
            "instruction": "Audit protocol types",
            "new_session": true,
            "open_on_glasses": false,
        ]
    )
    expectEqual(quietInstr.claimsLens, false, "open_on_glasses=false keeps claimsLens=false")
    expect(!link.sent.contains { $0.string("kind") == "brain.handoff" },
           "open_on_glasses=false does not emit brain.handoff")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["newSession"] as? Bool, true,
                "Passed newSession=true to /v1/config")

    // 3) `approve`
    MockSSEURLProtocol.reset([configAck("Approved tool request on Codex.")])
    let approveOut = await belt.execute(
        name: "agent_terminal_action",
        arguments: ["action": "approve", "agent": "codex"]
    )
    expectEqual(approveOut.claimsLens, false, "approve returns conversational receipt")
    expect(approveOut.output.contains("Approved"), "approve output confirms approval")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["action"] as? String, "decide_approval",
                "approve posts decide_approval")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["decision"] as? String, "approve",
                "approve posts decision=approve")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["provider"] as? String, "codex",
                "approve posts provider=codex")

    // 4) `deny`
    MockSSEURLProtocol.reset([configAck("Denied tool request on Codex.")])
    let denyOut = await belt.execute(
        name: "agent_terminal_action",
        arguments: ["action": "deny", "agent": "codex"]
    )
    expectEqual(denyOut.claimsLens, false, "deny returns conversational receipt")
    expect(denyOut.output.contains("Denied"), "deny output confirms denial")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["decision"] as? String, "deny",
                "deny posts decision=deny")

    // 5) `cancel_run`
    MockSSEURLProtocol.reset([configAck("Cancelled active run on Codex.")])
    let cancelOut = await belt.execute(
        name: "agent_terminal_action",
        arguments: ["action": "cancel_run", "agent": "codex"]
    )
    expectEqual(cancelOut.claimsLens, false, "cancel_run returns conversational receipt")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["action"] as? String, "cancel_run",
                "cancel_run posts action=cancel_run")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["provider"] as? String, "codex",
                "cancel_run posts provider=codex")

    // 6) `new_session`
    MockSSEURLProtocol.reset([configAck("Started new Claude Code session.")])
    let newSessOut = await belt.execute(
        name: "agent_terminal_action",
        arguments: ["action": "new_session", "agent": "claude"]
    )
    expectEqual(newSessOut.claimsLens, false, "new_session returns conversational receipt")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["action"] as? String, "new_session",
                "new_session posts action=new_session")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["lane"] as? String, "claude",
                "new_session posts lane=claude")

    // 7) `set_model`
    MockSSEURLProtocol.reset([configAck("Switched Claude Code to claude-sonnet-4-6.")])
    let setModelOut = await belt.execute(
        name: "agent_terminal_action",
        arguments: ["action": "set_model", "agent": "claude", "model": "claude-sonnet-4-6"]
    )
    expectEqual(setModelOut.claimsLens, false, "set_model returns conversational receipt")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["action"] as? String, "set_model",
                "set_model posts action=set_model")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["provider"] as? String, "claude",
                "set_model posts provider=claude")
    expectEqual(MockSSEURLProtocol.recordedBodies().first?["model"] as? String, "claude-sonnet-4-6",
                "set_model posts model=claude-sonnet-4-6")

    // System prompt check for Mac-pinned Messages & Agent Terminal guidance
    let sys = OnPhoneAI.assistantSystem(now: Date(), area: "Chicago, United States", snippets: nil)
    expect(
        sys.contains("agent_terminal_action") && sys.contains("Mac mini"),
        "OnPhoneAI.assistantSystem documents Mac mini Messages & Agent Terminal tools"
    )
}

func runEntry() {
    let sema = DispatchSemaphore(value: 0)
    Task { @MainActor in
        let (brain, link) = makeTestBrain()
        await testStripPoliteLeadInAndDeterministicRouting(brain: brain, link: link)
        testToolBeltSchemas(brain: brain)
        await testDirectToolExecutionAndHandoff(brain: brain, link: link)
        await testGeminiThoughtSignatureAndToolStreaming(brain: brain, link: link)
        await testAlwaysOnMacHandoffMessagesAndTerminal(brain: brain, link: link)
        print("PASS: all \(passedAssertions) assistant omnifunction assertions succeeded.")
        sema.signal()
    }
    while sema.wait(timeout: .now()) == .timedOut {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
}
#endif

runEntry()
