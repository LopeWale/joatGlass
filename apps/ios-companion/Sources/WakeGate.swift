import Foundation

/// "JOAT" / "Hey JOAT" — wake-word gate for JOAT Brain on the iPhone.
///
/// Matches exact "JOAT"/"Hey JOAT" phrases anywhere in an utterance, plus
/// sentence-opening phonetic transcriptions produced by Apple's on-device
/// `DictationTranscriber` ("Hey Joe", "Hey Jo", "Jot", "Jolt", and
/// request-headed "Joe, can you…" / "Joe, what's…") while ignoring casual
/// bystander speech such as "Joe said he'd be late" or "Joe, pass the salt".
enum WakeGate {
    /// Longest first, so "hey joat" strips before "joat" matches.
    static let words: [[String]] = [
        "hey joat", "hey jote",
        "hey joke", "hey jokes", "hey choke", "hey jot", "hey jolt",
        "joat", "jote",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// Sentence-opening phonetic variants written by Apple's `DictationTranscriber`
    /// when the wearer says "Hey JOAT" or "JOAT, …".
    static let anchored: [[String]] = [
        "hey joe", "hey jo", "jot", "jolt",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// Assistant request/question lead tokens that distinguish Apple Dictation's
    /// `"Joe, can you tell me my location"` / `"Joe, what's on my calendar"`
    /// (wearer saying `"JOAT, …"`, hardware log 2026-10-23) from casual speech
    /// like `"Joe said he'd be late"` or `"Joe, pass the salt"`.
    private static let joeRequestLeads: Set<String> = [
        "what", "whats", "where", "wheres", "when", "who", "how", "why",
        "can", "could", "would", "will", "please",
        "open", "show", "read", "check", "tell", "remind", "remember",
        "note", "take", "send", "text", "message", "email", "reply",
        "start", "stop", "pause", "resume", "set", "skip", "reset",
        "navigate", "drive", "walk", "bike", "cycle", "search", "find",
        "look", "summarize", "clear", "do", "does", "give", "list",
        "add", "complete", "mark", "ask",
    ]

    /// How deep into an utterance a leading wake phrase may start.
    static let plausibleLeadTokens = 2

    /// nil = not addressed to JOAT; "" = the name alone; else the request.
    static func strip(_ text: String) -> String? {
        strip(text, maxStart: nil)
    }

    /// `strip`, restricted to utterances where the wake word opens the sentence.
    static func stripLeading(_ text: String) -> String? {
        strip(text, maxStart: plausibleLeadTokens)
    }

    private static func strip(_ text: String, maxStart: Int?) -> String? {
        let tokens = Self.tokens(text)
        guard !tokens.isEmpty else { return nil }
        let candidates = words.map { ($0, false) } + anchored.map { ($0, true) }
        for (wake, atStart) in candidates {
            let n = wake.count
            guard tokens.count >= n else { continue }
            var last = atStart ? 0 : tokens.count - n
            if let maxStart { last = min(last, maxStart) }
            guard last >= 0 else { continue }
            for i in 0...last where Array(tokens[i..<(i + n)]) == wake {
                return removeWakeTokens(wake, from: text)
            }
        }
        // Sentence-opening "Joe <request-lead> …" (Apple Dictation's transcription of "JOAT, …"):
        if tokens.first == "joe", tokens.count >= 2, joeRequestLeads.contains(tokens[1]) {
            return removeWakeTokens(["joe"], from: text)
        }
        return nil
    }

    private static func removeWakeTokens(_ wake: [String], from text: String) -> String {
        let pattern = "\\b" + wake.map { NSRegularExpression.escapedPattern(for: $0) }
            .joined(separator: "[^A-Za-z0-9]+") + "\\b"
        var query = text.replacingOccurrences(
            of: pattern,
            with: " ",
            options: [.regularExpression, .caseInsensitive],
            range: text.range(of: pattern, options: [.regularExpression, .caseInsensitive])
        )
        query = query.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        query = query.replacingOccurrences(of: "\\s+([,.!?])", with: "$1", options: .regularExpression)
        query = query.replacingOccurrences(of: ",+([.!?])", with: "$1", options: .regularExpression)
        query = query.replacingOccurrences(of: "^[\\s,.\\-—]+", with: "", options: .regularExpression)
        while let last = query.last, " ,-—".contains(last) { query.removeLast() }
        return query
    }

    private static func tokens(_ text: String) -> [String] {
        let lower = text.lowercased()
        let regex = try! NSRegularExpression(pattern: "[a-z0-9]+")
        return regex.matches(in: lower, range: NSRange(lower.startIndex..., in: lower)).compactMap {
            Range($0.range, in: lower).map { String(lower[$0]) }
        }
    }
}
