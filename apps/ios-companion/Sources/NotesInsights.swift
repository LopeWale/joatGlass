import Foundation

/// The pure rules behind notes' inline AI insights (design 2026-11-23, Goal 7):
/// when a page is read and summarized, how far it zooms, which model reads
/// handwriting first, and what the page / list shows while that happens.
/// Foundation only, so tests/ios/test_notes_insights.swift can compile it alone.
enum NotesInsights {
    /// Handwriting OCR and AI summary run only after closing/leaving a modified
    /// page — never on a timer while the wearer is actively editing it.
    static let summarizesWhileEditing = false

    /// In the open page editor, only show the inline AI card when a previous
    /// summary already exists or the user explicitly started OCR/summary from
    /// the inspector — never an expanding placeholder banner while writing.
    static func shouldShowInlineEditorCard(
        hasSummary: Bool,
        hasKeyPoints: Bool,
        hasTodos: Bool,
        reading: Bool,
        summarizing: Bool
    ) -> Bool {
        hasSummary || hasKeyPoints || hasTodos || reading || summarizing
    }

    // MARK: Zoom

    /// Out to half of width-fit, in to at least 4×.
    static func zoomLimits(fit: Double) -> (min: Double, max: Double) {
        let lo = fit * 0.5
        return (lo, max(4.0, fit * 4.0))
    }

    static func clampZoom(_ zoom: Double, fit: Double) -> Double {
        let z = zoomLimits(fit: fit)
        return min(max(zoom, z.min), z.max)
    }

    /// Insets that keep a page smaller than the screen in its middle.
    static func centringInset(content: (width: Double, height: Double),
                              bounds: (width: Double, height: Double)) -> (left: Double, top: Double) {
        (max(0, (bounds.width - content.width) / 2), max(0, (bounds.height - content.height) / 2))
    }

    // MARK: Handwriting OCR

    enum Provider: String, Equatable { case claude, gemini }

    /// Claude reads messy handwriting best: first whenever its key is on the
    /// phone, whatever the chat engine; Gemini is the backup (or the only one).
    static func ocrOrder(hasClaude: Bool, hasGemini: Bool) -> [Provider] {
        (hasClaude ? [.claude] : []) + (hasGemini ? [.gemini] : [])
    }

    // MARK: What the page and the list show

    enum Status: Equatable {
        case readingHandwriting, summarizing, noKey, ready, stale, pending, empty

        var label: String {
            switch self {
            case .readingHandwriting: return "Reading handwriting…"
            case .summarizing: return "Summarizing…"
            case .noKey: return "No AI key on this phone: add a Claude or Gemini key in Home to get summaries."
            case .ready: return "AI summary"
            case .stale: return "Edited — summary updates when you close this page"
            case .pending: return "Summary updates when you close this page."
            case .empty: return "Write or type, and the summary is generated when you close the page."
            }
        }

        var inFlight: Bool { self == .readingHandwriting || self == .summarizing }
    }

    static func status(reading: Bool, summarizing: Bool, configured: Bool,
                       hasSummary: Bool, stale: Bool, hasContent: Bool) -> Status {
        if reading { return .readingHandwriting }
        if summarizing { return .summarizing }
        if !configured, !hasSummary || stale { return hasContent ? .noKey : .empty }
        if hasSummary { return stale ? .stale : .ready }
        return hasContent ? .pending : .empty
    }

    /// The one or two lines under a note in the Notes list.
    static func rowLine(summary: String?, keyPoints: [String], paragraphs: [String]) -> String? {
        func clean(_ s: String?) -> String? {
            let t = (s ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        return clean(summary) ?? keyPoints.lazy.compactMap(clean).first ?? paragraphs.lazy.compactMap(clean).first
    }

    // MARK: One run per note at a time

    /// A request while a note's run is in flight is remembered and run once
    /// afterwards, so the newest words are always the ones summarized.
    struct RunQueue {
        private var running = Set<String>()
        private var again = Set<String>()

        func isRunning(_ id: String) -> Bool { running.contains(id) }

        /// true: start now; false: one is running, it will rerun when done.
        mutating func begin(_ id: String) -> Bool {
            if running.contains(id) { again.insert(id); return false }
            running.insert(id)
            return true
        }

        /// true: a request came in meanwhile; run again.
        @discardableResult
        mutating func finish(_ id: String) -> Bool {
            running.remove(id)
            return again.remove(id) != nil
        }
    }
}
