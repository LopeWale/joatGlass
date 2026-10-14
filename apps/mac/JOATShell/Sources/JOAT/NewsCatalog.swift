import Foundation

/// One free feed the News tab can read: a publisher's own key-free RSS.
struct NewsSource: Identifiable, Equatable, Decodable, Sendable {
    let id: String
    let name: String
    let url: String
    /// On until the wearer changes this subject's sources.
    let isDefault: Bool

    private enum CodingKeys: String, CodingKey {
        case id, name, url, isDefault = "default"
    }
}

/// A subject on the News page, with the sources it can read.
struct NewsCategory: Identifiable, Equatable, Decodable, Sendable {
    let id: String
    let label: String
    let sources: [NewsSource]

    var defaultSourceIDs: [String] { sources.filter(\.isDefault).map(\.id) }
}

/// The News tab's catalog: apps/mac/joat/services/news_sources.json, the
/// one list the engine polls from (services/news.py) and this page offers.
/// project.yml bundles that very file, so the two cannot drift.
struct NewsCatalog: Equatable, Decodable, Sendable {
    let defaultCategories: [String]
    let categories: [NewsCategory]

    static let shared: NewsCatalog = bundled ?? development ?? fallback

    static func decode(_ data: Data) -> NewsCatalog? {
        try? JSONDecoder().decode(NewsCatalog.self, from: data)
    }

    /// The copy inside JOAT.app (project.yml: Resources).
    static var bundled: NewsCatalog? {
        guard let url = Bundle.main.url(forResource: "news_sources", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return decode(data)
    }

    /// `swift build` and `swift test` have no app bundle: the engine's own
    /// copy in this checkout (Sources/JOAT → apps/mac/joat/services).
    static var development: NewsCatalog? {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("../../../joat/services/news_sources.json")
            .standardizedFileURL
        guard let data = try? Data(contentsOf: url) else { return nil }
        return decode(data)
    }

    /// Only if the catalog is missing altogether: the subjects without
    /// their sources, so the page still offers what the engine knows.
    static let fallback = NewsCatalog(
        defaultCategories: ["top", "technology", "world"],
        categories: [
            ("top", "Top stories"), ("world", "World"), ("us", "US"), ("business", "Business"),
            ("technology", "Technology"), ("science", "Science"), ("health", "Health"),
            ("sports", "Sports"), ("entertainment", "Entertainment"),
        ].map { NewsCategory(id: $0.0, label: $0.1, sources: []) }
    )

    func category(_ id: String) -> NewsCategory? {
        categories.first { $0.id == id }
    }

    var sourceIDs: [String] { categories.flatMap { $0.sources.map(\.id) } }
}
