import Foundation

/// Snapshot the engine publishes (services/status.py). Decoded defensively
/// FOR REAL: every field is decodeIfPresent with a default, so a missing or
/// renamed key degrades one pill instead of blanking the whole view. (The
/// synthesized Decodable init required every key — the doc comment claimed
/// defensiveness the code did not have.)
struct EngineStatus: Decodable {
    var at: Double = 0
    var glasses = "disconnected"
    var device = ""
    var mic = "off"
    var notifications = "off"
    var stt = "off"
    var llm = "off"
    var debate = "off"
    var lastTranscript = ""
    var lastReply = ""
    var lastCard = ""
    var counts: Counts = .init()

    struct Counts: Decodable {
        var utterances = 0
        var cards = 0

        enum CodingKeys: String, CodingKey { case utterances, cards }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            utterances = (try? c.decodeIfPresent(Int.self, forKey: .utterances)) ?? 0
            cards = (try? c.decodeIfPresent(Int.self, forKey: .cards)) ?? 0
        }
    }

    enum CodingKeys: String, CodingKey {
        case at, glasses, device, mic, notifications, stt, llm, debate
        case lastTranscript = "last_transcript"
        case lastReply = "last_reply"
        case lastCard = "last_card"
        case counts
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func s(_ key: CodingKeys, _ fallback: String) -> String {
            (try? c.decodeIfPresent(String.self, forKey: key)) ?? fallback
        }
        at = (try? c.decodeIfPresent(Double.self, forKey: .at)) ?? 0
        glasses = s(.glasses, "disconnected")
        device = s(.device, "")
        mic = s(.mic, "off")
        notifications = s(.notifications, "off")
        stt = s(.stt, "off")
        llm = s(.llm, "off")
        debate = s(.debate, "off")
        lastTranscript = s(.lastTranscript, "")
        lastReply = s(.lastReply, "")
        lastCard = s(.lastCard, "")
        counts = (try? c.decodeIfPresent(Counts.self, forKey: .counts)) ?? .init()
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
