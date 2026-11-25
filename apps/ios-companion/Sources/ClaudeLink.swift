import Foundation

/// The phone asking Claude directly (the wearer's choice, 2026-10-23): note
/// summaries, titles, a note's tasks and everyday questions work with the
/// Mac off. Anthropic's Messages API with Keychain (`com.joat.brain.ai` /
/// `"claude"`), `Documents/claude.json`, or `Bundle.main/claude.json`.
///
/// Models (platform.claude.com, 2026-10-23): Haiku 5.5 answers questions
/// (fast, $0.10 / $0.50 per million tokens, web search for anything
/// current); Sonnet 5.5 reads notes ($2 / $10), where the quality of the
/// summary is the point.
final class ClaudeLink: @unchecked Sendable {
    struct Config: Decodable {
        let key: String
        let chatModel: String?
        let noteModel: String?
    }

    static let endpoint = URL(string: "https://api.anthropic.com/v1/messages")!
    static let chatModel = "claude-haiku-5-5"
    static let noteModel = "claude-sonnet-5-5"
    /// agent_endpoint.py MAX_LENS_CHARS
    static let maxLensChars = 600
    static let deltaThrottleSeconds: TimeInterval = 0.12

    private let explicitConfig: Config?
    private let fileConfig: Config?
    private let usesDynamicKeychain: Bool

    var config: Config? { activeConfig }

    private var activeConfig: Config? {
        if !usesDynamicKeychain { return explicitConfig }
        if let keychainKey = AIKeychain.load(account: "claude") {
            return Config(key: keychainKey, chatModel: fileConfig?.chatModel, noteModel: fileConfig?.noteModel)
        }
        return fileConfig
    }

    /// Where the wearer is, roughly: "Chicago, United States", the
    /// city and the country code, for the answer and for web search.
    var area: (() async -> (context: String, city: String?, country: String?)?)?

    convenience init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("claude.json")
        let candidates = [docs, Bundle.main.url(forResource: "claude", withExtension: "json")].compactMap { $0 }
        let loaded = candidates.lazy
            .compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }
            .map { Config(key: AIKeychain.normalizeKey($0.key), chatModel: $0.chatModel, noteModel: $0.noteModel) }
            .first { !$0.key.isEmpty }
        self.init(fileConfig: loaded, session: nil)
    }

    init(config: Config?, session: URLSession? = nil) {
        let normalized = config.flatMap { cfg -> Config? in
            let k = AIKeychain.normalizeKey(cfg.key)
            return k.isEmpty ? nil : Config(key: k, chatModel: cfg.chatModel, noteModel: cfg.noteModel)
        }
        self.explicitConfig = normalized
        self.fileConfig = nil
        self.usesDynamicKeychain = false
        self.session = session ?? Self.makeDefaultSession()
        Log.add(normalized == nil ? "claude: not configured" : "claude: \(normalized?.chatModel ?? Self.chatModel) / \(normalized?.noteModel ?? Self.noteModel)")
    }

    private init(fileConfig: Config?, session: URLSession?) {
        self.explicitConfig = nil
        self.fileConfig = fileConfig
        self.usesDynamicKeychain = true
        self.session = session ?? Self.makeDefaultSession()
        let initial = AIKeychain.load(account: "claude").map {
            Config(key: $0, chatModel: fileConfig?.chatModel, noteModel: fileConfig?.noteModel)
        } ?? fileConfig
        Log.add(initial == nil ? "claude: not configured" : "claude: \(initial?.chatModel ?? Self.chatModel) / \(initial?.noteModel ?? Self.noteModel)")
    }

    var configured: Bool { activeConfig != nil }

    static var isCurrentlyConfigured: Bool {
        if AIKeychain.load(account: "claude") != nil { return true }
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("claude.json")
        let candidates = [docs, Bundle.main.url(forResource: "claude", withExtension: "json")].compactMap { $0 }
        return candidates.lazy
            .compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }
            .contains { !AIKeychain.normalizeKey($0.key).isEmpty }
    }

    private let session: URLSession

    private static func makeDefaultSession() -> URLSession {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 90
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }

    /// SSE streaming chat (`"stream": true`, `content_block_delta` `text_delta`),
    /// emitting cumulative `Self.lensText` updates throttled at ~120 ms.
    func stream(
        turns: [[String: String]],
        system: String? = nil,
        onDelta: @escaping @MainActor (String) -> Void
    ) async -> Result<String, MacLink.Failure> {
        let resolvedSystem: String
        if let system {
            resolvedSystem = system
        } else {
            let area = await self.area?()
            var s = Self.chatSystem(now: Date(), area: area?.context)
            if area == nil { s += " The phone has no position for the wearer right now." }
            resolvedSystem = s
        }
        return await streamOnce(turns: turns, system: resolvedSystem, thinking: false, onDelta: onDelta)
    }

    func stream(
        _ turns: [[String: String]],
        system: String? = nil,
        onDelta: @escaping @MainActor (String) -> Void,
        completion: @escaping @MainActor (Result<String, MacLink.Failure>) -> Void
    ) {
        Task { @MainActor in
            let res = await self.stream(turns: turns, system: system, onDelta: onDelta)
            completion(res)
        }
    }

    private func streamOnce(
        turns: [[String: String]],
        system: String,
        thinking: Bool,
        onDelta: @escaping @MainActor (String) -> Void
    ) async -> Result<String, MacLink.Failure> {
        guard let config = activeConfig else { return .failure(.notConfigured) }
        let messages = Self.messages(turns)
        guard !messages.isEmpty else { return .failure(.empty) }
        let model = config.chatModel ?? Self.chatModel

        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.setValue("text/event-stream", forHTTPHeaderField: "accept")
        request.setValue(config.key, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")

        var body: [String: Any] = [
            "model": model,
            "max_tokens": 600,
            "system": system,
            "messages": messages,
            "stream": true,
        ]
        if !thinking { body["thinking"] = Self.noThinking(model) }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let started = Date()
        do {
            let (bytes, response) = try await session.bytes(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else {
                var errBytes = Data()
                for try await b in bytes {
                    errBytes.append(b)
                    if errBytes.count >= 4096 { break }
                }
                let doc = try? JSONSerialization.jsonObject(with: errBytes) as? [String: Any]
                let kind = ((doc?["error"] as? [String: Any])?["type"] as? String) ?? "?"
                let said = ((doc?["error"] as? [String: Any])?["message"] as? String) ?? ""
                let ms = Int(Date().timeIntervalSince(started) * 1000)
                Log.add("claude stream refused: HTTP \(status) \(kind) after \(ms) ms: \(said.prefix(160))")
                if status == 400, !thinking, said.lowercased().contains("thinking") {
                    return await streamOnce(turns: turns, system: system, thinking: true, onDelta: onDelta)
                }
                switch status {
                case 401, 403: return .failure(.claude("Claude refused this phone's key."))
                case 429, 529: return .failure(.claude("Claude is busy right now. Try again in a moment."))
                default: return .failure(.claude("Claude could not answer (error \(status))."))
                }
            }

            var accumulated = ""
            var lastEmitted = ""
            var lastEmitAt = Date.distantPast

            for try await rawLine in bytes.lines {
                let line = rawLine.trimmingCharacters(in: CharacterSet(charactersIn: "\r "))
                guard line.hasPrefix("data:") else { continue }
                let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                if payload.isEmpty || payload == "[DONE]" { continue }
                guard let data = payload.data(using: .utf8),
                      let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }

                let eventType = doc["type"] as? String
                if eventType == "content_block_delta",
                   let delta = doc["delta"] as? [String: Any],
                   delta["type"] as? String == "text_delta",
                   let piece = delta["text"] as? String, !piece.isEmpty {
                    accumulated += piece
                    let clean = Self.lensText(accumulated)
                    guard !clean.isEmpty, clean != lastEmitted else { continue }
                    let now = Date()
                    if lastEmitAt == .distantPast || now.timeIntervalSince(lastEmitAt) >= Self.deltaThrottleSeconds {
                        lastEmitted = clean
                        lastEmitAt = now
                        await onDelta(clean)
                    }
                } else if eventType == "error", accumulated.isEmpty {
                    let msg = ((doc["error"] as? [String: Any])?["message"] as? String) ?? ""
                    Log.add("claude stream error event: \(msg.prefix(160))")
                    return .failure(.claude("Claude could not finish answering."))
                }
            }

            let final = Self.lensText(accumulated)
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            Log.add("claude \(model) streamed in \(ms) ms (\(final.count) chars)")
            if final.isEmpty { return .failure(.empty) }
            if final != lastEmitted { await onDelta(final) }
            return .success(final)
        } catch {
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            Log.add("claude stream unreachable after \(ms) ms: \((error as NSError).code)")
            return .failure(.claude("I can't reach Claude right now. Is the phone online?"))
        }
    }

    struct ToolUseItem {
        let id: String
        let name: String
        let args: [String: Any]
    }

    /// Stateful SSE event parser for Anthropic `/v1/messages` supporting interleaved
    /// `text_delta` and `tool_use` + `input_json_delta` content blocks.
    struct SSETurnAccumulator {
        private struct BlockState {
            var type: String
            var text: String = ""
            var id: String = ""
            var name: String = ""
            var initialInput: [String: Any] = [:]
            var partialJSON: String = ""
            var thinking: String = ""
            var signature: String = ""
        }

        private var blocksByIndex: [Int: BlockState] = [:]
        private var orderedIndices: [Int] = []
        private(set) var accumulatedText: String = ""
        private(set) var hasSeenToolUse: Bool = false
        private(set) var streamErrorMessage: String?

        @discardableResult
        mutating func ingestSSELine(_ rawLine: String) -> String? {
            let line = rawLine.trimmingCharacters(in: CharacterSet(charactersIn: "\r "))
            guard line.hasPrefix("data:") else { return nil }
            let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            if payload.isEmpty || payload == "[DONE]" { return nil }
            guard let data = payload.data(using: .utf8),
                  let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return nil
            }
            return ingestEvent(doc)
        }

        @discardableResult
        mutating func ingestEvent(_ doc: [String: Any]) -> String? {
            guard let eventType = doc["type"] as? String else { return nil }
            switch eventType {
            case "content_block_start":
                guard let index = (doc["index"] as? NSNumber)?.intValue,
                      let block = doc["content_block"] as? [String: Any],
                      let bType = block["type"] as? String else { return nil }
                if blocksByIndex[index] == nil {
                    orderedIndices.append(index)
                }
                var state = BlockState(type: bType)
                if bType == "tool_use" {
                    hasSeenToolUse = true
                    state.id = (block["id"] as? String) ?? "toolu_\(index)"
                    state.name = (block["name"] as? String) ?? ""
                    state.initialInput = (block["input"] as? [String: Any]) ?? [:]
                } else if bType == "text" {
                    if let initialText = block["text"] as? String, !initialText.isEmpty {
                        state.text = initialText
                        blocksByIndex[index] = state
                        if !hasSeenToolUse {
                            accumulatedText += initialText
                            return initialText
                        }
                        return nil
                    }
                } else if bType == "thinking" {
                    state.thinking = (block["thinking"] as? String) ?? ""
                    state.signature = (block["signature"] as? String) ?? ""
                }
                blocksByIndex[index] = state
                return nil

            case "content_block_delta":
                let index = (doc["index"] as? NSNumber)?.intValue ?? 0
                guard let delta = doc["delta"] as? [String: Any],
                      let dType = delta["type"] as? String else { return nil }
                if blocksByIndex[index] == nil {
                    orderedIndices.append(index)
                    blocksByIndex[index] = BlockState(type: dType == "input_json_delta" ? "tool_use" : "text")
                }
                switch dType {
                case "text_delta":
                    guard let piece = delta["text"] as? String, !piece.isEmpty else { return nil }
                    blocksByIndex[index]?.text += piece
                    guard !hasSeenToolUse else { return nil }
                    accumulatedText += piece
                    return piece
                case "input_json_delta":
                    hasSeenToolUse = true
                    if let part = delta["partial_json"] as? String {
                        blocksByIndex[index]?.partialJSON += part
                    }
                    return nil
                case "thinking_delta":
                    if let t = delta["thinking"] as? String {
                        blocksByIndex[index]?.thinking += t
                    }
                    return nil
                case "signature_delta":
                    if let sig = delta["signature"] as? String {
                        blocksByIndex[index]?.signature += sig
                    }
                    return nil
                default:
                    return nil
                }

            case "error":
                streamErrorMessage = ((doc["error"] as? [String: Any])?["message"] as? String)
                    ?? "Claude stream error"
                return nil

            default:
                return nil
            }
        }

        private static func decodeArgs(partialJSON: String, fallback: [String: Any]) -> [String: Any] {
            let trimmed = partialJSON.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty,
               let data = trimmed.data(using: .utf8),
               let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return parsed
            }
            return fallback
        }

        var toolUses: [ToolUseItem] {
            var out: [ToolUseItem] = []
            for idx in orderedIndices.sorted() {
                guard let b = blocksByIndex[idx], b.type == "tool_use", !b.name.isEmpty else { continue }
                let args = Self.decodeArgs(partialJSON: b.partialJSON, fallback: b.initialInput)
                out.append(ToolUseItem(id: b.id, name: b.name, args: args))
            }
            return out
        }

        func assistantContentBlocks() -> [[String: Any]] {
            var out: [[String: Any]] = []
            for idx in orderedIndices.sorted() {
                guard let b = blocksByIndex[idx] else { continue }
                switch b.type {
                case "thinking":
                    if !b.thinking.isEmpty || !b.signature.isEmpty {
                        var d: [String: Any] = ["type": "thinking", "thinking": b.thinking]
                        if !b.signature.isEmpty { d["signature"] = b.signature }
                        out.append(d)
                    }
                case "text":
                    if !b.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        out.append(["type": "text", "text": b.text])
                    }
                case "tool_use":
                    guard !b.name.isEmpty else { continue }
                    let args = Self.decodeArgs(partialJSON: b.partialJSON, fallback: b.initialInput)
                    out.append([
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": args,
                    ])
                default:
                    break
                }
            }
            return out
        }
    }

    static func buildToolResultMessage(
        _ items: [(call: ToolUseItem, outcome: PhoneToolOutcome)]
    ) -> [String: Any] {
        let blocks: [[String: Any]] = items.map { item in
            [
                "type": "tool_result",
                "tool_use_id": item.call.id,
                "content": item.outcome.output,
            ]
        }
        return ["role": "user", "content": blocks]
    }

    /// Multi-round tool-calling SSE chat on `claude-haiku-5-5` (`/v1/messages`).
    func streamWithTools(
        turns: [[String: String]],
        system: String,
        toolBelt: PhoneToolBelt,
        maxToolRounds: Int = 3,
        onDelta: @escaping @MainActor (String) -> Void
    ) async -> Result<AssistantTurnResult, MacLink.Failure> {
        guard let config = activeConfig else { return .failure(.notConfigured) }
        var runningMessages = Self.messages(turns)
        guard !runningMessages.isEmpty else { return .failure(.empty) }
        let model = config.chatModel ?? Self.chatModel
        let toolDefs = await MainActor.run { toolBelt.claudeDefinitions }
        var allowThinkingFallback = false

        for round in 0...maxToolRounds {
            let roundResult = await performToolStreamRound(
                config: config,
                model: model,
                system: system,
                messages: runningMessages,
                toolDefs: toolDefs,
                forceNoTools: round == maxToolRounds,
                thinking: allowThinkingFallback,
                onDelta: onDelta
            )

            let turn: SSETurnAccumulator
            let lastEmitted: String
            let ms: Int

            switch roundResult {
            case .retryWithThinking:
                allowThinkingFallback = true
                let retried = await performToolStreamRound(
                    config: config,
                    model: model,
                    system: system,
                    messages: runningMessages,
                    toolDefs: toolDefs,
                    forceNoTools: round == maxToolRounds,
                    thinking: true,
                    onDelta: onDelta
                )
                switch retried {
                case .completed(let t, let le, let elapsed):
                    turn = t; lastEmitted = le; ms = elapsed
                case .failed(let f):
                    return .failure(f)
                case .retryWithThinking:
                    return .failure(.claude("Claude could not answer."))
                }
            case .completed(let t, let le, let elapsed):
                turn = t; lastEmitted = le; ms = elapsed
            case .failed(let f):
                return .failure(f)
            }

            let calls = turn.toolUses
            if calls.isEmpty || round == maxToolRounds {
                let final = Self.lensText(turn.accumulatedText)
                Log.add("claude \(model) answered in \(ms) ms (round=\(round), \(final.count) chars)")
                if final.isEmpty { return .failure(.empty) }
                if final != lastEmitted { await onDelta(final) }
                return .success(.answer(final))
            }

            Log.add("claude \(model) requested \(calls.count) tool(s) in \(ms) ms (round=\(round))")
            var outcomes: [(call: ToolUseItem, outcome: PhoneToolOutcome)] = []
            for call in calls {
                let outcome = await toolBelt.execute(name: call.name, arguments: call.args)
                outcomes.append((call, outcome))
            }

            if outcomes.contains(where: { $0.outcome.claimsLens }) {
                let firstReceipt = outcomes.first {
                    $0.outcome.claimsLens && ($0.outcome.spokenReceipt?.isEmpty == false)
                }?.outcome.spokenReceipt
                return .success(.claimedLens(spokenReceipt: firstReceipt))
            }

            runningMessages.append(["role": "assistant", "content": turn.assistantContentBlocks()])
            runningMessages.append(Self.buildToolResultMessage(outcomes))
        }

        return .failure(.empty)
    }

    private enum ToolRoundOutcome {
        case completed(SSETurnAccumulator, String, Int)
        case retryWithThinking
        case failed(MacLink.Failure)
    }

    private func performToolStreamRound(
        config: Config,
        model: String,
        system: String,
        messages: [[String: Any]],
        toolDefs: [[String: Any]],
        forceNoTools: Bool,
        thinking: Bool,
        onDelta: @escaping @MainActor (String) -> Void
    ) async -> ToolRoundOutcome {
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.setValue("text/event-stream", forHTTPHeaderField: "accept")
        request.setValue(config.key, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")

        var body: [String: Any] = [
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": messages,
            "stream": true,
        ]
        if !toolDefs.isEmpty {
            body["tools"] = toolDefs
            if forceNoTools {
                body["tool_choice"] = ["type": "none"]
            }
        }
        if !thinking {
            body["thinking"] = Self.noThinking(model)
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let started = Date()
        do {
            let (bytes, response) = try await session.bytes(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else {
                var errBytes = Data()
                for try await b in bytes {
                    errBytes.append(b)
                    if errBytes.count >= 4096 { break }
                }
                let doc = try? JSONSerialization.jsonObject(with: errBytes) as? [String: Any]
                let kind = ((doc?["error"] as? [String: Any])?["type"] as? String) ?? "?"
                let said = ((doc?["error"] as? [String: Any])?["message"] as? String) ?? ""
                let ms = Int(Date().timeIntervalSince(started) * 1000)
                Log.add("claude tool stream refused: HTTP \(status) \(kind) after \(ms) ms: \(said.prefix(160))")
                if status == 400, !thinking, said.lowercased().contains("thinking") {
                    return .retryWithThinking
                }
                switch status {
                case 401, 403: return .failed(.claude("Claude refused this phone's key."))
                case 429, 529: return .failed(.claude("Claude is busy right now. Try again in a moment."))
                default: return .failed(.claude("Claude could not answer (error \(status))."))
                }
            }

            var turn = SSETurnAccumulator()
            var lastEmitted = ""
            var lastEmitAt = Date.distantPast

            for try await rawLine in bytes.lines {
                if let added = turn.ingestSSELine(rawLine), !added.isEmpty {
                    let clean = Self.lensText(turn.accumulatedText)
                    guard !clean.isEmpty, clean != lastEmitted else { continue }
                    let now = Date()
                    if lastEmitAt == .distantPast || now.timeIntervalSince(lastEmitAt) >= Self.deltaThrottleSeconds {
                        lastEmitted = clean
                        lastEmitAt = now
                        await onDelta(clean)
                    }
                }
                if let errMsg = turn.streamErrorMessage, turn.accumulatedText.isEmpty, turn.toolUses.isEmpty {
                    Log.add("claude tool stream error event: \(errMsg.prefix(160))")
                    return .failed(.claude("Claude could not finish answering."))
                }
            }

            let ms = Int(Date().timeIntervalSince(started) * 1000)
            return .completed(turn, lastEmitted, ms)
        } catch {
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            Log.add("claude tool stream unreachable after \(ms) ms: \((error as NSError).code)")
            return .failed(.claude("I can't reach Claude right now. Is the phone online?"))
        }
    }

    /// A question in a conversation: lens-sized plain text, spoken aloud.
    func ask(_ turns: [[String: String]], completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        Task { @MainActor in
            let area = await self.area?()
            var system = Self.chatSystem(now: Date(), area: area?.context)
            if area == nil { system += " The phone has no position for the wearer right now." }
            var search: [String: Any] = ["type": "web_search_20250305", "name": "web_search", "max_uses": 3]
            var where_: [String: Any] = ["type": "approximate", "timezone": TimeZone.current.identifier]
            if let city = area?.city { where_["city"] = city }
            if let country = area?.country, country.count == 2 { where_["country"] = country }
            search["user_location"] = where_
            let messages = Self.messages(turns)
            guard !messages.isEmpty else { completion(.failure(.empty)); return }
            self.send(model: self.activeConfig?.chatModel ?? Self.chatModel, system: system, messages: messages,
                      maxTokens: 600, tools: [search], thinking: false) { result in
                completion(result.map { Self.lensText($0) })
            }
        }
    }

    /// A one-shot task on a note: the instructions, then the material.
    func complete(instructions: String, text: String, completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        send(model: activeConfig?.noteModel ?? Self.noteModel, system: instructions,
             messages: [["role": "user", "content": text]], maxTokens: 2000, tools: nil, thinking: false,
             completion: completion)
    }

    static let handwritingSystem = """
        You transcribe a notebook page: its handwriting, and any screenshots or photos pasted into it (a slide from \
        a course video, say). Write out exactly the words written on it, in reading order, keeping the writer's \
        spelling. Put each sentence, list item or heading on its own line, and join the lines of a sentence the \
        writer wrapped onto the next line. Where there is a sketch or diagram instead of words, write [sketch: a few \
        words saying what it shows]. After the handwriting, write out the words of each pasted image in order, the \
        same way: its title on its own line, then its points, one per line, leaving out app buttons, video player \
        controls and other screen furniture; for a picture, chart or diagram with no words, write [image: a few \
        words saying what it shows]. If there are no words at all, reply with the single word NONE. No commentary, \
        no markdown.
        """

    /// A notebook page (Notebook.swift): the handwriting as PNGs from top to
    /// bottom, then the pasted images as JPEGs; all of it as text. The
    /// images go before the text (vision docs).
    func readHandwriting(_ pngs: [Data], pasted: [Data] = [],
                         completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        func image(_ data: Data, _ type: String) -> [String: Any] {
            ["type": "image", "source": ["type": "base64", "media_type": type, "data": data.base64EncodedString()]]
        }
        var content = pngs.map { image($0, "image/png") } + pasted.map { image($0, "image/jpeg") }
        var about: [String] = []
        switch pngs.count {
        case 0: break
        case 1: about.append("The first image is the page's handwriting.")
        default: about.append("The first \(pngs.count) images are the page's handwriting, from top to bottom.")
        }
        switch pasted.count {
        case 0: break
        case 1: about.append(pngs.isEmpty ? "This image was pasted into the page." : "The last image was pasted into the page.")
        default: about.append(pngs.isEmpty ? "These \(pasted.count) images were pasted into the page, in order."
                                           : "The last \(pasted.count) were pasted into the page, in order.")
        }
        content.append(["type": "text", "text": about.joined(separator: " ")])
        send(model: activeConfig?.noteModel ?? Self.noteModel, system: Self.handwritingSystem,
             messages: [["role": "user", "content": content]], maxTokens: 4000, tools: nil, thinking: false,
             completion: completion)
    }

    /// An AI cue during a note (Cues.swift): Haiku, one short line. The
    /// system blocks carry the prep material, cached across the note.
    func cue(system: [[String: Any]], text: String, completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        // Haiku 5.5 thinks first unless told not to, and 160 tokens of
        // thinking left no cue at all (2026-10-29: 3 of 5 replies empty).
        send(model: activeConfig?.chatModel ?? Self.chatModel, system: system,
             messages: [["role": "user", "content": text]], maxTokens: 160, tools: nil, thinking: false,
             completion: completion)
    }

    static func chatSystem(now: Date, area: String?) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "EEEE, MMMM d, yyyy, h:mm a zzz"
        var s = "You are JOAT, the assistant on the wearer's Even Realities G2 smart glasses. "
            + "Your answer is shown on a small lens and read aloud, so answer in one to three short sentences "
            + "of plain text: no markdown, lists, headings, links or emoji. "
            + "Use web search for anything current: news, weather, scores, prices, opening hours, events. "
            + "It is \(f.string(from: now))."
        if let area {
            s += " The wearer's current position, from the phone's GPS: \(area)."
                + " Use it for anything about where they are or what is nearby."
        }
        return s
    }

    /// The conversation as Messages API turns: user/assistant alternating,
    /// starting with the user, the newest user turn last.
    static func messages(_ turns: [[String: String]]) -> [[String: Any]] {
        var out: [[String: Any]] = []
        for t in turns {
            let role = t["role"] == "assistant" ? "assistant" : "user"
            let text = (t["text"] ?? t["content"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if out.isEmpty, role == "assistant" { continue }
            if let last = out.last, last["role"] as? String == role {
                out[out.count - 1]["content"] = "\(last["content"] as? String ?? "")\n\(text)"
            } else {
                out.append(["role": role, "content": text])
            }
        }
        while out.last?["role"] as? String == "assistant" { out.removeLast() }
        return out
    }

    /// `system` is a string, or content blocks (for cache_control).
    private func send(model: String, system: Any, messages: [[String: Any]], maxTokens: Int,
                      tools: [[String: Any]]?, thinking: Bool = true, attempt: Int = 0,
                      completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        guard let config = activeConfig else { DispatchQueue.main.async { completion(.failure(.notConfigured)) }; return }
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.setValue(config.key, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        var body: [String: Any] = ["model": model, "max_tokens": maxTokens, "system": system, "messages": messages]
        if let tools { body["tools"] = tools }
        if !thinking { body["thinking"] = Self.noThinking(model) }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let started = Date()
        session.dataTask(with: request) { data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let doc = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                let ms = Int(Date().timeIntervalSince(started) * 1000)
                if let error {
                    Log.add("claude unreachable after \(ms) ms: \((error as NSError).code)")
                    completion(.failure(.claude("I can't reach Claude right now. Is the phone online?")))
                    return
                }
                guard status == 200, let doc else {
                    let kind = ((doc?["error"] as? [String: Any])?["type"] as? String) ?? "?"
                    let said = ((doc?["error"] as? [String: Any])?["message"] as? String) ?? ""
                    Log.add("claude refused: HTTP \(status) \(kind) after \(ms) ms: \(said.prefix(160))")
                    // A model that takes another way of saying "don't think":
                    // ask once more with its default rather than fail.
                    if status == 400, !thinking, said.lowercased().contains("thinking") {
                        self.send(model: model, system: system, messages: messages, maxTokens: maxTokens,
                                  tools: tools, thinking: true, attempt: attempt, completion: completion)
                        return
                    }
                    switch status {
                    case 401, 403: completion(.failure(.claude("Claude refused this phone's key.")))
                    case 429, 529: completion(.failure(.claude("Claude is busy right now. Try again in a moment.")))
                    default: completion(.failure(.claude("Claude could not answer (error \(status)).")))
                    }
                    return
                }
                let content = doc["content"] as? [[String: Any]] ?? []
                // A long server-tool turn can pause; hand it back to finish.
                if doc["stop_reason"] as? String == "pause_turn", attempt < 2 {
                    Log.add("claude paused after \(ms) ms; continuing")
                    self.send(model: model, system: system,
                              messages: messages + [["role": "assistant", "content": content]],
                              maxTokens: maxTokens, tools: tools, thinking: thinking, attempt: attempt + 1,
                              completion: completion)
                    return
                }
                // The answer is the text after the last search result: what
                // comes before it is "I'll look that up", not the answer.
                let lastResult = content.lastIndex { ($0["type"] as? String)?.hasSuffix("_tool_result") == true }
                let answerBlocks = lastResult.map { Array(content[content.index(after: $0)...]) } ?? content
                let text = answerBlocks.compactMap { $0["type"] as? String == "text" ? $0["text"] as? String : nil }
                    .joined().trimmingCharacters(in: .whitespacesAndNewlines)
                if doc["stop_reason"] as? String == "max_tokens" { Log.add("claude \(model) hit max_tokens") }
                let searches = ((doc["usage"] as? [String: Any])?["server_tool_use"] as? [String: Any])?["web_search_requests"] as? Int ?? 0
                Log.add("claude \(model) answered in \(ms) ms (\(text.count) chars, \(searches) searches)")
                completion(text.isEmpty ? .failure(.empty) : .success(text))
            }
        }.resume()
    }

    /// How each model is told to answer without thinking first (API,
    /// 2026-10-30): Haiku 5.5 takes "disabled" and refuses "between_tools";
    /// Sonnet 5.5 refuses "disabled" and takes "between_tools".
    static func noThinking(_ model: String) -> [String: Any] {
        model.lowercased().contains("haiku") ? ["type": "disabled"] : ["type": "between_tools"]
    }

    /// agent_endpoint.lens_text: markdown gone, whitespace collapsed, cut at
    /// a sentence or word boundary.
    static func lensText(_ text: String, maxChars: Int = maxLensChars) -> String {
        var t = text.replacingOccurrences(of: "```[\\s\\S]*?```", with: " ", options: .regularExpression)
        t = t.replacingOccurrences(of: "`", with: "")
        t = t.replacingOccurrences(of: "\\[([^\\]]+)\\]\\([^)]*\\)", with: "$1", options: .regularExpression)
        t = t.replacingOccurrences(of: "(?m)^\\s{0,3}#{1,6}\\s*", with: "", options: .regularExpression)
        t = t.replacingOccurrences(of: "(?m)^\\s*(?:[-*•]|\\d+[.)])\\s+", with: "", options: .regularExpression)
        t = t.replacingOccurrences(of: "(\\*\\*|__)([\\s\\S]*?)\\1", with: "$2", options: .regularExpression)
        t = t.replacingOccurrences(of: "(?<!\\w)[*_](\\S[^*_]*?)[*_](?!\\w)", with: "$1", options: .regularExpression)
        t = t.collapsed
        guard maxChars > 0, t.count > maxChars else { return t }
        var cut = String(t.prefix(maxChars))
        let boundary = [". ", "? ", "! "].compactMap { cut.range(of: $0, options: .backwards)?.lowerBound }.max()
        if let b = boundary, cut.distance(from: cut.startIndex, to: b) >= maxChars / 2 {
            return String(cut[...b])
        }
        if let space = cut.range(of: " ", options: .backwards)?.lowerBound,
           cut.distance(from: cut.startIndex, to: space) >= maxChars / 2 {
            cut = String(cut[..<space])
        }
        while let last = cut.last, " ,;:".contains(last) { cut.removeLast() }
        return cut + "…"
    }
}
