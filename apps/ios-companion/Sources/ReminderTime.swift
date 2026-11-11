import Foundation

/// "call mom tomorrow at 3pm" → title "Call mom", due Fri 15:00.
///
/// Apple's NSDataDetector reads absolute and day phrases ("tomorrow at 3pm",
/// "Friday at 9", "October 30 at 2:30 pm", "next Monday morning", "tonight
/// at 8"). Measured on 2026-10-22 it misses relative times ("in 20
/// minutes", "in an hour") and a bare hour ("at 5"), returns noon for a day
/// with no time, and returns today for a time already past. Those four are
/// handled here.
enum ReminderTime {
    struct Parsed {
        let title: String
        let due: Date
        /// The wearer said a time (false: the 09:00 default was used).
        let explicitTime: Bool
    }

    /// The Mac's default for a reminder with a day but no time
    /// (note_insights._due_date: 09:00 local).
    static let defaultHour = 9

    static let relative = Grammar.regex(
        #"\b(?:in|after) (?<amount>half an hour|an hour|a minute|"#
        + Grammar.number + #" (?:minutes?|mins?|hours?))\b"#)
    static let bareHour = Grammar.regex(
        #"\bat (?<h>\d{1,2})(?::(?<m>\d{2}))?\b(?!\s*(?:am|pm|a\.m\.|p\.m\.|o'clock))"#)
    static let timeWords = Grammar.regex(
        #"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|\bnoon\b|\bmidnight\b|\bmorning\b|\bafternoon\b|\bevening\b|\btonight\b|\bat \d{1,2}\b|o'clock"#)
    static let dateWords = Grammar.regex(
        #"\b(?:today|tomorrow|tonight|next|this|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{1,2}/\d{1,2})\b"#)

    /// `rest` is what followed "remind me (to)". nil when no time was said.
    static func parse(_ rest: String, now: Date = Date(), calendar: Calendar = .current) -> Parsed? {
        let lower = rest.lowercased()

        // 1. Relative: "in 20 minutes", "in an hour".
        if let m = relative.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
           let r = Range(m.range(withName: "amount"), in: lower) {
            let amount = String(lower[r])
            let minutes = amount == "a minute" ? 1 : Grammar.durationMinutes(amount)
            if let minutes, minutes > 0, let whole = Range(m.range, in: rest) {
                return Parsed(title: cleanTitle(rest.replacingCharacters(in: whole, with: " ")),
                              due: now.addingTimeInterval(Double(minutes) * 60), explicitTime: true)
            }
        }

        // 2. Apple's detector: days, dates and clock times.
        if let det = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.date.rawValue),
           let m = det.firstMatch(in: rest, range: NSRange(rest.startIndex..., in: rest)),
           var due = m.date, let whole = Range(m.range, in: rest) {
            let said = rest[whole].lowercased()
            let hasTime = timeWords.firstMatch(in: said, range: NSRange(said.startIndex..., in: said)) != nil
            let hasDate = dateWords.firstMatch(in: said, range: NSRange(said.startIndex..., in: said)) != nil
            if !hasTime {
                due = calendar.date(bySettingHour: defaultHour, minute: 0, second: 0, of: due) ?? due
            }
            if due <= now && !hasDate {
                due = calendar.date(byAdding: .day, value: 1, to: due) ?? due
            }
            guard due > now else { return nil }   // "today" with 09:00 already gone: ask
            return Parsed(title: cleanTitle(rest.replacingCharacters(in: whole, with: " ")),
                          due: due, explicitTime: hasTime)
        }

        // 3. A bare hour, "at 5": 1-6 read as afternoon, 7-11 the next one
        //    coming, 12 as noon. The confirmation shows the time chosen.
        if let m = bareHour.firstMatch(in: lower, range: NSRange(lower.startIndex..., in: lower)),
           let hr = Range(m.range(withName: "h"), in: lower), var hour = Int(lower[hr]), (1...12).contains(hour) {
            let minute = Range(m.range(withName: "m"), in: lower).flatMap { Int(lower[$0]) } ?? 0
            guard minute < 60 else { return nil }
            if (1...6).contains(hour) { hour += 12 }
            func at(_ h: Int, dayOffset: Int) -> Date? {
                let day = calendar.date(byAdding: .day, value: dayOffset, to: now)!
                return calendar.date(bySettingHour: h, minute: minute, second: 0, of: day)
            }
            var due = at(hour, dayOffset: 0)!
            if due <= now, (7...11).contains(hour), let pm = at(hour + 12, dayOffset: 0), pm > now { due = pm }
            if due <= now { due = at(hour, dayOffset: 1)! }
            guard let whole = Range(m.range, in: rest) else { return nil }
            return Parsed(title: cleanTitle(rest.replacingCharacters(in: whole, with: " ")),
                          due: due, explicitTime: true)
        }
        return nil
    }

    /// What is left once the time is gone: "to call mom  " → "Call mom".
    static func cleanTitle(_ text: String) -> String {
        var t = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        // "at 5pm to call mom": the detector takes "5pm" and leaves "at".
        let leading = ["to ", "that ", "about ", "for ", "at ", "on ", "by "]
        let trailing = [" at", " on", " by", " in", " for", " to", " and", ","]
        var changed = true
        while changed {
            changed = false
            t = t.trimmingCharacters(in: CharacterSet(charactersIn: " ,.;:!?"))
            for l in leading where t.lowercased().hasPrefix(l) { t = String(t.dropFirst(l.count)); changed = true }
            for s in trailing where t.lowercased().hasSuffix(s) { t = String(t.dropLast(s.count)); changed = true }
        }
        guard let first = t.first else { return t }
        return first.uppercased() + t.dropFirst()
    }
}
