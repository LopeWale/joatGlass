import Foundation

enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let bool = try? value.decode(Bool.self) { self = .bool(bool) }
        else if let number = try? value.decode(Double.self) { self = .number(number) }
        else if let string = try? value.decode(String.self) { self = .string(string) }
        else if let array = try? value.decode([JSONValue].self) { self = .array(array) }
        else if let object = try? value.decode([String: JSONValue].self) { self = .object(object) }
        else { throw DecodingError.dataCorruptedError(in: value, debugDescription: "invalid JSON") }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .null: try value.encodeNil()
        case .bool(let bool): try value.encode(bool)
        case .number(let number): try value.encode(number)
        case .string(let string): try value.encode(string)
        case .array(let array): try value.encode(array)
        case .object(let object): try value.encode(object)
        }
    }
}

struct BridgeRequest: Codable, Sendable {
    let version: Int
    let requestID: String
    let method: String
    let deadlineUnixMS: Int64
    let attemptID: String?
    let payloadDigest: String?
    let body: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case version, method, body
        case requestID = "requestId"
        case deadlineUnixMS = "deadlineUnixMs"
        case attemptID = "attemptId"
        case payloadDigest
    }
}

struct BridgeTypedError: Codable, Equatable, Sendable {
    let code: String
    let message: String
    let retryable: Bool
}

struct BridgeResponse: Codable, Sendable {
    let version: Int
    let requestID: String
    let storeInstanceID: String
    let storeGeneration: UInt64
    let authorization: EventKitAuthorizationSnapshot
    let commitPhase: String
    let result: JSONValue?
    let error: BridgeTypedError?

    enum CodingKeys: String, CodingKey {
        case version, authorization, result, error
        case requestID = "requestId"
        case storeInstanceID = "storeInstanceId"
        case storeGeneration, commitPhase
    }
}

enum BridgeContract {
    static let version = 1
    static let maximumFrameBytes = 1_048_576

    static func encode<T: Encodable>(_ value: T) throws -> JSONValue {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(value)
        return try JSONDecoder().decode(JSONValue.self, from: data)
    }

    static func decode<T: Decodable>(
        _ type: T.Type,
        from body: [String: JSONValue]
    ) throws -> T {
        let data = try JSONEncoder().encode(JSONValue.object(body))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }

    /// Canonical write-body encoding shared with Python's
    /// json.dumps(..., ensure_ascii=False, separators=(",", ":"), sort_keys=True).
    static func canonicalJSONData<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let encoded = try encoder.encode(value)
        let object = try JSONSerialization.jsonObject(with: encoded)
        return try JSONSerialization.data(
            withJSONObject: object,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
    }
}
