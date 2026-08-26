import AppKit
import ApplicationServices
import Foundation

/// Reads OTHER apps' notifications via the Accessibility API and hands them
/// to the Python engine (docs/MAC_APP.md §Notification source).
///
/// WHY NOT the Biome store: it carries only bundle-id + an app-chosen
/// identifier + timestamp — for Messages that identifier is an opaque UUID,
/// so the content a triage card needs does not exist there, at any latency.
/// The rendered banner DOES carry title/subtitle/body, and the AX API can
/// read it. There is no sanctioned API for this on any stack; Apple's
/// position is that reading other apps' notifications is unsupported.
///
/// Robustness stance mirrors the Biome parser: do NOT depend on Apple's
/// private view hierarchy. Walk whatever tree exists and collect its static
/// text in visual order; a layout change degrades to fewer fields, never a
/// crash. Read-only — the observer never actions or dismisses a banner.
///
/// Transport to the engine is an append-only JSONL file rather than a
/// socket: either side can restart independently, the handoff survives, and
/// it adds no network surface.
@MainActor
final class NotificationWatcher {
    static let shared = NotificationWatcher()

    /// Notification Center's UI process — the banners' owner.
    private static let bundleID = "com.apple.notificationcenterui"

    /// A window with more static texts than this is the Notification Center
    /// LIST (the user opened the panel), not a live banner. Ignoring it stops
    /// a backlog flood from being replayed as fresh cards.
    private static let maxTextsPerBanner = 8

    private var timer: Timer?
    private var seen: [String: Date] = [:]
    private let outURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JOAT/notifications.jsonl")

    var isTrusted: Bool { AXIsProcessTrusted() }

    /// Ask once, with the system prompt. The user must then relaunch — TCC
    /// applies Accessibility to the running process only after restart.
    func requestAccess() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    }

    func start() {
        guard timer == nil else { return }
        try? FileManager.default.createDirectory(
            at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        // Banners live ~5 s, so a 1 s sweep cannot miss one. Polling beats an
        // AXObserver here: observer registration dies with each respawn of
        // the notificationcenterui process, and a missed re-registration is
        // silent, whereas a poll self-heals on the next tick.
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.sweep() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    // MARK: sweep

    private func sweep() {
        guard AXIsProcessTrusted() else { return }
        guard let app = NSRunningApplication
            .runningApplications(withBundleIdentifier: Self.bundleID).first else { return }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        guard let windows = copy(axApp, kAXWindowsAttribute) as? [AXUIElement] else { return }

        let now = Date()
        seen = seen.filter { now.timeIntervalSince($0.value) < 300 }  // 5 min memory

        for window in windows {
            var texts: [String] = []
            collectText(window, into: &texts, depth: 0)
            guard texts.count >= 1, texts.count <= Self.maxTextsPerBanner else { continue }
            let key = texts.joined(separator: "\u{1}")
            if seen[key] != nil { continue }
            seen[key] = now
            emit(texts: texts, at: now)
        }
    }

    /// Depth-first, order-preserving text harvest. AXValue first (static
    /// text), then AXTitle/AXDescription — banners have been observed using
    /// each depending on the posting app.
    private func collectText(_ el: AXUIElement, into out: inout [String], depth: Int) {
        if depth > 12 || out.count > 32 { return }
        for key in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
            if let s = copy(el, key) as? String {
                let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
                if !t.isEmpty, !out.contains(t) { out.append(t) }
            }
        }
        if let kids = copy(el, kAXChildrenAttribute) as? [AXUIElement] {
            for kid in kids { collectText(kid, into: &out, depth: depth + 1) }
        }
    }

    private func copy(_ el: AXUIElement, _ attr: String) -> CFTypeRef? {
        var v: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success else {
            return nil
        }
        return v
    }

    // MARK: emit

    private func emit(texts: [String], at date: Date) {
        // Banner convention: [app name, title, body…] — but apps vary, so
        // keep every line and let the engine's triage see the whole thing.
        let payload: [String: Any] = [
            "at": date.timeIntervalSince1970,
            "app": texts.first ?? "",
            "title": texts.count > 1 ? texts[1] : (texts.first ?? ""),
            "body": texts.count > 2 ? texts[2...].joined(separator: " ") : "",
            "lines": texts,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        if let h = try? FileHandle(forWritingTo: outURL) {
            try? h.seekToEnd()
            try? h.write(contentsOf: Data(line.utf8))
            try? h.close()
        } else {
            try? Data(line.utf8).write(to: outURL)
        }
    }
}
