import Foundation

/// The News tab's configuration, exactly as the engine reads it
/// (apps/mac/joat/services/news.py, `NewsConfig`): which free-feed
/// categories to poll, any feeds of the wearer's own, and the cadence.
/// Keys are camelCase because the JSON is the contract between this app
/// and the Python brain; the brain re-reads the file when it changes, so
/// saving here is enough — no restart, no message.
struct NewsCategory: Identifiable, Equatable {
    let id: String
    let label: String
}

struct NewsConfig: Codable, Equatable {
    /// Category ids the engine knows, in the order the Mac app lists them.
    /// Each maps to three key-free feeds on the engine side (BBC, NPR,
    /// Google News); an id the engine does not know is ignored there.
    static let categories: [NewsCategory] = [
        NewsCategory(id: "top", label: "Top stories"),
        NewsCategory(id: "world", label: "World"),
        NewsCategory(id: "us", label: "US"),
        NewsCategory(id: "business", label: "Business"),
        NewsCategory(id: "technology", label: "Technology"),
        NewsCategory(id: "science", label: "Science"),
        NewsCategory(id: "health", label: "Health"),
        NewsCategory(id: "sports", label: "Sports"),
        NewsCategory(id: "entertainment", label: "Entertainment"),
    ]
    static let defaultCategories = ["top", "technology", "world"]
    static let minimumRefreshMinutes = 5
    static let maximumRefreshMinutes = 24 * 60

    var categories: [String] = NewsConfig.defaultCategories
    var customFeeds: [String] = []
    var refreshMinutes: Int = 15

    /// The engine clamps too; clamping here keeps the file honest.
    var normalized: NewsConfig {
        var copy = self
        let known = Set(NewsConfig.categories.map(\.id))
        copy.categories = categories.filter { known.contains($0) }
        copy.customFeeds = customFeeds
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { $0.hasPrefix("https://") || $0.hasPrefix("http://") }
        copy.refreshMinutes = min(
            NewsConfig.maximumRefreshMinutes,
            max(NewsConfig.minimumRefreshMinutes, refreshMinutes)
        )
        return copy
    }

    /// Decoding is tolerant of a hand-edited file: any missing key keeps
    /// its default, and the engine tolerates the same shapes.
    init() {}

    init(categories: [String], customFeeds: [String] = [], refreshMinutes: Int = 15) {
        self.categories = categories
        self.customFeeds = customFeeds
        self.refreshMinutes = refreshMinutes
    }

    private enum CodingKeys: String, CodingKey {
        case categories, customFeeds, refreshMinutes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        categories = try container.decodeIfPresent([String].self, forKey: .categories)
            ?? NewsConfig.defaultCategories
        customFeeds = try container.decodeIfPresent([String].self, forKey: .customFeeds) ?? []
        refreshMinutes = try container.decodeIfPresent(Int.self, forKey: .refreshMinutes) ?? 15
    }
}

/// Reads and writes `~/Library/Application Support/JOAT/news.json`, the
/// same directory the engine keeps its other state in. Writes are atomic
/// and private (0600), like the engine's own JSON files.
struct NewsConfigStore {
    static let defaultURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/news.json")

    let url: URL

    init(url: URL = NewsConfigStore.defaultURL) {
        self.url = url
    }

    /// A missing or unreadable file is the defaults, never an error: an
    /// unreadable preference must not take the News tab down with it.
    func load() -> NewsConfig {
        guard let data = try? Data(contentsOf: url) else { return NewsConfig() }
        guard let config = try? JSONDecoder().decode(NewsConfig.self, from: data) else {
            return NewsConfig()
        }
        return config.normalized
    }

    func save(_ config: NewsConfig) throws {
        let directory = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(config.normalized)
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}
