import XCTest
@testable import JOAT

/// The glasses key moved from pairing.json into the keychain (2026-10-13).
@MainActor
final class PairingKeychainTests: XCTestCase {
    private final class MemorySecrets: PairingSecretStore {
        var value: String?
        var failSave = false
        func read() throws -> String? { value }
        func save(_ token: String) throws {
            if failSave { throw PairingKeychain.KeychainError.status(-25300) }
            value = token
        }
        func delete() throws { value = nil }
    }

    private var directory: URL!
    private var clock = Date(timeIntervalSince1970: 1_000_000)

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("pairing-keychain-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func holder(_ secrets: MemorySecrets) -> PairingKeyHolder {
        PairingKeyHolder(secrets: secrets, directory: directory, now: { [unowned self] in self.clock })
    }

    private func writeLegacy(token: String) throws {
        let data = try JSONSerialization.data(withJSONObject: ["token": token])
        try data.write(to: directory.appendingPathComponent("pairing.json"))
        try Data("{\"expiresAt\": 1}".utf8).write(to: directory.appendingPathComponent("pairing-window.json"))
    }

    private func exists(_ name: String) -> Bool {
        FileManager.default.fileExists(atPath: directory.appendingPathComponent(name).path)
    }

    func testTheFilesKeyMovesIntoTheKeychainAndTheFilesAreRemoved() throws {
        let secrets = MemorySecrets()
        try writeLegacy(token: "phone-key")
        let keys = holder(secrets)
        try keys.migrateLegacyFile()
        XCTAssertEqual(secrets.value, "phone-key")
        XCTAssertFalse(exists("pairing.json"))
        XCTAssertFalse(exists("pairing-window.json"))
        XCTAssertEqual(try keys.state().token, "phone-key")
    }

    func testAFileWrittenAfterTheMoveIsNeverTrusted() throws {
        let secrets = MemorySecrets()
        secrets.value = "phone-key"
        try writeLegacy(token: "planted-key")
        try holder(secrets).migrateLegacyFile()
        XCTAssertEqual(secrets.value, "phone-key")
        XCTAssertFalse(exists("pairing.json"))
    }

    func testTheFileStaysWhenTheKeychainCannotKeepTheKey() throws {
        let secrets = MemorySecrets()
        secrets.failSave = true
        try writeLegacy(token: "phone-key")
        XCTAssertThrowsError(try holder(secrets).migrateLegacyFile())
        XCTAssertTrue(exists("pairing.json"))
    }

    func testPairGlassesForgetsTheKeyAndOpensAShortWindow() throws {
        let secrets = MemorySecrets()
        secrets.value = "old-key"
        let keys = holder(secrets)
        let expires = try keys.openWindow()
        XCTAssertNil(secrets.value)
        XCTAssertEqual(expires, clock.addingTimeInterval(120))
        let open = try keys.state()
        XCTAssertNil(open.token)
        XCTAssertEqual(open.windowExpiresAt, expires)
        clock = clock.addingTimeInterval(121)
        XCTAssertNil(try keys.state().windowExpiresAt)
    }

    func testOnlyAKeyPairedInTheWindowIsKept() throws {
        let secrets = MemorySecrets()
        let keys = holder(secrets)
        XCTAssertThrowsError(try keys.keep("unasked-key"))
        XCTAssertNil(secrets.value)
        _ = try keys.openWindow()
        clock = clock.addingTimeInterval(130)  // within the acknowledgement grace
        try keys.keep("new-phone-key")
        XCTAssertEqual(secrets.value, "new-phone-key")
        XCTAssertNil(keys.windowExpiresAt)
        XCTAssertThrowsError(try keys.keep("second-key"))
        XCTAssertEqual(secrets.value, "new-phone-key")
    }
}
