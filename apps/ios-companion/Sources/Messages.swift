import Foundation
import Contacts
import UserNotifications

/// Texts on the glasses with the phone alone (2026-10-23). iOS lets no app
/// read or send Messages itself, so Apple's Shortcuts app carries them:
///   in:  a Message automation runs "Show Text on Glasses" (Intents.swift)
///        with the sender and the text;
///   out: after the glasses' confirm gate JOAT posts a notification; a
///        Notification automation runs "Get Text to Send", Send Message
///        (Show When Run off), Mark as Read and "Text Sent on Glasses".
/// Grammar and the confirm-before-send rule are the Mac's
/// (services/message_reply.py): "reply …", a bare "reply", "message <name>
/// saying …". Nothing leaves the phone before the wearer taps yes.
final class Messages: VoiceCommand {
    struct Text: Codable {
        let id: String
        let from: String
        let handle: String?
        let body: String
        let at: Date
        let outgoing: Bool
        /// Who wrote it, in a group thread (`from` is then the group).
        var speaker: String? = nil
    }

    struct Outgoing: Codable {
        let id: String
        let label: String
        let recipient: String
        let body: String
        let queuedAt: Date
        /// The glasses' draft this is (a tap reply): its states go back.
        var uiAction: String? = nil
        /// A group chat's name ("Family"): the send automation finds that
        /// conversation (iOS 27 Send Message takes group chats). Nil for one person.
        var group: String? = nil
    }

    static let storeKey = "joat.messages.v1"
    static let keep = 60
    /// message_reply.py MESSAGE_SELECTION_TTL_SECONDS
    static let selectionSeconds: TimeInterval = 120
    /// "Reply …" answers the newest text for this long.
    static let recentSeconds: TimeInterval = 600
    /// message_reply.py REPLY_PROMPT_TTL_SECONDS: "reply …" without the name.
    static let unaddressedSeconds: TimeInterval = 60
    /// A text the automation has not collected by then is dropped, never
    /// sent late.
    static let sendWindow: TimeInterval = 60
    /// The send automation's title filter. "•" is on the iPhone keyboard (·
    /// is not): the wearer's shared shortcut matches "JOAT • text to send".
    static let notificationTitle = "JOAT • text to send"

    private let hud: Hud
    /// Spam checks and contact picks when Apple's on-device model cannot answer.
    private let claude = ClaudeLink()
    /// Texts the spam check kept off the glasses, newest last.
    private(set) var filtered: [Text] = []
    private let confirm: ConfirmGate
    private let cards: Cards
    var say: ((String) -> Void)?
    var askFollowUp: ((String, @escaping (String) -> Void) -> Void)?
    /// The Messages page is open: repaint it.
    var onChange: (() -> Void)?
    /// The glasses' own wire (message.ui) and their questions (Prompts).
    var link: PluginLink?
    var prompts: Prompts?
    /// The conversation on the glasses changed (a reply went out): repaint it.
    var onThreadChange: (() -> Void)?

    private(set) var texts: [Text] = []
    private var outbox: [Outgoing] = []
    private var inFlight: [Outgoing] = []
    private var selected: (from: String, at: Date)?
    private var lastArrival = Date.distantPast

    init(hud: Hud, confirm: ConfirmGate, cards: Cards) {
        self.hud = hud
        self.confirm = confirm
        self.cards = cards
        if let data = UserDefaults.standard.data(forKey: Self.storeKey) {
            do { texts = try JSONDecoder().decode([Text].self, from: data) }
            catch { SafeStore.quarantine(defaultsKey: Self.storeKey, data, error) }
        }
    }

    private func persist() {
        if texts.count > Self.keep { texts.removeFirst(texts.count - Self.keep) }
        if let data = try? JSONEncoder().encode(texts) { UserDefaults.standard.set(data, forKey: Self.storeKey) }
    }

    // MARK: - In: the Message automation

    /// The wearer's automation names a group text "Group (Sender)".
    static let groupRe = try! NSRegularExpression(pattern: #"^(?<group>.+) \((?<person>[^()]+)\)$"#)

    static func looksLikeHandle(_ s: String) -> Bool {
        s.contains("@") || (s.filter(\.isNumber).count >= 5 && s.allSatisfy({ $0.isNumber || " +-().".contains($0) }))
    }

    /// A text from the Message automation: its sender's saved name, its
    /// group, then the spam check (SpamCheck: it can never stop a text the
    /// way a refused Shortcuts step does). Spam stays off the glasses.
    /// `done` runs once the text is on the glasses or kept off as spam (the
    /// Shortcuts action waits for it: iOS may suspend JOAT Brain as soon as
    /// the action returns).
    func arrived(from rawFrom: String, body rawBody: String, done: (() -> Void)? = nil) {
        var from = rawFrom.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        // "<Group Name> (<Sender>)" with no group name: one person.
        if from.hasPrefix("("), from.hasSuffix(")"), from.count > 2 {
            from = String(from.dropFirst().dropLast()).trimmingCharacters(in: .whitespaces)
        }
        let body = rawBody.trimmingCharacters(in: .whitespacesAndNewlines)
        Log.add("text in: from=\(from.isEmpty ? "?" : from) (\(body.count) chars)")
        guard !body.isEmpty else { done?(); return }
        var person = from.isEmpty ? "Unknown" : from
        var group: String?
        if let m = Grammar.match(Self.groupRe, from, groups: ["group", "person"]),
           let g = m["group"], let who = m["person"] {
            group = g
            person = who
        }
        // A number the automation could not name: Contacts may know it.
        let rawHandle = Self.looksLikeHandle(person) ? person : nil
        let savedName = rawHandle.flatMap(Recipients.name(forHandle:))
        let name = savedName ?? person
        let handle = rawHandle ?? Self.resolve(person)?.handle
        // The automation sends a name only when Contacts knows the sender
        // (else the number or email), so a name is a saved contact even when
        // two contacts share it and `resolve` cannot pick one.
        let saved = savedName != nil || rawHandle == nil
        let thread = group ?? name
        // An automation that fires twice for one text shows it once.
        if let last = texts.last, !last.outgoing, last.from == thread, last.body == body,
           Date().timeIntervalSince(last.at) < 10 { done?(); return }
        let text = Text(id: ConfirmGate.hex(8), from: thread, handle: group == nil ? handle : nil, body: body,
                        at: Date(), outgoing: false, speaker: group == nil ? nil : name)
        SpamCheck.check(from: name, saved: saved || group != nil, body: body, claude: claude) { [weak self] spam in
            defer { done?() }
            guard let self else { return }
            if spam == true {
                Log.add("text in: kept off the glasses as spam")
                self.filtered.append(text)
                if self.filtered.count > 30 { self.filtered.removeFirst(self.filtered.count - 30) }
                return
            }
            self.texts.append(text)
            self.persist()
            self.lastArrival = Date()
            if let group {
                self.cards.post(source: group, channel: "Messages", summary: "\(name): \(body)", body: "\(name): \(body)")
                self.say?("\(name) in \(group) says: \(body)")
            } else {
                self.cards.post(source: name, channel: "Messages", summary: body, body: body)
                self.say?("\(name) says: \(body)")
            }
            self.onChange?()
        }
    }

    // MARK: - Voice

    static let replyRe = try! NSRegularExpression(
        pattern: #"^(?:reply|respond)[,:]?(?:\s+(?:saying|with)[,:]?)?\s+(?<body>.+)$"#, options: [.caseInsensitive])
    static let bareReplyRe = try! NSRegularExpression(
        pattern: #"^(?:reply|respond)(?:\s+(?:to\s+)?(?:this|that|them|him|her|it))?$"#, options: [.caseInsensitive])
    static let newMessageRe = try! NSRegularExpression(
        pattern: #"^(?:message|text|send\s+(?:a\s+)?(?:message|text)(?:\s+to)?)[,:]?(?:\s+to)?\s+(?<recipient>.+?)[,:]?\s+(?:saying|with)[,:]?\s+(?<body>.+)$"#,
        options: [.caseInsensitive])

    /// "reply …" with no name, shortly after a text arrived.
    func takesUnaddressed(_ text: String, now: Date = Date()) -> Bool {
        guard now.timeIntervalSince(lastArrival) < Self.unaddressedSeconds else { return false }
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return Grammar.match(Self.replyRe, t) != nil || Grammar.match(Self.bareReplyRe, Grammar.normalize(t)) != nil
    }

    func handle(_ request: String) -> CommandReply? {
        let said = request.trimmingCharacters(in: .whitespacesAndNewlines)
        if Grammar.match(Self.bareReplyRe, Grammar.normalize(said)) != nil {
            guard let target = replyTarget() else { return CommandReply(text: Self.noTarget, activated: false) }
            askFollowUp?("What should I say to \(target.label)?") { [weak self] answer in
                self?.draft(label: target.label, recipient: target.recipient, body: answer)
            }
            return CommandReply(text: "", activated: false)
        }
        if let g = Grammar.match(Self.replyRe, said, groups: ["body"]), let body = g["body"] {
            guard let target = replyTarget() else { return CommandReply(text: Self.noTarget, activated: false) }
            draft(label: target.label, recipient: target.recipient, body: body)
            return CommandReply(text: "", activated: false)
        }
        if let g = Grammar.match(Self.newMessageRe, said, groups: ["recipient", "body"]),
           let name = g["recipient"], let body = g["body"] {
            // "my wife", a nickname, a number not in Contacts (Recipients).
            Recipients.find(name, claude: claude) { [weak self] match in
                guard let self else { return }
                guard let match else { self.finish("I can't find \(name) in your contacts."); return }
                self.draft(label: match.label, recipient: match.handle, body: body)
            }
            return CommandReply(text: "", activated: false)
        }
        return nil
    }

    static let noTarget = "No recent text to reply to. Open Messages and choose one."

    /// The conversation chosen on the Messages page, else the newest text.
    private func replyTarget(now: Date = Date()) -> (label: String, recipient: String)? {
        let from: String
        if let s = selected, now.timeIntervalSince(s.at) < Self.selectionSeconds {
            from = s.from
        } else if let last = texts.last(where: { !$0.outgoing }), now.timeIntervalSince(last.at) < Self.recentSeconds {
            from = last.from
        } else {
            return nil
        }
        return (from, replyRecipient(from))
    }

    /// A group thread ("Family"): its texts carry who wrote each one.
    private func isGroup(_ thread: String) -> Bool {
        texts.contains { $0.from == thread && $0.speaker != nil }
    }

    /// Where a reply to `thread` goes: a group by its name (the send
    /// automation finds that conversation; looking the name up in Contacts
    /// could text one person instead), a person by number or email.
    private func replyRecipient(_ thread: String) -> String {
        if isGroup(thread) { return thread }
        let known = texts.last(where: { $0.from == thread && $0.handle != nil })?.handle
        return known ?? Self.resolve(thread)?.handle ?? thread
    }

    private func draft(label: String, recipient: String, body rawBody: String) {
        let body = rawBody.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { finish("Nothing to send."); return }
        let prompt = "Send to \(Self.fit(label, units: 24)): “\(Self.fit(body, units: 80))”?"
        confirm.ask(prompt: prompt, payload: "messages.send|\(recipient)|\(body)") { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined: self.finish("Message cancelled.")
            case .unverified: self.finish("Message was not sent because confirmation could not be verified.")
            case .accepted: self.enqueue(label: label, recipient: recipient, body: body)
            }
        }
    }

    // MARK: - Out: the Notification automation

    private func enqueue(label: String, recipient: String, body: String, uiAction: String? = nil) {
        let group = isGroup(label) ? label : nil
        let item = Outgoing(id: ConfirmGate.hex(8), label: label, recipient: recipient, body: body, queuedAt: Date(),
                            uiAction: uiAction, group: group)
        outbox.append(item)
        let via = group != nil ? "group" : recipient == label ? "name" : "handle"
        Log.add("text out queued: to=\(label) via \(via) (\(body.count) chars)")
        let content = UNMutableNotificationContent()
        content.title = Self.notificationTitle
        content.body = "To \(label)"
        content.threadIdentifier = "joat.outbox"
        let request = UNNotificationRequest(identifier: "joat.outbox.\(item.id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [weak self] error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    Log.add("text out notification failed: \(error)")
                    self.outbox.removeAll { $0.id == item.id }
                    self.uiFailed(item, "Not sent: allow notifications for JOAT Brain.")
                    self.finish("Not sent: allow notifications for JOAT Brain.")
                    return
                }
                if item.uiAction == nil { self.hud.receipt("Sending to \(label)…") }
            }
        }
        // The automation never came: say so, and never send it late.
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.sendWindow) { [weak self] in
            guard let self, self.outbox.contains(where: { $0.id == item.id }) else { return }
            self.outbox.removeAll { $0.id == item.id }
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["joat.outbox.\(item.id)"])
            Log.add("text out expired: the Shortcuts automation did not collect it")
            self.uiFailed(item, "The Shortcuts automation did not run.")
            self.finish("The text to \(label) did not go out. The Shortcuts automation did not run.")
        }
    }

    /// "Get Text to Send": the oldest text the wearer confirmed.
    func nextOutgoing() -> Outgoing? {
        outbox.removeAll { Date().timeIntervalSince($0.queuedAt) > Self.sendWindow }
        guard !outbox.isEmpty else {
            Log.add("text out requested, none waiting")
            return nil
        }
        let item = outbox.removeFirst()
        inFlight.append(item)
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["joat.outbox.\(item.id)"])
        Log.add("text out collected by Shortcuts: to=\(item.label)")
        return item
    }

    /// "Text Sent on Glasses": Send Message ran without an error.
    func sent() {
        // The shortcut runs Get Text to Send, Send Message, then this, within
        // seconds: the text it sent is the newest one collected. One whose
        // Send Message failed never reports, so it expires instead of
        // taking the next text's confirmation (review, 2026-10-30).
        inFlight.removeAll { Date().timeIntervalSince($0.queuedAt) > 2 * Self.sendWindow }
        guard !inFlight.isEmpty else {
            Log.add("text sent reported, nothing in flight")
            return
        }
        let item = inFlight.removeLast()
        texts.append(Text(id: item.id, from: item.label, handle: item.recipient, body: item.body, at: Date(), outgoing: true))
        persist()
        Log.add("text out sent: to=\(item.label)")
        if let a = item.uiAction, let d = draftUI, d.id == a {
            ui(d, "sent", "Sent to \(item.label).")
            // Back on the thread with the reply as its newest row (▶).
            if threadSender == item.label { onThreadChange?() } else { hud.receipt("Sent to \(item.label).") }
        } else {
            finish("Sent to \(item.label).")
        }
        onChange?()
    }

    private func uiFailed(_ item: Outgoing, _ text: String) {
        guard let a = item.uiAction, let d = draftUI, d.id == a else { return }
        ui(d, "failed", text)
    }

    private func finish(_ text: String) {
        hud.receipt(text)
        say?(text)
    }

    // MARK: - The Messages page (capabilities._show_messages, the Mac's wire)

    /// The plugin knows these pages by their exact titles (MessagesViewState):
    /// "MESSAGES" is the picker whose rows open a conversation, "MESSAGES ·
    /// CONVERSATION" the thread a tap replies to. Any other title is drawn
    /// as an untappable page, which is why a tap did nothing before
    /// (2026-10-30: "MESSAGES · 3" and "MESSAGES · MOM").
    static let newMessageRow = "New message · voice recipient + text"

    /// The picker: New message first, then one row per sender, newest first.
    func pageList() -> (list: [String: Any], senders: [String]) {
        var senders: [String] = []
        for t in texts.reversed() where !senders.contains(t.from) { senders.append(t.from) }
        let shown = Array(senders.prefix(18))
        var rows = [Self.newMessageRow]
        rows += shown.map { sender -> String in
            let last = texts.last { $0.from == sender }!
            let preview = last.body.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            let who = last.outgoing ? "You: " : last.speaker.map { "\($0): " } ?? ""
            return "[\(Cards.age(last.at))] \(sender) · \(who)\(preview)"
        }
        if shown.isEmpty { rows.append("No texts yet · they arrive once the Shortcuts automation is on") }
        return (["title": "MESSAGES", "items": rows, "hint": "tap: select · left x2: back", "selectable": true], shown)
    }

    /// One conversation, newest message first, as the reader the glasses
    /// scroll; a tap replies. `traceId` is the tap on the picker that opened
    /// it: the plugin binds the reply's draft to that visit.
    func thread(_ sender: String, requestId: String?, traceId: String?) -> [String: Any] {
        selected = (sender, Date())
        threadSender = sender
        if let requestId, let traceId { visit = (requestId, traceId) }
        if let traceId { threadReaderId = traceId }
        let entries = texts.filter { $0.from == sender }.suffix(30).reversed()
        let newestOwn = entries.first?.outgoing == true
        let lines = entries.enumerated().map { i, t -> String in
            let mark = (i == 0 && newestOwn) ? " \(lastSendGlyph[t.id] ?? "▶")" : ""
            return "\(t.outgoing ? "You" : t.speaker ?? sender): \(t.body)\(mark)"
        }
        return ["title": "MESSAGES · CONVERSATION", "items": [String](),
                "body": "\(sender)\n\n" + (lines.isEmpty ? "No messages in this conversation yet." : lines.joined(separator: "\n")),
                "contextLabel": sender, "hint": "tap: reply · left x2: back", "selectable": true,
                "readerPage": ["id": threadReaderId, "index": 0, "count": 1]]
    }

    /// The thread's state glyph for a text still going out (▷) or refused (×).
    private var lastSendGlyph: [String: String] = [:]
    /// The visit the plugin bound to the picker tap: (requestId, selectionId).
    private var visit: (requestId: String, selectionId: String)?
    /// The conversation on the glasses, and its reader's id (kept for repaints).
    private var threadSender: String?
    private var threadReaderId = "thread-" + ConfirmGate.hex(8)

    // MARK: - A draft from the glasses (message_ui.py)

    private final class Draft {
        let id = "messages-ui-" + ConfirmGate.hex(16)
        let requestId: String
        let selectionId: String
        var label: String
        var recipient: String
        var state = "preparing"
        init(requestId: String, selectionId: String, label: String, recipient: String) {
            self.requestId = requestId; self.selectionId = selectionId
            self.label = label; self.recipient = recipient
        }
    }
    private var draftUI: Draft?

    private static let finishedStates: Set<String> = ["cancelled", "sent", "failed", "uncertain"]

    /// message.ui: exactly these six keys, or the plugin drops it.
    private func ui(_ d: Draft, _ state: String, _ text: String) {
        d.state = state
        link?.send(["kind": "message.ui", "requestId": d.requestId, "selectionId": d.selectionId,
                    "actionId": d.id, "state": state, "text": String(text.prefix(1000))])
        Log.add("tx message.ui \(state): \(text.prefix(80))")
        if Self.finishedStates.contains(state), draftUI === d { draftUI = nil }
    }

    /// A tap on the conversation: ask what to say, then confirm, then send.
    func beginReply() {
        guard let v = visit, let sender = threadSender else {
            finish("Open Messages and choose a conversation first.")
            return
        }
        if let d = draftUI, !Self.finishedStates.contains(d.state) { return }
        let d = Draft(requestId: v.requestId, selectionId: v.selectionId, label: sender,
                      recipient: replyRecipient(sender))
        draftUI = d
        ui(d, "preparing", "Preparing your message…")
        askBody(d)
    }

    /// "New message" on the picker: who, then what.
    func beginNew(requestId: String?, traceId: String?) {
        guard let requestId, let traceId else { return }
        visit = (requestId, traceId)
        threadSender = nil
        if let d = draftUI, !Self.finishedStates.contains(d.state) { return }
        let d = Draft(requestId: requestId, selectionId: traceId, label: "", recipient: "")
        draftUI = d
        ui(d, "preparing", "Preparing your message…")
        askRecipient(d, question: "Who should I text?", tries: 2)
    }

    private func askRecipient(_ d: Draft, question: String, tries: Int) {
        ui(d, "awaiting_input", question)
        prompts?.ask(question, ownerKind: "messages", ownerId: d.id, field: "recipient") { [weak self] said in
            guard let self, self.draftUI === d else { return }
            guard let said, !said.isEmpty else { self.ui(d, "cancelled", "Message cancelled."); return }
            // "my wife", a nickname, a number not in Contacts (Recipients).
            Recipients.find(said, claude: self.claude) { [weak self] match in
                guard let self, self.draftUI === d else { return }
                guard let match else {
                    if tries > 1 {
                        self.askRecipient(d, question: "I can't find \(Self.fit(said, units: 40)) in your contacts. Who should I text?",
                                          tries: tries - 1)
                    } else {
                        self.ui(d, "failed", "No contact matched \(said).")
                        self.finish("No contact matched \(said).")
                    }
                    return
                }
                d.label = match.label
                d.recipient = match.handle
                self.askBody(d)
            }
        }
    }

    private func askBody(_ d: Draft) {
        let question = "What should I say to \(Self.fit(d.label, units: 40))?"
        ui(d, "awaiting_input", question)
        prompts?.ask(question, ownerKind: "messages", ownerId: d.id, field: "body") { [weak self] said in
            guard let self, self.draftUI === d else { return }
            guard let said, !said.isEmpty else { self.ui(d, "cancelled", "Message cancelled."); return }
            self.confirmDraft(d, body: said)
        }
    }

    private func confirmDraft(_ d: Draft, body: String) {
        ui(d, "awaiting_confirmation", "Awaiting your confirmation on the glasses.")
        let prompt = "Send to \(Self.fit(d.label, units: 24))? “\(Self.fit(body, units: 80))”"
        confirm.ask(prompt: prompt, payload: "messages.send|\(d.recipient)|\(body)|\(d.id)",
                    diagnosticKind: "messages", messageUIActionId: d.id) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined: self.ui(d, "cancelled", "Message cancelled.")
            case .unverified: self.ui(d, "cancelled", "Message was not sent because confirmation could not be verified.")
            case .accepted:
                self.ui(d, "sending", "Sending to \(d.label)…")
                self.enqueue(label: d.label, recipient: d.recipient, body: body, uiAction: d.id)
            }
        }
    }

    /// The plugin dismissed the draft's question or card (2x tap).
    func cancelUI(actionId: String) {
        guard let d = draftUI, d.id == actionId else { return }
        prompts?.cancel("draft cancelled on the glasses")
        ui(d, "cancelled", "Message cancelled.")
    }

    /// Long press on a draft: say it again.
    func retryUI(actionId: String) {
        guard let d = draftUI, d.id == actionId else { return }
        prompts?.cancel("draft retried")
        ui(d, "cancelled", "Say it again.")
        if threadSender != nil { beginReply() }
    }

    /// The wearer left Messages: nothing of the draft survives.
    func leaveUI() {
        if let d = draftUI {
            prompts?.cancel("left Messages")
            ui(d, "cancelled", "Message cancelled.")
        }
        visit = nil
        threadSender = nil
    }

    /// The conversation is on the glasses (for repaint after a send).
    var threadOnGlass: String? { threadSender }

    // MARK: - Contacts

    /// A spoken or Shortcuts name to a number or email. A number or an email
    /// is taken as it is.
    static func resolve(_ raw: String) -> (label: String, handle: String)? {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return nil }
        let digits = name.filter(\.isNumber)
        if name.contains("@") || (digits.count >= 7 && name.allSatisfy({ $0.isNumber || " +-().".contains($0) })) {
            return (name, name)
        }
        let status = CNContactStore.authorizationStatus(for: .contacts)
        guard status == .authorized || status == .limited else {
            Log.add("contacts not allowed (\(status.rawValue)); \(name) stays a name")
            return nil
        }
        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey as CNKeyDescriptor, CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactNicknameKey as CNKeyDescriptor, CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
        ]
        let found = (try? CNContactStore().unifiedContacts(matching: CNContact.predicateForContacts(matchingName: name),
                                                         keysToFetch: keys)) ?? []
        let lower = name.lowercased()
        let exact = found.filter {
            "\($0.givenName) \($0.familyName)".trimmingCharacters(in: .whitespaces).lowercased() == lower
                || $0.nickname.lowercased() == lower || $0.givenName.lowercased() == lower
        }
        guard let contact = exact.count == 1 ? exact[0] : (found.count == 1 ? found[0] : nil) else {
            Log.add("contacts: \(found.count) matches for \(name)")
            return nil
        }
        let label = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
        let mobile = contact.phoneNumbers.first {
            $0.label == CNLabelPhoneNumberMobile || $0.label == CNLabelPhoneNumberiPhone
        } ?? contact.phoneNumbers.first
        if let phone = mobile?.value.stringValue { return (label.isEmpty ? name : label, phone) }
        if let email = contact.emailAddresses.first?.value as String? { return (label.isEmpty ? name : label, email) }
        return nil
    }

    static func fit(_ text: String, units: Int) -> String {
        let t = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard t.utf16.count > units else { return t }
        return t.utf16Prefix(units - 1).trimmingCharacters(in: .whitespaces) + "…"
    }
}
