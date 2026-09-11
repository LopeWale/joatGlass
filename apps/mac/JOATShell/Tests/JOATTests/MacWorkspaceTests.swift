import XCTest
@testable import JOAT

final class MacWorkspaceTests: XCTestCase {
    func testHealthyServicesCannotHideADisconnectedRelay() {
        var status = EngineStatus()
        status.at = Date().timeIntervalSince1970
        status.stt = "ready"
        status.llm = "ready"
        let state = MacReadiness(status: status, engineRunning: true)
        XCTAssertTrue(state.isLive)
        XCTAssertFalse(state.ready)
        XCTAssertEqual(state.title, "Connect your glasses")
    }

    func testStoppedOrStaleEngineCannotAdvertiseReadiness() {
        var status = EngineStatus()
        status.at = Date().timeIntervalSince1970
        status.glasses = "connected"
        status.stt = "ready"
        status.llm = "ready"
        XCTAssertTrue(MacReadiness(status: status, engineRunning: true).ready)
        XCTAssertFalse(MacReadiness(status: status, engineRunning: false).ready)
        status.at -= 60
        let stale = MacReadiness(status: status, engineRunning: true)
        XCTAssertFalse(stale.ready)
        XCTAssertEqual(stale.value("connected"), "Not reporting")
    }

    func testPreviewCannotStartAppServices() {
        XCTAssertFalse(EngineController.shouldRunAppServices(environment: ["JOAT_SHELL_PREVIEW": "1"]))
        XCTAssertFalse(EngineController.shouldRunAppServices(environment: [:], previewBundle: true))
        XCTAssertTrue(EngineController.shouldRunAppServices(environment: ["JOAT_SHELL_PREVIEW": "0"]))
    }

    @MainActor
    func testNewsFailureRetainsSelectionAndFeedDraft() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        // A directory where the file should be reliably fails even under an
        // administrator account; no dependence on chmod or live preferences.
        let model = NewsSettingsModel(store: NewsConfigStore(url: directory))
        let previous = model.config
        model.setCategory("science", enabled: true)
        XCTAssertTrue(model.failed)
        XCTAssertEqual(model.config, previous)
        model.newFeedURL = "https://example.com/rss"
        model.addFeed()
        XCTAssertEqual(model.config, previous)
        XCTAssertEqual(model.newFeedURL, "https://example.com/rss")
    }

    @MainActor
    func testNewsSuccessPersistsAndOnlyThenClearsDraft() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = NewsConfigStore(url: directory.appendingPathComponent("news.json"))
        let model = NewsSettingsModel(store: store)
        model.newFeedURL = " HTTPS://example.com/rss "
        XCTAssertTrue(model.canAddFeed)
        model.addFeed()
        XCTAssertEqual(model.newFeedURL, "")
        XCTAssertEqual(store.load().customFeeds, ["https://example.com/rss"])
        model.setCategory("science", enabled: true)
        model.setRefresh(60)
        XCTAssertEqual(store.load(), model.config)
        XCTAssertTrue(store.load().categories.contains("science"))
        XCTAssertEqual(store.load().refreshMinutes, 60)
        model.removeFeed("https://example.com/rss")
        XCTAssertTrue(store.load().customFeeds.isEmpty)
    }

    func testHandEditedNewsDuplicatesHaveStableUniqueIdentity() {
        let config = NewsConfig(categories: ["world", "world"], customFeeds: ["https://example.com/rss", " https://example.com/rss "])
        XCTAssertEqual(config.normalized.categories, ["world"])
        XCTAssertEqual(config.normalized.customFeeds, ["https://example.com/rss"])
    }

    @MainActor
    func testNewsRejectsMalformedDuplicateAndCredentialBearingURLs() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = NewsSettingsModel(store: NewsConfigStore(url: directory.appendingPathComponent("news.json")))
        for value in ["https://", "file:///etc/hosts", "https://user:password@example.com/feed"] {
            model.newFeedURL = value
            XCTAssertFalse(model.canAddFeed, value)
        }
        model.newFeedURL = "https://example.com/feed"
        model.addFeed()
        model.newFeedURL = "https://example.com/feed"
        XCTAssertFalse(model.canAddFeed)
    }
}
