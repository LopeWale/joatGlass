import CryptoKit
import Foundation
import XCTest
@testable import JOAT

final class OpenClawCredentialServiceTests: XCTestCase {
    func testIdentityIsStablePerCanonicalGatewayProfile() async throws {
        let store = MemoryOpenClawSecretStore()
        let service = OpenClawCredentialService(secretStore: store)
        let profile = String(repeating: "a", count: 64)

        let first = try await service.identity(profileID: profile)
        let second = try await service.identity(profileID: profile)

        XCTAssertEqual(first, second)
        XCTAssertEqual(first.deviceID.count, 64)
        XCTAssertTrue(first.deviceID.allSatisfy(\.isHexDigit))
        let publicKey = try XCTUnwrap(Data(base64URLUnpadded: first.publicKey))
        XCTAssertEqual(
            first.deviceID,
            SHA256.hash(data: publicKey).map { String(format: "%02x", $0) }.joined()
        )
        XCTAssertEqual(store.accounts.count, 1)
    }

    func testSignatureVerifiesButPrivateKeyNeverLeavesService() async throws {
        let store = MemoryOpenClawSecretStore()
        let service = OpenClawCredentialService(secretStore: store)
        let profile = String(repeating: "b", count: 64)
        let identity = try await service.identity(profileID: profile)

        let signature = try await service.sign(
            profileID: profile,
            payload: "v3|challenge-bound-payload"
        )

        let publicData = try XCTUnwrap(Data(base64URLUnpadded: identity.publicKey))
        let signatureData = try XCTUnwrap(Data(base64URLUnpadded: signature))
        let publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: publicData)
        XCTAssertTrue(
            publicKey.isValidSignature(
                signatureData,
                for: Data("v3|challenge-bound-payload".utf8)
            )
        )
        XCTAssertFalse(String(describing: identity).contains("private"))
    }

    func testDeviceTokenRoundTripsOnlyForBoundReadScope() async throws {
        let store = MemoryOpenClawSecretStore()
        let service = OpenClawCredentialService(secretStore: store)
        let profile = String(repeating: "c", count: 64)
        let identity = try await service.identity(profileID: profile)

        let missing = try await service.loadToken(
            profileID: profile,
            deviceID: identity.deviceID,
            role: "operator"
        )
        XCTAssertNil(missing)
        try await service.storeToken(
            profileID: profile,
            deviceID: identity.deviceID,
            role: "operator",
            token: "paired-device-token",
            scopes: ["operator.read"]
        )

        let record = try await service.loadToken(
            profileID: profile,
            deviceID: identity.deviceID,
            role: "operator"
        )
        XCTAssertEqual(record?.token, "paired-device-token")
        XCTAssertEqual(record?.scopes, ["operator.read"])
    }

    func testRouterExposesOnlyNarrowIdentitySignAndTokenOperations() async throws {
        let service = OpenClawCredentialService(secretStore: MemoryOpenClawSecretStore())
        let router = BridgeRouter(openClawCredentials: service)
        let profile = String(repeating: "d", count: 64)
        let deadline = Int64(Date().timeIntervalSince1970 * 1_000) + 5_000

        let identityResponse = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "identity",
            method: "openclaw.identity.get",
            deadlineUnixMS: deadline,
            attemptID: nil,
            payloadDigest: nil,
            body: ["profile_id": .string(profile)]
        ))
        XCTAssertNil(identityResponse.error)
        XCTAssertEqual(identityResponse.commitPhase, "local_secret_created_or_loaded")
        let identity = try XCTUnwrap(identityResponse.result?.objectValue)
        let deviceID = try XCTUnwrap(identity["device_id"]?.stringValue)
        XCTAssertEqual(deviceID.count, 64)

        let signResponse = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "sign",
            method: "openclaw.identity.sign",
            deadlineUnixMS: deadline,
            attemptID: nil,
            payloadDigest: nil,
            body: [
                "profile_id": .string(profile),
                "payload": .string("v3|challenge"),
            ]
        ))
        XCTAssertNil(signResponse.error)
        XCTAssertNotNil(signResponse.result?.objectValue?["signature"]?.stringValue)

        let storeResponse = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "token-store",
            method: "openclaw.token.store",
            deadlineUnixMS: deadline,
            attemptID: nil,
            payloadDigest: nil,
            body: [
                "profile_id": .string(profile),
                "device_id": .string(deviceID),
                "role": .string("operator"),
                "token": .string("paired-device-token"),
                "scopes": .array([.string("operator.read")]),
            ]
        ))
        XCTAssertNil(storeResponse.error)
        XCTAssertEqual(storeResponse.commitPhase, "local_secret_stored")
        let stored = try XCTUnwrap(storeResponse.result?.objectValue?["stored"])
        XCTAssertEqual(stored, JSONValue.bool(true))

        let loadResponse = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "token-load",
            method: "openclaw.token.load",
            deadlineUnixMS: deadline,
            attemptID: nil,
            payloadDigest: nil,
            body: [
                "profile_id": .string(profile),
                "device_id": .string(deviceID),
                "role": .string("operator"),
            ]
        ))
        XCTAssertNil(loadResponse.error)
        XCTAssertEqual(loadResponse.commitPhase, "local_secret_read")
        let scopes = try XCTUnwrap(loadResponse.result?.objectValue?["scopes"])
        XCTAssertEqual(scopes, JSONValue.array([.string("operator.read")]))
    }
}

private final class MemoryOpenClawSecretStore: OpenClawSecretStoring, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]

    var accounts: [String] {
        lock.withLock { Array(values.keys) }
    }

    func load(account: String) throws -> Data? {
        lock.withLock { values[account] }
    }

    func save(_ data: Data, account: String) throws {
        lock.withLock { values[account] = data }
    }
}

private extension Data {
    init?(base64URLUnpadded value: String) {
        var encoded = value.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
        self.init(base64Encoded: encoded)
    }
}

private extension JSONValue {
    var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }

    var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }
}
