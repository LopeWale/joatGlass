import Foundation
import XCTest
@testable import JOAT

final class OpenClawRuntimeConfigurationTests: XCTestCase {
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

    func testCanonicalizesSafeGatewayOriginAndFixesHermesAgent() throws {
        let configuration = try OpenClawRuntimeConfiguration(
            gatewayURL: "WSS://Mac-Mini.example.ts.net:443/"
        )

        XCTAssertEqual(configuration.gatewayURL, "wss://mac-mini.example.ts.net")
        XCTAssertEqual(configuration.agentID, "hermes")
    }

    func testRejectsDashboardSecretsAndNonGatewayOrigins() {
        for endpoint in [
            "https://mac-mini.example.ts.net",
            "ws://mac-mini.example.ts.net",
            "wss://mac-mini.example.ts.net:9443",
            "wss://user@mac-mini.example.ts.net",
            "wss://mac-mini.example.ts.net/glasses",
            "wss://mac-mini.example.ts.net?token=secret",
        ] {
            XCTAssertThrowsError(try OpenClawRuntimeConfiguration(gatewayURL: endpoint))
        }
    }

    func testSaveWritesExactOwnerOnlySchemaThatThePythonRuntimeAccepts() throws {
        let configuration = try OpenClawRuntimeConfiguration(
            gatewayURL: "wss://mac-mini.example.ts.net"
        )
        let path = directory
            .appendingPathComponent("Application Support/JOAT", isDirectory: true)
            .appendingPathComponent("openclaw.json")

        try configuration.save(to: path)

        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: path)) as? [String: String]
        )
        XCTAssertEqual(object, [
            "gateway_url": "wss://mac-mini.example.ts.net",
            "agent_id": "hermes",
        ])
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        let directoryAttributes = try FileManager.default.attributesOfItem(
            atPath: path.deletingLastPathComponent().path
        )
        XCTAssertEqual(
            (directoryAttributes[.posixPermissions] as? NSNumber)?.intValue,
            0o700
        )
        XCTAssertEqual(try OpenClawRuntimeConfiguration.load(from: path), configuration)
    }

    func testLoadRejectsExtraFieldsAndNonHermesAgent() throws {
        let path = directory.appendingPathComponent("openclaw.json")
        try Data(
            #"{"gateway_url":"wss://mac-mini.example.ts.net","agent_id":"main","token":"secret"}"#.utf8
        ).write(to: path)

        XCTAssertThrowsError(try OpenClawRuntimeConfiguration.load(from: path))
    }

    func testLoadRejectsGroupReadableConfigAndSaveRepairsItAtomically() throws {
        let path = directory.appendingPathComponent("openclaw.json")
        try Data(
            #"{"gateway_url":"wss://old-mac-mini.example.ts.net","agent_id":"hermes"}"#.utf8
        ).write(to: path)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o640],
            ofItemAtPath: path.path
        )
        XCTAssertThrowsError(try OpenClawRuntimeConfiguration.load(from: path))

        let configuration = try OpenClawRuntimeConfiguration(
            gatewayURL: "wss://new-mac-mini.example.ts.net"
        )
        try configuration.save(to: path)

        XCTAssertEqual(try OpenClawRuntimeConfiguration.load(from: path), configuration)
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }

    func testSaveWillNotFollowAnExistingSymbolicLink() throws {
        let target = directory.appendingPathComponent("target.json")
        try Data(#"{"keep":"me"}"#.utf8).write(to: target)
        let path = directory.appendingPathComponent("openclaw.json")
        try FileManager.default.createSymbolicLink(
            atPath: path.path,
            withDestinationPath: target.path
        )
        let configuration = try OpenClawRuntimeConfiguration(
            gatewayURL: "wss://mac-mini.example.ts.net"
        )

        XCTAssertThrowsError(try configuration.save(to: path))
        XCTAssertEqual(try Data(contentsOf: target), Data(#"{"keep":"me"}"#.utf8))
    }

    func testLoadRejectsBrokenSymbolicLinkInsteadOfTreatingItAsUnconfigured() throws {
        let path = directory.appendingPathComponent("openclaw.json")
        try FileManager.default.createSymbolicLink(
            atPath: path.path,
            withDestinationPath: directory.appendingPathComponent("missing.json").path
        )

        XCTAssertThrowsError(try OpenClawRuntimeConfiguration.load(from: path))
    }
}
