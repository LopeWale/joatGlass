// Standalone unit test suite for JOAT Brain multi-account Gmail engine:
// GmailAccountStore password normalization, RFC 5322 / MIME / RFC 2047 / HTML / reply stripping,
// byte-accurate IMAP {N}\r\n literal framing + quota-safe UIDVALIDITY/UIDNEXT sync planning,
// and SMTP RFC 5322 formatting + dot-stuffing.
//
// Compile & run (from repo root):
//   swiftc \
//     apps/ios-companion/Sources/GmailKeychain.swift \
//     apps/ios-companion/Sources/MIMEParser.swift \
//     apps/ios-companion/Sources/GmailTransport.swift \
//     tests/ios/test_gmail_mime_and_imap.swift \
//     -o /tmp/test_gmail && /tmp/test_gmail

import Foundation

private var passedAssertions = 0

private func expect(_ condition: @autoclosure () -> Bool, _ message: String, file: StaticString = #file, line: UInt = #line) {
    if !condition() {
        fputs("FAIL [\(file):\(line)]: \(message)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

private func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String, file: StaticString = #file, line: UInt = #line) {
    if actual != expected {
        fputs("FAIL [\(file):\(line)]: \(message)\n  actual:   \(actual)\n  expected: \(expected)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

private func testNormalizeAppPassword() {
    expectEqual(
        GmailAccountStore.normalizeAppPassword("abcd efgh-ijkl mnop"),
        "abcdefghijklmnop",
        "App password normalization strips spaces and hyphens"
    )
    expectEqual(
        GmailAccountStore.normalizeAppPassword("  wxyz\t1234\n5678-abcd  "),
        "wxyz12345678abcd",
        "App password normalization strips tabs and newlines"
    )
}

private func testMimeRfc2047AndFoldedHeaders() {
    // Base64 UTF-8 From ("Renée O'Connor") + adjacent Q-encoded UTF-8 folded Subject ("Q4 Launch — Ready 🚀")
    let raw = [
        "From: =?UTF-8?B?UmVuw6llIE8nQ29ubm9y?= <renee@example.com>",
        "To: \"Chen, Sarah\" <sarah@example.com>,",
        "  Alex Rivera <alex@example.org>",
        "Reply-To: team-reply@example.com",
        "Subject: =?UTF-8?Q?Q4_Launch_=E2=80=94_?=",
        " =?UTF-8?Q?Ready_=F0=9F=9A=80?=",
        "Date: Fri, 20 Nov 2026 14:15:30 -0600 (CST)",
        "Message-ID: <msg-001@example.com>",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "All systems are green for Tuesday.",
    ].joined(separator: "\r\n")

    let parsed = MIMEParser.parse(raw)
    expectEqual(parsed.from.name, "Renée O'Connor", "Decoded RFC 2047 B-encoded From name")
    expectEqual(parsed.from.email, "renee@example.com", "Extracted From email")
    expectEqual(parsed.to.count, 2, "Parsed two comma-separated To addresses without splitting inside quotes")
    expectEqual(parsed.to[0].name, "Chen, Sarah", "Preserved comma inside quoted display name")
    expectEqual(parsed.to[0].email, "sarah@example.com", "First To email")
    expectEqual(parsed.to[1].email, "alex@example.org", "Second To email")
    expectEqual(parsed.replyTo.first?.email, "team-reply@example.com", "Extracted Reply-To email")
    expectEqual(parsed.subject, "Q4 Launch — Ready 🚀", "Unfolded and joined adjacent RFC 2047 Q-encoded words")
    expectEqual(parsed.messageId, "<msg-001@example.com>", "Extracted Message-ID")
    expect(parsed.date != nil, "Parsed RFC 2822 Date with trailing timezone comment")
    expectEqual(parsed.cleanBody, "All systems are green for Tuesday.", "Plain text body")
    expectEqual(parsed.isBulkOrAutomated, false, "Direct human email is not marked bulk")
}

private func testMimeMultipartHtmlCssAndQuotedReplyStrip() {
    // 25+ KB CSS block before <body> + quoted-printable + Gmail reply chain
    let bigCssRule = ".rule-padding { font-family: Helvetica, Arial, sans-serif; color: #112233; padding: 12px 16px; }\n"
    let hugeCss = String(repeating: bigCssRule, count: 300) // ~28 KB of CSS

    let htmlPart = """
    <html>
    <head>
    <style type="text/css">
    \(hugeCss)
    </style>
    <script>console.log("ignore me");</script>
    </head>
    <body>
      <div>Ship it at 3pm.</div>
      <p>Budget &amp; timeline approved =E2=80=94 thanks!</p>
      <div class=3D"gmail_quote">
        On Fri, Nov 20, 2026 at 2:10 PM Alex Rivera &lt;alex@example.org&gt; wrote:
        <blockquote class=3D"gmail_quote">
          Can we ship at 3pm today?
        </blockquote>
      </div>
    </body>
    </html>
    """

    let rawHtmlOnlyMultipart = [
        "From: Sarah Chen <sarah@example.com>",
        "To: wearer@example.org",
        "Subject: Re: Deployment window",
        "Message-ID: <msg-html-2@example.com>",
        "In-Reply-To: <msg-html-1@example.com>",
        "References: <msg-html-1@example.com>",
        "MIME-Version: 1.0",
        "Content-Type: multipart/alternative; boundary=\"bound-42\"",
        "",
        "--bound-42",
        "Content-Type: text/html; charset=\"UTF-8\"",
        "Content-Transfer-Encoding: quoted-printable",
        "",
        htmlPart,
        "--bound-42--",
        "",
    ].joined(separator: "\r\n")

    let parsedHtml = MIMEParser.parse(rawHtmlOnlyMultipart)
    expect(!parsedHtml.cleanBody.contains("font-family"), "Stripped large <style> block from HTML email")
    expect(!parsedHtml.cleanBody.contains("console.log"), "Stripped <script> block from HTML email")
    expect(parsedHtml.cleanBody.contains("Ship it at 3pm."), "Extracted primary HTML div prose")
    expect(parsedHtml.cleanBody.contains("Budget & timeline approved — thanks!"), "Decoded HTML entities and quoted-printable UTF-8")
    expect(!parsedHtml.cleanBody.contains("Can we ship at 3pm today?"), "Stripped quoted reply block")
    expect(!parsedHtml.cleanBody.contains("wrote:"), "Stripped On ... wrote: attribution line")

    // Prefer text/plain when both text/plain and text/html are present, and strip Outlook-style Original Message
    let rawAlternative = [
        "From: Alex <alex@example.org>",
        "To: wearer@example.org",
        "Subject: Re: Contract review",
        "Message-ID: <msg-alt-2@example.org>",
        "MIME-Version: 1.0",
        "Content-Type: multipart/alternative; boundary=alt-boundary",
        "",
        "--alt-boundary",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: quoted-printable",
        "",
        "Signed copy is ready for launch.=",
        " Let's proceed.",
        "",
        "> Earlier quoted line",
        "-----Original Message-----",
        "From: Wearer",
        "Sent: Friday, November 20, 2026 1:00 PM",
        "Subject: Contract review",
        "",
        "Please check page 4.",
        "--alt-boundary",
        "Content-Type: text/html; charset=utf-8",
        "",
        "<p>HTML fallback that should not win over text/plain</p>",
        "--alt-boundary--",
    ].joined(separator: "\r\n")

    let parsedAlt = MIMEParser.parse(rawAlternative)
    expectEqual(parsedAlt.cleanBody, "Signed copy is ready for launch. Let's proceed.", "Preferred text/plain with soft line break and stripped quoted history")
}

private func testMimeThreadKeyGrouping() {
    let rootKey = MIMEParser.threadKey(
        messageId: "<root-100@example.com>",
        inReplyTo: nil,
        references: [],
        subject: "Q4 Launch"
    )
    let firstReplyKey = MIMEParser.threadKey(
        messageId: "<reply-101@example.com>",
        inReplyTo: "<root-100@example.com>",
        references: ["<root-100@example.com>"],
        subject: "Re: Q4 Launch"
    )
    let deepReplyChangedSubjectKey = MIMEParser.threadKey(
        messageId: "<reply-102@example.com>",
        inReplyTo: "<reply-101@example.com>",
        references: ["<root-100@example.com>", "<reply-101@example.com>"],
        subject: "Updated timeline (was Re: Q4 Launch)"
    )
    let inReplyToOnlyKey = MIMEParser.threadKey(
        messageId: "<reply-103@example.com>",
        inReplyTo: "<root-100@example.com>",
        references: [],
        subject: "Re: Q4 Launch"
    )

    expectEqual(rootKey, "<root-100@example.com>", "Root message uses its Message-ID")
    expectEqual(firstReplyKey, rootKey, "Reply groups with root via References")
    expectEqual(deepReplyChangedSubjectKey, rootKey, "Deep reply groups with root via first References entry")
    expectEqual(inReplyToOnlyKey, rootKey, "Reply without References groups via In-Reply-To")

    let subjA = MIMEParser.threadKey(messageId: "", inReplyTo: nil, references: [], subject: "Budget Review")
    let subjB = MIMEParser.threadKey(messageId: "<orphan@example.com>", inReplyTo: nil, references: [], subject: "Re: Fwd: FW[2]:   budget review  ")
    expectEqual(subjA, subjB, "Headerless Re:/Fwd: messages group via normalized subject")
}

private func testMimeBulkAndAutomatedDetection() {
    let newsletter = MIMEParser.parse([
        "From: Weekly Digest <digest@example.com>",
        "To: wearer@example.org",
        "Subject: Issue #88",
        "List-Unsubscribe: <https://example.com/unsubscribe>",
        "",
        "Top stories this week.",
    ].joined(separator: "\r\n"))
    expectEqual(newsletter.isBulkOrAutomated, true, "List-Unsubscribe marks message as bulk")

    let noreply = MIMEParser.parse([
        "From: Receipt Bot <noreply@example.com>",
        "To: wearer@example.org",
        "Subject: Your receipt",
        "",
        "Total: $12.00",
    ].joined(separator: "\r\n"))
    expectEqual(noreply.isBulkOrAutomated, true, "noreply@ local-part marks message as automated")

    let autoGen = MIMEParser.parse([
        "From: Calendar <calendar@example.com>",
        "Subject: Notification",
        "Auto-Submitted: auto-generated",
        "",
        "Event reminder",
    ].joined(separator: "\r\n"))
    expectEqual(autoGen.isBulkOrAutomated, true, "Auto-Submitted != no marks message as automated")

    let autoNo = MIMEParser.parse([
        "From: Colleague <colleague@example.com>",
        "Subject: Quick question",
        "Auto-Submitted: no",
        "",
        "Are you free at 4?",
    ].joined(separator: "\r\n"))
    expectEqual(autoNo.isBulkOrAutomated, false, "Auto-Submitted: no stays direct")
}

private func testImapLiteralAndUidNextDeltaSync() {
    let literalBody = "Subject: Test\r\nFrom: \"A (B)\" <a@b.com>\r\n\r\nBody with ) paren\r\n"
    let literalBytes = Data(literalBody.utf8)
    expectEqual(literalBytes.count, 61, "Sanity check literal byte length")

    // Exact test case with literal containing quotes, parens, and CRLFs, plus partial BODY[]<0> with fake {99} inside literal
    let secondBody = "From: Sarah <sarah@example.com>\r\nSubject: Fake {99}\r\n\r\nLine with {12}\r\nand )(\r\n"
    let secondBytes = Data(secondBody.utf8)

    var wire = Data()
    func appendASCII(_ s: String) { wire.append(Data(s.utf8)) }
    appendASCII("* 42 EXISTS\r\n")
    appendASCII("* OK [UIDVALIDITY 99] UIDs valid\r\n")
    appendASCII("* OK [UIDNEXT 1050] Predicted next UID\r\n")
    appendASCII("* 42 FETCH (UID 1049 FLAGS (\\Seen) BODY[] {\(literalBytes.count)}\r\n")
    wire.append(literalBytes)
    appendASCII(")\r\n")
    appendASCII("* 43 FETCH (UID 1050 BODY[]<0> {\(secondBytes.count)}\r\n")
    wire.append(secondBytes)
    appendASCII(" FLAGS (\\Flagged))\r\n")
    appendASCII("A002 OK [READ-ONLY] EXAMINE completed\r\n")

    // Feed in tiny 7-byte chunks to verify packet fragmentation resilience
    var buffer = GmailIMAPResponseParser.StreamBuffer()
    var events: [GmailIMAPResponseParser.Event] = []
    var idx = 0
    while idx < wire.count {
        let end = min(wire.count, idx + 7)
        buffer.append(wire.subdata(in: idx..<end))
        while let response = buffer.nextResponse() {
            events.append(contentsOf: GmailIMAPResponseParser.parse(response))
        }
        idx = end
    }

    expect(events.contains(.exists(42)), "Parsed * 42 EXISTS")
    expect(events.contains(.uidValidity(99)), "Parsed UIDVALIDITY 99")
    expect(events.contains(.uidNext(1050)), "Parsed UIDNEXT 1050")

    let fetches: [GmailIMAPResponseParser.FetchedMessage] = events.compactMap {
        if case .fetch(let f) = $0 { return f }
        return nil
    }
    expectEqual(fetches.count, 2, "Parsed both literal FETCH responses without corrupting on embedded parens or {99}")
    expectEqual(fetches[0].seq, 42, "First FETCH seq")
    expectEqual(fetches[0].uid, 1049, "First FETCH UID")
    expectEqual(fetches[0].isSeen, true, "First FETCH \\Seen flag")
    expectEqual(fetches[0].rawBody, literalBytes, "First FETCH literal bytes preserved verbatim")

    let parsedFirst = MIMEParser.parse(fetches[0].rawBody ?? Data())
    expectEqual(parsedFirst.from.name, "A (B)", "MIMEParser parsed From inside IMAP literal")
    expectEqual(parsedFirst.from.email, "a@b.com", "MIMEParser parsed email inside IMAP literal")
    expectEqual(parsedFirst.cleanBody, "Body with ) paren", "MIMEParser preserved body containing closing paren")

    expectEqual(fetches[1].uid, 1050, "Second FETCH UID")
    expectEqual(fetches[1].isSeen, false, "Second FETCH unseen (\\Flagged only)")
    expectEqual(fetches[1].rawBody, secondBytes, "Second FETCH BODY[]<0> literal preserved embedded {99} and {12}")

    let tagged = events.compactMap { event -> (String, String)? in
        if case .tagged(let tag, let status, _) = event { return (tag, status) }
        return nil
    }
    expectEqual(tagged.first?.0, "A002", "Parsed tagged completion tag")
    expectEqual(tagged.first?.1, "OK", "Parsed tagged completion OK status")

    // Verify quota-safe sync plan logic
    expectEqual(
        GmailIMAP.planSync(exists: 42, uidValidity: 99, uidNext: 1050, cachedUIDValidity: 99, cachedUIDNext: 1050, refreshFlags: false),
        .fastIdleExit,
        "Unchanged UIDVALIDITY & UIDNEXT exits immediately with zero FETCH"
    )
    expectEqual(
        GmailIMAP.planSync(exists: 42, uidValidity: 99, uidNext: 1050, cachedUIDValidity: 99, cachedUIDNext: 1050, refreshFlags: true),
        .refreshRecentFlags(sequenceRange: "23:42"),
        "Manual / 5-min idle poll refreshes only recent 20 UID FLAGS"
    )
    expectEqual(
        GmailIMAP.planSync(exists: 42, uidValidity: 99, uidNext: 1050, cachedUIDValidity: nil, cachedUIDNext: nil, refreshFlags: false),
        .bootstrap(sequenceRange: "23:42"),
        "First sync bootstraps newest 20 messages by sequence range without UID SEARCH ALL"
    )
    expectEqual(
        GmailIMAP.planSync(exists: 42, uidValidity: 99, uidNext: 1052, cachedUIDValidity: 99, cachedUIDNext: 1050, refreshFlags: false),
        .delta(uidRange: "1050:*"),
        "Advanced UIDNEXT fetches only delta UIDs >= cachedUIDNext"
    )
}

private func testSmtpFormattingAndDotStuffing() {
    let rawLines = "Hello there\r\n.foo\r\n.\r\n..already\r\nFinal line"
    let stuffed = GmailSMTPFormatter.dotStuff(rawLines)
    expectEqual(
        stuffed,
        "Hello there\r\n..foo\r\n..\r\n...already\r\nFinal line",
        "SMTP dot-stuffing doubles leading dots on every CRLF line"
    )

    let msg = GmailSMTPFormatter.formatMessage(
        fromName: "Wearer Name",
        fromEmail: "wearer@example.org",
        toEmail: "sarah@example.com",
        subject: "Re: Q4 Launch 🚀",
        body: "Sounds good.\n.foo\n.\nThanks!",
        inReplyTo: "<msg-001@example.com>",
        references: ["<msg-000@example.com>", "<msg-001@example.com>"],
        messageId: "<out-123@joatbrain.local>",
        date: Date(timeIntervalSince1970: 1_763_650_000)
    )
    let wirePayload = GmailSMTPFormatter.wireDataPayload(msg)
    expect(wirePayload.contains("In-Reply-To: <msg-001@example.com>\r\n"), "Included In-Reply-To header")
    expect(wirePayload.contains("References: <msg-000@example.com> <msg-001@example.com>\r\n"), "Included References header")
    expect(wirePayload.contains("\r\n..foo\r\n..\r\nThanks!\r\n.\r\n"), "Dot-stuffed body and terminated with CRLF.CRLF")
}

@main
enum GmailTestRunner {
    static func main() {
        testNormalizeAppPassword()
        testAccountStoreMetadataInvariants()
        testMimeRfc2047AndFoldedHeaders()
        testMimeMultipartHtmlCssAndQuotedReplyStrip()
        testMimeBase64CharsetAndTruncatedSlice()
        testMimeThreadKeyGrouping()
        testMimeBulkAndAutomatedDetection()
        testImapLiteralAndUidNextDeltaSync()
        testImapMarkSeenCommand()
        testSmtpFormattingAndDotStuffing()

        print("PASS: all \(passedAssertions) Gmail Keychain, MIME, IMAP literal/delta, and SMTP assertions succeeded.")
    }
}

private func testImapMarkSeenCommand() {
    expectEqual(
        GmailIMAP.markSeenCommand(for: [105, 101, 105, 0]),
        "UID STORE 101,105 +FLAGS.SILENT (\\Seen)",
        "GmailIMAP.markSeenCommand deduplicates, filters zero, sorts UIDs, and formats +FLAGS.SILENT (\\Seen)"
    )
    expectEqual(
        GmailIMAP.markSeenCommand(for: [0]),
        nil,
        "GmailIMAP.markSeenCommand returns nil when no valid positive UID exists"
    )
}

private func testAccountStoreMetadataInvariants() {
    expectEqual(GmailAccountStore.defaultLabel(for: "alex@gmail.com"), "alex", "Personal Gmail defaults to local-part")
    expectEqual(GmailAccountStore.defaultLabel(for: "alex@acme.example.com"), "Acme", "Workspace domain defaults to capitalized stem")
    expectEqual(GmailAccountStore.isValidEmail("user@example.com"), true, "Valid email accepted")
    expectEqual(GmailAccountStore.isValidEmail("not-an-email"), false, "Invalid email rejected")
}

private func testMimeBase64CharsetAndTruncatedSlice() {
    // Base64 body + 2-line wrapped "On ... wrote:" + truncated multipart without closing "--b--"
    let bodyText = "Confirmed for 9:30 AM tomorrow.\r\n\r\nOn Thu, Nov 19, 2026 at 4:45 PM Someone Longname <someone@example.org>\r\nwrote:\r\n> Does 9:30 AM work?"
    let b64 = Data(bodyText.utf8).base64EncodedString(options: [.lineLength64Characters, .endLineWithCarriageReturn])

    let rawTruncated = [
        "From: someone@example.org (Someone Longname)",
        "To: wearer@example.org",
        "Subject: Morning sync",
        "MIME-Version: 1.0",
        "Content-Type: multipart/mixed; boundary=\"mix-9\"",
        "",
        "--mix-9",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: base64",
        "",
        b64,
        // Intentionally omit "--mix-9--" to simulate a 128 KB BODY.PEEK[]<0.131072> cutoff
    ].joined(separator: "\r\n")

    let parsed = MIMEParser.parse(rawTruncated)
    expectEqual(parsed.from.name, "Someone Longname", "Parsed address comment display name")
    expectEqual(parsed.from.email, "someone@example.org", "Parsed address comment email")
    expectEqual(parsed.cleanBody, "Confirmed for 9:30 AM tomorrow.", "Decoded base64, salvaged truncated multipart, and stripped 2-line On...wrote:")
}
