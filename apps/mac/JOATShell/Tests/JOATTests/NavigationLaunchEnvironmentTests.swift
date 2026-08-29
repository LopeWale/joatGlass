import Foundation
import XCTest
@testable import JOAT

final class NavigationLaunchEnvironmentTests: XCTestCase {
    func testMapsLegacyRepoKeyToTheEngineContract() throws {
        let repo = try temporaryRepo(config: """
        MAPBOX_API_KEY=pk.local-development-token
        OPENAI_API_KEY=must-not-be-forwarded
        """)
        defer { try? FileManager.default.removeItem(at: repo) }

        let environment = EngineController.navigationLaunchEnvironment(
            inherited: ["PATH": "/usr/bin"],
            repo: repo
        )

        XCTAssertEqual(environment["MAPBOX_ACCESS_TOKEN"], "pk.local-development-token")
        XCTAssertEqual(environment["JOAT_EXPERIMENTAL_NAVIGATION"], "1")
        XCTAssertNil(environment["MAPBOX_API_KEY"])
        XCTAssertNil(environment["OPENAI_API_KEY"])
        XCTAssertEqual(environment["PATH"], "/usr/bin")
    }

    func testExplicitProcessConfigurationWinsOverRepoConfig() throws {
        let repo = try temporaryRepo(config: """
        MAPBOX_ACCESS_TOKEN=pk.repo-token
        JOAT_EXPERIMENTAL_NAVIGATION=1
        """)
        defer { try? FileManager.default.removeItem(at: repo) }

        let environment = EngineController.navigationLaunchEnvironment(
            inherited: [
                "MAPBOX_ACCESS_TOKEN": "pk.process-token",
                "JOAT_EXPERIMENTAL_NAVIGATION": "false",
            ],
            repo: repo
        )

        XCTAssertEqual(environment["MAPBOX_ACCESS_TOKEN"], "pk.process-token")
        XCTAssertEqual(environment["JOAT_EXPERIMENTAL_NAVIGATION"], "false")
    }

    func testMapsInheritedLegacyKeyWithoutForwardingTheLegacyName() throws {
        let repo = try temporaryRepo(config: "")
        defer { try? FileManager.default.removeItem(at: repo) }

        let environment = EngineController.navigationLaunchEnvironment(
            inherited: ["MAPBOX_API_KEY": "pk.process-legacy-token"],
            repo: repo
        )

        XCTAssertEqual(environment["MAPBOX_ACCESS_TOKEN"], "pk.process-legacy-token")
        XCTAssertNil(environment["MAPBOX_API_KEY"])
        XCTAssertNil(environment["JOAT_EXPERIMENTAL_NAVIGATION"])
    }

    func testMissingNavigationConfigLeavesInheritedEnvironmentUntouched() throws {
        let repo = try temporaryRepo(config: "OPENAI_API_KEY=not-navigation\n")
        defer { try? FileManager.default.removeItem(at: repo) }
        let inherited = ["PATH": "/usr/bin", "OTHER": "value"]

        XCTAssertEqual(
            EngineController.navigationLaunchEnvironment(inherited: inherited, repo: repo),
            inherited
        )
    }

    private func temporaryRepo(config: String) throws -> URL {
        let repo = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: repo, withIntermediateDirectories: true)
        try config.write(to: repo.appendingPathComponent(".env"), atomically: true, encoding: .utf8)
        return repo
    }
}
