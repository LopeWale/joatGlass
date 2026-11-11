import Foundation

/// AI cues while a QuickNote records (Even's Conversate cues; the wearer
/// picked all four kinds, 2026-10-29):
///   ANSWER      a question just asked that the prep material answers
///   SUGGESTION  a point to clarify or push back on, or a prep talking point
///               that fits now
///   CONCEPT     a term just used, in plain words
///   BIO         a person just named: a public figure, or one the prep
///               describes
/// Claude Haiku reads what was just said (with what came before, the prep
/// material and the cues already shown) and answers NONE or one cue. It is
/// asked when the talk pauses with something new said, or every 8 s of
/// steady talk, one request at a time; a cue for a note that has ended is
/// dropped.
final class Cues {
    struct Cue: Codable, Equatable {
        let kind: String
        let title: String
        let body: String
        let at: Date

        var label: String {
            switch kind {
            case "ANSWER": return "Answer"
            case "SUGGESTION": return "Suggestion"
            case "CONCEPT": return "Concept"
            case "BIO": return "Bio"
            default: return kind.capitalized
            }
        }
    }

    static let kinds: Set<String> = ["ANSWER", "SUGGESTION", "CONCEPT", "BIO"]
    static let enabledKey = "joat.cues.enabled"
    /// Steady talk: ask after this many new words, at most this often.
    static let steadyWords = 14
    static let minGap: TimeInterval = 8
    /// A pause after a few new words (a short question) asks too.
    static let pauseWords = 4
    static let pause: TimeInterval = 2.5
    static let titleChars = 28
    static let bodyChars = 120

    private let claude: ClaudeLink
    private(set) var given: [Cue] = []
    /// The prep note in use (picked in JOAT Brain or on the glasses), read
    /// at every request: talking points that land mid-note are used.
    weak var prep: Prep?
    /// The note started with "Skip & start": no prep note for it.
    private(set) var usesPrep = true
    /// The prep note this note's cues answer from.
    var prepNote: Prep.Note? { usesPrep ? prep?.active : nil }
    private var transcript = ""
    private var askedUpTo = 0
    private var lastAsk = Date.distantPast
    private var inFlight = false
    private var generation = 0
    private var pauseTimer: DispatchWorkItem?
    private var running = false

    /// A cue for the open note, on the main queue.
    var onCue: ((Cue) -> Void)?

    init(claude: ClaudeLink) { self.claude = claude }

    var enabled: Bool {
        UserDefaults.standard.object(forKey: Self.enabledKey) as? Bool ?? true
    }
    var available: Bool { claude.configured }

    /// A note opened: a clean slate, with the prep note it uses (none: Skip & start).
    func begin(usePrep: Bool = true) {
        usesPrep = usePrep
        generation += 1
        given = []
        transcript = ""
        askedUpTo = 0
        lastAsk = .distantPast
        inFlight = false
        running = true
        pauseTimer?.cancel()
        Log.add("cues: note opened\(prepNote.map { " with prep note “\($0.title)” (\($0.items.count) items)" } ?? " without prep")\(enabled ? "" : " (cues off)")")
    }

    #if DEBUG
    /// DebugSeed: a cue as if Claude had given it.
    func debugCue(_ cue: Cue) {
        given.append(cue)
        onCue?(cue)
    }
    #endif

    /// The note was saved or discarded.
    func end() {
        generation += 1
        running = false
        inFlight = false
        pauseTimer?.cancel()
    }

    /// Words that went into the note.
    func heard(_ text: String) {
        guard running else { return }
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return }
        transcript += (transcript.isEmpty ? "" : " ") + words
        pauseTimer?.cancel()
        let t = DispatchWorkItem { [weak self] in self?.consider(paused: true) }
        pauseTimer = t
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.pause, execute: t)
        consider(paused: false)
    }

    private var newPart: String {
        let start = transcript.index(transcript.startIndex, offsetBy: min(askedUpTo, transcript.count))
        return String(transcript[start...])
    }

    private func consider(paused: Bool) {
        guard running, enabled, available, !inFlight else { return }
        let count = newPart.split(whereSeparator: { $0.isWhitespace }).count
        let wait = Self.minGap - Date().timeIntervalSince(lastAsk)
        if paused, count >= Self.pauseWords, wait <= 0 { ask(); return }
        guard count >= Self.steadyWords else { return }
        if wait <= 0 { ask(); return }
        let g = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in
            guard let self, self.generation == g else { return }
            self.consider(paused: false)
        }
    }

    private func ask() {
        let said = newPart
        let earlier = String(transcript.prefix(min(askedUpTo, transcript.count)).suffix(1_500))
        askedUpTo = transcript.count
        lastAsk = Date()
        inFlight = true
        let g = generation
        let started = Date()
        claude.cue(system: Self.system(prep: prepNote), text: Self.request(earlier: earlier, said: String(said.suffix(900)), given: given)) { [weak self] result in
            guard let self, self.generation == g else { return }
            self.inFlight = false
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            switch result {
            case .success(let text):
                guard let cue = Self.parse(text) else { Log.add("cues: none (\(ms) ms)"); break }
                guard !self.given.contains(where: { $0.title.lowercased() == cue.title.lowercased() }) else {
                    Log.add("cues: repeat dropped: \(cue.title)")
                    break
                }
                self.given.append(cue)
                Log.add("cues: \(cue.kind) “\(cue.title)” in \(ms) ms")
                self.onCue?(cue)
            case .failure(let error):
                Log.add("cues: request failed: \(error)")
            }
            // Words that arrived while asking are judged next.
            self.consider(paused: false)
        }
    }

    // MARK: - The request

    static let instructions = """
    You help the wearer of smart glasses during a live conversation they are recording. \
    Speech recognition writes what is said (anyone in the room; no speaker names; it makes mistakes). \
    Decide whether ONE short cue on their lens would help right now, because of what was JUST SAID.

    Kinds of cue:
    ANSWER: someone just asked a question, or raised a fact, that the PREP MATERIAL answers. Use only the prep material. Never answer from general knowledge.
    SUGGESTION: a useful nudge for the wearer: something to clarify, a claim to question, a good question to ask, or a talking point from the prep material that fits this moment.
    CONCEPT: a technical term, acronym, product or idea was just used that the wearer may not know. Explain it plainly.
    BIO: a person was just named. Say who they are, only if they are a well-known public figure you are sure about, or the prep material describes them. Never guess about private people.

    Rules:
    - Answer NONE unless a cue is clearly useful now. Most of the time the answer is NONE.
    - Never repeat or rephrase a cue listed under ALREADY SHOWN.
    - Don't cue on small talk, filler, or words the recognizer garbled.
    - Answer with exactly one line and nothing else: NONE, or KIND | title | text
      title: at most 28 characters. text: at most 120 characters, plain words, no markdown.
    """

    static func system(prep: Prep.Note?) -> [[String: Any]] {
        var blocks: [[String: Any]] = [["type": "text", "text": instructions]]
        if let prep, !prep.items.isEmpty {
            var material = "PREP MATERIAL: \(prep.title)\n"
            if !prep.points.isEmpty { material += "Talking points:\n" + prep.points.map { "- \($0)" }.joined(separator: "\n") + "\n\n" }
            // Every item in the prep note (Even: the files in a Prep Note all count).
            material += prep.material
            // The same prefix on every request of the note: cached by Claude.
            blocks.append(["type": "text", "text": material, "cache_control": ["type": "ephemeral"]])
        } else {
            blocks.append(["type": "text", "text": "There is no prep material for this conversation, so never give an ANSWER cue."])
        }
        return blocks
    }

    static func request(earlier: String, said: String, given: [Cue]) -> String {
        var s = ""
        if !earlier.isEmpty { s += "EARLIER:\n\(earlier)\n\n" }
        s += "JUST SAID:\n\(said)\n\n"
        s += "ALREADY SHOWN:\n" + (given.isEmpty ? "(none)" : given.suffix(12).map { "- \($0.kind): \($0.title)" }.joined(separator: "\n"))
        return s
    }

    /// "KIND | title | text", or nil for NONE and anything malformed.
    static func parse(_ reply: String, at: Date = Date()) -> Cue? {
        let line = reply.split(whereSeparator: { $0.isNewline }).first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
        guard !line.uppercased().hasPrefix("NONE") else { return nil }
        let parts = line.split(separator: "|", maxSplits: 2).map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 3 else { return nil }
        let kind = parts[0].uppercased().trimmingCharacters(in: CharacterSet(charactersIn: "*: "))
        guard kinds.contains(kind), !parts[1].isEmpty, !parts[2].isEmpty else { return nil }
        let clean = { (s: String) in s.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "") }
        return Cue(kind: kind, title: Hud.shorten(clean(parts[1]), limit: titleChars),
                   body: Hud.shorten(clean(parts[2]), limit: bodyChars), at: at)
    }
}
