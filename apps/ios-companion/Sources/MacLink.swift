import Foundation

/// The phone asking the Mac when it can reach it (the wearer's choice,
/// 2026-10-22): the Mac's agent endpoint (services/agent_endpoint.py), the
/// same OpenAI-shaped door Even AI uses, served over the tailnet by
/// Tailscale Serve at https://<mac>.<tailnet>.ts.net/agent. The Mac answers
/// with Claude Code on the wearer's plan; no API key lives on the phone.
///
/// The address and the door's bearer token come from the build
/// (build_ios.sh writes Generated/mac-link.json from `tailscale status` and
/// ~/Library/Application Support/JOAT/agent-token; git ignores it).
final class MacLink {
    struct Config: Decodable {
        let url: String
        let token: String
    }

    enum Failure: Error {
        case notConfigured, unreachable, refused, tooSlow, failed(Int), empty
        /// On-phone AI's own failure message, already phrased for the wearer.
        case claude(String)
    }

    static let deltaThrottleSeconds: TimeInterval = 0.12

    let config: Config?
    let claude = ClaudeLink()
    private let customSession: URLSession?

    init(config explicitConfig: Config? = nil, session customSession: URLSession? = nil) {
        self.customSession = customSession
        if let explicitConfig {
            self.config = explicitConfig
        } else {
            let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("mac-link.json")
            let candidates = [docs, Bundle.main.url(forResource: "mac-link", withExtension: "json")].compactMap { $0 }
            self.config = candidates.lazy.compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }.first
        }
        Log.add(config == nil ? "mac link: not configured" : "mac link: \(URL(string: config!.url)?.host ?? "?")")
    }

    var macConfigured: Bool { config != nil }
    var hostLabel: String? { config.flatMap { URL(string: $0.url)?.host } }
    var configured: Bool { OnPhoneAI.shared.anyConfigured || claude.configured || config != nil }

    private lazy var session: URLSession = {
        if let customSession { return customSession }
        let c = URLSessionConfiguration.default
        // agent_endpoint REQUEST_TIMEOUT_S is 90 s; a dead route fails fast anyway.
        c.timeoutIntervalForRequest = 95
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    /// Streams SSE chunks (`"stream": true`, `choices[0].delta.content`) from the Mac's
    /// `/agent/v1/chat/completions` endpoint, throttling `onDelta` callbacks at ~120 ms.
    func stream(
        _ turns: [[String: String]],
        onDelta: @escaping @MainActor (String) -> Void,
        completion: @escaping @MainActor (Result<String, Failure>) -> Void
    ) {
        guard let config, let url = URL(string: config.url) else {
            Task { @MainActor in completion(.failure(.notConfigured)) }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        let messages = turns.map { ["role": $0["role"] ?? "user", "content": $0["text"] ?? $0["content"] ?? ""] }
        let body: [String: Any] = [
            "model": "joat",
            "stream": true,
            "messages": messages,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        let client = session
        let started = Date()
        Task { @MainActor in
            do {
                let (bytes, response) = try await client.bytes(for: request)
                if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                    let code = http.statusCode
                    let failure: Failure = code == 401 ? .refused : (code == 504 ? .tooSlow : .failed(code))
                    Log.add("mac link stream failed in \(Int(Date().timeIntervalSince(started) * 1000)) ms: HTTP \(code)")
                    completion(.failure(failure))
                    return
                }

                var accumulated = ""
                var lastEmitted = ""
                var lastEmitAt = Date.distantPast

                for try await rawLine in bytes.lines {
                    let line = rawLine.trimmingCharacters(in: CharacterSet(charactersIn: "\r "))
                    guard line.hasPrefix("data:") else { continue }
                    let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                    if payload.isEmpty { continue }
                    if payload == "[DONE]" { break }
                    guard let data = payload.data(using: .utf8),
                          let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let choices = doc["choices"] as? [[String: Any]],
                          let first = choices.first else { continue }

                    let piece = ((first["delta"] as? [String: Any])?["content"] as? String)
                        ?? ((first["message"] as? [String: Any])?["content"] as? String)
                        ?? ""
                    guard !piece.isEmpty else { continue }
                    accumulated += piece

                    let clean = ClaudeLink.lensText(accumulated)
                    guard !clean.isEmpty, clean != lastEmitted else { continue }
                    let now = Date()
                    if lastEmitAt == .distantPast || now.timeIntervalSince(lastEmitAt) >= Self.deltaThrottleSeconds {
                        lastEmitted = clean
                        lastEmitAt = now
                        onDelta(clean)
                    }
                }

                let final = ClaudeLink.lensText(accumulated)
                let ms = Int(Date().timeIntervalSince(started) * 1000)
                Log.add("mac link streamed in \(ms) ms (\(final.count) chars)")
                if final.isEmpty {
                    completion(.failure(.empty))
                } else {
                    if final != lastEmitted { onDelta(final) }
                    completion(.success(final))
                }
            } catch {
                Log.add("mac link stream unreachable: \((error as NSError).code)")
                completion(.failure(.unreachable))
            }
        }
    }

    /// Ask the Mac's assistant (or on-phone AI when configured and `!raw`).
    func ask(_ turns: [[String: String]], raw: Bool = false,
             completion: @escaping (Result<String, Failure>) -> Void) {
        if !raw {
            if claude.configured {
                claude.ask(turns, completion: completion)
                return
            }
            if OnPhoneAI.shared.anyConfigured {
                OnPhoneAI.shared.streamChat(
                    turns: turns,
                    area: nil,
                    macLink: self,
                    search: nil,
                    onDelta: { _ in },
                    completion: { result in completion(result) }
                )
                return
            }
        }
        askMacDirect(turns, raw: raw, completion: completion)
    }

    /// Direct non-streaming call to the Mac's `/agent` endpoint.
    func askMacDirect(_ turns: [[String: String]], raw: Bool = false,
                      completion: @escaping (Result<String, Failure>) -> Void) {
        guard let config, let url = URL(string: config.url) else {
            DispatchQueue.main.async { completion(.failure(.notConfigured)) }
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        var body: [String: Any] = ["model": "joat", "messages": turns.map { ["role": $0["role"] ?? "user", "content": $0["text"] ?? $0["content"] ?? ""] }]
        if raw { body["joat_raw"] = true }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let started = Date()
        session.dataTask(with: request) { data, response, error in
            let result: Result<String, Failure>
            if let error {
                Log.add("mac link unreachable: \((error as NSError).code)")
                result = .failure(.unreachable)
            } else if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                result = .failure(http.statusCode == 401 ? .refused : http.statusCode == 504 ? .tooSlow : .failed(http.statusCode))
            } else if let data,
                      let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let choices = doc["choices"] as? [[String: Any]],
                      let message = choices.first?["message"] as? [String: Any],
                      let content = message["content"] as? String, !content.isEmpty {
                result = .success(content)
            } else {
                result = .failure(.empty)
            }
            Log.add("mac link answered in \(Int(Date().timeIntervalSince(started) * 1000)) ms: \(result.isSuccess ? "ok" : "\(result)")")
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    /// A one-shot task: instructions, then the material (a transcript or note).
    /// Routes through `OnPhoneAI.shared` so summaries, questions, and commitments
    /// work with Gemini, Claude, or the Mac.
    func complete(instructions: String, text: String, completion: @escaping (Result<String, Failure>) -> Void) {
        OnPhoneAI.shared.complete(instructions: instructions, text: text, macLink: self, completion: completion)
    }

    /// What to tell the wearer when the assistant did not answer.
    static func explain(_ failure: Failure) -> String {
        switch failure {
        case .notConfigured: return "Add a Gemini or Claude API key in JOAT Brain › Home, or connect your Mac over Tailscale."
        case .unreachable: return "I can't reach the Mac right now. Is it awake, with JOAT running and Tailscale on?"
        case .refused: return "The Mac refused this phone's key. Rebuild JOAT Brain on the Mac."
        case .tooSlow: return "The Mac took too long to answer."
        case .failed(let code): return "The Mac could not answer (error \(code))."
        case .empty: return "The answer came back empty."
        case .claude(let said): return said
        }
    }
}

private extension Result {
    var isSuccess: Bool { if case .success = self { return true } else { return false } }
}
