import Foundation
import XCTest
@testable import JOAT

/// news.json is a contract with the Python engine (services/news.py):
/// camelCase keys, known category ids, http(s) feeds, a clamped cadence.
final class NewsConfigStoreTests: XCTestCase {
    private func temporaryStore() -> NewsConfigStore {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("joat-news-\(UUID().uuidString)")
        return NewsConfigStore(url: directory.appendingPathComponent("news.json"))
    }

    func testMissingFileIsTheDefaults() {
        let store = temporaryStore()
        XCTAssertEqual(store.load(), NewsConfig())
        XCTAssertEqual(NewsConfig().categories, ["top", "technology", "world"])
    }

    func testSaveWritesTheEngineShapeAtomicallyAndPrivately() throws {
        let store = temporaryStore()
        try store.save(NewsConfig(
            categories: ["science", "astrology", "top"],
            customFeeds: [" https://example.org/feed.xml ", "ftp://nope"],
            refreshMinutes: 1
        ))
        let data = try Data(contentsOf: store.url)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["categories"] as? [String], ["science", "top"])
        XCTAssertEqual(json["customFeeds"] as? [String], ["https://example.org/feed.xml"])
        XCTAssertEqual(json["refreshMinutes"] as? Int, 5)
        let attributes = try FileManager.default.attributesOfItem(atPath: store.url.path)
        let mode = ((attributes[.posixPermissions] as? Int) ?? 0) & 0o777
        XCTAssertEqual(mode, 0o600)
        XCTAssertEqual(store.load(), NewsConfig(
            categories: ["science", "top"],
            customFeeds: ["https://example.org/feed.xml"],
            refreshMinutes: 5
        ))
    }

    func testAHandEditedFileKeepsDefaultsForMissingKeys() throws {
        let store = temporaryStore()
        try FileManager.default.createDirectory(
            at: store.url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data("{\"customFeeds\": [\"https://a/b\"]}".utf8).write(to: store.url)
        let loaded = store.load()
        XCTAssertEqual(loaded.customFeeds, ["https://a/b"])
        XCTAssertEqual(loaded.categories, NewsConfig.defaultCategories)
        XCTAssertEqual(loaded.refreshMinutes, 15)
        try Data("{not json".utf8).write(to: store.url)
        XCTAssertEqual(store.load(), NewsConfig())
    }
}
