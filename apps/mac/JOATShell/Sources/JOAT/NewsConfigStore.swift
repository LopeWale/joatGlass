import Foundation

/// The News tab's configuration, exactly as the engine reads it
/// (apps/mac/joat/services/news.py, `NewsConfig`): which subjects to poll,
/// which of each subject's free sources, any feeds of the wearer's own,
/// and the cadence. Keys are camelCase because the JSON is the contract
/// between this app and the Python brain; the brain re-reads the file when
/// it changes, so saving here is enough — no restart, no message.
struct NewsConfig: Codable, Equatable {
    static var defaultCategories: [String] { NewsCatalog.shared.defaultCategories }
    static let minimumRefreshMinutes = 5
    static let maximumRefreshMinutes = 24 * 60

    var categories: [String] = NewsConfig.defaultCategories
    var customFeeds: [String] = []
    var refreshMinutes: Int = 15
    /// The catalog sources the wearer chose, by id; nil until they change
    /// one. A subject with none of its sources here reads its defaults —
    /// the engine's rule (`NewsConfig.enabled_sources`).
    var sources: [String]?

    /// The engine clamps too; clamping here keeps the file honest. One
    /// rule is mirrored on purpose: nothing selected and no feed of your
    /// own is the DEFAULT set, not an empty tab — the engine substitutes
    /// the defaults for that file, so the app must show what it will do.
    var normalized: NewsConfig { normalized(in: .shared) }

    func normalized(in catalog: NewsCatalog) -> NewsConfig {
        var copy = self
        let known = Set(catalog.categories.map(\.id))
        var seenCategories = Set<String>()
        var seenFeeds = Set<String>()
        copy.categories = categories.filter { known.contains($0) && seenCategories.insert($0).inserted }
        copy.customFeeds = customFeeds
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { $0.hasPrefix("https://") || $0.hasPrefix("http://") }
            .filter { seenFeeds.insert($0).inserted }
        if copy.categories.isEmpty && copy.customFeeds.isEmpty {
            copy.categories = catalog.defaultCategories
        }
        if let sources {
            // Catalog order, known ids only, each once.
            let chosen = Set(sources)
            copy.sources = catalog.sourceIDs.filter { chosen.contains($0) }
        }
        copy.refreshMinutes = min(
            NewsConfig.maximumRefreshMinutes,
            max(NewsConfig.minimumRefreshMinutes, refreshMinutes)
        )
        return copy
    }

    /// The sources the engine reads for `category`: those chosen there, or
    /// its defaults when none are.
    func activeSourceIDs(in category: NewsCategory) -> [String] {
        let chosen = Set(sources ?? [])
        let picked = category.sources.map(\.id).filter { chosen.contains($0) }
        return picked.isEmpty ? category.defaultSourceIDs : picked
    }

    /// Decoding is tolerant of a hand-edited file: any missing key keeps
    /// its default, and the engine tolerates the same shapes.
    init() {}

    init(categories: [String], customFeeds: [String] = [], refreshMinutes: Int = 15, sources: [String]? = nil) {
        self.categories = categories
        self.customFeeds = customFeeds
        self.refreshMinutes = refreshMinutes
        self.sources = sources
    }

    private enum CodingKeys: String, CodingKey {
        case categories, customFeeds, refreshMinutes, sources
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        categories = try container.decodeIfPresent([String].self, forKey: .categories)
            ?? NewsConfig.defaultCategories
        customFeeds = try container.decodeIfPresent([String].self, forKey: .customFeeds) ?? []
        refreshMinutes = try container.decodeIfPresent(Int.self, forKey: .refreshMinutes) ?? 15
        sources = try container.decodeIfPresent([String].self, forKey: .sources)
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
