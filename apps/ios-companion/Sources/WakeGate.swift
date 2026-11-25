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
        "hey joat", "hey jote", "hey joats", "hey jode",
        "hey joke", "hey jokes", "hey choke", "hey jot", "hey jolt",
        "joat", "jote", "joats", "jode",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// Sentence-opening phonetic variants written by Apple's `DictationTranscriber`
    /// when the wearer says "Hey JOAT" or "JOAT, …".
    static let anchored: [[String]] = [
        "hey joe", "hey jo", "jot", "jolt",
    ].sorted { $0.count > $1.count }.map { $0.split(separator: " ").map(String.init) }

    /// Standalone 1-word phonetic renderings when the wearer calls "JOAT" / "JOAT?"
    /// alone (or when `progressiveShortDictation` finalizes the name before a brief pause).
    private static let standaloneCallTokens: Set<String> = [
        "joat", "jote", "joats", "jode", "joe", "jo", "jot", "jolt", "joke", "jokes",
    ]

    /// Brief spoken hesitation or polite interjections that may precede a wake phrase.
    private static let hesitationTokens: Set<String> = [
        "um", "uh", "umm", "er", "ah", "oh", "okay", "ok", "yo", "hi", "hey", "so", "well",
    ]

    /// Assistant request/question lead tokens that distinguish Apple Dictation's
    /// `"Joe, can you tell me my location"` / `"Joe, what's on my calendar"` / `"Joe, is…"`
    /// from casual bystander speech like `"Joe said he'd be late"` or `"Joe, pass the salt"`.
    private static let joeRequestLeads: Set<String> = [
        "what", "whats", "where", "wheres", "when", "whens", "who", "whos", "how", "hows", "why", "which",
        "is", "are", "am", "can", "could", "would", "will", "should", "do", "does", "did", "please",
        "i", "im", "ive", "id", "let", "lets",
        "open", "show", "read", "check", "tell", "remind", "remember",
        "note", "take", "send", "text", "message", "email", "reply", "draft", "write", "call",
        "start", "stop", "pause", "resume", "set", "skip", "reset", "turn", "switch",
        "navigate", "drive", "walk", "bike", "cycle", "search", "find", "go", "head", "route", "directions",
        "look", "summarize", "clear", "give", "list", "get", "make", "create", "save", "delete", "remove",
        "add", "complete", "mark", "ask", "cancel", "close", "exit", "dismiss", "help", "run", "launch",
        "update", "change", "schedule", "weather", "time", "date",
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

        // Count leading hesitation words ("Um, Hey Joe…", "Okay, Joe, …") up to plausibleLeadTokens.
        var leadOffset = 0
        while leadOffset < min(plausibleLeadTokens, tokens.count - 1),
              hesitationTokens.contains(tokens[leadOffset]) {
            leadOffset += 1
        }

        // 1. Standalone call ("JOAT", "Joe.", "Jo?", "Joke?", "Okay, Joe.") -> "" ("Yes?"):
        let tailCount = tokens.count - leadOffset
        if tailCount == 1, standaloneCallTokens.contains(tokens[leadOffset]) {
            return ""
        }

        // 2. Exact & anchored multi-token / unambiguous wake words:
        let candidates = words.map { ($0, false) } + anchored.map { ($0, true) }
        for (wake, atStart) in candidates {
            let n = wake.count
            guard tokens.count >= n else { continue }
            var last = atStart ? min(leadOffset, tokens.count - n) : tokens.count - n
            if let maxStart { last = min(last, maxStart) }
            guard last >= 0 else { continue }
            for i in 0...last where Array(tokens[i..<(i + n)]) == wake {
                if i > 0, atStart {
                    return removeLeadingTokens(count: i + n, wake: wake, from: text)
                }
                return removeWakeTokens(wake, from: text)
            }
        }

        // 3. Sentence-opening "Joe/Jo <request-lead> …" (with optional hesitation lead-in):
        for idx in 0...leadOffset where idx + 1 < tokens.count {
            let head = tokens[idx]
            if (head == "joe" || head == "jo" || head == "joke"),
               joeRequestLeads.contains(tokens[idx + 1]) {
                return removeLeadingTokens(count: idx + 1, wake: [head], from: text)
            }
        }
        return nil
    }

    private static func removeLeadingTokens(count: Int, wake: [String], from text: String) -> String {
        // Strip leading hesitation + wake tokens from the front of `text`, preserving casing/punctuation of the query.
        let nsText = text as NSString
        let wordRe = try! NSRegularExpression(pattern: "[A-Za-z0-9'’]+")
        let matches = wordRe.matches(in: text, range: NSRange(location: 0, length: nsText.length))
        // Note: contractions like "what's" are 1 match in wordRe vs 2 in `[a-z0-9]+`, whereas hesitation
        // and wake words ("um", "okay", "hey", "joe") have no apostrophes (1-to-1 match).
        if count <= matches.count {
            let cutEnd = matches[count - 1].range.location + matches[count - 1].range.length
            var query = nsText.substring(from: cutEnd)
            query = query.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            query = query.replacingOccurrences(of: "^[\\s,.:;!?'’\\-—]+", with: "", options: .regularExpression)
            while let last = query.last, " ,-—".contains(last) { query.removeLast() }
            return query
        }
        return removeWakeTokens(wake, from: text)
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
