import Foundation
import XCTest
@testable import JOAT

final class MessagesAccessStatusTests: XCTestCase {
    func testReadableDatabaseIsGrantedWithoutReadingMessageContent() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let database = root.appendingPathComponent("chat.db")
        try Data("not message content".utf8).write(to: database)

        XCTAssertEqual(MessagesAccessProbe(databaseURL: database).state(), .granted)
    }

    func testMissingDatabaseIsNotReportedAsPermissionGranted() {
        let database = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathComponent("chat.db")

        XCTAssertEqual(MessagesAccessProbe(databaseURL: database).state(), .databaseMissing)
    }

    func testPermissionFailureMapsToBlockedRecoveryState() {
        let denied = NSError(
            domain: NSCocoaErrorDomain,
            code: CocoaError.fileReadNoPermission.rawValue
        )

        XCTAssertEqual(MessagesAccessProbe.state(for: denied), .blocked)
    }
}
