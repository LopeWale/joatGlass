import Foundation

/// The phone asking Claude directly (the wearer's choice, 2026-10-23): note
/// summaries, titles, a note's tasks and everyday questions work with the
/// Mac off. Anthropic's Messages API with the key build_ios.sh copies from
/// the repo's ignored .env into Generated/claude.json (ignored by git); the
/// key is inside this lab build, as the Mac link's token is.
///
/// Models (platform.claude.com, 2026-10-23): Haiku 5.5 answers questions
/// (fast, $0.10 / $0.50 per million tokens, web search for anything
/// current); Sonnet 5.5 reads notes ($2 / $10), where the quality of the
/// summary is the point.
final class ClaudeLink {
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

    let config: Config?
    /// Where the wearer is, roughly: "Oklahoma City, United States", the
    /// city and the country code, for the answer and for web search.
    var area: (() async -> (context: String, city: String?, country: String?)?)?

    convenience init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("claude.json")
        let candidates = [docs, Bundle.main.url(forResource: "claude", withExtension: "json")].compactMap { $0 }
        self.init(config: candidates.lazy.compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }
            .first { !$0.key.isEmpty })
    }

    init(config: Config?) {
        self.config = config
        Log.add(config == nil ? "claude: not configured" : "claude: \(config?.chatModel ?? Self.chatModel) / \(config?.noteModel ?? Self.noteModel)")
    }

    var configured: Bool { config != nil }

    private lazy var session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 90
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

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
            self.send(model: self.config?.chatModel ?? Self.chatModel, system: system, messages: messages,
                      maxTokens: 600, tools: [search], thinking: false) { result in
                completion(result.map { Self.lensText($0) })
            }
        }
    }

    /// A one-shot task on a note: the instructions, then the material.
    func complete(instructions: String, text: String, completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        send(model: config?.noteModel ?? Self.noteModel, system: instructions,
             messages: [["role": "user", "content": text]], maxTokens: 2000, tools: nil, thinking: false,
             completion: completion)
    }

    static let handwritingSystem = """
        You transcribe a handwritten notebook page. Write out exactly the words written on it, in reading order, \
        keeping the writer's spelling. Put each sentence, list item or heading on its own line, and join the lines \
        of a sentence the writer wrapped onto the next line. Where there is a sketch or diagram instead of \
        words, write [sketch: a few words saying what it shows]. If the page has no words at all, reply with the single \
        word NONE. No commentary, no markdown.
        """

    /// A notebook page (Notebook.swift) as PNGs from top to bottom: its
    /// handwriting as text. The images go before the text (vision docs).
    func readHandwriting(_ pngs: [Data], completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        var content: [[String: Any]] = pngs.map {
            ["type": "image", "source": ["type": "base64", "media_type": "image/png", "data": $0.base64EncodedString()]]
        }
        content.append(["type": "text", "text": pngs.count == 1
            ? "This image is one handwritten page."
            : "These \(pngs.count) images are one handwritten page, from top to bottom."])
        send(model: config?.noteModel ?? Self.noteModel, system: Self.handwritingSystem,
             messages: [["role": "user", "content": content]], maxTokens: 4000, tools: nil, thinking: false,
             completion: completion)
    }

    /// An AI cue during a note (Cues.swift): Haiku, one short line. The
    /// system blocks carry the prep material, cached across the note.
    func cue(system: [[String: Any]], text: String, completion: @escaping (Result<String, MacLink.Failure>) -> Void) {
        // Haiku 5.5 thinks first unless told not to, and 160 tokens of
        // thinking left no cue at all (2026-10-29: 3 of 5 replies empty).
        send(model: config?.chatModel ?? Self.chatModel, system: system,
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
        guard let config else { completion(.failure(.notConfigured)); return }
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
