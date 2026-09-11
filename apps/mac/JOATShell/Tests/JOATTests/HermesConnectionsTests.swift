import XCTest
@testable import JOAT

final class HermesConnectionsTests: XCTestCase {
    @MainActor
    func testUnreadableRosterCanBeBackedUpThenReplacedWithoutLosingOriginal() throws {
        try withURL { url in
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let original = Data("{broken but valuable configuration".utf8)
            try original.write(to: url)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            let model = HermesConnectionsModel(url: url)
            XCTAssertTrue(model.loadFailed)
            model.backUpAndReset()
            let backup = try XCTUnwrap(model.recoveryBackupURL)
            XCTAssertEqual(try Data(contentsOf: backup), original)
            XCTAssertFalse(model.loadFailed)
            model.drafts[0].gatewayURL = "wss://repaired.example.ts.net"
            model.save()
            XCTAssertEqual(try OpenClawRuntimeConfiguration.load(from: url)?.gatewayURL, "wss://repaired.example.ts.net")
            XCTAssertEqual(try Data(contentsOf: backup), original)
        }
    }

    @MainActor
    func testResetPreservesExternallyRepairedRosterAndRefusesSymlink() throws {
        try withURL { url in
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let target = url.deletingLastPathComponent().appendingPathComponent("other.json")
            try Data("untouched".utf8).write(to: target)
            try FileManager.default.createSymbolicLink(at: url, withDestinationURL: target)
            let model = HermesConnectionsModel(url: url)
            model.backUpAndReset()
            XCTAssertTrue(model.loadFailed)
            XCTAssertNil(model.recoveryBackupURL)
            XCTAssertEqual(try String(contentsOf: target, encoding: .utf8), "untouched")
            try FileManager.default.removeItem(at: url)
            let external = try OpenClawRuntimeConfiguration(gatewayURL: "wss://external.example.ts.net")
            try external.save(to: url)
            model.backUpAndReset()
            XCTAssertNil(model.recoveryBackupURL)
            XCTAssertFalse(model.loadFailed)
            XCTAssertEqual(model.drafts[0].gatewayURL, external.gatewayURL)
        }
    }

    private func withURL(_ body: (URL) throws -> Void) throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(directory.appendingPathComponent("openclaw.json"))
    }

    func testMultiMacRosterRoundTripsWithoutDroppingAnyHost() throws {
        try withURL { url in
            let hosts = try ["wss://first.example.ts.net", "wss://second.example.ts.net"].map {
                try OpenClawRuntimeConfiguration(gatewayURL: $0)
            }
            try OpenClawRuntimeConfiguration.saveAll(hosts, to: url)
            XCTAssertEqual(try OpenClawRuntimeConfiguration.loadAll(from: url), hosts)
            let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
            XCTAssertEqual(Set(raw.keys), ["hosts"])
            let entries = try XCTUnwrap(raw["hosts"] as? [[String: String]])
            XCTAssertEqual(entries.map { $0["agent_id"] }, ["hermes", "hermes"])
            XCTAssertEqual(try OpenClawRuntimeConfiguration.load(from: url), hosts[0])
            XCTAssertThrowsError(try hosts[0].save(to: url))
            XCTAssertEqual(try OpenClawRuntimeConfiguration.loadAll(from: url), hosts)
        }
    }

    func testEmptyDuplicateAndExcessiveRostersAreRejectedBeforeWriting() throws {
        try withURL { url in
            let host = try OpenClawRuntimeConfiguration(gatewayURL: "wss://first.example.ts.net")
            XCTAssertThrowsError(try OpenClawRuntimeConfiguration.saveAll([], to: url))
            XCTAssertThrowsError(try OpenClawRuntimeConfiguration.saveAll([host, host], to: url))
            let tooMany = try (0..<17).map { try OpenClawRuntimeConfiguration(gatewayURL: "wss://mac\($0).example.ts.net") }
            XCTAssertThrowsError(try OpenClawRuntimeConfiguration.saveAll(tooMany, to: url))
            XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        }
    }

    @MainActor
    func testEditorAddsMacAndPreservesStableDraftsThroughSave() throws {
        try withURL { url in
            let model = HermesConnectionsModel(url: url)
            model.drafts[0].gatewayURL = "wss://first.example.ts.net"
            model.addHost()
            model.drafts[1].gatewayURL = "wss://second.example.ts.net"
            let ids = model.drafts.map(\.id)
            XCTAssertTrue(model.canSave)
            model.save()
            XCTAssertFalse(model.failed)
            XCTAssertTrue(model.requiresRestart(after: nil))
            XCTAssertTrue(model.requiresRestart(after: Date(timeIntervalSince1970: 0)))
            XCTAssertFalse(model.requiresRestart(after: Date().addingTimeInterval(1)))
            XCTAssertFalse(model.canSave)
            XCTAssertEqual(model.drafts.map(\.id), ids)
            XCTAssertEqual(try OpenClawRuntimeConfiguration.loadAll(from: url).count, 2)
            model.removeHost(id: ids[1])
            model.save()
            XCTAssertEqual(try OpenClawRuntimeConfiguration.loadAll(from: url).count, 1)
        }
    }

    @MainActor
    func testEditorDoesNotOverwriteAnExternalRosterChange() throws {
        try withURL { url in
            let model = HermesConnectionsModel(url: url)
            model.drafts[0].gatewayURL = "wss://draft.example.ts.net"
            let external = try OpenClawRuntimeConfiguration(gatewayURL: "wss://external.example.ts.net")
            try external.save(to: url)
            model.save()
            XCTAssertTrue(model.failed)
            XCTAssertEqual(model.drafts[0].gatewayURL, "wss://draft.example.ts.net")
            XCTAssertEqual(try OpenClawRuntimeConfiguration.loadAll(from: url), [external])
        }
    }
}
