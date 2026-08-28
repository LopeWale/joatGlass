import Foundation
import XCTest
@testable import JOAT

final class PairingWindowStoreTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func testOpenWritesOwnerOnlyExpiringMarkerBeforeRemovingToken() throws {
        let tokenURL = directory.appendingPathComponent("pairing.json")
        try Data(#"{"token":"old"}"#.utf8).write(to: tokenURL)
        let store = PairingWindowStore(
            directory: directory,
            now: { Date(timeIntervalSince1970: 1_000) }
        )

        let expiresAt = try store.open(duration: 120)

        XCTAssertEqual(expiresAt.timeIntervalSince1970, 1_120)
        XCTAssertFalse(FileManager.default.fileExists(atPath: tokenURL.path))
        let markerURL = directory.appendingPathComponent("pairing-window.json")
        let payload = try JSONSerialization.jsonObject(
            with: Data(contentsOf: markerURL)
        ) as? [String: Double]
        XCTAssertEqual(payload?["expiresAt"], 1_120)
        let attributes = try FileManager.default.attributesOfItem(atPath: markerURL.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }

    func testOpenRejectsUnboundedDurationsWithoutChangingFiles() throws {
        let tokenURL = directory.appendingPathComponent("pairing.json")
        try Data(#"{"token":"old"}"#.utf8).write(to: tokenURL)
        let store = PairingWindowStore(directory: directory)

        XCTAssertThrowsError(try store.open(duration: 301))

        XCTAssertTrue(FileManager.default.fileExists(atPath: tokenURL.path))
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: directory.appendingPathComponent("pairing-window.json").path
        ))
    }
}
