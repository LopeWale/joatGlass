import Foundation

/// Pure, zero-dependency RFC 5322 + MIME + RFC 2047 email parser for JOAT Brain.
enum MIMEParser {
    struct Address: Codable, Equatable {
        let name: String
        let email: String
    }

    struct ParsedEmail: Equatable {
        let messageId: String
        let inReplyTo: String?
        let references: [String]
        let from: Address
        let to: [Address]
        let cc: [Address]
        let replyTo: [Address]
        let subject: String
        let date: Date?
        let plainBody: String
        let cleanBody: String
        let snippet: String
        let threadKey: String
        let isBulkOrAutomated: Bool
        let headers: [String: [String]]
    }

    // MARK: - Top-Level Entry Points

    static func parse(_ rawString: String) -> ParsedEmail {
        parse(Data(rawString.utf8))
    }

    static func parse(_ rawData: Data) -> ParsedEmail {
        let (headerData, bodyData) = splitHeadersAndBody(rawData)
        let headers = parseHeaders(headerData)

        let rawMessageId = firstHeader("message-id", in: headers) ?? ""
        let messageId = extractMessageIds(rawMessageId).first ?? ""

        let rawInReplyTo = firstHeader("in-reply-to", in: headers) ?? ""
        let inReplyTo = extractMessageIds(rawInReplyTo).first

        let allRefHeaders = (headers["references"] ?? []).joined(separator: " ")
        let references = extractMessageIds(allRefHeaders)

        let fromList = parseAddresses(firstHeader("from", in: headers) ?? "")
        let from = fromList.first ?? Address(name: "", email: "")
        let to = parseAddresses((headers["to"] ?? []).joined(separator: ", "))
        let cc = parseAddresses((headers["cc"] ?? []).joined(separator: ", "))
        let replyTo = parseAddresses((headers["reply-to"] ?? []).joined(separator: ", "))

        let subject = decodeRFC2047(firstHeader("subject", in: headers) ?? "")
            .split(whereSeparator: { $0 == "\r" || $0 == "\n" })
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        let date = parseDate(firstHeader("date", in: headers) ?? "")

        let extractedBody = extractEntityText(headers: headers, bodyData: bodyData, depth: 0)
        let plainBody = normalizeLineEndings(extractedBody).trimmingCharacters(in: .whitespacesAndNewlines)
        let cleaned = cleanReplyText(plainBody)
        let effectiveClean = cleaned.isEmpty ? plainBody : cleaned

        let snippetWords = effectiveClean.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let snippet = snippetWords.count > 180 ? String(snippetWords.prefix(179)).trimmingCharacters(in: .whitespaces) + "…" : snippetWords

        let computedThreadKey = threadKey(
            messageId: messageId,
            inReplyTo: inReplyTo,
            references: references,
            subject: subject
        )
        let bulk = isBulkOrAutomated(headers: headers, fromEmail: from.email)

        return ParsedEmail(
            messageId: messageId,
            inReplyTo: inReplyTo,
            references: references,
            from: from,
            to: to,
            cc: cc,
            replyTo: replyTo,
            subject: subject,
            date: date,
            plainBody: plainBody,
            cleanBody: effectiveClean,
            snippet: snippet,
            threadKey: computedThreadKey,
            isBulkOrAutomated: bulk,
            headers: headers
        )
    }

    // MARK: - Header Splitting & Unfolding

    private static func splitHeadersAndBody(_ data: Data) -> (Data, Data) {
        let bytes = [UInt8](data)
        let count = bytes.count
        var i = 0
        while i < count {
            if i + 3 < count,
               bytes[i] == 0x0D, bytes[i + 1] == 0x0A,
               bytes[i + 2] == 0x0D, bytes[i + 3] == 0x0A {
                return (data.subdata(in: 0..<i), data.subdata(in: (i + 4)..<count))
            }
            if i + 1 < count,
               bytes[i] == 0x0A, bytes[i + 1] == 0x0A {
                return (data.subdata(in: 0..<i), data.subdata(in: (i + 2)..<count))
            }
            i += 1
        }
        // Check if data begins with headers and has no body
        let text = decodeBytes(data, charset: "utf-8")
        if text.contains(":") {
            return (data, Data())
        }
        return (Data(), data)
    }

    private static func parseHeaders(_ headerData: Data) -> [String: [String]] {
        guard !headerData.isEmpty else { return [:] }
        let raw = decodeBytes(headerData, charset: "utf-8")
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")

        var unfoldedLines: [String] = []
        for line in raw.components(separatedBy: "\n") {
            if let first = line.first, (first == " " || first == "\t"), !unfoldedLines.isEmpty {
                let cont = line.drop(while: { $0 == " " || $0 == "\t" })
                unfoldedLines[unfoldedLines.count - 1] += " " + cont
            } else if !line.isEmpty {
                unfoldedLines.append(line)
            }
        }

        var map: [String: [String]] = [:]
        for line in unfoldedLines {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { continue }
            map[name, default: []].append(value)
        }
        return map
    }

    private static func firstHeader(_ name: String, in headers: [String: [String]]) -> String? {
        headers[name.lowercased()]?.first
    }

    // MARK: - RFC 2047 Encoded-Word Decoding

    private static let rfc2047Regex = try! NSRegularExpression(
        pattern: #"=\?([^?\s]+)\?([bBqQ])\?([^?]*)\?="#,
        options: []
    )

    static func decodeRFC2047(_ text: String) -> String {
        guard text.contains("=?") else { return text }
        let nsRange = NSRange(text.startIndex..., in: text)
        let matches = rfc2047Regex.matches(in: text, range: nsRange)
        guard !matches.isEmpty else { return text }

        struct Segment {
            let isEncoded: Bool
            let charset: String
            let bytes: Data
            let literal: String
        }

        var segments: [Segment] = []
        var cursor = text.startIndex

        for match in matches {
            guard let fullRange = Range(match.range, in: text),
                  let charsetRange = Range(match.range(at: 1), in: text),
                  let encodingRange = Range(match.range(at: 2), in: text),
                  let payloadRange = Range(match.range(at: 3), in: text) else {
                continue
            }

            if cursor < fullRange.lowerBound {
                let between = String(text[cursor..<fullRange.lowerBound])
                // RFC 2047 §6.2: linear whitespace between two encoded-words is ignored.
                if let prev = segments.last, prev.isEncoded, between.allSatisfy(\.isWhitespace) {
                    // Drop separating whitespace between adjacent encoded words.
                } else {
                    segments.append(Segment(isEncoded: false, charset: "", bytes: Data(), literal: between))
                }
            }

            let charset = String(text[charsetRange]).lowercased()
            let encoding = String(text[encodingRange]).uppercased()
            let payload = String(text[payloadRange])

            let decodedData: Data?
            if encoding == "B" {
                decodedData = decodeBase64(payload)
            } else {
                decodedData = decodeQEncoding(payload)
            }

            if let data = decodedData {
                if let last = segments.last, last.isEncoded, last.charset == charset {
                    var combined = last.bytes
                    combined.append(data)
                    segments[segments.count - 1] = Segment(isEncoded: true, charset: charset, bytes: combined, literal: "")
                } else {
                    segments.append(Segment(isEncoded: true, charset: charset, bytes: data, literal: ""))
                }
            } else {
                segments.append(Segment(isEncoded: false, charset: "", bytes: Data(), literal: String(text[fullRange])))
            }

            cursor = fullRange.upperBound
        }

        if cursor < text.endIndex {
            segments.append(Segment(isEncoded: false, charset: "", bytes: Data(), literal: String(text[cursor...])))
        }

        return segments.map { seg in
            seg.isEncoded ? decodeBytes(seg.bytes, charset: seg.charset) : seg.literal
        }.joined()
    }

    private static func decodeQEncoding(_ text: String) -> Data {
        let utf8 = Array(text.utf8)
        var out = Data()
        out.reserveCapacity(utf8.count)
        var i = 0
        while i < utf8.count {
            let b = utf8[i]
            if b == 0x5F { // '_' -> space (0x20) in RFC 2047 Q-encoding
                out.append(0x20)
                i += 1
            } else if b == 0x3D, i + 2 < utf8.count,
                      let hi = hexVal(utf8[i + 1]),
                      let lo = hexVal(utf8[i + 2]) {
                out.append((hi << 4) | lo)
                i += 3
            } else {
                out.append(b)
                i += 1
            }
        }
        return out
    }

    // MARK: - Address Parsing

    static func parseAddresses(_ raw: String) -> [Address] {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        // Split on commas outside quotes, angle brackets, and parentheses
        var chunks: [String] = []
        var current = ""
        var inQuotes = false
        var escaped = false
        var angleDepth = 0
        var parenDepth = 0

        for ch in trimmed {
            if escaped {
                current.append(ch)
                escaped = false
                continue
            }
            if ch == "\\" && inQuotes {
                current.append(ch)
                escaped = true
                continue
            }
            if ch == "\"" {
                inQuotes.toggle()
                current.append(ch)
                continue
            }
            if !inQuotes {
                if ch == "<" { angleDepth += 1 }
                else if ch == ">" { angleDepth = max(0, angleDepth - 1) }
                else if ch == "(" { parenDepth += 1 }
                else if ch == ")" { parenDepth = max(0, parenDepth - 1) }
                else if ch == "," && angleDepth == 0 && parenDepth == 0 {
                    chunks.append(current)
                    current = ""
                    continue
                }
            }
            current.append(ch)
        }
        if !current.isEmpty {
            chunks.append(current)
        }

        return chunks.compactMap(parseSingleAddress)
    }

    private static func parseSingleAddress(_ chunk: String) -> Address? {
        let s = chunk.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }

        // Form 1: Display Name <email@domain>
        if let openAngle = lastUnquotedIndex(of: "<", in: s),
           let closeAngle = s[openAngle...].firstIndex(of: ">"),
           openAngle < closeAngle {
            let rawEmail = s[s.index(after: openAngle)..<closeAngle]
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
            let rawName = String(s[..<openAngle]).trimmingCharacters(in: .whitespacesAndNewlines)
            let name = cleanDisplayName(rawName)
            guard !rawEmail.isEmpty else { return nil }
            return Address(name: name, email: rawEmail)
        }

        // Form 2: email@domain (Display Name)
        if let openParen = s.firstIndex(of: "("),
           let closeParen = s.lastIndex(of: ")"),
           openParen < closeParen {
            let before = s[..<openParen].trimmingCharacters(in: .whitespacesAndNewlines)
            if before.contains("@") && !before.contains(where: \.isWhitespace) {
                let comment = String(s[s.index(after: openParen)..<closeParen])
                return Address(name: cleanDisplayName(comment), email: before.lowercased())
            }
        }

        // Form 3: bare email@domain (or RFC 2047 decoded string)
        let decoded = decodeRFC2047(s).trimmingCharacters(in: .whitespacesAndNewlines)
        let unquoted = unquote(decoded)
        if unquoted.contains("@"), !unquoted.contains(where: \.isWhitespace) {
            return Address(name: "", email: unquoted.lowercased())
        }
        return nil
    }

    private static func lastUnquotedIndex(of target: Character, in s: String) -> String.Index? {
        var inQuotes = false
        var escaped = false
        var found: String.Index?
        var idx = s.startIndex
        while idx < s.endIndex {
            let ch = s[idx]
            if escaped {
                escaped = false
            } else if ch == "\\" && inQuotes {
                escaped = true
            } else if ch == "\"" {
                inQuotes.toggle()
            } else if !inQuotes && ch == target {
                found = idx
            }
            idx = s.index(after: idx)
        }
        return found
    }

    private static func cleanDisplayName(_ raw: String) -> String {
        var name = unquote(raw.trimmingCharacters(in: .whitespacesAndNewlines))
        name = decodeRFC2047(name).trimmingCharacters(in: .whitespacesAndNewlines)
        name = unquote(name)
        return name.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    private static func unquote(_ s: String) -> String {
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.count >= 2, (t.hasPrefix("\"") && t.hasSuffix("\"")) || (t.hasPrefix("'") && t.hasSuffix("'")) {
            t = String(t.dropFirst().dropLast())
            var out = ""
            var escaped = false
            for ch in t {
                if escaped {
                    out.append(ch)
                    escaped = false
                } else if ch == "\\" {
                    escaped = true
                } else {
                    out.append(ch)
                }
            }
            return out
        }
        return t
    }

    // MARK: - Message-IDs, Threading & Bulk Classification

    private static let angleIdRegex = try! NSRegularExpression(pattern: #"<([^<>\s]+)>"#, options: [])

    static func extractMessageIds(_ raw: String) -> [String] {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }
        let matches = angleIdRegex.matches(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed))
        if !matches.isEmpty {
            return matches.compactMap { m in
                guard let r = Range(m.range(at: 1), in: trimmed) else { return nil }
                return "<\(trimmed[r])>"
            }
        }
        let token = trimmed.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
        return token.isEmpty ? [] : ["<\(token)>"]
    }

    private static let replyPrefixRegex = try! NSRegularExpression(
        pattern: #"^(?:\s*(?:(?:re|fw|fwd)(?:\[\d+\])?\s*:\s*|\[\s*fwd\s*:\s*))+"#,
        options: [.caseInsensitive]
    )

    static func hasReplyOrForwardPrefix(_ subject: String) -> Bool {
        let range = NSRange(subject.startIndex..., in: subject)
        return replyPrefixRegex.firstMatch(in: subject, options: [.anchored], range: range) != nil
    }

    static func normalizeSubject(_ subject: String) -> String {
        var s = subject.trimmingCharacters(in: .whitespacesAndNewlines)
        let hadBracketFwd = s.lowercased().hasPrefix("[fwd:") && s.hasSuffix("]")
        s = replyPrefixRegex.stringByReplacingMatches(
            in: s,
            options: [.anchored],
            range: NSRange(s.startIndex..., in: s),
            withTemplate: ""
        )
        if hadBracketFwd && s.hasSuffix("]") {
            s.removeLast()
        }
        return s.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased()
    }

    /// Groups messages into threads using the root `References` ID (else `In-Reply-To`,
    /// else root `Message-ID` for original non-reply messages, else normalized subject
    /// stripped of `Re:`/`Fwd:` prefixes).
    static func threadKey(
        messageId: String,
        inReplyTo: String?,
        references: [String],
        subject: String
    ) -> String {
        if let rootRef = references.first(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) {
            return normalizeIdToken(rootRef)
        }
        if let reply = inReplyTo?.trimmingCharacters(in: .whitespaces), !reply.isEmpty {
            return normalizeIdToken(reply)
        }
        let normSubj = normalizeSubject(subject)
        let cleanMsgId = messageId.trimmingCharacters(in: .whitespaces)
        if !normSubj.isEmpty && (cleanMsgId.isEmpty || hasReplyOrForwardPrefix(subject)) {
            return "subject:\(normSubj)"
        }
        if !cleanMsgId.isEmpty {
            return normalizeIdToken(cleanMsgId)
        }
        return normSubj.isEmpty ? "subject:(no-subject)" : "subject:\(normSubj)"
    }

    private static func normalizeIdToken(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("<") && t.hasSuffix(">") { return t }
        return "<\(t)>"
    }

    static func isBulkOrAutomated(headers: [String: [String]], fromEmail: String) -> Bool {
        if let unsub = firstHeader("list-unsubscribe", in: headers), !unsub.isEmpty {
            return true
        }
        if let precedence = firstHeader("precedence", in: headers)?.lowercased().trimmingCharacters(in: .whitespaces),
           ["bulk", "list", "junk"].contains(precedence) {
            return true
        }
        if let autoSub = firstHeader("auto-submitted", in: headers)?.lowercased().trimmingCharacters(in: .whitespaces),
           !autoSub.isEmpty, autoSub != "no" {
            return true
        }
        let local = fromEmail.lowercased().split(separator: "@", maxSplits: 1).first.map(String.init) ?? ""
        if ["noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"].contains(local)
            || local.hasPrefix("noreply+")
            || local.hasPrefix("no-reply+")
            || local.hasSuffix("-noreply")
            || local.hasSuffix(".noreply")
            || local.hasSuffix("_noreply") {
            return true
        }
        return false
    }

    // MARK: - MIME Multipart & Transfer Decoding

    private static func extractEntityText(headers: [String: [String]], bodyData: Data, depth: Int) -> String {
        guard depth <= 8 else { return "" }
        let contentTypeRaw = firstHeader("content-type", in: headers) ?? "text/plain; charset=utf-8"
        let (mediaType, params) = parseHeaderParams(contentTypeRaw)
        let dispositionRaw = firstHeader("content-disposition", in: headers) ?? ""
        let (disposition, _) = parseHeaderParams(dispositionRaw)

        if disposition == "attachment" {
            return ""
        }

        if mediaType.hasPrefix("multipart/") {
            guard let boundary = params["boundary"], !boundary.isEmpty else {
                return decodePartString(bodyData, transferEncoding: firstHeader("content-transfer-encoding", in: headers), charset: params["charset"])
            }
            let parts = splitMultipart(bodyData, boundary: boundary)
            if mediaType == "multipart/alternative" {
                var plainCandidate: String?
                var htmlCandidate: String?
                var otherCandidate: String?
                for partData in parts {
                    let (pHeaderData, pBodyData) = splitHeadersAndBody(partData)
                    let pHeaders = parseHeaders(pHeaderData)
                    let (pType, _) = parseHeaderParams(firstHeader("content-type", in: pHeaders) ?? "text/plain")
                    let text = extractEntityText(headers: pHeaders, bodyData: pBodyData, depth: depth + 1)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { continue }
                    if pType == "text/plain" {
                        plainCandidate = text
                    } else if pType == "text/html" {
                        htmlCandidate = text
                    } else {
                        otherCandidate = text
                    }
                }
                return plainCandidate ?? htmlCandidate ?? otherCandidate ?? ""
            } else {
                var blocks: [String] = []
                for partData in parts {
                    let (pHeaderData, pBodyData) = splitHeadersAndBody(partData)
                    let pHeaders = parseHeaders(pHeaderData)
                    let text = extractEntityText(headers: pHeaders, bodyData: pBodyData, depth: depth + 1)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    if !text.isEmpty {
                        blocks.append(text)
                    }
                }
                return blocks.joined(separator: "\n\n")
            }
        }

        if mediaType == "text/plain" || mediaType.isEmpty {
            return decodePartString(
                bodyData,
                transferEncoding: firstHeader("content-transfer-encoding", in: headers),
                charset: params["charset"]
            )
        }

        if mediaType == "text/html" {
            let html = decodePartString(
                bodyData,
                transferEncoding: firstHeader("content-transfer-encoding", in: headers),
                charset: params["charset"]
            )
            return extractHTMLText(html)
        }

        return ""
    }

    private static func parseHeaderParams(_ raw: String) -> (main: String, params: [String: String]) {
        let parts = splitSemicolonParams(raw)
        guard let first = parts.first else { return ("", [:]) }
        let main = first.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        var params: [String: String] = [:]
        for p in parts.dropFirst() {
            guard let eq = p.firstIndex(of: "=") else { continue }
            var key = p[..<eq].trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if key.hasSuffix("*") { key.removeLast() }
            let val = unquote(String(p[p.index(after: eq)...]))
            params[key] = decodeRFC2047(val)
        }
        return (main, params)
    }

    private static func splitSemicolonParams(_ raw: String) -> [String] {
        var out: [String] = []
        var current = ""
        var inQuotes = false
        var escaped = false
        for ch in raw {
            if escaped {
                current.append(ch)
                escaped = false
                continue
            }
            if ch == "\\" && inQuotes {
                current.append(ch)
                escaped = true
                continue
            }
            if ch == "\"" {
                inQuotes.toggle()
                current.append(ch)
                continue
            }
            if ch == ";" && !inQuotes {
                out.append(current)
                current = ""
                continue
            }
            current.append(ch)
        }
        if !current.isEmpty { out.append(current) }
        return out
    }

    private static func splitMultipart(_ data: Data, boundary: String) -> [Data] {
        let marker = Array("--\(boundary)".utf8)
        let bytes = [UInt8](data)
        guard !marker.isEmpty, bytes.count >= marker.count else { return [] }

        var boundaryPositions: [(lineStart: Int, afterLine: Int, isClosing: Bool)] = []
        var lineStart = 0
        while lineStart < bytes.count {
            var lineEnd = lineStart
            while lineEnd < bytes.count && bytes[lineEnd] != 0x0A {
                lineEnd += 1
            }
            var contentEnd = lineEnd
            if contentEnd > lineStart && bytes[contentEnd - 1] == 0x0D {
                contentEnd -= 1
            }
            let lineLen = contentEnd - lineStart
            if lineLen >= marker.count && Array(bytes[lineStart..<(lineStart + marker.count)]) == marker {
                let tail = Array(bytes[(lineStart + marker.count)..<contentEnd])
                let trimmedTail = tail.drop(while: { $0 == 0x20 || $0 == 0x09 })
                if trimmedTail.isEmpty {
                    let nextPos = min(bytes.count, lineEnd + 1)
                    boundaryPositions.append((lineStart, nextPos, false))
                } else if trimmedTail.count >= 2 && trimmedTail.prefix(2) == [0x2D, 0x2D] {
                    let afterDashes = trimmedTail.dropFirst(2).drop(while: { $0 == 0x20 || $0 == 0x09 })
                    if afterDashes.isEmpty {
                        let nextPos = min(bytes.count, lineEnd + 1)
                        boundaryPositions.append((lineStart, nextPos, true))
                    }
                }
            }
            lineStart = lineEnd + 1
        }

        guard !boundaryPositions.isEmpty else { return [] }
        var parts: [Data] = []
        for idx in 0..<boundaryPositions.count {
            if boundaryPositions[idx].isClosing { break }
            let start = boundaryPositions[idx].afterLine
            let end: Int
            if idx + 1 < boundaryPositions.count {
                var rawEnd = boundaryPositions[idx + 1].lineStart
                if rawEnd >= 2 && bytes[rawEnd - 2] == 0x0D && bytes[rawEnd - 1] == 0x0A {
                    rawEnd -= 2
                } else if rawEnd >= 1 && bytes[rawEnd - 1] == 0x0A {
                    rawEnd -= 1
                }
                end = rawEnd
            } else {
                // Truncated 128 KB slice before closing boundary: salvage the in-progress part!
                end = bytes.count
            }
            if start <= end && start < bytes.count {
                parts.append(data.subdata(in: start..<end))
            }
        }
        return parts
    }

    private static func decodePartString(_ bodyData: Data, transferEncoding: String?, charset: String?) -> String {
        let enc = (transferEncoding ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let decodedBytes: Data
        switch enc {
        case "quoted-printable":
            decodedBytes = decodeQuotedPrintable(bodyData)
        case "base64":
            let ascii = String(decoding: bodyData, as: UTF8.self)
            decodedBytes = decodeBase64(ascii) ?? bodyData
        default:
            decodedBytes = bodyData
        }
        return decodeBytes(decodedBytes, charset: charset ?? "utf-8")
    }

    static func decodeQuotedPrintable(_ data: Data) -> Data {
        let bytes = [UInt8](data)
        var out = Data()
        out.reserveCapacity(bytes.count)
        var i = 0
        while i < bytes.count {
            let b = bytes[i]
            if b == 0x3D { // '='
                // Soft line break: '=' followed by optional spaces/tabs and CRLF or LF
                var j = i + 1
                while j < bytes.count && (bytes[j] == 0x20 || bytes[j] == 0x09) {
                    j += 1
                }
                if j < bytes.count && bytes[j] == 0x0D && j + 1 < bytes.count && bytes[j + 1] == 0x0A {
                    i = j + 2
                    continue
                }
                if j < bytes.count && bytes[j] == 0x0A {
                    i = j + 1
                    continue
                }
                if j == bytes.count {
                    // Trailing '=' at end of input
                    break
                }
                if i + 2 < bytes.count,
                   let hi = hexVal(bytes[i + 1]),
                   let lo = hexVal(bytes[i + 2]) {
                    out.append((hi << 4) | lo)
                    i += 3
                    continue
                }
            }
            out.append(b)
            i += 1
        }
        return out
    }

    private static func decodeBase64(_ raw: String) -> Data? {
        var clean = String(raw.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) })
        let rem = clean.count % 4
        if rem > 0 {
            clean += String(repeating: "=", count: 4 - rem)
        }
        return Data(base64Encoded: clean, options: [.ignoreUnknownCharacters])
    }

    private static func decodeBytes(_ data: Data, charset: String) -> String {
        let norm = charset.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch norm {
        case "", "utf-8", "utf8":
            if let s = String(data: data, encoding: .utf8) { return s }
        case "us-ascii", "ascii":
            if let s = String(data: data, encoding: .ascii) ?? String(data: data, encoding: .utf8) { return s }
        case "iso-8859-1", "iso_8859-1", "latin1", "latin-1":
            if let s = String(data: data, encoding: .isoLatin1) { return s }
        case "windows-1252", "cp1252", "x-cp1252":
            if let s = String(data: data, encoding: .windowsCP1252) { return s }
        default:
            let cfEnc = CFStringConvertIANACharSetNameToEncoding(norm as CFString)
            if cfEnc != kCFStringEncodingInvalidId {
                let nsEnc = CFStringConvertEncodingToNSStringEncoding(cfEnc)
                if let s = String(data: data, encoding: String.Encoding(rawValue: nsEnc)) {
                    return s
                }
            }
        }
        return String(data: data, encoding: .utf8)
            ?? String(data: data, encoding: .windowsCP1252)
            ?? String(data: data, encoding: .isoLatin1)
            ?? String(decoding: data, as: UTF8.self)
    }

    private static func hexVal(_ byte: UInt8) -> UInt8? {
        switch byte {
        case 0x30...0x39: return byte - 0x30
        case 0x41...0x46: return byte - 0x41 + 10
        case 0x61...0x66: return byte - 0x61 + 10
        default: return nil
        }
    }

    // MARK: - HTML Extraction & Quoted Reply Stripping

    private static let htmlDropBlocksRegex = try! NSRegularExpression(
        pattern: #"<!--[\s\S]*?-->|<(script|style|head|noscript|svg|template|title|xml)\b[^>]*>[\s\S]*?</\1\s*>|<(script|style)\b[^>]*>[\s\S]*$"#,
        options: [.caseInsensitive]
    )
    private static let htmlBlockquoteRegex = try! NSRegularExpression(
        pattern: #"<blockquote\b[^>]*>([\s\S]*?)</blockquote\s*>"#,
        options: [.caseInsensitive]
    )
    private static let htmlBrRegex = try! NSRegularExpression(
        pattern: #"<br\s*/?\s*>"#,
        options: [.caseInsensitive]
    )
    private static let htmlLiOpenRegex = try! NSRegularExpression(
        pattern: #"<li\b[^>]*>"#,
        options: [.caseInsensitive]
    )
    private static let htmlBlockBoundaryRegex = try! NSRegularExpression(
        pattern: #"</?(?:p|div|tr|table|section|article|header|footer|h[1-6]|ul|ol|pre|hr|blockquote)\b[^>]*>"#,
        options: [.caseInsensitive]
    )
    private static let htmlCellRegex = try! NSRegularExpression(
        pattern: #"</?(?:td|th)\b[^>]*>"#,
        options: [.caseInsensitive]
    )
    private static let htmlTagRegex = try! NSRegularExpression(
        pattern: #"<[^>]+>"#,
        options: []
    )
    private static let invisibleScalars: Set<Unicode.Scalar> = [
        "\u{00AD}", "\u{200B}", "\u{200C}", "\u{200D}", "\u{2060}", "\u{FEFF}", "\u{034F}",
    ]

    static func extractHTMLText(_ html: String) -> String {
        guard !html.isEmpty else { return "" }
        var s = htmlDropBlocksRegex.stringByReplacingMatches(
            in: html,
            range: NSRange(html.startIndex..., in: html),
            withTemplate: " "
        )

        // Prefix lines inside <blockquote>...</blockquote> with "> " so quoted replies strip cleanly.
        for _ in 0..<4 {
            let range = NSRange(s.startIndex..., in: s)
            guard let m = htmlBlockquoteRegex.firstMatch(in: s, range: range),
                  let fullR = Range(m.range, in: s),
                  let innerR = Range(m.range(at: 1), in: s) else { break }
            let innerText = extractHTMLPlainWithoutBlockquotes(String(s[innerR]))
            let quoted = innerText
                .components(separatedBy: "\n")
                .map { "> " + $0 }
                .joined(separator: "\n")
            s.replaceSubrange(fullR, with: "\n" + quoted + "\n")
        }

        return extractHTMLPlainWithoutBlockquotes(s)
    }

    private static func extractHTMLPlainWithoutBlockquotes(_ fragment: String) -> String {
        var s = htmlBrRegex.stringByReplacingMatches(in: fragment, range: NSRange(fragment.startIndex..., in: fragment), withTemplate: "\n")
        s = htmlLiOpenRegex.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: "\n- ")
        s = htmlBlockBoundaryRegex.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: "\n")
        s = htmlCellRegex.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: " ")
        s = htmlTagRegex.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: "")
        s = decodeHTMLEntities(s)
        s = String(s.unicodeScalars.filter { !invisibleScalars.contains($0) })
        s = s.replacingOccurrences(of: "\u{00A0}", with: " ")
        s = normalizeLineEndings(s)

        var lines: [String] = []
        var blankRun = 0
        for rawLine in s.components(separatedBy: "\n") {
            let trimmed = rawLine.split(whereSeparator: { $0 == " " || $0 == "\t" }).joined(separator: " ")
            if trimmed.isEmpty {
                blankRun += 1
                if blankRun <= 1 && !lines.isEmpty {
                    lines.append("")
                }
            } else {
                blankRun = 0
                lines.append(trimmed)
            }
        }
        return lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let namedHTMLEntities: [String: String] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "#39": "'",
        "nbsp": " ", "ndash": "–", "mdash": "—", "hellip": "…",
        "lsquo": "‘", "rsquo": "’", "ldquo": "“", "rdquo": "”",
        "bull": "•", "middot": "·", "copy": "©", "reg": "®", "trade": "™",
        "euro": "€", "pound": "£", "yen": "¥", "deg": "°", "times": "×",
        "eacute": "é", "egrave": "è", "aacute": "á", "agrave": "à",
        "iacute": "í", "oacute": "ó", "uacute": "ú", "ntilde": "ñ",
        "ccedil": "ç", "uuml": "ü", "ouml": "ö", "auml": "ä",
    ]

    static func decodeHTMLEntities(_ s: String) -> String {
        guard s.contains("&") else { return s }
        var out = ""
        out.reserveCapacity(s.count)
        var i = s.startIndex
        while i < s.endIndex {
            if s[i] == "&", let semi = s[i...].prefix(14).firstIndex(of: ";") {
                let token = String(s[s.index(after: i)..<semi])
                var decoded: String?
                if token.hasPrefix("#x") || token.hasPrefix("#X") {
                    decoded = UInt32(token.dropFirst(2), radix: 16).flatMap(Unicode.Scalar.init).map(String.init)
                } else if token.hasPrefix("#") {
                    decoded = UInt32(token.dropFirst(), radix: 10).flatMap(Unicode.Scalar.init).map(String.init)
                } else {
                    decoded = namedHTMLEntities[token] ?? namedHTMLEntities[token.lowercased()]
                }
                if let d = decoded {
                    out += d
                    i = s.index(after: semi)
                    continue
                }
            }
            out.append(s[i])
            i = s.index(after: i)
        }
        return out
    }

    private static let onWroteSingleRegex = try! NSRegularExpression(
        pattern: #"^On\s+.+\bwrote:\s*$"#,
        options: [.caseInsensitive]
    )
    private static let originalMessageSeparatorRegex = try! NSRegularExpression(
        pattern: #"^-{2,}\s*Original Message\s*-{2,}\s*$"#,
        options: [.caseInsensitive]
    )
    private static let underscoreSeparatorRegex = try! NSRegularExpression(
        pattern: #"^_{6,}\s*$"#,
        options: []
    )

    /// Strips quoted reply chains (`On ... wrote:`, `-----Original Message-----`,
    /// `From: ... Sent: ...`, leading `>` quote blocks) and collapses excessive blank lines.
    static func cleanReplyText(_ plain: String) -> String {
        let normalized = normalizeLineEndings(plain)
        let rawLines = normalized.components(separatedBy: "\n")
        var kept: [String] = []
        var i = 0

        while i < rawLines.count {
            let line = rawLines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Cut at -----Original Message-----
            if originalMessageSeparatorRegex.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)) != nil {
                break
            }

            // Cut at Outlook horizontal rule followed by From:
            if underscoreSeparatorRegex.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)) != nil,
               i + 1 < rawLines.count,
               rawLines[i + 1].trimmingCharacters(in: .whitespaces).lowercased().hasPrefix("from:") {
                break
            }

            // Cut at Outlook From: / Sent: (or Date:) / To: (or Subject:) header block
            if trimmed.lowercased().hasPrefix("from:"), i + 2 < rawLines.count {
                let next1 = rawLines[i + 1].trimmingCharacters(in: .whitespaces).lowercased()
                let next2 = rawLines[i + 2].trimmingCharacters(in: .whitespaces).lowercased()
                if (next1.hasPrefix("sent:") || next1.hasPrefix("date:")) &&
                    (next2.hasPrefix("to:") || next2.hasPrefix("subject:")) {
                    break
                }
            }

            // Cut at single-line "On ... wrote:"
            if onWroteSingleRegex.firstMatch(in: trimmed, range: NSRange(trimmed.startIndex..., in: trimmed)) != nil {
                break
            }

            // Cut at 2-line wrapped "On <date/person>\n... wrote:"
            if trimmed.hasPrefix("On ") && i + 1 < rawLines.count {
                let joined = trimmed + " " + rawLines[i + 1].trimmingCharacters(in: .whitespaces)
                if onWroteSingleRegex.firstMatch(in: joined, range: NSRange(joined.startIndex..., in: joined)) != nil {
                    break
                }
            }

            // Skip leading '>' quoted lines
            if trimmed.hasPrefix(">") {
                i += 1
                continue
            }

            kept.append(line.replacingOccurrences(of: #"[ \t]+$"#, with: "", options: .regularExpression))
            i += 1
        }

        // Collapse 3+ newlines (2+ empty lines) down to at most one blank line between paragraphs
        var collapsed: [String] = []
        var emptyCount = 0
        for line in kept {
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                emptyCount += 1
                if emptyCount <= 1 && !collapsed.isEmpty {
                    collapsed.append("")
                }
            } else {
                emptyCount = 0
                collapsed.append(line)
            }
        }
        return collapsed.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func normalizeLineEndings(_ s: String) -> String {
        s.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
    }

    // MARK: - RFC 2822 / IMAP Date Parsing

    private static let commentRegex = try! NSRegularExpression(pattern: #"\([^)]*\)"#, options: [])

    static func parseDate(_ raw: String) -> Date? {
        let stripped = commentRegex.stringByReplacingMatches(
            in: raw,
            range: NSRange(raw.startIndex..., in: raw),
            withTemplate: " "
        )
        let clean = stripped.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard !clean.isEmpty else { return nil }

        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)

        let formats = [
            "EEE, d MMM yyyy HH:mm:ss Z",
            "EEE, d MMM yyyy HH:mm:ss zzz",
            "d MMM yyyy HH:mm:ss Z",
            "d MMM yyyy HH:mm:ss zzz",
            "EEE, d MMM yyyy HH:mm Z",
            "EEE, d MMM yyyy HH:mm zzz",
            "d MMM yyyy HH:mm Z",
            "EEE, d MMM yy HH:mm:ss Z",
            "d-MMM-yyyy HH:mm:ss Z",
            "dd-MMM-yyyy HH:mm:ss Z",
            "EEE, d MMM yyyy HH:mm:ss",
        ]
        for format in formats {
            f.dateFormat = format
            if let d = f.date(from: clean) { return d }
        }

        let iso = ISO8601DateFormatter()
        for opts: ISO8601DateFormatter.Options in [[.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime]] {
            iso.formatOptions = opts
            if let d = iso.date(from: clean) { return d }
        }
        return nil
    }
}
