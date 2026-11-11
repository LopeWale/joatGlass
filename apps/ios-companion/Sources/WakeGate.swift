import Foundation

/// "JOAT" / "Hey JOAT" — the same words the Mac answers to.
///
/// SOURCE OF TRUTH: `apps/mac/joat/services/orchestrator.py` `WAKE_WORDS`.
/// This is a spike-time copy of the exact-match part of
/// `VoiceOrchestrator._strip_wake_word` (the fuzzy fallback is not ported).
/// It must move to one shared file before the phone brain stops being a lab
/// experiment; until then a change to the Mac list must be made here too.
enum WakeGate {
    /// Longest first, so "hey joat" strips before "joat" matches.
    static let words: [[String]] = [
        "hey joat", "hey jote",
        "hey joke", "hey jokes", "hey choke", "hey jot", "hey jolt",
        "joat", "jote",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// Phone only: what Apple's dictation writes for "Hey JOAT" in the
    /// wearer's voice that the Mac's Whisper never did ("hey Joe take me to
    /// …", "Joe, can you tell me my location" on 2026-10-23). These are an
    /// ordinary name, so like the Mac's anchored wake words they count only
    /// when they open the sentence: "I told Joe" is not addressed.
    static let anchored: [[String]] = [
        "hey joe", "hey jo", "joe", "jo",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// nil = not addressed to JOAT; "" = the name alone; else the request.
    static func strip(_ text: String) -> String? {
        let tokens = Self.tokens(text)
        guard !tokens.isEmpty else { return nil }
        let candidates = words.map { ($0, false) } + anchored.map { ($0, true) }
        for (wake, atStart) in candidates {
            let n = wake.count
            guard tokens.count >= n else { continue }
            for i in 0...(atStart ? 0 : tokens.count - n) where Array(tokens[i..<(i + n)]) == wake {
                // Rebuild from the ORIGINAL text minus the wake phrase.
                let pattern = "\\b" + wake.map { NSRegularExpression.escapedPattern(for: $0) }
                    .joined(separator: "[^A-Za-z0-9]+") + "\\b"
                var query = text.replacingOccurrences(of: pattern, with: " ",
                                                      options: [.regularExpression, .caseInsensitive],
                                                      range: text.range(of: pattern, options: [.regularExpression, .caseInsensitive]))
                query = query.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
                query = query.replacingOccurrences(of: "\\s+([,.!?])", with: "$1", options: .regularExpression)
                query = query.replacingOccurrences(of: ",+([.!?])", with: "$1", options: .regularExpression)
                query = query.replacingOccurrences(of: "^[\\s,.\\-—]+", with: "", options: .regularExpression)
                while let last = query.last, " ,-—".contains(last) { query.removeLast() }
                return query
            }
        }
        return nil
    }

    private static func tokens(_ text: String) -> [String] {
        let lower = text.lowercased()
        let regex = try! NSRegularExpression(pattern: "[a-z0-9]+")
        return regex.matches(in: lower, range: NSRange(lower.startIndex..., in: lower)).compactMap {
            Range($0.range, in: lower).map { String(lower[$0]) }
        }
    }
}
