import Foundation
import Contacts
import FoundationModels

/// Who the wearer means when they say who to text (2026-10-30). The wearer:
/// a name had to be said exactly ("my wife" is saved as "Honey Bun"), and
/// a number that is not in Contacts did not work, so his own shortcut asked
/// Apple Intelligence to pick the contact. The same here, before the glasses
/// ask "Send to Honey Bun?":
///   1. a number, said or typed ("four oh five, five five five, …");
///   2. an email address;
///   3. one exact name or nickname;
///   4. Apple's on-device model picks from the contact list, else Claude.
/// Nothing is sent before the glasses show the match and the wearer taps yes.
enum Recipients {
    struct Match {
        let label: String
        let handle: String
    }

    // MARK: - Numbers

    private static let digitWords: [String: String] = [
        "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "to": "2", "too": "2", "three": "3",
        "four": "4", "for": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    ]

    /// "405-555-1234", "+1 (405) 555 1234", "four oh five five five five one
    /// two three four", "double five": the digits, or nil when anything else
    /// was said. Seven digits at least.
    static func spokenNumber(_ said: String) -> String? {
        var out = ""
        var repeatNext = 1
        let tokens = said.lowercased()
            .replacingOccurrences(of: "+", with: " plus ")
            .components(separatedBy: CharacterSet(charactersIn: " ,.-()/\t"))
            .filter { !$0.isEmpty }
        guard !tokens.isEmpty else { return nil }
        for token in tokens {
            if token == "plus" { if out.isEmpty { out = "+" }; continue }
            if token == "double" { repeatNext = 2; continue }
            if token == "triple" { repeatNext = 3; continue }
            if token.allSatisfy(\.isNumber) {
                out += String(repeating: token, count: token.count == 1 ? repeatNext : 1)
            } else if let d = digitWords[token] {
                out += String(repeating: d, count: repeatNext)
            } else {
                return nil
            }
            repeatNext = 1
        }
        return out.filter(\.isNumber).count >= 7 ? out : nil
    }

    // MARK: - Contacts

    private struct Entry {
        let label: String
        let names: [String]
        let handle: String
    }

    private static let keys: [CNKeyDescriptor] = [
        CNContactGivenNameKey as CNKeyDescriptor, CNContactFamilyNameKey as CNKeyDescriptor,
        CNContactNicknameKey as CNKeyDescriptor, CNContactOrganizationNameKey as CNKeyDescriptor,
        CNContactPhoneNumbersKey as CNKeyDescriptor, CNContactEmailAddressesKey as CNKeyDescriptor,
    ]

    static var contactsAllowed: Bool {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        return status == .authorized || status == .limited
    }

    private static func handle(_ c: CNContact) -> String? {
        let mobile = c.phoneNumbers.first {
            $0.label == CNLabelPhoneNumberMobile || $0.label == CNLabelPhoneNumberiPhone
        } ?? c.phoneNumbers.first
        if let phone = mobile?.value.stringValue { return phone }
        return c.emailAddresses.first.map { $0.value as String }
    }

    private static func label(_ c: CNContact) -> String {
        let full = "\(c.givenName) \(c.familyName)".trimmingCharacters(in: .whitespaces)
        if !full.isEmpty { return full }
        if !c.nickname.isEmpty { return c.nickname }
        return c.organizationName
    }

    /// Every contact that can be texted, as the wearer saved them.
    private static func everyone() -> [Entry] {
        guard contactsAllowed else { return [] }
        var out: [Entry] = []
        let request = CNContactFetchRequest(keysToFetch: keys)
        try? CNContactStore().enumerateContacts(with: request) { c, _ in
            guard let h = handle(c) else { return }
            let name = label(c)
            guard !name.isEmpty else { return }
            let names = [name, c.nickname, c.organizationName].filter { !$0.isEmpty }
            out.append(Entry(label: name, names: names, handle: h))
        }
        return out
    }

    /// The saved name for a number or email ("+14055551234" → "Honey Bun").
    static func name(forHandle raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard contactsAllowed, !value.isEmpty else { return nil }
        let predicate: NSPredicate
        if value.contains("@") {
            predicate = CNContact.predicateForContacts(matchingEmailAddress: value)
        } else if value.filter(\.isNumber).count >= 5,
                  value.allSatisfy({ $0.isNumber || " +-().".contains($0) }) {
            predicate = CNContact.predicateForContacts(matching: CNPhoneNumber(stringValue: value))
        } else {
            return nil
        }
        guard let c = try? CNContactStore().unifiedContacts(matching: predicate, keysToFetch: keys).first else { return nil }
        let name = label(c)
        return name.isEmpty ? nil : name
    }

    // MARK: - Finding who was meant

    /// On the main queue: the match, or nil (the caller says it found no one).
    static func find(_ said: String, claude: ClaudeLink, completion: @escaping @MainActor (Match?) -> Void) {
        let words = said.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?"))
        guard !words.isEmpty else { Task { @MainActor in completion(nil) }; return }
        if let number = spokenNumber(words) {
            Log.add("recipient: a number (\(number.count) digits)")
            Task { @MainActor in completion(Match(label: number, handle: number)) }
            return
        }
        if words.contains("@"), !words.contains(" ") {
            Task { @MainActor in completion(Match(label: words, handle: words)) }
            return
        }
        Task.detached(priority: .userInitiated) {
            let all = everyone()
            let lower = words.lowercased()
            let exact = all.filter { e in e.names.contains { $0.lowercased() == lower } }
            if exact.count == 1 {
                Log.add("recipient: exact name")
                await completion(Match(label: exact[0].label, handle: exact[0].handle))
                return
            }
            guard !all.isEmpty else {
                Log.add("recipient: no contacts to choose from (allowed: \(contactsAllowed))")
                await completion(nil)
                return
            }
            // A model picks: on device first, Claude when it cannot.
            let shortlist = Array(ranked(all, for: lower).prefix(300))
            if let i = await onDevicePick(words, shortlist), shortlist.indices.contains(i) {
                Log.add("recipient: on-device pick")
                await completion(Match(label: shortlist[i].label, handle: shortlist[i].handle))
                return
            }
            let wide = Array(ranked(all, for: lower).prefix(1500))
            if claude.configured, let i = await claudePick(words, wide, claude: claude), wide.indices.contains(i) {
                Log.add("recipient: Claude pick")
                await completion(Match(label: wide[i].label, handle: wide[i].handle))
                return
            }
            Log.add("recipient: no match for \(words.count) chars")
            await completion(nil)
        }
    }

    /// Closest first: names sharing words or letters with what was said, so
    /// a long contact list still fits the on-device model.
    private static func ranked(_ all: [Entry], for said: String) -> [Entry] {
        let saidWords = Set(said.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init))
        func score(_ e: Entry) -> Int {
            var best = 0
            for name in e.names {
                let n = name.lowercased()
                var s = 0
                for w in saidWords where w.count >= 3 {
                    if n.contains(w) { s += 3 * w.count } else if n.contains(w.prefix(3)) { s += 2 }
                }
                best = max(best, s)
            }
            return best
        }
        return all.enumerated()
            .map { (i: $0.offset, e: $0.element, s: score($0.element)) }
            .sorted { $0.s != $1.s ? $0.s > $1.s : $0.i < $1.i }
            .map(\.e)
    }

    private static let instructions = """
    You pick which saved contact a person means when they say who to text. \
    They may say a nickname, part of a name, a misheard name, or how they \
    know the person (my wife, mom, my boss); contacts are often saved under \
    pet names such as "Honey Bun" or "Mama Bear". Reply with only the \
    number of the one contact they mean, or 0 when none fits.
    """

    private static func prompt(_ said: String, _ list: [Entry]) -> String {
        let rows = list.enumerated().map { i, e -> String in
            let extra = e.names.dropFirst().joined(separator: ", ")
            return "\(i + 1). \(e.label)\(extra.isEmpty ? "" : " (\(extra))")"
        }
        return "They said: \"\(said)\"\n\nContacts:\n" + rows.joined(separator: "\n")
    }

    private static func index(_ reply: String) -> Int? {
        guard let n = reply.split(whereSeparator: { !$0.isNumber }).first.flatMap({ Int($0) }), n > 0 else { return nil }
        return n - 1
    }

    private static func onDevicePick(_ said: String, _ list: [Entry]) async -> Int? {
        let model = SystemLanguageModel(useCase: .general, guardrails: .permissiveContentTransformations)
        guard model.isAvailable else { return nil }
        do {
            let session = LanguageModelSession(model: model, tools: [], instructions: instructions)
            let response = try await session.respond(to: prompt(said, list), options: GenerationOptions())
            return index(response.content)
        } catch {
            Log.add("recipient: on-device model failed: \(error)")
            return nil
        }
    }

    private static func claudePick(_ said: String, _ list: [Entry], claude: ClaudeLink) async -> Int? {
        await withCheckedContinuation { cont in
            claude.complete(instructions: instructions, text: prompt(said, list)) { result in
                cont.resume(returning: (try? result.get()).flatMap(index))
            }
        }
    }
}

/// Is an incoming text spam? (2026-10-30.) The wearer filters spam with
/// Apple's model in his Shortcuts automation, and when the model refuses a
/// text the whole automation stops: Shortcuts cannot catch an action's
/// error (Apple DTS, forums thread 813757), so real texts were lost too.
/// Here the same check cannot stop anything: Apple's on-device model in its
/// permissive mode (it reads any text and, for a String answer, throws no
/// guardrail error), Claude when that gives no verdict, and a text nobody
/// could judge is shown.
enum SpamCheck {
    static let instructions = """
    You screen incoming text messages for one person. Reply with one word: SPAM or OK.
    SPAM: unsolicited marketing, scams, phishing, fake delivery, bank, toll or account alerts, \
    prizes, crypto, loans, political or donation blasts, strangers' "wrong number" openers.
    OK: anyone the person knows (a saved contact, family, friends, coworkers), group chats, \
    one-time codes, and real appointment, order or delivery updates.
    """

    /// true: spam; false: not spam; nil: no verdict (show it). On the main
    /// queue, once, within `seconds`.
    ///   - A saved contact or a group chat is never checked: spam comes from
    ///     numbers Contacts does not know, and a model's mistake there hid
    ///     real texts (2026-11-09).
    ///   - No verdict in time shows the text: the automation hands it over
    ///     in the background, where iOS gives an action about 30 s
    ///     (LongRunningIntent docs), and Apple's on-device model is rate
    ///     limited for a background app on battery (Apple DTS, forums 793787).
    static func check(from: String, saved: Bool, body: String, claude: ClaudeLink, seconds: Double = 10,
                      completion: @escaping @MainActor (Bool?) -> Void) {
        if saved {
            Task { @MainActor in completion(false) }
            return
        }
        let once = Once(completion)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            once.fire(nil, why: "no verdict in \(Int(seconds)) s")
        }
        let text = "From: \(from) (not in contacts)\nMessage: \(String(body.prefix(1500)))"
        Task.detached(priority: .userInitiated) {
            if let verdict = await onDevice(text) { await once.fire(verdict); return }
            guard claude.configured else { await once.fire(nil); return }
            let verdict: Bool? = await withCheckedContinuation { cont in
                claude.complete(instructions: instructions, text: text) { result in
                    cont.resume(returning: (try? result.get()).flatMap(parse))
                }
            }
            await once.fire(verdict)
        }
    }

    /// The first answer wins; a model that answers after the time limit is dropped.
    private final class Once: @unchecked Sendable {
        private var completion: (@MainActor (Bool?) -> Void)?
        init(_ completion: @escaping @MainActor (Bool?) -> Void) { self.completion = completion }

        @MainActor func fire(_ verdict: Bool?, why: String? = nil) {
            guard let c = completion else { return }
            completion = nil
            if let why { Log.add("spam check: \(why); the text is shown") }
            c(verdict)
        }
    }

    private static func onDevice(_ text: String) async -> Bool? {
        let model = SystemLanguageModel(useCase: .general, guardrails: .permissiveContentTransformations)
        guard model.isAvailable else { return nil }
        do {
            let session = LanguageModelSession(model: model, tools: [], instructions: instructions)
            let response = try await session.respond(to: text, options: GenerationOptions())
            return parse(response.content)
        } catch {
            Log.add("spam check: on-device model failed: \(error)")
            return nil
        }
    }

    /// The first word decides. A reply that only mentions spam ("OK, not
    /// spam", "Not spam") is not a SPAM verdict: it was read as one, and the
    /// text was kept off the glasses and out of Recent (2026-11-09).
    static func parse(_ reply: String) -> Bool? {
        let words = reply.uppercased().split(whereSeparator: { !$0.isLetter }).map(String.init)
        guard let first = words.first else { return nil }
        if first == "SPAM" { return true }
        if first == "OK" { return false }
        if first == "NOT", words.dropFirst().first == "SPAM" { return false }
        return nil
    }
}
