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

    func testNothingSelectedMirrorsTheEnginesDefaultFallback() throws {
        // services/news.py substitutes the defaults for an empty selection
        // with no custom feed; the app must show the same, not "0 feeds".
        XCTAssertEqual(NewsConfig(categories: []).normalized.categories, NewsConfig.defaultCategories)
        XCTAssertEqual(
            NewsConfig(categories: [], customFeeds: ["https://a/b"]).normalized.categories, []
        )
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

    // MARK: Sources (news_sources.json, shared with services/news.py)

    /// The engine's own file, read the way the app reads its bundled copy.
    private func engineCatalog() throws -> NewsCatalog {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // Tests/JOATTests
            .appendingPathComponent("../../../joat/services/news_sources.json")
            .standardizedFileURL
        return try XCTUnwrap(NewsCatalog.decode(Data(contentsOf: url)))
    }

    func testTheEnginesCatalogIsTheOneThisPageOffers() throws {
        let catalog = try engineCatalog()
        XCTAssertEqual(NewsCatalog.shared, catalog)
        XCTAssertEqual(catalog.defaultCategories, ["top", "technology", "world"])
        XCTAssertEqual(Set(catalog.sourceIDs).count, catalog.sourceIDs.count)
        for category in catalog.categories {
            XCTAssertFalse(category.defaultSourceIDs.isEmpty, category.id)
            for source in category.sources {
                XCTAssertTrue(source.url.hasPrefix("https://"), source.id)
                XCTAssertFalse(source.url.contains("news.google.com"), source.id)
            }
        }
    }

    func testUnchangedSourcesAreNotWrittenAndChosenOnesKeepCatalogOrder() throws {
        let catalog = try engineCatalog()
        let store = temporaryStore()
        try store.save(NewsConfig(categories: ["world"]))
        let plain = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: store.url)) as? [String: Any])
        XCTAssertNil(plain["sources"])
        let config = NewsConfig(categories: ["world", "science"], sources: ["npr-science", "dw", "made-up", "dw"])
        XCTAssertEqual(config.normalized(in: catalog).sources, ["dw", "npr-science"])
    }

    func testASubjectWithNoneChosenReadsItsDefaultsLikeTheEngine() throws {
        let catalog = try engineCatalog()
        let world = try XCTUnwrap(catalog.category("world"))
        let sports = try XCTUnwrap(catalog.category("sports"))
        let config = NewsConfig(categories: ["world", "sports"], sources: ["dw"])
        XCTAssertEqual(config.activeSourceIDs(in: world), ["dw"])
        XCTAssertEqual(config.activeSourceIDs(in: sports), sports.defaultSourceIDs)
    }

    @MainActor
    func testChoosingSourcesWritesOnlyThatSubjectAndNeverEmptiesIt() throws {
        let store = temporaryStore()
        let model = NewsSettingsModel(store: store)
        let world = try XCTUnwrap(NewsCatalog.shared.category("world"))
        let dw = try XCTUnwrap(world.sources.first { $0.id == "dw" })
        model.setSource(dw, in: world, enabled: true)
        XCTAssertEqual(Set(store.load().sources ?? []), Set(world.defaultSourceIDs + ["dw"]))
        // Down to one source: that one stays on.
        for id in world.defaultSourceIDs {
            model.setSource(try XCTUnwrap(world.sources.first { $0.id == id }), in: world, enabled: false)
        }
        XCTAssertEqual(store.load().sources, ["dw"])
        XCTAssertFalse(model.canSwitchOff(dw, in: world))
        model.setSource(dw, in: world, enabled: false)
        XCTAssertEqual(store.load().sources, ["dw"])
        // Back to the recommended set: nothing spelled out any more.
        model.useRecommendedSources()
        XCTAssertNil(store.load().sources)
        XCTAssertTrue(model.usesRecommendedSources)
    }
}
