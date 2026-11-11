import Foundation

/// What the Mac's HudWriter (services/hud.py) and session cards
/// (channels/cards.py Card.render, session._paint) put on the lens, as
/// downlinks the plugin already renders.
final class Hud {
    private let link: PluginLink
    init(link: PluginLink) { self.link = link }

    /// hud.py MAX_OVERLAY_CHARS.
    static let maxOverlayChars = 512
    /// cards.py HUD_LINE_CHARS.
    static let cardLineChars = 46

    /// A compact acknowledgement over the current page (HudWriter.receipt),
    /// then settled for as long as it takes to read (HudWriter.reading_ms).
    func receipt(_ text: String) {
        link.send(["kind": "assistant.overlay", "operation": "replace",
                   "text": String(text.prefix(Self.maxOverlayChars)),
                   "presentation": "receipt", "answersWearer": true])
        link.send(["kind": "assistant.overlay", "operation": "settle",
                   "dismissAfterMs": Self.readingMs(text)])
    }

    /// hud.py reading_ms: 175 wpm at ~5.5 chars a word, plus 1.5 s, in 6-15 s.
    static func readingMs(_ text: String) -> Int {
        let words = Double(text.count) / 5.5
        let needed = Int(words / 175 * 60_000) + 1_500
        return max(6_000, min(15_000, needed))
    }

    /// cards.py shorten: trim on a word boundary, never mid-word.
    static func shorten(_ text: String, limit: Int = cardLineChars) -> String {
        let clean = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard clean.count > limit else { return clean }
        let head = String(clean.prefix(limit))
        var cut = head
        if let space = head.lastIndex(of: " ") { cut = String(head[..<space]) }
        if cut.isEmpty { cut = head }
        while let last = cut.last, ",.;:".contains(last) { cut.removeLast() }
        return cut + "…"
    }
}
