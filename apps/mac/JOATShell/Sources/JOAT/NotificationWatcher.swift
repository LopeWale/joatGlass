import AppKit
import ApplicationServices
import Foundation

/// Reads OTHER apps' notification banners via the Accessibility API and
/// hands them to the Python engine (docs/MAC_APP.md §Notification source).
///
/// WHY NOT the system stores: `usernoted`'s db2 sqlite is a 0-byte migration
/// cookie on macOS 27, and the Biome `Notification.Delivery` stream carries
/// only bundle-id + an app-chosen identifier + timestamp — for Messages that
/// identifier is an opaque UUID. Neither has the text. The rendered banner
/// does, and AX can read it. There is no sanctioned API for this on any
/// stack; Apple's position is that reading other apps' notifications is
/// unsupported.
///
/// STRUCTURE (verified against macOS 26/27 practitioner sources and the
/// strings in NotificationCenter.app itself):
///
///     Application (com.apple.notificationcenterui)
///     └─ AXWindow "Notification Center"      ← every window has this title
///        └─ … AXScrollArea "AXNotificationListItems"
///           └─ AXGroup  subrole=AXNotificationCenter{Banner,Alert}[Stack]
///              ├─ AXStaticText AXIdentifier="title"
///              ├─ AXStaticText AXIdentifier="subtitle"
///              └─ AXStaticText AXIdentifier="body"
///
/// Matching is by SUBROLE, never by window title: NotificationCenter.app
/// also owns the desktop widget windows (Stocks, Weather, Clock, Calendar,
/// Reminders, Screen Time) and titles them identically — an earlier
/// title-based filter captured 100% widgets and 0% banners.
@MainActor
final class NotificationWatcher {
    static let shared = NotificationWatcher()

    private static let bundleID = "com.apple.notificationcenterui"

    /// All four are required. macOS renders the "Persistent" notification
    /// style with the Alert subroles and "Temporary" with Banner — matching
    /// one pair silently misses every app configured the other way.
    private static let bannerSubroles: Set<String> = [
        "AXNotificationCenterBanner", "AXNotificationCenterBannerStack",
        "AXNotificationCenterAlert", "AXNotificationCenterAlertStack",
    ]

    private var timer: Timer?
    private var sweepCount = 0
    private var seen: [String: Date] = [:]
    /// Banners present in the previous sweep, so a banner that lingers for
    /// its several-second display is emitted once, not once per second.
    private var previous: Set<String> = []

    private let outURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/notifications.jsonl")

    var isTrusted: Bool { AXIsProcessTrusted() }

    /// `defaults write com.joat.shell AXDebug -bool true` records every
    /// banner the sweep sees — the practical way to re-verify Apple's
    /// private hierarchy, which has been reshuffled every major release.
    private var debugEnabled: Bool { UserDefaults.standard.bool(forKey: "AXDebug") }

    func requestAccess() {
        // The literal, not kAXTrustedCheckOptionPrompt: that global is a
        // mutable var in the C header, which Swift 6 strict concurrency
        // rejects. The key string itself is stable public API.
        let opts = ["AXTrustedCheckOptionPrompt": true]
        _ = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    }

    func start() {
        guard timer == nil else { return }
        try? FileManager.default.createDirectory(
            at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        appendDebug("watcher start() reached; trusted=\(AXIsProcessTrusted())")
        // Poll rather than rely on AXObserver alone: a delivered banner takes
        // ~1s to enter the tree, and appended notifications reuse the existing
        // window WITHOUT firing kAXWindowCreated (reproduced by others across
        // five notifications: one event, four silent). Polling self-heals.
        // Timer(timeInterval:) + RunLoop.main.add did NOT fire in this app
        // (start() logged, sweeps never did). scheduledTimer installs on the
        // current runloop in default mode, which is what actually runs here.
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.sweep() }
        }
        // NEVER sweep synchronously here. AX queries block on the target
        // process, and start() can be reached during SwiftUI's App.init()
        // (before the runloop exists) — a synchronous sweep there deadlocked
        // the whole app inside dispatch_once, which also kept the timer from
        // ever firing. Let the first tick happen on the runloop.
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    // MARK: sweep

    private func sweep() {
        sweepCount += 1
        // The first few sweeps always report vitals: a silent watcher is
        // indistinguishable from a working one with nothing to report, and
        // AXIsProcessTrusted can read stale-true after a re-sign while the
        // actual queries fail.
        let vitals = sweepCount <= 3 || debugEnabled

        guard AXIsProcessTrusted() else {
            if vitals { appendDebug("NOT TRUSTED for accessibility") }
            return
        }
        guard let app = NSRunningApplication
            .runningApplications(withBundleIdentifier: Self.bundleID).first else {
            if vitals { appendDebug("notificationcenterui NOT RUNNING") }
            return
        }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        // Windows only: the application element's children also include the
        // AXMenuBar, whose "Apple/File/Edit" items are not notifications.
        guard let windows = copy(axApp, kAXWindowsAttribute) as? [AXUIElement] else {
            if vitals { appendDebug("windows query FAILED (AX denied despite trusted?)") }
            return
        }

        let now = Date()
        seen = seen.filter { now.timeIntervalSince($0.value) < 300 }

        var banners: [AXUIElement] = []
        var budget = Budget()
        for (i, window) in windows.enumerated() {
            if vitals {
                let sub = copy(window, kAXSubroleAttribute) as? String ?? "-"
                let ident = copy(window, "AXIdentifier") as? String ?? "-"
                let kids = (copy(window, kAXChildrenAttribute) as? [AXUIElement])?.count ?? -1
                appendDebug("  win[\(i)] subrole=\(sub) id=\(ident) kids=\(kids)")
            }
            findBanners(window, depth: 0, budget: &budget, into: &banners)
        }
        if vitals {
            appendDebug("sweep \(sweepCount): windows=\(windows.count) "
                        + "banners=\(banners.count) nodes=\(budget.nodes)")
        }

        var current: Set<String> = []
        var fresh: [(id: String, app: String, title: String, body: String)] = []
        for banner in banners {
            guard let f = fields(of: banner) else { continue }
            let id = "\(f.app)\u{1}\(f.title)\u{1}\(f.body)"
            current.insert(id)
            if !previous.contains(id) {
                fresh.append((id, f.app, f.title, f.body))
            }
            if debugEnabled {
                appendDebug("banner app=\(f.app) title=\(f.title) body=\(f.body.prefix(80))")
            }
        }

        // The first sweep must not replay what is already on screen as new.
        let priming = previous.isEmpty && !current.isEmpty
        previous = current
        if priming { return }

        for item in fresh where seen[item.id] == nil {
            seen[item.id] = now
            emit(app: item.app, title: item.title, body: item.body, at: now)
        }
    }

    /// The banner sits ~7 levels down and its window is full-screen sized,
    /// so neither depth nor geometry is a safe shortcut — search by subrole.
    ///
    /// STRICTLY BOUNDED. Every AX read is a blocking IPC round-trip to
    /// NotificationCenter, and an unbounded depth-14 walk across six
    /// full-screen windows never returned at all (verified by sampling the
    /// hung process). The budget and deadline keep a sweep well inside its
    /// 1 s tick even when the tree is huge or the target is slow.
    private struct Budget {
        var nodes = 0
        let maxNodes = 1500
        let deadline = Date().addingTimeInterval(0.6)
        var exhausted: Bool {
            mutating get {
                nodes += 1
                return nodes > maxNodes || Date() > deadline
            }
        }
    }

    /// Menus are the other half of this process's tree and can never contain
    /// a notification — pruning them removes most of the traversal cost.
    private static let prunedRoles: Set<String> = [
        "AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem",
    ]

    private func findBanners(_ el: AXUIElement, depth: Int,
                             budget: inout Budget, into: inout [AXUIElement]) {
        if depth > 12 || budget.exhausted { return }
        if let role = copy(el, kAXRoleAttribute) as? String,
           Self.prunedRoles.contains(role) { return }
        if let sub = copy(el, kAXSubroleAttribute) as? String,
           Self.bannerSubroles.contains(sub) {
            into.append(el)
            return
        }
        guard let kids = copy(el, kAXChildrenAttribute) as? [AXUIElement] else { return }
        for kid in kids.prefix(60) {
            findBanners(kid, depth: depth + 1, budget: &budget, into: &into)
        }
    }

    /// Each AXStaticText carries an AXIdentifier of "title"/"subtitle"/"body".
    /// Preferred over the legacy AXDescription blob, which comma-joins the
    /// fields and is undecodable when the text itself contains a comma.
    private func fields(of banner: AXUIElement) -> (app: String, title: String, body: String)? {
        var keyed: [String: String] = [:]
        var ordered: [String] = []
        collectFields(banner, depth: 0, keyed: &keyed, ordered: &ordered)

        let title = keyed["title"] ?? (ordered.count > 1 ? ordered[1] : "")
        let subtitle = keyed["subtitle"] ?? ""
        let bodyText = keyed["body"]
            ?? (ordered.count > 2 ? ordered[2...].joined(separator: " ") : "")
        let app = keyed["header"] ?? ordered.first ?? ""
        let body = [subtitle, bodyText].filter { !$0.isEmpty }.joined(separator: " ")
        if title.isEmpty && body.isEmpty { return nil }
        return (app, title, body)
    }

    private func collectFields(_ el: AXUIElement, depth: Int,
                               keyed: inout [String: String],
                               ordered: inout [String]) {
        if depth > 12 { return }
        if let value = copy(el, kAXValueAttribute) as? String {
            let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                if let id = copy(el, "AXIdentifier") as? String, keyed[id] == nil {
                    keyed[id] = text
                }
                if !ordered.contains(text) { ordered.append(text) }
            }
        }
        if let kids = copy(el, kAXChildrenAttribute) as? [AXUIElement] {
            for kid in kids {
                collectFields(kid, depth: depth + 1, keyed: &keyed, ordered: &ordered)
            }
        }
    }

    private func copy(_ el: AXUIElement, _ attr: String) -> CFTypeRef? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success else {
            return nil
        }
        return v
    }

    // MARK: output

    private func emit(app: String, title: String, body: String, at date: Date) {
        let payload: [String: Any] = [
            "at": date.timeIntervalSince1970,
            "app": app, "title": title, "body": body,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        append(line, to: outURL)
    }

    private func appendDebug(_ line: String) {
        append("\(Date().timeIntervalSince1970) \(line)\n",
               to: outURL.deletingLastPathComponent().appendingPathComponent("ax-debug.log"))
    }

    private func append(_ line: String, to url: URL) {
        if let h = try? FileHandle(forWritingTo: url) {
            try? h.seekToEnd()
            try? h.write(contentsOf: Data(line.utf8))
            try? h.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
    }
}
