import Observation
import SwiftUI

/// A successful write is the commit point. Failed writes must not leave the
/// controls displaying a selection the engine will never read.
@MainActor @Observable
final class NewsSettingsModel {
    private(set) var config: NewsConfig
    var newFeedURL = ""
    private(set) var notice: String?
    private(set) var failed = false
    private let store: NewsConfigStore

    init(store: NewsConfigStore = NewsConfigStore()) {
        self.store = store
        config = store.load()
    }

    private var cleanFeedURL: String? {
        let text = newFeedURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: text), let host = parts.host, !host.isEmpty,
              host.rangeOfCharacter(from: .whitespacesAndNewlines) == nil,
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              parts.user == nil, parts.password == nil else { return nil }
        parts.scheme = parts.scheme?.lowercased()
        return parts.url?.absoluteString
    }

    var canAddFeed: Bool {
        guard let cleanFeedURL else { return false }
        return !config.customFeeds.contains(cleanFeedURL)
    }

    var feedValidationMessage: String? {
        guard !newFeedURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        guard let cleanFeedURL else { return "Enter a full http:// or https:// feed address without a username or password." }
        return config.customFeeds.contains(cleanFeedURL) ? "This feed is already in your list." : nil
    }

    func setCategory(_ id: String, enabled: Bool) {
        var next = config
        next.categories.removeAll { $0 == id }
        if enabled { next.categories.append(id) }
        let selected = Set(next.categories)
        next.categories = NewsConfig.categories.map(\.id).filter { selected.contains($0) }
        _ = save(next)
    }

    func addFeed() {
        guard canAddFeed, let cleanFeedURL else { return }
        var next = config
        next.customFeeds.append(cleanFeedURL)
        if save(next) { newFeedURL = "" }
    }

    func removeFeed(_ feed: String) {
        var next = config
        next.customFeeds.removeAll { $0 == feed }
        _ = save(next)
    }

    func setRefresh(_ minutes: Int) {
        var next = config
        next.refreshMinutes = minutes
        _ = save(next)
    }

    private func save(_ next: NewsConfig) -> Bool {
        let normalized = next.normalized
        do {
            try store.save(normalized)
            config = normalized
            failed = false
            notice = "Saved on this Mac. The engine will use these choices on its next news pass."
            return true
        } catch {
            failed = true
            notice = "Could not save your changes. Your previous choices are still shown. \(error.localizedDescription)"
            return false
        }
    }
}

struct NewsSettingsView: View {
    @Bindable var model: NewsSettingsModel
    @ObservedObject var commands: EngineCommandChannel

    var body: some View {
        WorkspacePage(title: "News", subtitle: "Choose what appears in News on your glasses.") {
            LocalCommandView(commands: commands, action: .newsRefresh,
                             title: "Refresh News", detail: "Fetch your selected feeds now on this Mac.")
            GroupBox("Your interests") {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Headlines from BBC, NPR, and Google News. Choose the subjects you want to follow.")
                        .font(.callout).foregroundStyle(.secondary)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 175), alignment: .leading)], alignment: .leading, spacing: 16) {
                        ForEach(NewsConfig.categories) { category in
                            Toggle(category.label, isOn: Binding(
                                get: { model.config.categories.contains(category.id) },
                                set: { model.setCategory(category.id, enabled: $0) }
                            )).toggleStyle(.checkbox)
                        }
                    }
                    Text("If you remove every interest and custom feed, JOAT restores its default interests.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            }
            GroupBox("Your feeds") {
                VStack(alignment: .leading, spacing: 14) {
                    if model.config.customFeeds.isEmpty {
                        Label("Add an RSS or Atom feed from a publication you follow.", systemImage: "dot.radiowaves.left.and.right")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(model.config.customFeeds, id: \.self) { feed in
                            HStack(spacing: 12) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(URL(string: feed)?.host ?? feed).font(.callout.weight(.medium))
                                    Text(feed).font(.caption).foregroundStyle(.secondary)
                                        .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                                }
                                Spacer(minLength: 8)
                                Button("Remove", systemImage: "minus.circle") { model.removeFeed(feed) }
                                    .labelStyle(.iconOnly).buttonStyle(.borderless)
                                    .help("Remove \(URL(string: feed)?.host ?? "feed")")
                                    .accessibilityLabel("Remove \(URL(string: feed)?.host ?? feed)")
                            }
                            Divider()
                        }
                    }
                    HStack {
                        TextField("RSS or Atom feed URL", text: $model.newFeedURL,
                                  prompt: Text("https://publication.com/feed.xml"))
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { model.addFeed() }
                        Button("Add Feed", systemImage: "plus") { model.addFeed() }
                            .disabled(!model.canAddFeed)
                    }
                    if let message = model.feedValidationMessage {
                        Text(message).font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Refresh interval").font(.headline)
                    Text("Feeds are fetched on this Mac.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Stepper("Every \(model.config.refreshMinutes) minutes", value: Binding(
                    get: { model.config.refreshMinutes }, set: { model.setRefresh($0) }
                ), in: NewsConfig.minimumRefreshMinutes...NewsConfig.maximumRefreshMinutes, step: 5)
                    .fixedSize()
            }
            if let notice = model.notice { WorkspaceNotice(text: notice, isError: model.failed) }
        }
    }
}
