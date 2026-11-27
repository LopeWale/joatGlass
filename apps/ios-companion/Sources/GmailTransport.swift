import Foundation
import Network

// MARK: - Transport Errors

enum GmailTransportError: LocalizedError, Equatable {
    case authenticationFailed
    case overQuota
    case timedOut(String)
    case connectionFailed(String)
    case protocolError(String)
    case recipientRejected(String)

    var errorDescription: String? {
        switch self {
        case .authenticationFailed:
            return "Gmail rejected the email or App Password. Create a 16-character App Password in your Google Account."
        case .overQuota:
            return "Gmail temporarily rate-limited IMAP bandwidth for this account. It will resume automatically."
        case .timedOut(let stage):
            return "Gmail timed out during \(stage). Check your connection and try again."
        case .connectionFailed(let detail):
            return "Could not connect to Gmail (\(detail))."
        case .protocolError(let detail):
            return "Gmail returned an unexpected response (\(detail))."
        case .recipientRejected(let recipient):
            return "Gmail rejected recipient \(recipient)."
        }
    }
}

// MARK: - Byte-Accurate IMAP Response Parser

enum GmailIMAPResponseParser {
    struct Response: Equatable {
        enum Segment: Equatable {
            case text(String)
            case literal(Data)
        }
        let segments: [Segment]
    }

    struct FetchedMessage: Equatable {
        let seq: Int
        let uid: UInt32
        let flags: [String]
        let internalDate: Date?
        let rawBody: Data?

        var isSeen: Bool {
            flags.contains { $0.caseInsensitiveCompare("\\Seen") == .orderedSame }
        }
    }

    enum Event: Equatable {
        case exists(Int)
        case uidValidity(UInt32)
        case uidNext(UInt32)
        case fetch(FetchedMessage)
        case tagged(tag: String, status: String, message: String)
        case untagged(String)
    }

    /// Incremental byte-accurate framing buffer supporting `{N}\r\n<N raw bytes>` literals
    /// across arbitrary TCP/TLS packet boundaries.
    struct StreamBuffer {
        private var storage: [UInt8] = []
        private var cursor: Int = 0
        private var activeSegments: [Response.Segment] = []
        private var pendingLiteralLength: Int?

        init() {}

        var hasBufferedBytes: Bool {
            cursor < storage.count || !activeSegments.isEmpty || pendingLiteralLength != nil
        }

        mutating func append(_ data: Data) {
            guard !data.isEmpty else { return }
            storage.append(contentsOf: data)
        }

        mutating func nextResponse() -> Response? {
            while true {
                if let needed = pendingLiteralLength {
                    let available = storage.count - cursor
                    guard available >= needed else {
                        compactIfNeeded()
                        return nil
                    }
                    let slice = Data(storage[cursor..<(cursor + needed)])
                    cursor += needed
                    pendingLiteralLength = nil
                    activeSegments.append(.literal(slice))
                    continue
                }

                guard let (lineEnd, nextCursor) = findLineBreak(from: cursor) else {
                    compactIfNeeded()
                    return nil
                }

                let lineString = String(decoding: storage[cursor..<lineEnd], as: UTF8.self)
                cursor = nextCursor

                if let (prefix, literalLen) = Self.extractTrailingLiteral(from: lineString) {
                    activeSegments.append(.text(prefix))
                    pendingLiteralLength = literalLen
                    continue
                } else {
                    activeSegments.append(.text(lineString))
                    let finished = Response(segments: activeSegments)
                    activeSegments.removeAll(keepingCapacity: true)
                    compactIfNeeded(force: cursor == storage.count)
                    return finished
                }
            }
        }

        private func findLineBreak(from start: Int) -> (lineEnd: Int, nextCursor: Int)? {
            var i = start
            let n = storage.count
            while i < n {
                if storage[i] == 0x0A { // '\n'
                    if i > start && storage[i - 1] == 0x0D {
                        return (i - 1, i + 1)
                    }
                    return (i, i + 1)
                }
                i += 1
            }
            return nil
        }

        private mutating func compactIfNeeded(force: Bool = false) {
            if force && cursor == storage.count {
                storage.removeAll(keepingCapacity: true)
                cursor = 0
            } else if cursor >= 8192 {
                storage.removeFirst(cursor)
                cursor = 0
            }
        }

        /// Detects `{N}` (or `{N+}`) at the end of an IMAP line outside double quotes.
        static func extractTrailingLiteral(from line: String) -> (prefix: String, length: Int)? {
            guard line.hasSuffix("}") else { return nil }
            var inQuotes = false
            var escaped = false
            var lastOpenBraceOutsideQuotes: String.Index?

            var idx = line.startIndex
            while idx < line.endIndex {
                let ch = line[idx]
                if escaped {
                    escaped = false
                } else if ch == "\\" && inQuotes {
                    escaped = true
                } else if ch == "\"" {
                    inQuotes.toggle()
                } else if !inQuotes && ch == "{" {
                    lastOpenBraceOutsideQuotes = idx
                }
                idx = line.index(after: idx)
            }

            guard !inQuotes, let openBrace = lastOpenBraceOutsideQuotes else { return nil }
            let closeBrace = line.index(before: line.endIndex)
            guard openBrace < closeBrace else { return nil }
            var digitsSlice = line[line.index(after: openBrace)..<closeBrace]
            if digitsSlice.hasSuffix("+") {
                digitsSlice = digitsSlice.dropLast()
            }
            guard !digitsSlice.isEmpty, digitsSlice.allSatisfy(\.isASCII), digitsSlice.allSatisfy(\.isNumber),
                  let count = Int(digitsSlice), count >= 0 else {
                return nil
            }
            return (String(line[..<openBrace]), count)
        }
    }

    // MARK: - Event Extraction

    private static let existsRegex = try! NSRegularExpression(
        pattern: #"^\*\s+(\d+)\s+EXISTS\b"#,
        options: [.caseInsensitive]
    )
    private static let uidValidityRegex = try! NSRegularExpression(
        pattern: #"\[UIDVALIDITY\s+(\d+)\]"#,
        options: [.caseInsensitive]
    )
    private static let uidNextRegex = try! NSRegularExpression(
        pattern: #"\[UIDNEXT\s+(\d+)\]"#,
        options: [.caseInsensitive]
    )
    private static let fetchPrefixRegex = try! NSRegularExpression(
        pattern: #"^\*\s+(\d+)\s+FETCH\s*\("#,
        options: [.caseInsensitive]
    )
    private static let taggedRegex = try! NSRegularExpression(
        pattern: #"^([A-Za-z0-9]+)\s+(OK|NO|BAD)\b\s*(.*)$"#,
        options: [.caseInsensitive]
    )

    static func parse(_ response: Response) -> [Event] {
        guard let firstSeg = response.segments.first else { return [] }
        let head: String
        switch firstSeg {
        case .text(let s): head = s.trimmingCharacters(in: .whitespaces)
        case .literal: return []
        }
        guard !head.isEmpty else { return [] }

        var events: [Event] = []
        let headRange = NSRange(head.startIndex..., in: head)

        if let m = existsRegex.firstMatch(in: head, range: headRange),
           let r = Range(m.range(at: 1), in: head),
           let n = Int(head[r]) {
            events.append(.exists(n))
        }

        if let m = uidValidityRegex.firstMatch(in: head, range: headRange),
           let r = Range(m.range(at: 1), in: head),
           let v = UInt32(head[r]) {
            events.append(.uidValidity(v))
        }

        if let m = uidNextRegex.firstMatch(in: head, range: headRange),
           let r = Range(m.range(at: 1), in: head),
           let u = UInt32(head[r]) {
            events.append(.uidNext(u))
        }

        if let m = fetchPrefixRegex.firstMatch(in: head, range: headRange),
           let seqRange = Range(m.range(at: 1), in: head),
           let seq = Int(head[seqRange]) {
            if let fetched = parseFetch(seq: seq, response: response) {
                events.append(.fetch(fetched))
            }
            return events
        }

        if !head.hasPrefix("*") && !head.hasPrefix("+"),
           let m = taggedRegex.firstMatch(in: head, range: headRange),
           let tagR = Range(m.range(at: 1), in: head),
           let statusR = Range(m.range(at: 2), in: head),
           let msgR = Range(m.range(at: 3), in: head) {
            events.append(.tagged(
                tag: String(head[tagR]),
                status: String(head[statusR]).uppercased(),
                message: String(head[msgR]).trimmingCharacters(in: .whitespaces)
            ))
            return events
        }

        if events.isEmpty {
            events.append(.untagged(head))
        }
        return events
    }

    // MARK: - FETCH Attribute Tokenizer

    private enum Token {
        case lparen
        case rparen
        case atom(String)
        case quoted(String)
        case bytes(Data)
    }

    private static func parseFetch(seq: Int, response: Response) -> FetchedMessage? {
        let tokens = tokenize(response: response)
        // Locate outer '(' following `* <seq> FETCH`
        guard let openIdx = tokens.firstIndex(where: { if case .lparen = $0 { return true }; return false }) else {
            return nil
        }

        var uid: UInt32 = 0
        var flags: [String] = []
        var internalDate: Date?
        var rawBody: Data?

        var i = openIdx + 1
        while i < tokens.count {
            switch tokens[i] {
            case .rparen:
                return FetchedMessage(seq: seq, uid: uid, flags: flags, internalDate: internalDate, rawBody: rawBody)
            case .atom(let rawKey):
                let key = rawKey.uppercased()
                i += 1
                guard i < tokens.count else { break }

                if key == "UID" {
                    if case .atom(let s) = tokens[i], let v = UInt32(s) {
                        uid = v
                    }
                    i += 1
                } else if key == "FLAGS" {
                    if case .lparen = tokens[i] {
                        i += 1
                        var parsedFlags: [String] = []
                        while i < tokens.count {
                            if case .rparen = tokens[i] {
                                i += 1
                                break
                            }
                            switch tokens[i] {
                            case .atom(let f), .quoted(let f):
                                parsedFlags.append(f)
                            default:
                                break
                            }
                            i += 1
                        }
                        flags = parsedFlags
                    } else {
                        i = skipValue(tokens, from: i)
                    }
                } else if key == "INTERNALDATE" {
                    switch tokens[i] {
                    case .quoted(let s), .atom(let s):
                        internalDate = MIMEParser.parseDate(s)
                    default:
                        break
                    }
                    i += 1
                } else if key.hasPrefix("BODY[") || key.hasPrefix("BODY.PEEK[") || key == "RFC822" {
                    switch tokens[i] {
                    case .bytes(let data):
                        rawBody = data
                    case .quoted(let s):
                        rawBody = Data(s.utf8)
                    case .atom(let s) where s.uppercased() != "NIL":
                        rawBody = Data(s.utf8)
                    default:
                        break
                    }
                    i += 1
                } else {
                    i = skipValue(tokens, from: i)
                }
            default:
                i += 1
            }
        }

        return FetchedMessage(seq: seq, uid: uid, flags: flags, internalDate: internalDate, rawBody: rawBody)
    }

    private static func skipValue(_ tokens: [Token], from index: Int) -> Int {
        guard index < tokens.count else { return index }
        if case .lparen = tokens[index] {
            var depth = 1
            var j = index + 1
            while j < tokens.count && depth > 0 {
                switch tokens[j] {
                case .lparen: depth += 1
                case .rparen: depth -= 1
                default: break
                }
                j += 1
            }
            return j
        }
        return index + 1
    }

    private static func tokenize(response: Response) -> [Token] {
        var out: [Token] = []
        for seg in response.segments {
            switch seg {
            case .literal(let data):
                out.append(.bytes(data))
            case .text(let text):
                tokenizeText(text, into: &out)
            }
        }
        return out
    }

    private static func tokenizeText(_ s: String, into out: inout [Token]) {
        var i = s.startIndex
        while i < s.endIndex {
            let ch = s[i]
            if ch.isWhitespace {
                i = s.index(after: i)
                continue
            }
            if ch == "(" {
                out.append(.lparen)
                i = s.index(after: i)
                continue
            }
            if ch == ")" {
                out.append(.rparen)
                i = s.index(after: i)
                continue
            }
            if ch == "\"" {
                i = s.index(after: i)
                var str = ""
                var escaped = false
                while i < s.endIndex {
                    let c = s[i]
                    i = s.index(after: i)
                    if escaped {
                        str.append(c)
                        escaped = false
                    } else if c == "\\" {
                        escaped = true
                    } else if c == "\"" {
                        break
                    } else {
                        str.append(c)
                    }
                }
                out.append(.quoted(str))
                continue
            }

            // Atom (keep `[...]` section specs and optional `<...>` origin octets attached, e.g. `BODY[]<0>`)
            var atom = ""
            var bracketDepth = 0
            while i < s.endIndex {
                let c = s[i]
                if bracketDepth > 0 {
                    atom.append(c)
                    if c == "[" { bracketDepth += 1 }
                    else if c == "]" { bracketDepth -= 1 }
                    i = s.index(after: i)
                    continue
                }
                if c.isWhitespace || c == "(" || c == ")" || c == "\"" {
                    break
                }
                if c == "[" { bracketDepth += 1 }
                atom.append(c)
                i = s.index(after: i)
            }
            if !atom.isEmpty {
                out.append(.atom(atom))
            }
        }
    }
}

// MARK: - Quota-Safe Gmail IMAP Client (imap.gmail.com:993)

enum GmailIMAP {
    static let defaultHost = "imap.gmail.com"
    static let defaultPort: UInt16 = 993
    static let commandTimeout: TimeInterval = 20
    static let bodyPeekSliceBytes = 131_072
    static let bootstrapWindow = 20

    enum SyncPlan: Equatable {
        case fastIdleExit
        case refreshRecentFlags(sequenceRange: String)
        case bootstrap(sequenceRange: String)
        case bootstrapEmpty
        case delta(uidRange: String)
    }

    enum SyncOutcome {
        case idle(uidValidity: UInt32, uidNext: UInt32, flagUpdates: [UInt32: Bool]?)
        case bootstrap(uidValidity: UInt32, uidNext: UInt32, messages: [GmailIMAPResponseParser.FetchedMessage])
        case delta(
            uidValidity: UInt32,
            uidNext: UInt32,
            newMessages: [GmailIMAPResponseParser.FetchedMessage],
            flagUpdates: [UInt32: Bool]?
        )
    }

    /// Pure sync planner: enforces fast idle exit, sequence-bounded bootstrap (never `UID SEARCH ALL`),
    /// and `UIDNEXT` delta fetching.
    static func planSync(
        exists: Int,
        uidValidity: UInt32,
        uidNext: UInt32,
        cachedUIDValidity: UInt32?,
        cachedUIDNext: UInt32?,
        refreshFlags: Bool
    ) -> SyncPlan {
        let startSeq = max(1, exists - (bootstrapWindow - 1))
        let recentRange = "\(startSeq):\(max(1, exists))"

        if let cv = cachedUIDValidity, let cn = cachedUIDNext, cv == uidValidity {
            if uidNext == cn {
                if refreshFlags && exists > 0 {
                    return .refreshRecentFlags(sequenceRange: recentRange)
                }
                return .fastIdleExit
            }
            if uidNext > cn {
                return .delta(uidRange: "\(cn):*")
            }
        }

        if exists <= 0 {
            return .bootstrapEmpty
        }
        return .bootstrap(sequenceRange: recentRange)
    }

    static func escapeQuoted(_ raw: String) -> String {
        var out = ""
        out.reserveCapacity(raw.count + 4)
        for ch in raw where ch != "\r" && ch != "\n" {
            if ch == "\\" || ch == "\"" {
                out.append("\\")
            }
            out.append(ch)
        }
        return out
    }

    static func markSeenCommand(for uids: [UInt32]) -> String? {
        let valid = Array(Set(uids.filter { $0 > 0 && $0 < UInt32.max - 200_000 })).sorted()
        guard !valid.isEmpty else { return nil }
        let sequence = valid.map(String.init).joined(separator: ",")
        return "UID STORE \(sequence) +FLAGS.SILENT (\\Seen)"
    }

    static func markSeen(
        email: String,
        appPassword: String,
        uids: [UInt32],
        host: String = defaultHost,
        port: UInt16 = defaultPort
    ) async throws {
        guard let storeCmd = markSeenCommand(for: uids) else { return }
        let client = TLSSocketClient(host: host, port: port, timeout: commandTimeout)
        defer { client.close() }
        try await client.connect()
        var session = IMAPSession(client: client)
        try await session.readGreeting()
        try await session.login(email: email, appPassword: appPassword)
        _ = try await session.command("SELECT \"INBOX\"", stage: "SELECT INBOX")
        _ = try await session.command(storeCmd, stage: "UID STORE \\Seen")
        try? await session.logout()
    }

    static func verifyCredentials(
        email: String,
        appPassword: String,
        host: String = defaultHost,
        port: UInt16 = defaultPort
    ) async throws {
        let client = TLSSocketClient(host: host, port: port, timeout: commandTimeout)
        defer { client.close() }
        try await client.connect()
        var session = IMAPSession(client: client)
        try await session.readGreeting()
        try await session.login(email: email, appPassword: appPassword)
        try? await session.logout()
    }

    static func syncInbox(
        email: String,
        appPassword: String,
        cachedUIDValidity: UInt32?,
        cachedUIDNext: UInt32?,
        refreshFlags: Bool,
        host: String = defaultHost,
        port: UInt16 = defaultPort
    ) async throws -> SyncOutcome {
        let client = TLSSocketClient(host: host, port: port, timeout: commandTimeout)
        defer { client.close() }
        try await client.connect()
        var session = IMAPSession(client: client)

        try await session.readGreeting()
        try await session.login(email: email, appPassword: appPassword)

        // Strictly read-only EXAMINE so unread Gmail messages are NEVER marked \Seen!
        let examineEvents = try await session.command("EXAMINE \"INBOX\"", stage: "EXAMINE INBOX")
        var exists = 0
        var uidValidity: UInt32 = 0
        var uidNext: UInt32 = 0

        for ev in examineEvents {
            switch ev {
            case .exists(let n): exists = n
            case .uidValidity(let v): uidValidity = v
            case .uidNext(let u): uidNext = u
            default: break
            }
        }

        // Fallback STATUS only if server omitted UIDVALIDITY or UIDNEXT in EXAMINE response codes
        if uidValidity == 0 || uidNext == 0 {
            let statusEvents = try await session.command(
                "STATUS \"INBOX\" (MESSAGES UIDVALIDITY UIDNEXT)",
                stage: "STATUS INBOX"
            )
            for ev in statusEvents {
                if case .untagged(let line) = ev {
                    if uidValidity == 0, let v = Self.extractStatusNumber("UIDVALIDITY", in: line) {
                        uidValidity = UInt32(v)
                    }
                    if uidNext == 0, let u = Self.extractStatusNumber("UIDNEXT", in: line) {
                        uidNext = UInt32(u)
                    }
                }
            }
        }

        let plan = planSync(
            exists: exists,
            uidValidity: uidValidity,
            uidNext: uidNext,
            cachedUIDValidity: cachedUIDValidity,
            cachedUIDNext: cachedUIDNext,
            refreshFlags: refreshFlags
        )

        switch plan {
        case .fastIdleExit:
            try? await session.logout()
            return .idle(uidValidity: uidValidity, uidNext: uidNext, flagUpdates: nil)

        case .refreshRecentFlags(let seqRange):
            let flagEvents = try await session.command(
                "FETCH \(seqRange) (UID FLAGS)",
                stage: "FETCH FLAGS"
            )
            let flagMap = Self.extractFlagMap(from: flagEvents)
            try? await session.logout()
            return .idle(uidValidity: uidValidity, uidNext: uidNext, flagUpdates: flagMap)

        case .bootstrapEmpty:
            try? await session.logout()
            return .bootstrap(uidValidity: uidValidity, uidNext: max(1, uidNext), messages: [])

        case .bootstrap(let seqRange):
            let fetchEvents = try await session.command(
                "FETCH \(seqRange) (UID FLAGS INTERNALDATE BODY.PEEK[]<0.\(bodyPeekSliceBytes)>)",
                stage: "FETCH bootstrap"
            )
            let messages = fetchEvents.compactMap { ev -> GmailIMAPResponseParser.FetchedMessage? in
                if case .fetch(let f) = ev, f.uid > 0 { return f }
                return nil
            }
            let highestUID = messages.map(\.uid).max() ?? 0
            let nextWatermark = max(uidNext, highestUID &+ 1)
            try? await session.logout()
            return .bootstrap(uidValidity: uidValidity, uidNext: nextWatermark, messages: messages)

        case .delta(let uidRange):
            let minUID = cachedUIDNext ?? 1
            let fetchEvents = try await session.command(
                "UID FETCH \(uidRange) (UID FLAGS INTERNALDATE BODY.PEEK[]<0.\(bodyPeekSliceBytes)>)",
                stage: "UID FETCH delta"
            )
            let newMessages = fetchEvents.compactMap { ev -> GmailIMAPResponseParser.FetchedMessage? in
                if case .fetch(let f) = ev, f.uid >= minUID { return f }
                return nil
            }
            var flagMap: [UInt32: Bool]?
            if refreshFlags && exists > 0 {
                let startSeq = max(1, exists - (bootstrapWindow - 1))
                let flagEvents = try await session.command(
                    "FETCH \(startSeq):\(exists) (UID FLAGS)",
                    stage: "FETCH FLAGS"
                )
                flagMap = Self.extractFlagMap(from: flagEvents)
            }
            let highestUID = newMessages.map(\.uid).max() ?? (minUID - 1)
            let nextWatermark = max(uidNext, highestUID &+ 1)
            try? await session.logout()
            return .delta(
                uidValidity: uidValidity,
                uidNext: nextWatermark,
                newMessages: newMessages,
                flagUpdates: flagMap
            )
        }
    }

    private static func extractFlagMap(from events: [GmailIMAPResponseParser.Event]) -> [UInt32: Bool] {
        var out: [UInt32: Bool] = [:]
        for ev in events {
            if case .fetch(let f) = ev, f.uid > 0 {
                out[f.uid] = !f.isSeen // value = unread Bool
            }
        }
        return out
    }

    private static func extractStatusNumber(_ key: String, in line: String) -> Int? {
        let pattern = "\\b\(NSRegularExpression.escapedPattern(for: key))\\s+(\\d+)\\b"
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
              let r = Range(m.range(at: 1), in: line) else { return nil }
        return Int(line[r])
    }

    private struct IMAPSession {
        let client: TLSSocketClient
        var buffer = GmailIMAPResponseParser.StreamBuffer()
        var counter = 0

        mutating func readGreeting() async throws {
            while true {
                let resp = try await nextResponse(stage: "greeting")
                let events = GmailIMAPResponseParser.parse(resp)
                for ev in events {
                    if case .untagged(let line) = ev {
                        let upper = line.uppercased()
                        if upper.hasPrefix("* OK") { return }
                        if upper.hasPrefix("* BYE") {
                            if upper.contains("OVERQUOTA") { throw GmailTransportError.overQuota }
                            throw GmailTransportError.protocolError(line)
                        }
                    }
                }
                // Any valid `* OK` greeting with response codes (like `* OK ...`) returns cleanly
                if let first = resp.segments.first, case .text(let s) = first, s.uppercased().hasPrefix("* OK") {
                    return
                }
            }
        }

        mutating func login(email: String, appPassword: String) async throws {
            let cmd = "LOGIN \"\(GmailIMAP.escapeQuoted(email))\" \"\(GmailIMAP.escapeQuoted(appPassword))\""
            _ = try await command(cmd, stage: "LOGIN", isAuth: true)
        }

        mutating func logout() async throws {
            _ = try await command("LOGOUT", stage: "LOGOUT")
        }

        mutating func command(_ rawCommand: String, stage: String, isAuth: Bool = false) async throws -> [GmailIMAPResponseParser.Event] {
            counter += 1
            let tag = String(format: "A%03d", counter)
            try await client.send(Data("\(tag) \(rawCommand)\r\n".utf8), stage: stage)

            var collected: [GmailIMAPResponseParser.Event] = []
            while true {
                let resp = try await nextResponse(stage: stage)
                let events = GmailIMAPResponseParser.parse(resp)
                for ev in events {
                    if case .tagged(let rxTag, let status, let message) = ev, rxTag.caseInsensitiveCompare(tag) == .orderedSame {
                        if status == "OK" {
                            return collected
                        }
                        let upperMsg = message.uppercased()
                        if upperMsg.contains("OVERQUOTA") {
                            throw GmailTransportError.overQuota
                        }
                        if isAuth || upperMsg.contains("AUTHENTICATIONFAILED") || upperMsg.contains("CREDENTIALS") || upperMsg.contains("WEBALERT") {
                            throw GmailTransportError.authenticationFailed
                        }
                        throw GmailTransportError.protocolError("\(stage) \(status): \(message)")
                    } else {
                        if case .untagged(let line) = ev, line.uppercased().contains("[OVERQUOTA]") {
                            throw GmailTransportError.overQuota
                        }
                        collected.append(ev)
                    }
                }
            }
        }

        private mutating func nextResponse(stage: String) async throws -> GmailIMAPResponseParser.Response {
            while true {
                if let resp = buffer.nextResponse() {
                    return resp
                }
                let chunk = try await client.receive(stage: stage)
                buffer.append(chunk)
            }
        }
    }
}

// MARK: - RFC 5322 Message Formatter & SMTP Client (smtp.gmail.com:465)

enum GmailSMTPFormatter {
    static func authPlainToken(email: String, appPassword: String) -> String {
        Data("\0\(email)\0\(appPassword)".utf8).base64EncodedString()
    }

    /// RFC 5321 §4.5.2 transparency: any line starting with `.` has an extra `.` prepended.
    static func dotStuff(_ raw: String) -> String {
        let normalized = raw
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        return normalized
            .components(separatedBy: "\n")
            .map { $0.hasPrefix(".") ? "." + $0 : $0 }
            .joined(separator: "\r\n")
    }

    /// Dot-stuffed payload terminated with `<CRLF>.<CRLF>`.
    static func wireDataPayload(_ message: String) -> String {
        let stuffed = dotStuff(message)
        if stuffed.hasSuffix("\r\n") {
            return stuffed + ".\r\n"
        }
        return stuffed + "\r\n.\r\n"
    }

    static func sanitizeHeader(_ raw: String) -> String {
        raw.replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func sanitizeAngleAddr(_ raw: String) -> String {
        let parsed = MIMEParser.parseAddresses(raw).first?.email ?? raw
        return parsed.replacingOccurrences(of: "<", with: "")
            .replacingOccurrences(of: ">", with: "")
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func encodeRFC2047IfNeeded(_ value: String) -> String {
        let clean = sanitizeHeader(value)
        guard !clean.isEmpty else { return "" }
        if clean.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value <= 0x7E }) {
            return clean
        }
        // Chunk UTF-8 scalars so each encoded-word stays well under RFC 2047's 75-char limit
        var words: [String] = []
        var chunk = ""
        for ch in clean {
            let candidate = chunk + String(ch)
            if candidate.utf8.count > 36 && !chunk.isEmpty {
                words.append("=?UTF-8?B?\(Data(chunk.utf8).base64EncodedString())?=")
                chunk = String(ch)
            } else {
                chunk = candidate
            }
        }
        if !chunk.isEmpty {
            words.append("=?UTF-8?B?\(Data(chunk.utf8).base64EncodedString())?=")
        }
        return words.joined(separator: "\r\n ")
    }

    static func encodeDisplayName(_ name: String) -> String {
        let clean = sanitizeHeader(name)
        guard !clean.isEmpty else { return "" }
        if !clean.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value <= 0x7E }) {
            return encodeRFC2047IfNeeded(clean)
        }
        let escaped = clean.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }

    static func formatRFC2822Date(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "EEE, dd MMM yyyy HH:mm:ss Z"
        return f.string(from: date)
    }

    static func formatMessage(
        fromName: String,
        fromEmail: String,
        toEmail: String,
        subject: String,
        body: String,
        inReplyTo: String? = nil,
        references: [String] = [],
        messageId: String? = nil,
        date: Date = Date()
    ) -> String {
        let cleanFromEmail = sanitizeAngleAddr(fromEmail)
        let cleanToEmail = sanitizeAngleAddr(toEmail)
        let fromHeader: String
        let encodedName = encodeDisplayName(fromName)
        if encodedName.isEmpty {
            fromHeader = cleanFromEmail
        } else {
            fromHeader = "\(encodedName) <\(cleanFromEmail)>"
        }

        let msgId = messageId.map(normalizeMessageId) ?? "<\(UUID().uuidString.lowercased())@joatbrain.local>"
        var lines: [String] = [
            "From: \(fromHeader)",
            "To: \(cleanToEmail)",
            "Subject: \(encodeRFC2047IfNeeded(subject))",
            "Date: \(formatRFC2822Date(date))",
            "Message-ID: \(msgId)",
        ]

        if let reply = inReplyTo?.trimmingCharacters(in: .whitespacesAndNewlines), !reply.isEmpty {
            let normReply = normalizeMessageId(reply)
            lines.append("In-Reply-To: \(normReply)")
            var allRefs = references.map(normalizeMessageId).filter { !$0.isEmpty }
            if !allRefs.contains(normReply) {
                allRefs.append(normReply)
            }
            if !allRefs.isEmpty {
                lines.append("References: \(allRefs.joined(separator: " "))")
            }
        } else if !references.isEmpty {
            let allRefs = references.map(normalizeMessageId).filter { !$0.isEmpty }
            if !allRefs.isEmpty {
                lines.append("References: \(allRefs.joined(separator: " "))")
            }
        }

        lines.append("MIME-Version: 1.0")
        lines.append("Content-Type: text/plain; charset=utf-8")
        lines.append("Content-Transfer-Encoding: 8bit")
        lines.append("")

        let normalizedBody = body
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
            .joined(separator: "\r\n")
        lines.append(normalizedBody)
        return lines.joined(separator: "\r\n")
    }

    private static func normalizeMessageId(_ raw: String) -> String {
        let clean = sanitizeHeader(raw)
        guard !clean.isEmpty else { return "" }
        if clean.hasPrefix("<") && clean.hasSuffix(">") { return clean }
        return "<\(clean)>"
    }
}

enum GmailSMTP {
    static let defaultHost = "smtp.gmail.com"
    static let defaultPort: UInt16 = 465
    static let commandTimeout: TimeInterval = 25

    struct SentReceipt: Equatable {
        let messageId: String
        let sentAt: Date
    }

    @discardableResult
    static func send(
        email: String,
        appPassword: String,
        fromName: String,
        toEmail: String,
        subject: String,
        body: String,
        inReplyTo: String? = nil,
        references: [String] = [],
        host: String = defaultHost,
        port: UInt16 = defaultPort
    ) async throws -> SentReceipt {
        let cleanFrom = GmailSMTPFormatter.sanitizeAngleAddr(email)
        let cleanTo = GmailSMTPFormatter.sanitizeAngleAddr(toEmail)
        guard !cleanTo.isEmpty, cleanTo.contains("@") else {
            throw GmailTransportError.recipientRejected(toEmail)
        }

        let client = TLSSocketClient(host: host, port: port, timeout: commandTimeout)
        defer { client.close() }
        try await client.connect()
        var session = SMTPSession(client: client)

        try await session.expect(code: 220, stage: "SMTP greeting")
        try await session.sendCommand("EHLO joatbrain.local", expectedCode: 250, stage: "EHLO")

        let token = GmailSMTPFormatter.authPlainToken(email: cleanFrom, appPassword: appPassword)
        do {
            try await session.sendCommand("AUTH PLAIN \(token)", expectedCode: 235, stage: "AUTH PLAIN")
        } catch GmailTransportError.protocolError(let detail) {
            if detail.contains("535") || detail.contains("534") || detail.contains("530") {
                throw GmailTransportError.authenticationFailed
            }
            throw GmailTransportError.protocolError(detail)
        }

        try await session.sendCommand("MAIL FROM:<\(cleanFrom)>", expectedCode: 250, stage: "MAIL FROM")
        do {
            try await session.sendCommand("RCPT TO:<\(cleanTo)>", expectedCode: 250, stage: "RCPT TO")
        } catch {
            throw GmailTransportError.recipientRejected(cleanTo)
        }

        try await session.sendCommand("DATA", expectedCode: 354, stage: "DATA")

        let sentAt = Date()
        let messageId = "<\(UUID().uuidString.lowercased())@joatbrain.local>"
        let rfc5322 = GmailSMTPFormatter.formatMessage(
            fromName: fromName,
            fromEmail: cleanFrom,
            toEmail: cleanTo,
            subject: subject,
            body: body,
            inReplyTo: inReplyTo,
            references: references,
            messageId: messageId,
            date: sentAt
        )
        let payload = GmailSMTPFormatter.wireDataPayload(rfc5322)
        try await client.send(Data(payload.utf8), stage: "DATA payload")
        try await session.expect(code: 250, stage: "DATA completion")

        try? await session.sendCommand("QUIT", expectedCode: 221, stage: "QUIT")
        return SentReceipt(messageId: messageId, sentAt: sentAt)
    }

    private struct SMTPSession {
        let client: TLSSocketClient
        var lineBuffer: [UInt8] = []

        mutating func sendCommand(_ command: String, expectedCode: Int, stage: String) async throws {
            try await client.send(Data("\(command)\r\n".utf8), stage: stage)
            try await expect(code: expectedCode, stage: stage)
        }

        mutating func expect(code expected: Int, stage: String) async throws {
            var lines: [String] = []
            while true {
                let line = try await nextLine(stage: stage)
                lines.append(line)
                // Multi-line SMTP replies have '-' at index 3 (`250-SIZE`), final line has ' ' or end-of-line (`250 OK`)
                guard line.count >= 3, let code = Int(line.prefix(3)) else {
                    throw GmailTransportError.protocolError("\(stage): \(line)")
                }
                let fourthIndex = line.index(line.startIndex, offsetBy: 3, limitedBy: line.endIndex)
                let isContinuation = fourthIndex != nil && fourthIndex! < line.endIndex && line[fourthIndex!] == "-"
                if !isContinuation {
                    guard code == expected else {
                        throw GmailTransportError.protocolError("\(stage) HTTP/SMTP \(code): \(lines.joined(separator: " | "))")
                    }
                    return
                }
            }
        }

        private mutating func nextLine(stage: String) async throws -> String {
            while true {
                if let lfIndex = lineBuffer.firstIndex(of: 0x0A) {
                    var lineEnd = lfIndex
                    if lineEnd > 0 && lineBuffer[lineEnd - 1] == 0x0D {
                        lineEnd -= 1
                    }
                    let line = String(decoding: lineBuffer[0..<lineEnd], as: UTF8.self)
                    lineBuffer.removeFirst(lfIndex + 1)
                    return line
                }
                let chunk = try await client.receive(stage: stage)
                lineBuffer.append(contentsOf: chunk)
            }
        }
    }
}

// MARK: - Bounded TLS NWConnection Wrapper

private final class TLSSocketClient: @unchecked Sendable {
    private let connection: NWConnection
    private let queue: DispatchQueue
    private let timeout: TimeInterval

    init(host: String, port: UInt16, timeout: TimeInterval) {
        self.queue = DispatchQueue(label: "com.joatlabs.joatbrain.gmail.tls.\(UUID().uuidString.prefix(8))")
        self.timeout = timeout
        let endpoint = NWEndpoint.hostPort(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: port)!
        )
        self.connection = NWConnection(to: endpoint, using: .tls)
    }

    func connect() async throws {
        let _: Void = try await withTimeout(stage: "TLS connect") { [connection, queue] (once: OnceResult<Void>) in
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    once.succeed(())
                case .failed(let err):
                    once.fail(GmailTransportError.connectionFailed(err.localizedDescription))
                case .cancelled:
                    once.fail(GmailTransportError.connectionFailed("cancelled"))
                default:
                    break
                }
            }
            connection.start(queue: queue)
        }
    }

    func send(_ data: Data, stage: String) async throws {
        let _: Void = try await withTimeout(stage: stage) { [connection] (once: OnceResult<Void>) in
            connection.send(content: data, completion: .contentProcessed { error in
                if let error {
                    once.fail(GmailTransportError.connectionFailed(error.localizedDescription))
                } else {
                    once.succeed(())
                }
            })
        }
    }

    func receive(stage: String) async throws -> Data {
        try await withTimeout(stage: stage) { [connection] once in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { data, _, isComplete, error in
                if let error {
                    once.fail(GmailTransportError.connectionFailed(error.localizedDescription))
                    return
                }
                if let data, !data.isEmpty {
                    once.succeed(data)
                    return
                }
                if isComplete {
                    once.fail(GmailTransportError.connectionFailed("connection closed by server"))
                    return
                }
                once.succeed(Data())
            }
        }
    }

    func close() {
        connection.stateUpdateHandler = nil
        connection.cancel()
    }

    private func withTimeout<T: Sendable>(
        stage: String,
        operation: @escaping @Sendable (OnceResult<T>) -> Void
    ) async throws -> T {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<T, Error>) in
            let once = OnceResult(cont)
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now() + timeout)
            timer.setEventHandler { [weak connection] in
                if once.fail(GmailTransportError.timedOut(stage)) {
                    connection?.cancel()
                }
            }
            once.attachTimer(timer)
            timer.resume()
            queue.async {
                operation(once)
            }
        }
    }
}

private final class OnceResult<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Error>?
    private var timer: DispatchSourceTimer?

    init(_ continuation: CheckedContinuation<T, Error>) {
        self.continuation = continuation
    }

    func attachTimer(_ t: DispatchSourceTimer) {
        lock.lock()
        if continuation == nil {
            t.cancel()
        } else {
            timer = t
        }
        lock.unlock()
    }

    @discardableResult
    func succeed(_ value: T) -> Bool {
        lock.lock()
        let c = continuation
        let t = timer
        continuation = nil
        timer = nil
        lock.unlock()
        t?.cancel()
        c?.resume(returning: value)
        return c != nil
    }

    @discardableResult
    func fail(_ error: Error) -> Bool {
        lock.lock()
        let c = continuation
        let t = timer
        continuation = nil
        timer = nil
        lock.unlock()
        t?.cancel()
        c?.resume(throwing: error)
        return c != nil
    }
}
