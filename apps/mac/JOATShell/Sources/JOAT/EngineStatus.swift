import Foundation

/// Snapshot the engine publishes (services/status.py). Decoded defensively:
/// a field the engine hasn't published yet must not blank the whole view.
struct EngineStatus: Decodable {
    var at: Double = 0
    var glasses = "disconnected"
    var device = ""
    var mic = "off"
    var notifications = "off"
    var stt = "off"
    var llm = "off"
    var debate = "off"
    var session = "idle"
    var lastTranscript = ""
    var lastReply = ""
    var lastCard = ""
    var counts: Counts = .init()

    struct Counts: Decodable {
        var utterances = 0
        var cards = 0
        var debates = 0
    }

    enum CodingKeys: String, CodingKey {
        case at, glasses, device, mic, notifications, stt, llm, debate, session
        case lastTranscript = "last_transcript"
        case lastReply = "last_reply"
        case lastCard = "last_card"
        case counts
    }

    /// Stale = the engine stopped writing (crashed, or never started).
    var isFresh: Bool { Date().timeIntervalSince1970 - at < 15 }

    static let url = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/status.json")

    static func load() -> EngineStatus? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(EngineStatus.self, from: data)
    }
}
