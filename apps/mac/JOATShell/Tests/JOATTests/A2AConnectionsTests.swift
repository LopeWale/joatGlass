import XCTest
@testable import JOAT

final class A2AConnectionsTests: XCTestCase {
    func testEndpointPreservesRoutingPathAndRejectsCredentials() throws {
        XCTAssertEqual(try A2AConnectionConfiguration.canonicalEndpoint("https://HOST.example:9900/"), "https://host.example:9900/")
        XCTAssertEqual(try A2AConnectionConfiguration.canonicalEndpoint("https://host.example/a%2Fb/"), "https://host.example/a%2Fb/")
        for invalid in ["http://host.example/", "https://user:token@host.example/", "https://host.example/?token=x", "https://host.example/#x", "https://host.example/a b", "https://host.example:0/"] {
            XCTAssertThrowsError(try A2AConnectionConfiguration.canonicalEndpoint(invalid))
        }
    }

    func testNativeBridgeReturnsTokenOnlyForEnabledExactDestination() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("a2a.json")
        let profile = try A2AConnectionProfile(id: "openclaw", label: "OpenClaw", endpoint: "https://host.example:8443/a2a/v1", enabled: true)
        try A2AConnectionConfiguration.save([profile], to: file)
        let store = MemoryA2AStore()
        let credentials = A2ACredentialService(secretStore: store, configURL: file)
        try credentials.storeToken(profileID: profile.id, endpoint: profile.endpoint, token: "test-peer-token")
        let router = BridgeRouter(a2aCredentials: credentials)
        func request(_ endpoint: String) -> BridgeRequest {
            BridgeRequest(version: 1, requestID: UUID().uuidString, method: "a2a.token.load",
                          deadlineUnixMS: Int64(Date().timeIntervalSince1970 * 1000) + 10000,
                          attemptID: nil, payloadDigest: nil,
                          body: ["profile_id": .string(profile.id), "endpoint": .string(endpoint)])
        }
        let result = await router.handle(request(profile.endpoint))
        XCTAssertNil(result.error)
        XCTAssertEqual(result.result, .object(["token": .string("test-peer-token")]))
        let wrong = await router.handle(request("https://other.example:8443/a2a/v1"))
        XCTAssertNil(wrong.error)
        XCTAssertEqual(wrong.result, .null)
        let disabled = try A2AConnectionProfile(id: profile.id, label: profile.label, endpoint: profile.endpoint, enabled: false)
        try A2AConnectionConfiguration.save([disabled], to: file)
        let revoked = await router.handle(request(profile.endpoint))
        XCTAssertEqual(revoked.result, .null)
        XCTAssertFalse(try String(contentsOf: file, encoding: .utf8).contains("test-peer-token"))
    }

    @MainActor
    func testSaveClearsTokenAndExternalEditDoesNotGetOverwritten() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("a2a.json")
        let model = A2AConnectionsModel(url: file, credentials: A2ACredentialService(secretStore: MemoryA2AStore(), configURL: file))
        model.add("openclaw")
        model.drafts[0].token = "test-peer-token"
        XCTAssertTrue(model.canPersist)
        model.save()
        XCTAssertFalse(model.failed)
        XCTAssertEqual(model.drafts[0].token, "")
        XCTAssertFalse(model.canPersist)
        XCTAssertTrue(model.requiresRestart(after: nil))
        try A2AConnectionConfiguration.save([], to: file)
        model.drafts[0].label = "Changed"
        model.save()
        XCTAssertTrue(model.failed)
        XCTAssertEqual(try A2AConnectionConfiguration.load(from: file), [])
    }
}

private final class MemoryA2AStore: A2ASecretStoring, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]
    func load(account: String) throws -> Data? { lock.withLock { values[account] } }
    func save(_ data: Data, account: String) throws { lock.withLock { values[account] = data } }
}
