import Foundation

/// One tab's content, as `services/panels.py` PanelSnapshot.
struct PanelSnapshot {
    var rows: [String]
    var headline: String? = nil
    var empty: String? = nil
    /// HudPanelPomodoro wire object, pomodoro tab only.
    var pomodoro: [String: Any]? = nil
}

/// A tab's content provider (panels.py PanelProvider).
protocol PanelProvider: AnyObject {
    var tab: String { get }
    /// Rows above the items that open nothing (News's "[!] feeds unavailable").
    var statusRows: Int { get }
    func snapshot() -> PanelSnapshot
    /// A long press: fetch again. The publisher pushes afterwards either way.
    func refresh(completion: @escaping () -> Void)
    /// A row tap. `true` when it opened a reader the provider sent itself;
    /// `false` when the row was an action whose effect shows in the next push.
    func open(index: Int, completion: @escaping (Bool) -> Void)
}

/// The right-panel tabs, pushed proactively: a port of
/// `apps/mac/joat/services/panels.py` PanelPublisher and the `hud.panel`
/// wire in `core/protocol.py` (HudPanel, panel_request_id).
final class PanelPublisher: BrainFeature {
    let name = "panels"
    let uplinkKinds: Set<String> = ["input"]

    /// protocol.py PANEL_TABS, in the Mac's provider order (__main__.py).
    static let order = ["planner", "news", "pomodoro", "search", "quicknotes"]
    /// panels.py PANEL_TITLES: the focus-page title a row tap carries.
    static let titles = ["news": "NEWS", "pomodoro": "POMODORO", "search": "SEARCH"]
    static let rowLimit = 20
    static let rowCharacters = 64

    private let link: PluginLink
    private var providers: [String: PanelProvider] = [:]
    private var revisions: [String: Int] = [:]
    private var opening: Set<String> = []

    init(link: PluginLink) { self.link = link }

    func register(_ provider: PanelProvider) {
        providers[provider.tab] = provider
        revisions[provider.tab] = revisions[provider.tab] ?? 0
    }

    func requestId(_ tab: String) -> String { "panel-\(tab)-\(revisions[tab] ?? 0)" }

    // MARK: BrainFeature

    /// A plugin that just connected has an empty cache: fill it (no activate).
    func pluginConnected() { publishAll() }

    func handle(kind: String, message: [String: Any]) {
        guard kind == "input", let event = message.object("event"),
              let container = event.string("containerName") else { return }
        let title = event.string("selectedName")
        guard let tab = Self.tab(forTitle: title), providers[tab] != nil else { return }
        switch container {
        case "capdata":
            if let index = event.int("selectedIndex") {
                select(tab: tab, index: index, requestId: event.string("requestId"))
            }
        case "caprefresh":
            refresh(tab: tab, requestId: event.string("requestId"))
        default:
            break
        }
    }

    // MARK: Publishing

    func publish(_ tab: String, activate: Bool = false) {
        guard let provider = providers[tab] else { return }
        let revision = (revisions[tab] ?? 0) + 1
        revisions[tab] = revision
        let s = provider.snapshot()
        var wire: [String: Any] = [
            "kind": "hud.panel",
            "tab": tab,
            "revision": revision,
            "rows": s.rows.prefix(Self.rowLimit).map { $0.utf16Prefix(Self.rowCharacters) },
        ]
        if let h = s.headline { wire["headline"] = h.utf16Prefix(Self.rowCharacters) }
        if let e = s.empty { wire["empty"] = e.utf16Prefix(Self.rowCharacters) }
        if let p = s.pomodoro { wire["pomodoro"] = p }
        if activate { wire["activate"] = true }
        link.send(wire)
        Log.add("tx hud.panel \(tab) rev=\(revision) rows=\(min(s.rows.count, Self.rowLimit))\(activate ? " activate" : "")")
    }

    func publishAll() {
        for tab in Self.order where providers[tab] != nil { publish(tab) }
    }

    /// A voice open: the plugin switches to and expands the tab.
    func activate(_ tab: String) { publish(tab, activate: true) }

    // MARK: Taps and refresh

    private func select(tab: String, index: Int, requestId: String?) {
        guard requestId == self.requestId(tab) else {
            Log.add("dropping stale panel tap tab=\(tab) request=\(requestId ?? "nil")")
            return
        }
        guard let provider = providers[tab] else { return }
        let offset = provider.statusRows
        if index < offset { publish(tab); return }
        guard !opening.contains(tab) else { return }
        opening.insert(tab)
        provider.open(index: index - offset) { [weak self] openedReader in
            DispatchQueue.main.async {
                self?.opening.remove(tab)
                if !openedReader { self?.publish(tab) }
            }
        }
    }

    private func refresh(tab: String, requestId: String?) {
        if let requestId, !requestId.hasPrefix("panel-\(tab)-") { return }
        providers[tab]?.refresh { [weak self] in
            DispatchQueue.main.async { self?.publish(tab) }
        }
    }

    static func tab(forTitle title: String?) -> String? {
        guard let title else { return nil }
        // "NEWS" and "NEWS · ARTICLE" both belong to the news tab.
        let family = title.components(separatedBy: " · ").first ?? title
        return titles.first { $0.value == family }?.key
    }
}

extension String {
    /// protocol.py utf16_slice: at most `n` UTF-16 units, never splitting a
    /// surrogate pair.
    func utf16Prefix(_ n: Int) -> String {
        if utf16.count <= n { return self }
        var out = ""
        var used = 0
        for ch in self {
            let w = String(ch).utf16.count
            if used + w > n { break }
            out.append(ch)
            used += w
        }
        return out
    }
}
