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
        /// Claude's own failure, already said for the wearer.
        case claude(String)
    }

    let config: Config?
    /// Claude, asked directly when the build carries a key (the wearer's
    /// choice, 2026-10-23); the Mac only when it does not.
    let claude = ClaudeLink()

    init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("mac-link.json")
        let candidates = [docs, Bundle.main.url(forResource: "mac-link", withExtension: "json")].compactMap { $0 }
        config = candidates.lazy.compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }.first
        Log.add(config == nil ? "mac link: not configured" : "mac link: \(URL(string: config!.url)?.host ?? "?")")
    }

    var configured: Bool { claude.configured || config != nil }

    private lazy var session: URLSession = {
        let c = URLSessionConfiguration.default
        // agent_endpoint REQUEST_TIMEOUT_S is 90 s; a dead route fails fast anyway.
        c.timeoutIntervalForRequest = 95
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    /// Ask the Mac's assistant. `turns` are the conversation so far
    /// (role "user" / "assistant"), the newest user turn last. `raw` asks
    /// for the whole answer with its lines (a note's summary) instead of
    /// lens-sized text.
    func ask(_ turns: [[String: String]], raw: Bool = false,
             completion: @escaping (Result<String, Failure>) -> Void) {
        if claude.configured, !raw {
            claude.ask(turns, completion: completion)
            return
        }
        guard let config, let url = URL(string: config.url) else { completion(.failure(.notConfigured)); return }
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

    /// A one-shot task: instructions, then the material (a transcript).
    func complete(instructions: String, text: String, completion: @escaping (Result<String, Failure>) -> Void) {
        if claude.configured {
            claude.complete(instructions: instructions, text: text, completion: completion)
            return
        }
        ask([["role": "user", "text": "\(instructions)\n\n\(text)"]], raw: true, completion: completion)
    }

    /// What to tell the wearer when the Mac did not answer.
    static func explain(_ failure: Failure) -> String {
        switch failure {
        case .notConfigured: return "This phone has no link to the Mac set up yet."
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
