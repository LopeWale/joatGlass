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
    var openClaw: OpenClaw = .init()
    var agentTerminal: AgentTerminal = .init()
    var a2aConnections: [String: A2AConnection] = [:]
    var brainstorm: Brainstorm = .init()
    var personalData: PersonalData = .init()
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

    struct OpenClaw: Decodable {
        var state = "not_configured"
        var endpointHost = ""
        var scopes: [String] = []
        var reason = "endpoint_missing"

        enum CodingKeys: String, CodingKey {
            case state, scopes, reason
            case endpointHost = "endpoint_host"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "not_configured"
            endpointHost = (try? c.decodeIfPresent(String.self, forKey: .endpointHost)) ?? ""
            scopes = (try? c.decodeIfPresent([String].self, forKey: .scopes)) ?? []
            reason = (try? c.decodeIfPresent(String.self, forKey: .reason)) ?? "endpoint_missing"
        }
    }

    struct A2AConnection: Decodable {
        let state: String
        let reason: String
    }

    struct AgentTerminal: Decodable {
        var state = "unavailable"
        var reason = "gateway_not_ready"

        enum CodingKeys: String, CodingKey { case state, reason }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "unavailable"
            reason = (try? c.decodeIfPresent(String.self, forKey: .reason)) ?? "gateway_not_ready"
        }
    }

    struct Brainstorm: Decodable {
        var state = "unavailable"
        var seatCount = 0
        var phase = "idle"

        enum CodingKeys: String, CodingKey {
            case state, phase
            case seatCount = "seat_count"
        }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "unavailable"
            seatCount = (try? c.decodeIfPresent(Int.self, forKey: .seatCount)) ?? 0
            phase = (try? c.decodeIfPresent(String.self, forKey: .phase)) ?? "idle"
        }
    }

    /// The engine's fixed-state proof of access to the app-owned EventKit
    /// bridge. This is deliberately separate from the Mac app's TCC snapshot.
    struct PersonalData: Decodable {
        var state = "checking"

        enum CodingKeys: String, CodingKey { case state }

        init() {}

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "checking"
        }
    }

    enum CodingKeys: String, CodingKey {
        case at, glasses, device, mic, notifications, stt, llm, debate, brainstorm
        case openClaw = "openclaw"
        case agentTerminal = "agent_terminal"
        case a2aConnections = "a2a_connections"
        case personalData = "personal_data"
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
        openClaw = (try? c.decodeIfPresent(OpenClaw.self, forKey: .openClaw)) ?? .init()
        agentTerminal = (try? c.decodeIfPresent(AgentTerminal.self, forKey: .agentTerminal)) ?? .init()
        a2aConnections = (try? c.decodeIfPresent([String: A2AConnection].self, forKey: .a2aConnections)) ?? [:]
        brainstorm = (try? c.decodeIfPresent(Brainstorm.self, forKey: .brainstorm)) ?? .init()
        personalData = (try? c.decodeIfPresent(PersonalData.self, forKey: .personalData)) ?? .init()
        lastTranscript = s(.lastTranscript, "")
        lastReply = s(.lastReply, "")
        lastCard = s(.lastCard, "")
        counts = (try? c.decodeIfPresent(Counts.self, forKey: .counts)) ?? .init()
    }

    /// Stale = the engine stopped writing (crashed, or never started).
    var isFresh: Bool {
        let age = Date().timeIntervalSince1970 - at
        return age >= 0 && age < 15
    }

    static let url = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/status.json")

    static func load() -> EngineStatus? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(EngineStatus.self, from: data)
    }
}
