import XCTest
@testable import JOAT

final class EngineControllerIdentityTests: XCTestCase {
    func testDebugAndInstalledAppsCannotOwnTheSameEnginePort() {
        XCTAssertEqual(
            EngineController.defaultEnginePort(bundleIdentifier: "com.joat.shell"),
            8_787
        )
        XCTAssertEqual(
            EngineController.defaultEnginePort(bundleIdentifier: "com.joat.shell.debug"),
            18_787
        )
        XCTAssertEqual(
            EngineController.defaultSttPort(bundleIdentifier: "com.joat.shell"),
            50_060
        )
        XCTAssertEqual(
            EngineController.defaultSttPort(bundleIdentifier: "com.joat.shell.debug"),
            51_060
        )
    }

    func testUnknownBundleKeepsTheProductionDefault() {
        XCTAssertEqual(
            EngineController.defaultEnginePort(bundleIdentifier: nil),
            8_787
        )
        XCTAssertEqual(
            EngineController.defaultSttPort(bundleIdentifier: nil),
            50_060
        )
    }

    func testXCTestHostNeverStartsLiveJOATServices() {
        XCTAssertFalse(EngineController.shouldRunAppServices(environment: [
            "XCTestConfigurationFilePath": "/tmp/joat-tests.xctestconfiguration"
        ]))
        XCTAssertTrue(EngineController.shouldRunAppServices(environment: [:]))
    }

    func testBrainstormAcceptanceArmIsFixedAndOwnerOnly() throws {
        let home = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: home) }

        let arm = try EngineController.writeG2BrainstormAcceptanceArm(homeDirectory: home)

        XCTAssertEqual(
            try String(contentsOf: arm, encoding: .utf8),
            EngineController.g2BrainstormAcceptanceArmSchema
        )
        let attributes = try FileManager.default.attributesOfItem(atPath: arm.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }
}
