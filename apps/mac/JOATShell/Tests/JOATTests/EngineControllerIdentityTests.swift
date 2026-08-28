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
}
