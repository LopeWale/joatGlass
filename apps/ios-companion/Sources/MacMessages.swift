import Foundation
import Combine

/// The phone reading and sending texts through the wearer's home Mac (the
/// wearer's choice, 2026-11-18: the glasses stay on the iPhone alone; the
/// phone's brain talks to the Mac in the background and passes texts both
/// ways). The Mac side is services/messages_endpoint.py, served over the
/// tailnet by Tailscale Serve at https://<mac>.<tailnet>.ts.net/messages.
///
/// Nothing here is personal to one wearer: the address and the bearer token
/// come from the build (scripts/build_ios.sh writes Generated/mac-messages.json
/// from `tailscale status` and the Mac's messages-token; git ignores it).
///
/// The Mac never shows the phone a chat GUID or a number it did not already
/// have: a conversation is an opaque thread id, sent back to reply.
final class MacMessages: ObservableObject {
    struct Config: Decodable {
        /// https://<mac>.<tailnet>.ts.net/messages
        let url: String
        let token: String
    }

    struct Incoming {
        let id: Int
        let thread: String
        /// The group's name, else the contact (or the number).
        let label: String
        /// Who wrote it, in a group.
        let person: String
        let group: Bool
        let replyable: Bool
        let text: String
        let at: Date
        let attachment: Bool
    }

    enum Route {
        case thread(String)
        case to(String)
    }

    struct Failure: Error {
        let text: String
        /// The Mac may already have handed the text to Messages.
        let possiblySent: Bool
    }

    let config: Config?
    var configured: Bool { config != nil }
    var hostLabel: String? { config.flatMap { URL(string: $0.url)?.host } }
    @Published private(set) var reachable = false

    /// A text older than this when the phone catches up (it was asleep, or off
    /// the tailnet) still gets its own card on the glasses; it is only not
    /// read aloud (Messages.present).
    static let staleSeconds: TimeInterval = 600
    private static let afterKey = "joat.macmessages.after.v1"
    private static let longPollSeconds = 20

    private static func makeDefaultSession() -> URLSession {
        let c = URLSessionConfiguration.default
        // The Mac holds an events call open for up to 25 s.
        c.timeoutIntervalForRequest = 40
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }

    private let session: URLSession
    private var poller: Task<Void, Never>?

    convenience init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("mac-messages.json")
        let candidates = [docs, Bundle.main.url(forResource: "mac-messages", withExtension: "json")].compactMap { $0 }
        let loaded = candidates.lazy.compactMap { try? JSONDecoder().decode(Config.self, from: Data(contentsOf: $0)) }.first
        self.init(config: loaded, session: nil)
    }

    init(config: Config?, session: URLSession? = nil) {
        self.config = config
        self.session = session ?? Self.makeDefaultSession()
        Log.add(config == nil ? "mac messages: not configured" : "mac messages: \(URL(string: config!.url)?.host ?? "?")")
    }

    // MARK: - In: long poll

    /// Start listening. `deliver` gets each new text and calls `done` once
    /// the text is on the glasses (or kept off them): the next text, and the
    /// saved position, wait for it. Safe to call again: one poller runs.
    func start(deliver: @escaping @MainActor (Incoming, @escaping () -> Void) -> Void) {
        guard config != nil, poller == nil else { return }
        poller = Task { [weak self] in
            var pause: UInt64 = 5
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    try await self.pollOnce(deliver: deliver)
                    await MainActor.run { if !self.reachable { self.reachable = true } }
                    pause = 5
                } catch is CancellationError {
                    return
                } catch {
                    await MainActor.run { if self.reachable { self.reachable = false } }
                    Log.add("mac messages: \((error as NSError).code), retrying in \(pause) s")
                    try? await Task.sleep(nanoseconds: pause * 1_000_000_000)
                    pause = min(pause * 2, 60)
                }
            }
        }
    }

    func stop() {
        poller?.cancel()
        poller = nil
        DispatchQueue.main.async { self.reachable = false }
    }

    private func pollOnce(deliver: @escaping @MainActor (Incoming, @escaping () -> Void) -> Void) async throws {
        guard let after = UserDefaults.standard.object(forKey: Self.afterKey) as? Int else {
            // First contact: begin from now, never replay the Mac's history.
            let doc = try await get("/v1/events")
            if let mark = doc["after"] as? Int { UserDefaults.standard.set(mark, forKey: Self.afterKey) }
            Log.add("mac messages: listening from \((doc["after"] as? Int) ?? 0)")
            return
        }
        let doc = try await get("/v1/events?after=\(after)&wait=\(Self.longPollSeconds)")
        let texts = (doc["texts"] as? [[String: Any]] ?? []).compactMap(Self.incoming)
        // Every text, however late, goes to the glasses as its own card.
        for text in texts {
            await withCheckedContinuation { (finished: CheckedContinuation<Void, Never>) in
                Task { @MainActor in deliver(text) { finished.resume() } }
            }
        }
        if let mark = doc["after"] as? Int { UserDefaults.standard.set(mark, forKey: Self.afterKey) }
        // The Mac answers at once when it has more, and after the wait when not.
    }

    private static func incoming(_ d: [String: Any]) -> Incoming? {
        guard let id = d["id"] as? Int, let thread = d["thread"] as? String,
              let text = d["text"] as? String else { return nil }
        let label = (d["label"] as? String) ?? ""
        return Incoming(id: id, thread: thread, label: label,
                        person: (d["person"] as? String) ?? label,
                        group: d["group"] as? Bool ?? false,
                        replyable: d["replyable"] as? Bool ?? true,
                        text: text,
                        at: Date(timeIntervalSince1970: d["at"] as? Double ?? Date().timeIntervalSince1970),
                        attachment: d["attachment"] as? Bool ?? false)
    }

    // MARK: - History (the Mac's Messages page: recent conversations, each with its history)

    struct ThreadRow {
        let id: String
        let label: String
        let group: Bool
        let preview: String
        let at: Date
        let unread: Bool
    }

    struct HistoryEntry {
        let person: String
        let fromMe: Bool
        let text: String
        let at: Date
    }

    /// The Mac's recent conversations, newest first (chat.db, all of them,
    /// not only what this phone saw arrive), optionally filtered by `?q=`.
    func threads(limit: Int = 30, query: String? = nil) async throws -> [ThreadRow] {
        let clamped = max(1, min(limit, 50))
        var path = "/v1/threads?limit=\(clamped)"
        if let trimmed = query?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty {
            let clipped = String(trimmed.prefix(120))
            var allowed = CharacterSet.urlQueryAllowed
            allowed.remove(charactersIn: "&=+#")
            if let encoded = clipped.addingPercentEncoding(withAllowedCharacters: allowed) {
                path += "&q=\(encoded)"
            }
        }
        let doc = try await get(path)
        return (doc["threads"] as? [[String: Any]] ?? []).compactMap { d in
            guard let id = d["id"] as? String, let label = d["label"] as? String else { return nil }
            return ThreadRow(id: id, label: label, group: d["group"] as? Bool ?? false,
                             preview: d["preview"] as? String ?? "",
                             at: Date(timeIntervalSince1970: d["at"] as? Double ?? 0),
                             unread: d["unread"] as? Bool ?? false)
        }
    }

    /// One conversation's latest messages, oldest first.
    func history(_ threadId: String, limit: Int = 30) async throws -> [HistoryEntry] {
        let clamped = max(1, min(limit, 50))
        let doc = try await get("/v1/threads/\(threadId)/messages?limit=\(clamped)")
        return (doc["messages"] as? [[String: Any]] ?? []).compactMap { d in
            guard let text = d["text"] as? String else { return nil }
            return HistoryEntry(person: d["person"] as? String ?? "", fromMe: d["from_me"] as? Bool ?? false,
                                text: text, at: Date(timeIntervalSince1970: d["at"] as? Double ?? 0))
        }.sorted { $0.at < $1.at }
    }

    // MARK: - Out

    /// Mark an opaque conversation read on the Mac Mini (`/v1/read`), re-listing
    /// recent threads once if the Mac restarted since issuing `threadId`.
    func markRead(_ threadId: String, completion: ((Bool) -> Void)? = nil) {
        Task {
            let ok = await markReadAsync(threadId)
            if let completion {
                await MainActor.run { completion(ok) }
            }
        }
    }

    @discardableResult
    func markReadAsync(_ threadId: String) async -> Bool {
        await attemptMarkRead(threadId, retry: true)
    }

    private func attemptMarkRead(_ threadId: String, retry: Bool) async -> Bool {
        do {
            _ = try await post("/v1/read", ["thread": threadId])
            return true
        } catch let error as HTTPError {
            if error.status == 404, error.type == "unknown_thread", retry {
                _ = try? await get("/v1/threads?limit=30")
                return await attemptMarkRead(threadId, retry: false)
            }
            return false
        } catch {
            return false
        }
    }

    /// Text a conversation (by its thread id) or a number or email. A thread
    /// the Mac no longer holds (it restarted) is re-announced by asking for
    /// its recent threads, then tried once more.
    func send(_ route: Route, text: String, clientId: String,
              completion: @escaping (Result<Void, Failure>) -> Void) {
        Task {
            let result = await attemptSend(route, text: text, clientId: clientId, retry: true)
            await MainActor.run { completion(result) }
        }
    }

    private func attemptSend(_ route: Route, text: String, clientId: String, retry: Bool) async -> Result<Void, Failure> {
        var body: [String: Any] = ["text": text, "client_id": clientId]
        switch route {
        case .thread(let id): body["thread"] = id
        case .to(let handle): body["to"] = handle
        }
        do {
            _ = try await post("/v1/send", body)
            return .success(())
        } catch let error as HTTPError {
            if error.status == 404, error.type == "unknown_thread", retry {
                _ = try? await get("/v1/threads?limit=30")
                return await attemptSend(route, text: text, clientId: clientId, retry: false)
            }
            return .failure(Self.failure(error))
        } catch {
            let code = (error as NSError).code
            // Once the request left the phone, a lost answer is not a refusal.
            let maybe = [NSURLErrorTimedOut, NSURLErrorNetworkConnectionLost].contains(code)
            Log.add("mac messages send failed: \(code)")
            return .failure(Failure(
                text: maybe ? "The Mac did not answer. The text may have been sent: check Messages."
                            : "I can't reach the Mac. Is it awake, with JOAT running and Tailscale on?",
                possiblySent: maybe))
        }
    }

    private static func failure(_ e: HTTPError) -> Failure {
        switch (e.status, e.type) {
        case (401, _): return Failure(text: "The Mac refused this phone's key. Rebuild JOAT Brain on the Mac.", possiblySent: false)
        case (404, "unknown_thread"): return Failure(text: "I lost that conversation. Open it again from Messages.", possiblySent: false)
        case (400, "not_replyable"): return Failure(text: "That conversation can't be replied to from here.", possiblySent: false)
        case (502, "send_failed"):
            return Failure(text: e.possiblySent ? "Messages may have sent it: check Messages on the Mac."
                                                : "Messages on the Mac did not accept the text.",
                           possiblySent: e.possiblySent)
        case (502, _): return Failure(text: "Messages is not readable on the Mac. Check its Full Disk Access.", possiblySent: false)
        default: return Failure(text: "The Mac could not send it (error \(e.status)).", possiblySent: false)
        }
    }

    // MARK: - HTTP

    private struct HTTPError: Error {
        let status: Int
        let type: String
        let possiblySent: Bool
    }

    private func request(_ path: String, method: String, body: Data? = nil) throws -> URLRequest {
        guard let config, let url = URL(string: config.url + path) else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        return request
    }

    private func run(_ request: URLRequest) async throws -> [String: Any] {
        do {
            let (data, response) = try await session.data(for: request)
            let doc = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else {
                if status == 401 || status >= 500 {
                    await MainActor.run { if self.reachable { self.reachable = false } }
                }
                let err = doc["error"] as? [String: Any]
                throw HTTPError(status: status, type: err?["type"] as? String ?? "",
                                possiblySent: err?["possibly_sent"] as? Bool ?? false)
            }
            await MainActor.run { if !self.reachable { self.reachable = true } }
            return doc
        } catch {
            if !(error is HTTPError), !(error is CancellationError) {
                await MainActor.run { if self.reachable { self.reachable = false } }
            }
            throw error
        }
    }

    private func get(_ path: String) async throws -> [String: Any] {
        try await run(try request(path, method: "GET"))
    }

    private func post(_ path: String, _ body: [String: Any]) async throws -> [String: Any] {
        try await run(try request(path, method: "POST", body: try JSONSerialization.data(withJSONObject: body)))
    }
}
