import Foundation

/// A deterministic voice command's answer (services/commands.py
/// CommandReply). `activated` means the command switched the lens to a page,
/// which the Mac answers with a receipt over it instead of a conversation turn.
struct CommandReply {
    let text: String
    let activated: Bool
}

/// One link in the command chain the orchestrator runs ahead of any model.
protocol VoiceCommand: AnyObject {
    func handle(_ request: String) -> CommandReply?
}

/// Regex helpers for ports of the Mac's Python grammars.
enum Grammar {
    static func regex(_ pattern: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: pattern, options: [])
    }

    /// panel_commands._normalize: collapse spaces, drop terminal punctuation,
    /// lowercase.
    static func normalize(_ text: String) -> String {
        var t = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        while let last = t.last, ".,!?".contains(last) { t.removeLast() }
        return t.trimmingCharacters(in: .whitespaces).lowercased()
    }

    /// Whole-string match; returns the named groups that matched.
    static func match(_ re: NSRegularExpression, _ text: String, groups: [String] = []) -> [String: String]? {
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.firstMatch(in: text, range: range),
              m.range.location == 0, m.range.length == range.length else { return nil }
        var out: [String: String] = [:]
        for g in groups {
            let r = m.range(withName: g)
            if r.location != NSNotFound, let rr = Range(r, in: text) { out[g] = String(text[rr]) }
        }
        return out
    }

    // panel_commands.py number and duration words
    static let units: [(String, Int)] = [
        ("zero", 0), ("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5), ("six", 6),
        ("seven", 7), ("eight", 8), ("nine", 9), ("ten", 10), ("eleven", 11), ("twelve", 12),
        ("thirteen", 13), ("fourteen", 14), ("fifteen", 15), ("sixteen", 16), ("seventeen", 17),
        ("eighteen", 18), ("nineteen", 19),
    ]
    static let tens: [(String, Int)] = [
        ("twenty", 20), ("thirty", 30), ("forty", 40), ("fifty", 50), ("sixty", 60),
        ("seventy", 70), ("eighty", 80), ("ninety", 90),
    ]
    static let numberWords = (units + tens).map { $0.0 }.sorted { $0.count > $1.count }.joined(separator: "|")
    static let number = #"(?:\d{1,4}|(?:\#(numberWords))(?:[ -](?:\#(units.map { $0.0 }.joined(separator: "|"))))?)"#
    static let duration = #"(?:half an hour|(?:an|one) hour|\#(number) (?:minutes?|mins?|hours?)|\#(number))"#

    static func spokenNumber(_ text: String) -> Int? {
        let t = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "-", with: " ")
        if let n = Int(t) { return n }
        guard !t.isEmpty else { return nil }
        let u = Dictionary(uniqueKeysWithValues: units)
        let d = Dictionary(uniqueKeysWithValues: tens)
        var total = 0
        for word in t.split(separator: " ").map(String.init) {
            if let v = d[word] { total += v } else if let v = u[word] { total += v } else { return nil }
        }
        return total
    }

    /// panel_commands._duration_minutes: whole minutes; a bare number is minutes.
    static func durationMinutes(_ text: String) -> Int? {
        let t = text.trimmingCharacters(in: .whitespaces)
        if t == "half an hour" { return 30 }
        if t == "an hour" || t == "one hour" { return 60 }
        let re = regex("^(\(number))(?: (minutes?|mins?|hours?))?$")
        let range = NSRange(t.startIndex..., in: t)
        guard let m = re.firstMatch(in: t, range: range), let r1 = Range(m.range(at: 1), in: t),
              let value = spokenNumber(String(t[r1])) else { return nil }
        let unit = Range(m.range(at: 2), in: t).map { String(t[$0]) } ?? ""
        return unit.hasPrefix("hour") ? value * 60 : value
    }
}

/// Start, pause, resume, skip, reset, lengths, sessions, timers and "how
/// long is left": a port of panel_commands.py PomodoroCommands.
final class PomodoroCommands: VoiceCommand {
    private let pomodoro: Pomodoro
    private let panels: PanelPublisher

    init(pomodoro: Pomodoro, panels: PanelPublisher) {
        self.pomodoro = pomodoro
        self.panels = panels
    }

    private static let d = Grammar.duration
    private static let n = Grammar.number
    private static let kindWords = "focus|work|pomodoro|short break|long break|break"
    static let lengthKinds = ["focus": "focus", "work": "focus", "pomodoro": "focus",
                              "short break": "short_break", "break": "short_break", "long break": "long_break"]

    static let timerStart = Grammar.regex(
        #"^(?:(?:set|start|begin|run|make)(?: me)?(?: a| an)? (?:timer|countdown)(?: for| of)? (?<amount>\#(d))"#
        + #"|(?:set|start|begin|run|make)(?: me)?(?: a| an)? (?<amounttwo>\#(d)) (?:timer|countdown)"#
        + #"|(?:count ?down|time me for|time) (?<amountthree>\#(d)))$"#)
    static let setLength = Grammar.regex(
        #"^(?:set|change|make)(?: my| the)? (?<kind>\#(kindWords))(?: (?:time|length|timer|block|session))?"#
        + #"(?: to| for)? (?<amount>\#(d))$"#
        + #"|^(?<kindtwo>\#(kindWords)) (?:time|length) (?:to )?(?<amounttwo>\#(d))$"#)
    static let setSessions = Grammar.regex(
        #"^(?:(?:set|change|make)(?: my| the)? (?:sessions?|blocks?|rounds?|cycles?)(?: per set)?"#
        + #"(?: to)? (?<count>\#(n))"#
        + #"|(?:set|change|make)(?: my| the)? (?:number of |count of )(?:sessions?|blocks?|rounds?|cycles?)"#
        + #"(?: to)? (?<counttwo>\#(n))"#
        + #"|(?:do |use |have )?(?<countthree>\#(n)) (?:sessions?|blocks?|rounds?|focus blocks?)"#
        + #"(?: (?:per|a|in a|before (?:the|a) long break))?(?: set)?)$"#)
    static let start = Grammar.regex(#"^(?:start|begin|run)(?: a| my| the)? (?:pomodoro|focus(?: block| session| timer)?|timer)$"#)
    static let pause = Grammar.regex(#"^pause(?: the| my)? (?:pomodoro|timer|focus)$|^pause$"#)
    static let resume = Grammar.regex(#"^(?:resume|continue|unpause)(?: the| my)? (?:pomodoro|timer|focus)$|^resume$"#)
    static let skip = Grammar.regex(
        #"^skip(?: the| this| my)? (?:break|pomodoro|block|focus)$|^(?:end|finish)(?: the| this| my)? (?:break|block) (?:now|early)$"#)
    static let reset = Grammar.regex(#"^(?:stop|reset|cancel|end)(?: the| my)? (?:pomodoro|timer|focus(?: block| session)?)$"#)
    static let open = Grammar.regex(#"^(?:open|show)(?: me)? (?:the |my )?(?:pomodoro|timer|focus timer)$"#)
    static let status = Grammar.regex(
        #"^(?:how (?:long|much time) is left|how long (?:do i have )?left|time left|"#
        + #"how(?:'s| is) (?:the |my )?(?:pomodoro|timer)|pomodoro status|timer status|"#
        + #"how long is left on (?:the |my )?(?:pomodoro|timer))$"#)

    func handle(_ request: String) -> CommandReply? {
        let t = Grammar.normalize(request)
        if let g = Grammar.match(Self.timerStart, t, groups: ["amount", "amounttwo", "amountthree"]) {
            guard let raw = g["amount"] ?? g["amounttwo"] ?? g["amountthree"],
                  let minutes = Grammar.durationMinutes(raw) else { return nil }
            let reply = pomodoro.startTimer(minutes: minutes)
            panels.activate("pomodoro")
            return CommandReply(text: reply, activated: true)
        }
        if let g = Grammar.match(Self.setLength, t, groups: ["kind", "kindtwo", "amount", "amounttwo"]) {
            guard let raw = g["amount"] ?? g["amounttwo"], let minutes = Grammar.durationMinutes(raw),
                  let kindWord = g["kind"] ?? g["kindtwo"], let kind = Self.lengthKinds[kindWord] else { return nil }
            let reply = pomodoro.setLength(kind, minutes: minutes)
            panels.activate("pomodoro")
            return CommandReply(text: reply, activated: true)
        }
        if let g = Grammar.match(Self.setSessions, t, groups: ["count", "counttwo", "countthree"]) {
            guard let raw = g["count"] ?? g["counttwo"] ?? g["countthree"],
                  let count = Grammar.spokenNumber(raw) else { return nil }
            let reply = pomodoro.setSessions(count)
            panels.activate("pomodoro")
            return CommandReply(text: reply, activated: true)
        }
        if Grammar.match(Self.open, t) != nil {
            panels.activate("pomodoro")
            return CommandReply(text: "Opening the timer. " + pomodoro.statusText(), activated: true)
        }
        if Grammar.match(Self.start, t) != nil {
            let reply = pomodoro.start()
            panels.activate("pomodoro")
            return CommandReply(text: reply, activated: true)
        }
        if Grammar.match(Self.pause, t) != nil {
            if pomodoro.phase == "idle" { return t == "pause" ? nil : CommandReply(text: "Nothing is running.", activated: false) }
            return CommandReply(text: pomodoro.pause(), activated: false)
        }
        if Grammar.match(Self.resume, t) != nil {
            if pomodoro.phase == "idle" { return t == "resume" ? nil : CommandReply(text: pomodoro.start(), activated: false) }
            return CommandReply(text: pomodoro.resume(), activated: false)
        }
        if Grammar.match(Self.skip, t) != nil {
            if pomodoro.phase == "idle" { return CommandReply(text: "Nothing is running.", activated: false) }
            return CommandReply(text: pomodoro.skip(), activated: false)
        }
        if Grammar.match(Self.reset, t) != nil {
            if pomodoro.phase == "idle" { return CommandReply(text: "No pomodoro is running.", activated: false) }
            return CommandReply(text: pomodoro.reset(), activated: false)
        }
        if Grammar.match(Self.status, t) != nil {
            return CommandReply(text: pomodoro.statusText(), activated: false)
        }
        return nil
    }
}

/// "JOAT, speak your answers" / "JOAT, stop talking": spoken replies on or
/// off, kept across launches. The answer is on the lens either way.
final class SpokenRepliesCommand: VoiceCommand {
    private let voice: Voice

    init(voice: Voice) { self.voice = voice }

    static let on = Grammar.regex(
        #"^(?:(?:turn on|enable|start|switch on)(?: the| your)? (?:spoken replies|voice replies|spoken answers|voice|speaking|talking|reading (?:aloud|out loud))"#
        + #"|(?:speak|say|read)(?: your| the)? (?:answers|replies)(?: (?:aloud|out loud|in my ear))?|talk to me|voice on)$"#)
    static let off = Grammar.regex(
        #"^(?:(?:turn off|disable|switch off)(?: the| your)? (?:spoken replies|voice replies|spoken answers|voice|speaking|talking|reading (?:aloud|out loud))"#
        + #"|stop (?:talking|speaking|reading (?:aloud|out loud))|be quiet|(?:don['’]t|do not) (?:talk|speak)|mute(?: your)? voice|no voice|voice off)$"#)

    func handle(_ request: String) -> CommandReply? {
        let t = Grammar.normalize(request)
        if Grammar.match(Self.on, t) != nil {
            voice.spokenReplies = true
            return CommandReply(text: "Spoken replies are on, in your headphones.", activated: true)
        }
        if Grammar.match(Self.off, t) != nil {
            voice.spokenReplies = false
            return CommandReply(text: "Spoken replies are off. Answers stay on the lens.", activated: true)
        }
        return nil
    }
}
