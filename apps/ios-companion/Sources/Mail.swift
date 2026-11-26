import Foundation
import SwiftUI
import UIKit
import Contacts
import Combine
import FoundationModels

/// Standalone multi-account Gmail engine on the iPhone (`imap.gmail.com:993` +
/// `smtp.gmail.com:465`), serving both the `Mail` tab in `JOAT Brain` and the
/// `MAIL` capability + voice/confirm flows on the Even Realities G2 glasses.
final class Mail: ObservableObject, VoiceCommand, HoldableChannel {
    struct MessageItem: Codable, Identifiable, Equatable {
        let id: String
        let uid: UInt32
        let accountId: String
        let accountLabel: String
        let accountEmail: String
        let threadKey: String
        let messageIdHeader: String
        let inReplyTo: String?
        let references: [String]
        let fromName: String
        let fromEmail: String
        let replyToEmail: String
        let toEmails: [String]
        let subject: String
        let snippet: String
        let body: String
        let at: Date
        var unread: Bool
        let outgoing: Bool
        let isBulk: Bool
    }

    struct ThreadSummary: Identifiable, Equatable {
        let id: String          // "\(accountId)|\(threadKey)"
        let accountId: String
        let accountLabel: String
        let accountEmail: String
        let subject: String
        let participantLabel: String
        let replyEmail: String
        let preview: String
        let at: Date
        let unread: Bool
        let messages: [MessageItem]
    }

    private struct SyncWatermark: Codable {
        var uidValidity: UInt32
        var uidNext: UInt32
        var lastFlagsRefreshAt: Date?
    }

    static let keepPerAccount = 60
    static let activePollSeconds: TimeInterval = 45
    static let backgroundPollSeconds: TimeInterval = 180
    static let flagsRefreshInterval: TimeInterval = 300
    static let staleArrivalSeconds: TimeInterval = 600
    static let selectionSeconds: TimeInterval = 120
    static let recentReplySeconds: TimeInterval = 600
    static let unaddressedSeconds: TimeInterval = 60
    static let newEmailRow = "+ New email"
    static let noTarget = "No recent email to reply to. Open Mail and choose one."

    @Published private(set) var threads: [ThreadSummary] = []
    @Published private(set) var syncing: Bool = false
    @Published private(set) var lastSyncAt: Date? = nil
    @Published private(set) var accountErrors: [String: String] = [:]
    @AppStorage("joat.mail.directOnly") var directOnlyOnGlasses: Bool = true

    let accountStore: GmailAccountStore
    var link: PluginLink?
    var prompts: Prompts?
    var say: ((String) -> Void)?
    var askFollowUp: ((String, @escaping (String) -> Void) -> Void)?
    var onChange: (() -> Void)?
    var onThreadChange: (() -> Void)?
    var glassesReady: (() -> Bool)?
    private(set) var lastArrival: Date = .distantPast
    var threadOnGlass: String? { threadIdOnGlass }

    private let hud: Hud
    private let confirm: ConfirmGate
    private let cards: Cards
    private let claude = ClaudeLink()
    private let cacheURL: URL

    private var messagesByAccount: [String: [MessageItem]] = [:]
    private var heldIds: [String] = []
    private var selectedThread: (id: String, at: Date)?
    private var visit: (requestId: String, selectionId: String)?
    private var threadIdOnGlass: String?
    private var threadReaderId = "mail-thread-" + ConfirmGate.hex(8)
    private var poller: Task<Void, Never>?
    private var activeSyncTask: Task<Void, Never>?
    private var cancellables: Set<AnyCancellable> = []

    init(hud: Hud, confirm: ConfirmGate, cards: Cards, accountStore: GmailAccountStore = .shared) {
        self.hud = hud
        self.confirm = confirm
        self.cards = cards
        self.accountStore = accountStore
        self.cacheURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("mail-cache.json")
        loadCache()
        rebuildThreads(notify: false)
    }

    // MARK: - Lifecycle & Adaptive Polling

    func start() {
        guard poller == nil else { return }

        accountStore.$accounts
            .dropFirst()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                self.pruneRemovedAccounts()
                self.rebuildThreads(notify: true)
                Task { @MainActor [weak self] in
                    await self?.syncNow()
                }
            }
            .store(in: &cancellables)

        NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in
                    await self?.performSyncIfNeeded(manual: false)
                }
            }
            .store(in: &cancellables)

        poller = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.performSyncIfNeeded(manual: false)
            while !Task.isCancelled {
                let targetInterval = self.isActivelyUsed ? Self.activePollSeconds : Self.backgroundPollSeconds
                var waited: TimeInterval = 0
                while waited < targetInterval && !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                    waited += 5
                    if self.isActivelyUsed && waited >= Self.activePollSeconds {
                        break
                    }
                }
                if Task.isCancelled { return }
                await self.performSyncIfNeeded(manual: false)
            }
        }
    }

    private var isActivelyUsed: Bool {
        if UIApplication.shared.applicationState == .active { return true }
        if let server = link as? LoopbackServer, server.clients > 0 { return true }
        if let ready = glassesReady, ready() { return true }
        return false
    }

    func syncNow() async {
        await MainActor.run { () -> Task<Void, Never> in
            if let existing = self.activeSyncTask {
                return Task { @MainActor in
                    await existing.value
                    await self.runSyncCycle(manual: true)
                }
            }
            let task = Task { @MainActor in
                await self.runSyncCycle(manual: true)
            }
            self.activeSyncTask = task
            return task
        }.value
    }

    @MainActor
    private func performSyncIfNeeded(manual: Bool) async {
        guard activeSyncTask == nil else { return }
        let task = Task { @MainActor in
            await self.runSyncCycle(manual: manual)
        }
        activeSyncTask = task
        await task.value
    }

    @MainActor
    private func runSyncCycle(manual: Bool) async {
        defer { activeSyncTask = nil }
        let accounts = accountStore.enabledAccounts
        guard !accounts.isEmpty else {
            syncing = false
            return
        }

        syncing = true
        defer { syncing = false }

        var anyMutated = false
        var newIncomingItems: [(GmailAccount, MessageItem)] = []
        let now = Date()

        for account in accounts {
            guard let password = accountStore.appPassword(for: account.id), !password.isEmpty else {
                accountErrors[account.id] = "Missing App Password. Re-enter it in Mail › Accounts."
                continue
            }

            let watermark = loadWatermark(for: account.id)
            let shouldRefreshFlags = manual
                || watermark?.lastFlagsRefreshAt == nil
                || now.timeIntervalSince(watermark?.lastFlagsRefreshAt ?? .distantPast) >= Self.flagsRefreshInterval

            do {
                let outcome = try await GmailIMAP.syncInbox(
                    email: account.email,
                    appPassword: password,
                    cachedUIDValidity: watermark?.uidValidity,
                    cachedUIDNext: watermark?.uidNext,
                    refreshFlags: shouldRefreshFlags
                )
                accountErrors.removeValue(forKey: account.id)

                switch outcome {
                case .idle(let uidValidity, let uidNext, let flagUpdates):
                    let refreshedAt = flagUpdates != nil ? now : watermark?.lastFlagsRefreshAt
                    saveWatermark(SyncWatermark(uidValidity: uidValidity, uidNext: uidNext, lastFlagsRefreshAt: refreshedAt), for: account.id)
                    if let flagUpdates, applyFlagUpdates(flagUpdates, accountId: account.id) {
                        anyMutated = true
                    }

                case .bootstrap(let uidValidity, let uidNext, let fetched):
                    // First sync: populate Inbox quietly without raising notification cards.
                    let converted = fetched.map { makeMessageItem(from: $0, account: account) }
                    mergeMessages(converted, into: account)
                    saveWatermark(SyncWatermark(uidValidity: uidValidity, uidNext: uidNext, lastFlagsRefreshAt: now), for: account.id)
                    anyMutated = true
                    Log.add("mail bootstrap (\(account.label)): cached \(converted.count) message(s), uidNext=\(uidNext)")

                case .delta(let uidValidity, let uidNext, let newFetched, let flagUpdates):
                    if let flagUpdates, applyFlagUpdates(flagUpdates, accountId: account.id) {
                        anyMutated = true
                    }
                    let existingUIDs = Set((messagesByAccount[account.id] ?? []).map(\.uid))
                    let existingMessageIds = Set((messagesByAccount[account.id] ?? []).map(\.messageIdHeader).filter { !$0.isEmpty })
                    let converted = newFetched.map { makeMessageItem(from: $0, account: account) }
                    for item in converted {
                        let isAlreadyKnown = existingUIDs.contains(item.uid)
                            || (!item.messageIdHeader.isEmpty && existingMessageIds.contains(item.messageIdHeader))
                        if !isAlreadyKnown && !item.outgoing && item.unread {
                            newIncomingItems.append((account, item))
                        }
                    }
                    if !converted.isEmpty {
                        mergeMessages(converted, into: account)
                        anyMutated = true
                    }
                    let refreshedAt = flagUpdates != nil ? now : watermark?.lastFlagsRefreshAt
                    saveWatermark(SyncWatermark(uidValidity: uidValidity, uidNext: uidNext, lastFlagsRefreshAt: refreshedAt), for: account.id)
                    if !converted.isEmpty {
                        Log.add("mail delta (\(account.label)): +\(converted.count) message(s), uidNext=\(uidNext)")
                    }
                }
            } catch {
                let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                accountErrors[account.id] = message
                Log.add("mail sync failed (\(account.label)): \(message)")
            }
        }

        lastSyncAt = Date()
        if anyMutated {
            persistCache()
            rebuildThreads(notify: true)
            if threadIdOnGlass != nil {
                onThreadChange?()
            }
        }

        for (account, item) in newIncomingItems.sorted(by: { $0.1.at < $1.1.at }) {
            await handleIncomingArrival(item, account: account)
        }
    }

    // MARK: - Incoming Card Gating (Direct-Only + SpamCheck + MessageHold)

    @MainActor
    private func handleIncomingArrival(_ item: MessageItem, account: GmailAccount) async {
        if directOnlyOnGlasses && item.isBulk {
            Log.add("mail in (\(account.label)): skipped glasses card for bulk/automated email")
            return
        }

        let isKnownSender = isKnownCorrespondent(email: item.fromEmail)
        let checkText = item.snippet.isEmpty ? item.subject : "\(item.subject) — \(item.snippet)"

        let isSpam: Bool? = await withCheckedContinuation { cont in
            SpamCheck.check(
                from: item.fromName,
                saved: isKnownSender,
                body: checkText,
                claude: claude
            ) { verdict in
                cont.resume(returning: verdict)
            }
        }

        if isSpam == true {
            Log.add("mail in (\(account.label)): kept off the glasses as spam")
            return
        }

        lastArrival = Date()
        presentArrival(item.id)
    }

    private func isKnownCorrespondent(email: String) -> Bool {
        let lower = email.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !lower.isEmpty else { return false }
        if Recipients.name(forHandle: lower) != nil {
            return true
        }
        for list in messagesByAccount.values {
            if list.contains(where: { $0.outgoing && $0.toEmails.contains(where: { $0.lowercased() == lower }) }) {
                return true
            }
        }
        return false
    }

    private func presentArrival(_ itemId: String) {
        guard findMessage(byId: itemId) != nil else { return }
        if let ready = glassesReady, !ready() {
            if !heldIds.contains(itemId) {
                heldIds.append(itemId)
            }
            Log.add("mail in: held until glasses are ready (\(heldIds.count) waiting)")
            onChange?()
            return
        }
        showCard(for: itemId, speak: true)
    }

    func showHeld() {
        guard !heldIds.isEmpty else { return }
        let ids = heldIds
        heldIds.removeAll()
        Log.add("mail in: showing \(ids.count) held email(s)")
        for id in ids {
            showCard(for: id, speak: false)
        }
    }

    private func showCard(for itemId: String, speak: Bool) {
        guard let item = findMessage(byId: itemId) else { return }
        let multiAccount = accountStore.enabledAccounts.count >= 2
        let source = multiAccount ? "[\(item.accountLabel)] \(item.fromName)" : item.fromName
        let summary = item.snippet.isEmpty ? item.subject : "\(item.subject) — \(item.snippet)"
        let fullBody = item.body.isEmpty ? summary : "\(item.subject)\n\(item.body)"
        cards.post(source: source, channel: "Mail", summary: summary, body: fullBody,
                   at: item.at, originId: "mail:\(item.id)")

        let fresh = speak && Date().timeIntervalSince(item.at) <= Self.staleArrivalSeconds
        if fresh {
            say?("Email from \(item.fromName): \(item.subject)")
        }
        onChange?()
    }

    var activitySummaryRow: String {
        if accountStore.enabledAccounts.isEmpty {
            return "Mail · not connected"
        }
        let unread = threads.filter(\.unread).count
        return unread > 0
            ? "Mail · \(threads.count) threads (\(unread) unread)"
            : "Mail · \(threads.count) recent threads"
    }

    private func findMessage(byId id: String) -> MessageItem? {
        for list in messagesByAccount.values {
            if let found = list.last(where: { $0.id == id }) {
                return found
            }
        }
        return nil
    }

    // MARK: - Glasses Capability Surfaces (`MAIL` & `MAIL · CONVERSATION`)

    func pageList() -> (list: [String: Any], threadIds: [String]) {
        let enabled = accountStore.enabledAccounts
        if enabled.isEmpty {
            return (
                [
                    "title": "MAIL · SETUP",
                    "items": [
                        "No Gmail account connected yet.",
                        "Open JOAT Brain › Mail to add Gmail.",
                    ],
                    "hint": "left x2: back",
                    "selectable": false,
                ],
                []
            )
        }

        let visibleThreads = threads.filter { summary in
            guard directOnlyOnGlasses else { return true }
            // Keep threads that have at least one direct message or any user reply
            return summary.messages.contains(where: { !$0.isBulk || $0.outgoing })
        }

        let shown = Array(visibleThreads.prefix(18))
        let multiAccount = enabled.count >= 2
        var rows: [String] = [Self.newEmailRow]

        for t in shown {
            let age = Cards.age(t.at)
            let dot = t.unread ? " •" : ""
            let preview = t.preview.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            let tail = preview.isEmpty ? t.subject : "\(Self.fit(t.subject, units: 22)) — \(preview)"

            if multiAccount {
                let acct = Self.fit(t.accountLabel, units: 10)
                let shortWho = Self.shortParticipant(t.participantLabel, units: 12)
                rows.append("[\(age)] [\(acct)] \(shortWho)\(dot) · \(tail)")
            } else {
                let who = Self.fit(t.participantLabel, units: 16)
                rows.append("[\(age)] \(who)\(dot) · \(tail)")
            }
        }

        if shown.isEmpty {
            rows.append(syncing ? "Syncing your Gmail inbox…" : "No direct emails in your inbox yet")
        }

        return (
            [
                "title": "MAIL",
                "items": rows,
                "hint": "tap: select · left x2: back",
                "selectable": true,
            ],
            shown.map(\.id)
        )
    }

    func thread(_ threadId: String, requestId: String?, traceId: String?) -> [String: Any] {
        selectedThread = (threadId, Date())
        threadIdOnGlass = threadId
        if let requestId, let traceId { visit = (requestId, traceId) }
        if let traceId { threadReaderId = traceId }

        markThreadReadLocally(threadId)

        guard let summary = threads.first(where: { $0.id == threadId }) else {
            return [
                "title": "MAIL · CONVERSATION",
                "items": [String](),
                "body": "Conversation unavailable.\n\nReturn to Mail to refresh your inbox.",
                "contextLabel": "Mail",
                "hint": "left x2: back",
                "selectable": false,
                "readerPage": ["id": threadReaderId, "index": 0, "count": 1],
            ]
        }

        let header = "\(summary.subject)\n\(summary.participantLabel) · via \(summary.accountLabel) (\(summary.accountEmail))"
        let lines = summary.messages.suffix(25).reversed().map { msg -> String in
            let speaker = msg.outgoing ? "You" : (msg.fromName.isEmpty ? msg.fromEmail : msg.fromName)
            let prose = msg.body.isEmpty ? msg.snippet : msg.body
            return "\(speaker): \(prose)"
        }
        let content = lines.isEmpty ? "No messages in this thread yet." : lines.joined(separator: "\n\n")

        return [
            "title": "MAIL · CONVERSATION",
            "items": [String](),
            "body": "\(header)\n\n\(content)",
            "contextLabel": summary.participantLabel,
            "hint": "tap: reply · left x2: back",
            "selectable": true,
            "readerPage": ["id": threadReaderId, "index": 0, "count": 1],
        ]
    }

    private func markThreadReadLocally(_ threadId: String) {
        guard let summary = threads.first(where: { $0.id == threadId }),
              summary.unread,
              var list = messagesByAccount[summary.accountId] else { return }
        let targetIds = Set(summary.messages.map(\.id))
        var changed = false
        for i in list.indices where targetIds.contains(list[i].id) && list[i].unread {
            list[i].unread = false
            changed = true
        }
        if changed {
            messagesByAccount[summary.accountId] = list
            persistCache()
            rebuildThreads(notify: false)
        }
    }

    // MARK: - Glasses Draft UI (`message.ui` + `Prompts` + `ConfirmGate`)

    private final class Draft {
        let id = "messages-ui-" + ConfirmGate.hex(16)
        let requestId: String
        let selectionId: String
        let isReply: Bool
        var account: GmailAccount
        var threadId: String?
        var threadKey: String?
        var recipientLabel: String
        var recipientEmail: String
        var subject: String
        var inReplyTo: String?
        var references: [String]
        var state = "preparing"

        init(
            requestId: String,
            selectionId: String,
            isReply: Bool,
            account: GmailAccount,
            threadId: String? = nil,
            threadKey: String? = nil,
            recipientLabel: String = "",
            recipientEmail: String = "",
            subject: String = "",
            inReplyTo: String? = nil,
            references: [String] = []
        ) {
            self.requestId = requestId
            self.selectionId = selectionId
            self.isReply = isReply
            self.account = account
            self.threadId = threadId
            self.threadKey = threadKey
            self.recipientLabel = recipientLabel
            self.recipientEmail = recipientEmail
            self.subject = subject
            self.inReplyTo = inReplyTo
            self.references = references
        }
    }

    private var draftUI: Draft?
    private static let finishedStates: Set<String> = ["cancelled", "sent", "failed", "uncertain"]

    private func ui(_ d: Draft, _ state: String, _ text: String) {
        d.state = state
        link?.send([
            "kind": "message.ui",
            "requestId": d.requestId,
            "selectionId": d.selectionId,
            "actionId": d.id,
            "state": state,
            "text": String(text.prefix(1000)),
        ])
        Log.add("tx mail message.ui \(state): \(text.prefix(80))")
        if Self.finishedStates.contains(state), draftUI === d {
            draftUI = nil
        }
    }

    func beginReply() {
        guard let v = visit,
              let tid = threadIdOnGlass,
              let summary = threads.first(where: { $0.id == tid }) else {
            finish("Open Mail and choose an email thread first.")
            return
        }
        if let d = draftUI, !Self.finishedStates.contains(d.state) { return }
        guard let account = accountStore.accounts.first(where: { $0.id == summary.accountId && $0.enabled }) else {
            finish("That Gmail account is currently disabled.")
            return
        }
        guard !summary.replyEmail.isEmpty else {
            finish("That email cannot be replied to directly.")
            return
        }

        let latest = summary.messages.last
        let replySubj = Self.replySubject(for: summary.subject)
        let inReplyTo = latest?.messageIdHeader.nonEmpty
        var refs = latest?.references ?? []
        if let mid = inReplyTo, !refs.contains(mid) {
            refs.append(mid)
        }

        let d = Draft(
            requestId: v.requestId,
            selectionId: v.selectionId,
            isReply: true,
            account: account,
            threadId: summary.id,
            threadKey: latest?.threadKey,
            recipientLabel: summary.participantLabel,
            recipientEmail: summary.replyEmail,
            subject: replySubj,
            inReplyTo: inReplyTo,
            references: refs
        )
        draftUI = d
        ui(d, "preparing", "Preparing your reply…")
        askReplyBody(d)
    }

    func beginNew(requestId: String?, traceId: String?) {
        guard let requestId, let traceId else { return }
        visit = (requestId, traceId)
        threadIdOnGlass = nil
        if let d = draftUI, !Self.finishedStates.contains(d.state) { return }
        guard let account = accountStore.defaultAccount, account.enabled else {
            finish("Open JOAT Brain › Mail to connect a Gmail account first.")
            return
        }

        let d = Draft(
            requestId: requestId,
            selectionId: traceId,
            isReply: false,
            account: account
        )
        draftUI = d
        ui(d, "preparing", "Preparing your email…")
        askRecipient(d, question: "Who should I email?", tries: 2)
    }

    private func askRecipient(_ d: Draft, question: String, tries: Int) {
        ui(d, "awaiting_input", question)
        prompts?.ask(question, ownerKind: "messages", ownerId: d.id, field: "recipient", hold: true) { [weak self] said in
            guard let self, self.draftUI === d else { return }
            guard let said, !said.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                self.ui(d, "cancelled", "Email cancelled.")
                return
            }
            self.prompts?.note("\(Self.fit(said, units: 40)) · looking up…")
            self.resolveEmailRecipient(said) { [weak self] match in
                guard let self, self.draftUI === d else { return }
                guard let match else {
                    if tries > 1 {
                        self.askRecipient(
                            d,
                            question: "I couldn't find an email for \(Self.fit(said, units: 36)). Who should I email?",
                            tries: tries - 1
                        )
                    } else {
                        self.prompts?.cancel("no email matched")
                        self.ui(d, "failed", "No email address found for \(said).")
                        self.finish("No email address found for \(said).")
                    }
                    return
                }
                d.recipientLabel = match.label
                d.recipientEmail = match.handle
                self.askNewBody(d)
            }
        }
    }

    private func askReplyBody(_ d: Draft) {
        let question = "What should I reply to \(Self.fit(d.recipientLabel, units: 36))?"
        ui(d, "awaiting_input", question)
        prompts?.ask(question, ownerKind: "messages", ownerId: d.id, field: "body") { [weak self] said in
            guard let self, self.draftUI === d else { return }
            let clean = said?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard !clean.isEmpty else {
                self.ui(d, "cancelled", "Reply cancelled.")
                return
            }
            self.confirmUIDraft(d, body: clean)
        }
    }

    private func askNewBody(_ d: Draft) {
        let question = "What should the email to \(Self.fit(d.recipientLabel, units: 36)) say?"
        ui(d, "awaiting_input", question)
        prompts?.ask(question, ownerKind: "messages", ownerId: d.id, field: "body", hold: true) { [weak self] said in
            guard let self, self.draftUI === d else { return }
            let clean = said?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard !clean.isEmpty else {
                self.ui(d, "cancelled", "Email cancelled.")
                return
            }
            self.prompts?.note("Preparing email…")
            self.generateSubject(for: clean) { [weak self] subject in
                guard let self, self.draftUI === d else { return }
                d.subject = subject
                self.prompts?.cancel("moving to confirm")
                self.confirmUIDraft(d, body: clean)
            }
        }
    }

    private func confirmUIDraft(_ d: Draft, body: String) {
        ui(d, "awaiting_confirmation", "Awaiting your confirmation on the glasses.")
        let acctLabel = Self.fit(d.account.label, units: 16)
        let shortEmail = Self.fit(d.recipientEmail, units: 36)
        let header = d.isReply
            ? "Reply (\(acctLabel)) to \(shortEmail)"
            : "Email (\(acctLabel)) to \(shortEmail)"
        let prompt = ConfirmGate.fitMessagePrompt(header: header, body: body, maxUnits: 270)
        let payload = "mail.send|\(d.account.id)|\(d.recipientEmail)|\(d.subject)|\(body)|\(d.id)"

        confirm.ask(
            prompt: prompt,
            payload: payload,
            diagnosticKind: "messages",
            messageUIActionId: d.id
        ) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                self.ui(d, "cancelled", "Email cancelled.")
            case .unverified:
                self.ui(d, "cancelled", "Email was not sent because confirmation could not be verified.")
            case .accepted:
                self.ui(d, "sending", "Sending to \(d.recipientLabel)…")
                self.deliverDraft(
                    account: d.account,
                    recipientLabel: d.recipientLabel,
                    recipientEmail: d.recipientEmail,
                    subject: d.subject,
                    body: body,
                    threadKey: d.threadKey,
                    inReplyTo: d.inReplyTo,
                    references: d.references,
                    uiDraft: d
                )
            }
        }
    }

    func cancelUI(actionId: String) {
        guard let d = draftUI, d.id == actionId else { return }
        prompts?.cancel("mail draft cancelled on the glasses")
        confirm.cancelPending(forMessageActionId: d.id, reason: "mail draft cancelled")
        ui(d, "cancelled", "Email cancelled.")
    }

    func retryUI(actionId: String) {
        guard let d = draftUI, d.id == actionId else { return }
        let wasReply = d.isReply
        let savedAccount = d.account
        let savedLabel = d.recipientLabel
        let savedEmail = d.recipientEmail
        let reqId = d.requestId
        let selId = d.selectionId
        prompts?.cancel("mail draft retried")
        confirm.cancelPending(forMessageActionId: d.id, reason: "mail draft retried")
        ui(d, "cancelled", "Say it again.")

        if wasReply, threadIdOnGlass != nil {
            beginReply()
        } else if !savedEmail.isEmpty {
            let retryDraft = Draft(
                requestId: reqId,
                selectionId: selId,
                isReply: false,
                account: savedAccount,
                recipientLabel: savedLabel,
                recipientEmail: savedEmail
            )
            draftUI = retryDraft
            ui(retryDraft, "preparing", "Preparing your email…")
            askNewBody(retryDraft)
        }
    }

    func returnedToList() {
        if let d = draftUI {
            prompts?.cancel("returned to Mail list")
            confirm.cancelPending(forMessageActionId: d.id, reason: "returned to Mail list")
            ui(d, "cancelled", "Email cancelled.")
        }
        threadIdOnGlass = nil
    }

    func leaveUI() {
        if let d = draftUI {
            prompts?.cancel("left Mail")
            confirm.cancelPending(forMessageActionId: d.id, reason: "left Mail")
            ui(d, "cancelled", "Email cancelled.")
        }
        visit = nil
        threadIdOnGlass = nil
    }

    // MARK: - Voice Commands (`VoiceCommand`)

    private static let explicitBareEmailReplyRe = try! NSRegularExpression(
        pattern: #"^(?:reply|respond)\s+(?:to\s+(?:the\s+|that\s+|this\s+)?(?:last\s+|latest\s+)?(?:email|mail)|by\s+email)$"#,
        options: [.caseInsensitive]
    )
    private static let explicitEmailReplyWithBodyRe = try! NSRegularExpression(
        pattern: #"^(?:reply|respond)\s+(?:to\s+(?:the\s+|that\s+|this\s+)?(?:last\s+|latest\s+)?(?:email|mail)|by\s+email)[,:]?(?:\s+(?:saying|with)[,:]?)?\s+(?<body>.+)$"#,
        options: [.caseInsensitive]
    )
    private static let sendFromAccountEmailRe = try! NSRegularExpression(
        pattern: #"^(?:send\s+(?:an?\s+)?(?:email|mail)\s+from\s+(?<account>.+?)\s+to\s+(?<recipient>.+?)|(?:email|mail)\s+(?<recipient2>.+?)\s+from\s+(?<account2>.+?)|send\s+(?:an?\s+)?(?:email|mail)\s+to\s+(?<recipient3>.+?)\s+from\s+(?<account3>.+?))[,:]?\s+(?:saying|with)[,:]?\s+(?<body>.+)$"#,
        options: [.caseInsensitive]
    )
    private static let newEmailWithBodyRe = try! NSRegularExpression(
        pattern: #"^(?:email|mail|send\s+(?:an?\s+)?(?:email|mail)(?:\s+to)?)[,:]?(?:\s+to)?\s+(?<recipient>.+?)[,:]?\s+(?:saying|with)[,:]?\s+(?<body>.+)$"#,
        options: [.caseInsensitive]
    )
    private static let newEmailRecipientOnlyRe = try! NSRegularExpression(
        pattern: #"^(?:email|send\s+(?:an?\s+)?email\s+to)\s+(?<recipient>.+)$"#,
        options: [.caseInsensitive]
    )

    /// "reply …" with no name, only while an email thread is on the lens
    /// (design 2026-11-23, Goal 4): an arrival alone is not enough.
    func takesUnaddressed(_ text: String, now: Date = Date()) -> Bool {
        guard threadIdOnGlass != nil else { return false }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if Messages.isAIReplyRequest(trimmed) { return false }
        return Grammar.match(Messages.replyRe, trimmed) != nil
            || Grammar.match(Messages.bareReplyRe, Grammar.normalize(trimmed)) != nil
    }

    func handle(_ request: String) -> CommandReply? {
        let said = request.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = Grammar.normalize(said)

        // 1. Explicit "reply to email"
        if Grammar.match(Self.explicitBareEmailReplyRe, normalized) != nil {
            guard let target = replyTarget() else { return CommandReply(text: Self.noTarget, activated: false) }
            askFollowUp?("What should I reply to \(target.summary.participantLabel)?") { [weak self] answer in
                self?.draftVoiceReply(target: target, body: answer)
            }
            return CommandReply(text: "", activated: false)
        }

        if let g = Grammar.match(Self.explicitEmailReplyWithBodyRe, said, groups: ["body"]),
           let body = g["body"] {
            guard let target = replyTarget() else { return CommandReply(text: Self.noTarget, activated: false) }
            draftVoiceReply(target: target, body: body)
            return CommandReply(text: "", activated: false)
        }

        // 2. Generic "reply" / "reply saying ..." when an email target is active/recent
        if Grammar.match(Messages.bareReplyRe, normalized) != nil {
            guard let target = replyTarget() else {
                return threadIdOnGlass != nil ? CommandReply(text: Self.noTarget, activated: false) : nil
            }
            askFollowUp?("What should I reply to \(target.summary.participantLabel)?") { [weak self] answer in
                self?.draftVoiceReply(target: target, body: answer)
            }
            return CommandReply(text: "", activated: false)
        }

        if !Messages.isAIReplyRequest(said),
           let g = Grammar.match(Messages.replyRe, said, groups: ["body"]),
           let body = g["body"] {
            guard let target = replyTarget() else {
                return threadIdOnGlass != nil ? CommandReply(text: Self.noTarget, activated: false) : nil
            }
            draftVoiceReply(target: target, body: body)
            return CommandReply(text: "", activated: false)
        }

        // 3. New email with explicit "from <Account>"
        if let g = Grammar.match(
            Self.sendFromAccountEmailRe,
            said,
            groups: ["account", "recipient", "recipient2", "account2", "recipient3", "account3", "body"]
        ), let body = g["body"] {
            let acctQuery = g["account"] ?? g["account2"] ?? g["account3"]
            let who = g["recipient"] ?? g["recipient2"] ?? g["recipient3"] ?? ""
            composeVoiceEmail(recipientQuery: who, body: body, accountHint: acctQuery)
            return CommandReply(text: "", activated: false)
        }

        // 4. New email: "email <person> saying <body>" / "send an email to <person> saying <body>"
        if let g = Grammar.match(Self.newEmailWithBodyRe, said, groups: ["recipient", "body"]),
           let who = g["recipient"], let body = g["body"] {
            composeVoiceEmail(recipientQuery: who, body: body, accountHint: nil)
            return CommandReply(text: "", activated: false)
        }

        // 5. Recipient-only: "email <person>" -> follow-up for body
        if let g = Grammar.match(Self.newEmailRecipientOnlyRe, said, groups: ["recipient"]),
           let who = g["recipient"],
           Messages.isLikelyRecipientOnlyTarget(who) {
            guard accountStore.defaultAccount != nil else {
                return CommandReply(text: "No Gmail account is connected yet. Add one in JOAT Brain › Mail.", activated: false)
            }
            resolveEmailRecipient(who) { [weak self] match in
                guard let self else { return }
                guard let match else {
                    self.finish("I couldn't find an email address for \(who).")
                    return
                }
                self.askFollowUp?("What should the email to \(match.label) say?") { [weak self] answer in
                    guard let self, let account = self.accountStore.defaultAccount else { return }
                    self.confirmVoiceNewEmail(account: account, recipient: match, body: answer)
                }
            }
            return CommandReply(text: "", activated: false)
        }

        return nil
    }

    private func replyTarget(now: Date = Date()) -> (account: GmailAccount, summary: ThreadSummary)? {
        let candidateSummary: ThreadSummary?
        if let tid = threadIdOnGlass, let open = threads.first(where: { $0.id == tid }) {
            candidateSummary = open
        } else if let s = selectedThread,
                  now.timeIntervalSince(s.at) < Self.selectionSeconds,
                  let chosen = threads.first(where: { $0.id == s.id }) {
            candidateSummary = chosen
        } else if let newest = threads.first(where: { $0.messages.contains(where: { !$0.outgoing }) }),
                  now.timeIntervalSince(newest.at) < Self.recentReplySeconds {
            candidateSummary = newest
        } else {
            candidateSummary = nil
        }

        guard let summary = candidateSummary,
              !summary.replyEmail.isEmpty,
              let account = accountStore.accounts.first(where: { $0.id == summary.accountId && $0.enabled }) else {
            return nil
        }
        return (account, summary)
    }

    private func draftVoiceReply(target: (account: GmailAccount, summary: ThreadSummary), body rawBody: String) {
        let body = rawBody.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else {
            finish("Nothing to send.")
            return
        }
        let summary = target.summary
        let account = target.account
        let latest = summary.messages.last
        let replySubj = Self.replySubject(for: summary.subject)
        let inReplyTo = latest?.messageIdHeader.nonEmpty
        var refs = latest?.references ?? []
        if let mid = inReplyTo, !refs.contains(mid) {
            refs.append(mid)
        }

        let acctLabel = Self.fit(account.label, units: 16)
        let shortEmail = Self.fit(summary.replyEmail, units: 36)
        let prompt = ConfirmGate.fitMessagePrompt(
            header: "Reply (\(acctLabel)) to \(shortEmail)",
            body: body,
            maxUnits: 270
        )

        confirm.ask(
            prompt: prompt,
            payload: "mail.reply|\(account.id)|\(summary.replyEmail)|\(replySubj)|\(body)",
            diagnosticKind: "messages"
        ) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                self.finish("Email cancelled.")
            case .unverified:
                self.finish("Email was not sent because confirmation could not be verified.")
            case .accepted:
                self.hud.receipt("Sending reply to \(summary.participantLabel)…")
                self.deliverDraft(
                    account: account,
                    recipientLabel: summary.participantLabel,
                    recipientEmail: summary.replyEmail,
                    subject: replySubj,
                    body: body,
                    threadKey: latest?.threadKey,
                    inReplyTo: inReplyTo,
                    references: refs,
                    uiDraft: nil
                )
            }
        }
    }

    private func composeVoiceEmail(recipientQuery: String, body rawBody: String, accountHint: String?) {
        let body = rawBody.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else {
            finish("Nothing to send.")
            return
        }
        guard let account = resolveSendingAccount(hint: accountHint) else {
            if let hint = accountHint {
                finish("No enabled Gmail account matched \(hint).")
            } else {
                finish("No Gmail account is connected yet. Add one in JOAT Brain › Mail.")
            }
            return
        }

        resolveEmailRecipient(recipientQuery) { [weak self] match in
            guard let self else { return }
            guard let match else {
                self.finish("I couldn't find an email address for \(recipientQuery).")
                return
            }
            self.confirmVoiceNewEmail(account: account, recipient: match, body: body)
        }
    }

    private func confirmVoiceNewEmail(account: GmailAccount, recipient: Recipients.Match, body rawBody: String) {
        let body = rawBody.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else {
            finish("Nothing to send.")
            return
        }

        generateSubject(for: body) { [weak self] subject in
            guard let self else { return }
            self.presentVoiceNewEmailConfirmation(
                account: account,
                recipient: recipient,
                subject: subject,
                body: body
            )
        }
    }

    private func presentVoiceNewEmailConfirmation(
        account: GmailAccount,
        recipient: Recipients.Match,
        subject: String,
        body: String
    ) {
        let acctLabel = Self.fit(account.label, units: 16)
        let shortEmail = Self.fit(recipient.handle, units: 36)
        let prompt = ConfirmGate.fitMessagePrompt(
            header: "Email (\(acctLabel)) to \(shortEmail)",
            body: body,
            maxUnits: 270
        )
        confirm.ask(
            prompt: prompt,
            payload: "mail.send|\(account.id)|\(recipient.handle)|\(subject)|\(body)",
            diagnosticKind: "messages"
        ) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                self.finish("Email cancelled.")
            case .unverified:
                self.finish("Email was not sent because confirmation could not be verified.")
            case .accepted:
                self.hud.receipt("Sending email to \(recipient.label)…")
                self.deliverDraft(
                    account: account,
                    recipientLabel: recipient.label,
                    recipientEmail: recipient.handle,
                    subject: subject,
                    body: body,
                    threadKey: nil,
                    inReplyTo: nil,
                    references: [],
                    uiDraft: nil
                )
            }
        }
    }

    // MARK: - Assistant Tool Belt Hooks

    func activeGlassThreadSummary() -> ThreadSummary? {
        guard let tid = threadIdOnGlass else { return nil }
        return threads.first(where: { $0.id == tid })
    }

    func defaultThreadSummaryForTool(now: Date = Date()) -> ThreadSummary? {
        if let open = activeGlassThreadSummary() { return open }
        if let s = selectedThread,
           now.timeIntervalSince(s.at) < Self.selectionSeconds,
           let chosen = threads.first(where: { $0.id == s.id }) {
            return chosen
        }
        return threads.first
    }

    func selectThreadForTool(_ threadId: String) {
        selectedThread = (threadId, Date())
        markThreadReadLocally(threadId)
    }

    func correspondentCandidatesForTool() -> [(label: String, phone: String?, email: String?)] {
        recentCorrespondentCandidates().map { (label: $0.label, phone: nil, email: $0.email) }
    }

    @MainActor
    func prepareFromTool(
        mode rawMode: String,
        recipient rawRecipient: String?,
        body rawBody: String?,
        accountHint: String?
    ) async -> PhoneToolOutcome {
        let mode = rawMode.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard mode == "new" || mode == "reply" else {
            return PhoneToolOutcome("error: mode must be 'new' or 'reply'.", claimsLens: false)
        }
        let cleanRecipient = rawRecipient?.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanBody = rawBody?.trimmingCharacters(in: .whitespacesAndNewlines)

        if mode == "reply" {
            let filteredThreads: [ThreadSummary]
            if let hint = accountHint?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(), !hint.isEmpty {
                filteredThreads = threads.filter {
                    $0.accountLabel.lowercased().contains(hint) || $0.accountEmail.lowercased().contains(hint)
                }
            } else {
                filteredThreads = threads
            }

            if let rec = cleanRecipient, !rec.isEmpty {
                let needle = (Recipients.savedRelationship(for: rec)?.label ?? rec).lowercased()
                if let matched = filteredThreads.first(where: {
                    !$0.replyEmail.isEmpty && (
                        $0.participantLabel.lowercased().contains(needle)
                        || $0.replyEmail.lowercased().contains(needle)
                        || $0.subject.lowercased().contains(needle)
                    )
                }) {
                    selectedThread = (matched.id, Date())
                }
            }

            let resolvedTarget: (account: GmailAccount, summary: ThreadSummary)? = replyTarget() ?? {
                guard let fallbackSummary = filteredThreads.first(where: {
                    !$0.replyEmail.isEmpty && $0.messages.contains(where: { !$0.outgoing })
                }),
                let account = accountStore.accounts.first(where: { $0.id == fallbackSummary.accountId && $0.enabled }) else {
                    return nil
                }
                return (account, fallbackSummary)
            }()

            guard let target = resolvedTarget else {
                finish(Self.noTarget)
                return PhoneToolOutcome(Self.noTarget, claimsLens: true)
            }

            if let body = cleanBody, !body.isEmpty {
                draftVoiceReply(target: target, body: body)
                return PhoneToolOutcome(
                    confirm.hasPending
                        ? "Email reply (\(target.account.label)) to \(target.summary.replyEmail) is awaiting touchpad confirmation on the glasses."
                        : "Email reply could not be prepared.",
                    claimsLens: true
                )
            }

            let question = "What should I reply to \(target.summary.participantLabel)?"
            askFollowUp?(question) { [weak self] answer in
                self?.draftVoiceReply(target: target, body: answer)
            }
            return PhoneToolOutcome("Asked the wearer on the glasses: \(question)", claimsLens: true)
        }

        // mode == "new"
        guard let account = resolveSendingAccount(hint: accountHint) else {
            let msg: String
            if let hint = accountHint, !hint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                msg = "No enabled Gmail account matched \(hint)."
            } else {
                msg = "No Gmail account is connected yet. Add one in JOAT Brain › Mail."
            }
            finish(msg)
            return PhoneToolOutcome(msg, claimsLens: true)
        }

        guard let who = cleanRecipient, !who.isEmpty else {
            let capturedBody = cleanBody
            askFollowUp?("Who should I email?") { [weak self] whoAnswer in
                guard let self else { return }
                self.resolveEmailRecipient(whoAnswer) { [weak self] match in
                    guard let self else { return }
                    guard let match else {
                        self.finish("I couldn't find an email address for \(whoAnswer).")
                        return
                    }
                    if let body = capturedBody, !body.isEmpty {
                        self.confirmVoiceNewEmail(account: account, recipient: match, body: body)
                    } else {
                        self.askFollowUp?("What should the email to \(match.label) say?") { [weak self] bodyAnswer in
                            self?.confirmVoiceNewEmail(account: account, recipient: match, body: bodyAnswer)
                        }
                    }
                }
            }
            return PhoneToolOutcome("Asked the wearer who to email.", claimsLens: true)
        }

        let match: Recipients.Match? = await withCheckedContinuation { cont in
            resolveEmailRecipient(who, seconds: 6) { resolved in
                cont.resume(returning: resolved)
            }
        }
        guard let match else {
            finish("I couldn't find an email address for \(who).")
            return PhoneToolOutcome("No email address found for \(who).", claimsLens: true)
        }

        if let body = cleanBody, !body.isEmpty {
            let subject: String = await withCheckedContinuation { cont in
                generateSubject(for: body) { generated in
                    cont.resume(returning: generated)
                }
            }
            presentVoiceNewEmailConfirmation(
                account: account,
                recipient: match,
                subject: subject,
                body: body
            )
            return PhoneToolOutcome(
                confirm.hasPending
                    ? "Email (\(account.label)) to \(match.handle) is awaiting touchpad confirmation on the glasses."
                    : "Email could not be prepared.",
                claimsLens: true
            )
        }

        let question = "What should the email to \(match.label) say?"
        askFollowUp?(question) { [weak self] answer in
            self?.confirmVoiceNewEmail(account: account, recipient: match, body: answer)
        }
        return PhoneToolOutcome("Asked the wearer on the glasses: \(question)", claimsLens: true)
    }

    private func resolveSendingAccount(hint: String?) -> GmailAccount? {
        let enabled = accountStore.enabledAccounts
        guard !enabled.isEmpty else { return nil }
        guard let rawHint = hint?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
              !rawHint.isEmpty else {
            return accountStore.defaultAccount
        }
        return enabled.first {
            $0.label.lowercased() == rawHint
                || $0.email.lowercased() == rawHint
                || $0.email.lowercased().contains(rawHint)
                || $0.label.lowercased().contains(rawHint)
        }
    }

    // MARK: - Outbound SMTP Delivery & App-Side Composer API

    static func verifyAccount(email: String, appPassword: String) async throws {
        let cleanEmail = email.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard GmailAccountStore.isValidEmail(cleanEmail) else {
            throw GmailKeychainError.invalidEmail
        }
        let normalizedPassword = GmailAccountStore.normalizeAppPassword(appPassword)
        guard !normalizedPassword.isEmpty else {
            throw GmailKeychainError.invalidAppPassword
        }
        try await GmailIMAP.verifyCredentials(email: cleanEmail, appPassword: normalizedPassword)
    }

    @MainActor
    func sendFromApp(
        accountId: String,
        to: String,
        subject: String,
        body: String,
        replyToThreadId: String?
    ) async throws {
        let account = accountStore.accounts.first(where: { $0.id == accountId })
            ?? accountStore.defaultAccount
        guard let account else {
            throw GmailKeychainError.invalidEmail
        }
        guard let appPassword = accountStore.appPassword(for: account.id), !appPassword.isEmpty else {
            throw GmailKeychainError.invalidAppPassword
        }

        let cleanTo = MIMEParser.parseAddresses(to).first?.email
            ?? to.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard GmailAccountStore.isValidEmail(cleanTo) else {
            throw GmailTransportError.recipientRejected(to)
        }

        let cleanBody = body.trimmingCharacters(in: .whitespacesAndNewlines)
        var resolvedSubject = subject.trimmingCharacters(in: .whitespacesAndNewlines)
        var inReplyTo: String?
        var references: [String] = []
        var existingThreadKey: String?

        if let tid = replyToThreadId, let summary = threads.first(where: { $0.id == tid }) {
            if resolvedSubject.isEmpty {
                resolvedSubject = Self.replySubject(for: summary.subject)
            }
            if let latest = summary.messages.last {
                inReplyTo = latest.messageIdHeader.nonEmpty
                references = latest.references
                if let mid = inReplyTo, !references.contains(mid) {
                    references.append(mid)
                }
                existingThreadKey = latest.threadKey
            }
        }
        if resolvedSubject.isEmpty {
            resolvedSubject = Self.fallbackSubject(for: cleanBody)
        }

        let receipt = try await GmailSMTP.send(
            email: account.email,
            appPassword: appPassword,
            fromName: account.displayName,
            toEmail: cleanTo,
            subject: resolvedSubject,
            body: cleanBody,
            inReplyTo: inReplyTo,
            references: references
        )

        recordSentMessage(
            account: account,
            toEmail: cleanTo,
            subject: resolvedSubject,
            body: cleanBody,
            messageId: receipt.messageId,
            sentAt: receipt.sentAt,
            existingThreadKey: existingThreadKey,
            inReplyTo: inReplyTo,
            references: references
        )
    }

    private func deliverDraft(
        account: GmailAccount,
        recipientLabel: String,
        recipientEmail: String,
        subject: String,
        body: String,
        threadKey: String?,
        inReplyTo: String?,
        references: [String],
        uiDraft: Draft?
    ) {
        guard let appPassword = accountStore.appPassword(for: account.id), !appPassword.isEmpty else {
            let msg = "Missing App Password for \(account.label). Check Mail › Accounts."
            if let d = uiDraft { ui(d, "failed", msg) }
            finish(msg)
            return
        }

        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let receipt = try await GmailSMTP.send(
                    email: account.email,
                    appPassword: appPassword,
                    fromName: account.displayName,
                    toEmail: recipientEmail,
                    subject: subject,
                    body: body,
                    inReplyTo: inReplyTo,
                    references: references
                )
                self.recordSentMessage(
                    account: account,
                    toEmail: recipientEmail,
                    subject: subject,
                    body: body,
                    messageId: receipt.messageId,
                    sentAt: receipt.sentAt,
                    existingThreadKey: threadKey,
                    inReplyTo: inReplyTo,
                    references: references
                )
                Log.add("mail sent (\(account.label)) to \(recipientEmail)")
                if let d = uiDraft {
                    self.ui(d, "sent", "Sent to \(recipientLabel).")
                    if d.threadId != nil && self.threadIdOnGlass == d.threadId {
                        self.onThreadChange?()
                        self.say?("Email sent to \(recipientLabel).")
                    } else {
                        self.finish("Email sent to \(recipientLabel).")
                    }
                } else {
                    self.finish("Email sent to \(recipientLabel).")
                }
            } catch {
                let errText = (error as? LocalizedError)?.errorDescription ?? "Could not send the email right now."
                Log.add("mail send failed (\(account.label)): \(errText)")
                if let d = uiDraft {
                    self.ui(d, "failed", errText)
                }
                self.finish(errText)
            }
        }
    }

    private func recordSentMessage(
        account: GmailAccount,
        toEmail: String,
        subject: String,
        body: String,
        messageId: String,
        sentAt: Date,
        existingThreadKey: String?,
        inReplyTo: String?,
        references: [String]
    ) {
        let computedKey = existingThreadKey ?? MIMEParser.threadKey(
            messageId: messageId,
            inReplyTo: inReplyTo,
            references: references,
            subject: subject
        )
        let snippetWords = body.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let snippet = snippetWords.count > 180 ? String(snippetWords.prefix(179)) + "…" : snippetWords
        let localUID = UInt32.max - UInt32(abs(messageId.hashValue) % 100_000)
        let item = MessageItem(
            id: "\(account.id):sent:\(UUID().uuidString.lowercased())",
            uid: localUID,
            accountId: account.id,
            accountLabel: account.label,
            accountEmail: account.email,
            threadKey: computedKey,
            messageIdHeader: messageId,
            inReplyTo: inReplyTo,
            references: references,
            fromName: account.displayName.isEmpty ? account.label : account.displayName,
            fromEmail: account.email,
            replyToEmail: toEmail,
            toEmails: [toEmail],
            subject: subject,
            snippet: snippet,
            body: body,
            at: sentAt,
            unread: false,
            outgoing: true,
            isBulk: false
        )
        mergeMessages([item], into: account)
        persistCache()
        rebuildThreads(notify: true)
        if threadIdOnGlass != nil {
            onThreadChange?()
        }
    }

    private func finish(_ text: String) {
        hud.receipt(text)
        say?(text)
    }

    // MARK: - Auto Subject Generation & Email Recipient Lookup

    private static let subjectInstructions = """
    Write a concise, natural email subject line of 3 to 6 words summarizing this email body. \
    Output ONLY the subject line in plain text with no quotes, no 'Subject:' prefix, and no trailing period.
    """

    private func generateSubject(for body: String, completion: @escaping @MainActor (String) -> Void) {
        let fallback = Self.fallbackSubject(for: body)
        guard OnPhoneAI.shared.anyConfigured, Bundle.main.bundleIdentifier != nil else {
            Task { @MainActor in completion(fallback) }
            return
        }

        let once = SubjectOnce(fallback: fallback, completion: completion)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 3_500_000_000)
            once.fire(fallback)
        }

        OnPhoneAI.shared.complete(
            instructions: Self.subjectInstructions,
            text: String(body.prefix(1500)),
            macLink: nil
        ) { result in
            let cleaned: String
            if case .success(let raw) = result {
                cleaned = Self.cleanGeneratedSubject(raw) ?? fallback
            } else {
                cleaned = fallback
            }
            Task { @MainActor in
                once.fire(cleaned)
            }
        }
    }

    private final class SubjectOnce: @unchecked Sendable {
        private var completion: (@MainActor (String) -> Void)?
        private let fallback: String

        init(fallback: String, completion: @escaping @MainActor (String) -> Void) {
            self.fallback = fallback
            self.completion = completion
        }

        @MainActor func fire(_ subject: String) {
            guard let c = completion else { return }
            completion = nil
            c(subject.isEmpty ? fallback : subject)
        }
    }

    static func cleanGeneratedSubject(_ raw: String) -> String? {
        var line = raw
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first(where: { !$0.isEmpty }) ?? ""
        if line.lowercased().hasPrefix("subject:") {
            line = String(line.dropFirst("subject:".count)).trimmingCharacters(in: .whitespaces)
        }
        line = line.trimmingCharacters(in: CharacterSet(charactersIn: "\"'“”‘’"))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = line.last, ".".contains(last) {
            line.removeLast()
        }
        guard !line.isEmpty else { return nil }
        return String(line.prefix(96))
    }

    static func fallbackSubject(for body: String) -> String {
        let words = body.split(whereSeparator: \.isWhitespace).prefix(6).map(String.init)
        var joined = words.joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?\"'"))
        if joined.isEmpty { joined = "Quick note" }
        return joined.prefix(1).uppercased() + joined.dropFirst()
    }

    static func replySubject(for original: String) -> String {
        let trimmed = original.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return "Re: (No Subject)" }
        if trimmed.lowercased().hasPrefix("re:") { return trimmed }
        return "Re: \(trimmed)"
    }

    private struct EmailCandidate {
        let label: String
        let names: [String]
        let email: String
    }

    private func resolveEmailRecipient(
        _ rawSaid: String,
        seconds: Double = 10,
        completion: @escaping @MainActor (Recipients.Match?) -> Void
    ) {
        let cleaned = rawSaid
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?"))
        guard !cleaned.isEmpty else {
            Task { @MainActor in completion(nil) }
            return
        }

        if let directEmail = Self.extractSpokenOrLiteralEmail(cleaned) {
            let savedLabel = Recipients.name(forHandle: directEmail) ?? directEmail
            Task { @MainActor in
                completion(Recipients.Match(label: savedLabel, handle: directEmail))
            }
            return
        }

        // Snapshot recent Gmail correspondents on caller thread before detaching
        let recentSnapshot = recentCorrespondentCandidates()
        if let rel = Recipients.savedRelationship(for: cleaned) {
            if let relEmail = rel.email, GmailAccountStore.isValidEmail(relEmail) {
                Task { @MainActor in
                    completion(Recipients.Match(label: rel.label, handle: relEmail.lowercased()))
                }
                return
            }
            if let fromRecent = recentSnapshot.first(where: { $0.label.caseInsensitiveCompare(rel.label) == .orderedSame }) {
                Task { @MainActor in
                    completion(Recipients.Match(label: rel.label, handle: fromRecent.email))
                }
                return
            }
        }
        let once = EmailMatchOnce(completion)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            once.fire(nil)
        }

        Task.detached(priority: .userInitiated) {
            let contactCandidates = Self.contactEmailCandidates()
            // Merge recent Gmail correspondents first, followed by Contacts email entries
            var seenEmails: Set<String> = []
            var pool: [EmailCandidate] = []
            for item in (recentSnapshot + contactCandidates) {
                let key = item.email.lowercased()
                if seenEmails.insert(key).inserted {
                    pool.append(item)
                }
            }

            let lower = cleaned.lowercased()
            let exact = pool.filter { c in
                c.names.contains { $0.lowercased() == lower }
                    || c.email.lowercased().split(separator: "@").first.map(String.init) == lower
            }
            if exact.count == 1 {
                await once.fire(Recipients.Match(label: exact[0].label, handle: exact[0].email))
                return
            }
            if let exactRecent = recentSnapshot.first(where: { c in c.names.contains { $0.lowercased() == lower } }) {
                await once.fire(Recipients.Match(label: exactRecent.label, handle: exactRecent.email))
                return
            }

            let spokenTokens = Self.nameWords(lower)
            if !spokenTokens.isEmpty {
                let subsetMatches = pool.filter { c in
                    c.names.contains { spokenTokens.isSubset(of: Self.nameWords($0.lowercased())) }
                }
                if subsetMatches.count == 1 {
                    await once.fire(Recipients.Match(label: subsetMatches[0].label, handle: subsetMatches[0].email))
                    return
                }
                let recentSubset = recentSnapshot.filter { c in
                    c.names.contains { spokenTokens.isSubset(of: Self.nameWords($0.lowercased())) }
                }
                if recentSubset.count == 1 {
                    await once.fire(Recipients.Match(label: recentSubset[0].label, handle: recentSubset[0].email))
                    return
                }
            }

            guard !pool.isEmpty else {
                await once.fire(nil)
                return
            }

            let rankedPool = Array(Self.rankCandidates(pool, for: lower).prefix(250))
            if let idx = await Self.pickEmailCandidate(said: cleaned, candidates: rankedPool),
               rankedPool.indices.contains(idx) {
                let picked = rankedPool[idx]
                await once.fire(Recipients.Match(label: picked.label, handle: picked.email))
                return
            }

            await once.fire(nil)
        }
    }

    private func recentCorrespondentCandidates() -> [EmailCandidate] {
        var allMessages: [MessageItem] = []
        for list in messagesByAccount.values {
            allMessages.append(contentsOf: list)
        }
        allMessages.sort { $0.at > $1.at }

        let ownEmails = Set(accountStore.accounts.map { $0.email.lowercased() })
        var out: [EmailCandidate] = []
        var seen: Set<String> = []

        for m in allMessages {
            if !m.outgoing && !m.isBulk {
                let email = (m.replyToEmail.isEmpty ? m.fromEmail : m.replyToEmail).lowercased()
                if !email.isEmpty, !ownEmails.contains(email), seen.insert(email).inserted {
                    let label = m.fromName.isEmpty ? email : m.fromName
                    out.append(EmailCandidate(label: label, names: [label, email], email: email))
                }
            } else if m.outgoing {
                for rawTo in m.toEmails {
                    let email = rawTo.lowercased()
                    if !email.isEmpty, !ownEmails.contains(email), seen.insert(email).inserted {
                        let saved = Recipients.name(forHandle: email) ?? email
                        out.append(EmailCandidate(label: saved, names: [saved, email], email: email))
                    }
                }
            }
        }
        return out
    }

    private static func contactEmailCandidates() -> [EmailCandidate] {
        guard Recipients.contactsAllowed else { return [] }
        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactNicknameKey as CNKeyDescriptor,
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
        ]
        var out: [EmailCandidate] = []
        let req = CNContactFetchRequest(keysToFetch: keys)
        try? CNContactStore().enumerateContacts(with: req) { contact, _ in
            guard let primaryEmail = (contact.emailAddresses.first?.value as String?)?
                .trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
                  !primaryEmail.isEmpty else { return }
            let full = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
            let label = !full.isEmpty ? full : (!contact.nickname.isEmpty ? contact.nickname : (!contact.organizationName.isEmpty ? contact.organizationName : primaryEmail))
            let names = [full, contact.nickname, contact.organizationName].filter { !$0.isEmpty }
            out.append(EmailCandidate(label: label, names: names.isEmpty ? [label] : names, email: primaryEmail))
        }
        return out
    }

    private static func extractSpokenOrLiteralEmail(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if GmailAccountStore.isValidEmail(trimmed) {
            return trimmed.lowercased()
        }
        if let parsed = MIMEParser.parseAddresses(trimmed).first?.email, GmailAccountStore.isValidEmail(parsed) {
            return parsed.lowercased()
        }
        let spoken = trimmed.lowercased()
            .replacingOccurrences(of: " at ", with: "@")
            .replacingOccurrences(of: " dot ", with: ".")
            .replacingOccurrences(of: " dash ", with: "-")
            .replacingOccurrences(of: " hyphen ", with: "-")
            .replacingOccurrences(of: " underscore ", with: "_")
            .replacingOccurrences(of: " ", with: "")
        if GmailAccountStore.isValidEmail(spoken) {
            return spoken
        }
        return nil
    }

    private static func nameWords(_ text: String) -> Set<String> {
        Set(text.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init).filter { $0.count >= 2 })
    }

    private static func rankCandidates(_ all: [EmailCandidate], for query: String) -> [EmailCandidate] {
        let words = Set(query.split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init))
        func score(_ c: EmailCandidate) -> Int {
            var best = 0
            for n in (c.names + [c.email]) {
                let low = n.lowercased()
                var s = 0
                for w in words where w.count >= 2 {
                    if low.contains(w) { s += 3 * w.count }
                }
                best = max(best, s)
            }
            return best
        }
        return all.enumerated()
            .map { (idx: $0.offset, item: $0.element, s: score($0.element)) }
            .sorted { $0.s != $1.s ? $0.s > $1.s : $0.idx < $1.idx }
            .map(\.item)
    }

    private static func pickEmailCandidate(said: String, candidates: [EmailCandidate]) async -> Int? {
        let instructions = """
        You pick which email recipient the speaker means. They may say a first name, nickname, \
        organization, or relationship. Reply with ONLY the 1-based number of the matching recipient, \
        or 0 when none fits.
        """
        let rows = candidates.enumerated().map { idx, c in
            "\(idx + 1). \(c.label) <\(c.email)>"
        }.joined(separator: "\n")
        let prompt = "They said: \"\(said)\"\n\nRecipients:\n\(rows)"

        func parseIndex(_ text: String) -> Int? {
            guard let n = text.split(whereSeparator: { !$0.isNumber }).first.flatMap({ Int($0) }), n > 0 else {
                return nil
            }
            return n - 1
        }

        let model = SystemLanguageModel(useCase: .general, guardrails: .permissiveContentTransformations)
        if model.isAvailable {
            if let response = try? await LanguageModelSession(model: model, tools: [], instructions: instructions)
                .respond(to: prompt, options: GenerationOptions()),
               let idx = parseIndex(response.content) {
                return idx
            }
        }

        guard OnPhoneAI.shared.anyConfigured else { return nil }
        return await withCheckedContinuation { cont in
            OnPhoneAI.shared.complete(instructions: instructions, text: prompt, macLink: nil) { result in
                cont.resume(returning: (try? result.get()).flatMap(parseIndex))
            }
        }
    }

    private final class EmailMatchOnce: @unchecked Sendable {
        private var completion: (@MainActor (Recipients.Match?) -> Void)?
        init(_ completion: @escaping @MainActor (Recipients.Match?) -> Void) {
            self.completion = completion
        }
        @MainActor func fire(_ match: Recipients.Match?) {
            guard let c = completion else { return }
            completion = nil
            c(match)
        }
    }

    // MARK: - Thread Assembly & Disk Cache

    private func makeMessageItem(from fetched: GmailIMAPResponseParser.FetchedMessage, account: GmailAccount) -> MessageItem {
        let parsed = MIMEParser.parse(fetched.rawBody ?? Data())
        let outgoing = parsed.from.email.caseInsensitiveCompare(account.email) == .orderedSame
        let savedContactName = parsed.from.email.isEmpty ? nil : Recipients.name(forHandle: parsed.from.email)
        let fromName: String
        if !parsed.from.name.isEmpty {
            fromName = parsed.from.name
        } else if let savedContactName, !savedContactName.isEmpty {
            fromName = savedContactName
        } else if !parsed.from.email.isEmpty {
            fromName = parsed.from.email.split(separator: "@").first.map(String.init) ?? parsed.from.email
        } else {
            fromName = "Unknown"
        }

        let toEmails = parsed.to.map(\.email).filter { !$0.isEmpty }
        let replyTo: String
        if outgoing {
            replyTo = toEmails.first ?? parsed.replyTo.first?.email ?? ""
        } else if let explicitReplyTo = parsed.replyTo.first?.email, !explicitReplyTo.isEmpty {
            replyTo = explicitReplyTo
        } else {
            replyTo = parsed.from.email
        }

        let subject = parsed.subject.isEmpty ? "(No Subject)" : parsed.subject
        let body = parsed.cleanBody.isEmpty ? parsed.plainBody : parsed.cleanBody
        let timestamp = parsed.date ?? fetched.internalDate ?? Date()

        return MessageItem(
            id: "\(account.id):\(fetched.uid)",
            uid: fetched.uid,
            accountId: account.id,
            accountLabel: account.label,
            accountEmail: account.email,
            threadKey: parsed.threadKey,
            messageIdHeader: parsed.messageId,
            inReplyTo: parsed.inReplyTo,
            references: parsed.references,
            fromName: fromName,
            fromEmail: parsed.from.email,
            replyToEmail: replyTo,
            toEmails: toEmails,
            subject: subject,
            snippet: parsed.snippet,
            body: body,
            at: timestamp,
            unread: !fetched.isSeen && !outgoing,
            outgoing: outgoing,
            isBulk: parsed.isBulkOrAutomated
        )
    }

    private func mergeMessages(_ incoming: [MessageItem], into account: GmailAccount) {
        var existing = messagesByAccount[account.id] ?? []
        for item in incoming {
            if let uidIdx = existing.firstIndex(where: { $0.uid == item.uid && !item.id.contains(":sent:") }) {
                existing[uidIdx] = item
            } else if !item.messageIdHeader.isEmpty,
                      let midIdx = existing.firstIndex(where: { $0.messageIdHeader == item.messageIdHeader }) {
                existing[midIdx] = item
            } else {
                existing.append(item)
            }
        }
        existing.sort {
            if $0.at != $1.at { return $0.at < $1.at }
            return $0.uid < $1.uid
        }
        if existing.count > Self.keepPerAccount {
            existing.removeFirst(existing.count - Self.keepPerAccount)
        }
        messagesByAccount[account.id] = existing
    }

    private func applyFlagUpdates(_ flagMap: [UInt32: Bool], accountId: String) -> Bool {
        guard var list = messagesByAccount[accountId] else { return false }
        var changed = false
        for i in list.indices {
            if let serverUnread = flagMap[list[i].uid], !list[i].outgoing, list[i].unread != serverUnread {
                list[i].unread = serverUnread
                changed = true
            }
        }
        if changed {
            messagesByAccount[accountId] = list
        }
        return changed
    }

    private func pruneRemovedAccounts() {
        let validIds = Set(accountStore.accounts.map(\.id))
        let beforeCount = messagesByAccount.count
        messagesByAccount = messagesByAccount.filter { validIds.contains($0.key) }
        accountErrors = accountErrors.filter { validIds.contains($0.key) }
        if messagesByAccount.count != beforeCount {
            persistCache()
        }
    }

    private func rebuildThreads(notify: Bool) {
        let enabled = accountStore.enabledAccounts
        var built: [ThreadSummary] = []

        for account in enabled {
            let items = (messagesByAccount[account.id] ?? []).map { item -> MessageItem in
                if item.accountLabel == account.label && item.accountEmail == account.email {
                    return item
                }
                return MessageItem(
                    id: item.id,
                    uid: item.uid,
                    accountId: item.accountId,
                    accountLabel: account.label,
                    accountEmail: account.email,
                    threadKey: item.threadKey,
                    messageIdHeader: item.messageIdHeader,
                    inReplyTo: item.inReplyTo,
                    references: item.references,
                    fromName: item.fromName,
                    fromEmail: item.fromEmail,
                    replyToEmail: item.replyToEmail,
                    toEmails: item.toEmails,
                    subject: item.subject,
                    snippet: item.snippet,
                    body: item.body,
                    at: item.at,
                    unread: item.unread,
                    outgoing: item.outgoing,
                    isBulk: item.isBulk
                )
            }

            // Map Message-IDs and normalized subjects to canonical thread keys within this account
            var keyByMessageId: [String: String] = [:]
            var keyByNormSubject: [String: String] = [:]
            var grouped: [String: [MessageItem]] = [:]

            for item in items {
                var canonicalKey = item.threadKey
                for ref in item.references {
                    if let existing = keyByMessageId[ref] {
                        canonicalKey = existing
                        break
                    }
                }
                if let reply = item.inReplyTo, let existing = keyByMessageId[reply] {
                    canonicalKey = existing
                }
                let normSubj = MIMEParser.normalizeSubject(item.subject)
                if canonicalKey.hasPrefix("subject:"), let existing = keyByNormSubject[normSubj] {
                    canonicalKey = existing
                }

                if !item.messageIdHeader.isEmpty {
                    keyByMessageId[item.messageIdHeader] = canonicalKey
                }
                if !normSubj.isEmpty && keyByNormSubject[normSubj] == nil {
                    keyByNormSubject[normSubj] = canonicalKey
                }
                grouped[canonicalKey, default: []].append(item)
            }

            for (tKey, rawGroup) in grouped {
                let sorted = rawGroup.sorted {
                    if $0.at != $1.at { return $0.at < $1.at }
                    return $0.uid < $1.uid
                }
                guard let latest = sorted.last else { continue }
                let firstSubject = sorted.first?.subject ?? latest.subject
                let displaySubject = MIMEParser.hasReplyOrForwardPrefix(firstSubject)
                    ? firstSubject
                    : firstSubject

                let latestIncoming = sorted.last(where: { !$0.outgoing })
                let participantLabel: String
                let replyEmail: String
                if let inc = latestIncoming {
                    participantLabel = inc.fromName.isEmpty ? inc.fromEmail : inc.fromName
                    replyEmail = inc.replyToEmail.isEmpty ? inc.fromEmail : inc.replyToEmail
                } else {
                    let toEmail = latest.toEmails.first ?? ""
                    participantLabel = Recipients.name(forHandle: toEmail) ?? (toEmail.isEmpty ? account.email : toEmail)
                    replyEmail = toEmail
                }

                let preview = latest.outgoing
                    ? (latest.snippet.isEmpty ? "You replied" : "You: \(latest.snippet)")
                    : latest.snippet
                let hasUnread = sorted.contains(where: { $0.unread && !$0.outgoing })

                built.append(ThreadSummary(
                    id: "\(account.id)|\(tKey)",
                    accountId: account.id,
                    accountLabel: account.label,
                    accountEmail: account.email,
                    subject: displaySubject,
                    participantLabel: participantLabel,
                    replyEmail: replyEmail,
                    preview: preview,
                    at: latest.at,
                    unread: hasUnread,
                    messages: sorted
                ))
            }
        }

        built.sort { $0.at > $1.at }
        threads = built
        if notify {
            onChange?()
        }
    }

    private func loadCache() {
        guard FileManager.default.fileExists(atPath: cacheURL.path) else { return }
        do {
            let data = try Data(contentsOf: cacheURL)
            messagesByAccount = try JSONDecoder().decode([String: [MessageItem]].self, from: data)
        } catch {
            SafeStore.quarantine(cacheURL, error)
            messagesByAccount = [:]
        }
    }

    private func persistCache() {
        do {
            let data = try JSONEncoder().encode(messagesByAccount)
            try data.write(to: cacheURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            Log.add("mail cache write failed: \(error.localizedDescription)")
        }
    }

    private func loadWatermark(for accountId: String) -> SyncWatermark? {
        guard let data = UserDefaults.standard.data(forKey: "joat.gmail.sync.\(accountId)") else {
            return nil
        }
        return try? JSONDecoder().decode(SyncWatermark.self, from: data)
    }

    private func saveWatermark(_ watermark: SyncWatermark, for accountId: String) {
        if let data = try? JSONEncoder().encode(watermark) {
            UserDefaults.standard.set(data, forKey: "joat.gmail.sync.\(accountId)")
        }
    }

    // MARK: - String Fitting Helpers

    static func fit(_ text: String, units: Int) -> String {
        let t = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard t.utf16.count > units else { return t }
        guard units > 1 else { return "…" }
        return t.utf16Prefix(units - 1).trimmingCharacters(in: .whitespaces) + "…"
    }

    private static func shortParticipant(_ label: String, units: Int) -> String {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.contains("@"), let firstWord = trimmed.split(separator: " ").first, firstWord.count >= 2 {
            return fit(String(firstWord), units: units)
        }
        return fit(trimmed, units: units)
    }

    #if DEBUG
    @MainActor
    func debugSeedSampleInbox() {
        poller?.cancel()
        poller = nil
        activeSyncTask?.cancel()
        activeSyncTask = nil
        syncing = false
        accountErrors = [:]

        let now = Date()
        let workAcct = GmailAccount(
            id: "debug-work",
            label: "Work",
            email: "work@example.com",
            displayName: "Studio Team",
            enabled: true,
            isDefault: true,
            addedAt: now.addingTimeInterval(-86_400)
        )
        let personalAcct = GmailAccount(
            id: "debug-personal",
            label: "Personal",
            email: "personal@example.com",
            displayName: "Personal",
            enabled: true,
            isDefault: false,
            addedAt: now.addingTimeInterval(-43_200)
        )
        accountStore.debugSetAccounts([workAcct, personalAcct])

        messagesByAccount = [
            workAcct.id: [
                MessageItem(
                    id: "debug-work:101",
                    uid: 101,
                    accountId: workAcct.id,
                    accountLabel: workAcct.label,
                    accountEmail: workAcct.email,
                    threadKey: "<q4-launch-1@example.com>",
                    messageIdHeader: "<q4-launch-1@example.com>",
                    inReplyTo: nil,
                    references: [],
                    fromName: "Sarah Chen",
                    fromEmail: "sarah@example.com",
                    replyToEmail: "sarah@example.com",
                    toEmails: [workAcct.email],
                    subject: "Q4 Launch Checklist",
                    snippet: "Can we lock the release notes before 3 PM today?",
                    body: "Hi team,\n\nCan we lock the Q4 launch checklist and release notes before 3 PM today so hardware QA can sign off?",
                    at: now.addingTimeInterval(-900),
                    unread: false,
                    outgoing: false,
                    isBulk: false
                ),
                MessageItem(
                    id: "debug-work:102",
                    uid: 102,
                    accountId: workAcct.id,
                    accountLabel: workAcct.label,
                    accountEmail: workAcct.email,
                    threadKey: "<q4-launch-1@example.com>",
                    messageIdHeader: "<q4-launch-2@example.com>",
                    inReplyTo: "<q4-launch-1@example.com>",
                    references: ["<q4-launch-1@example.com>"],
                    fromName: "Studio Team",
                    fromEmail: workAcct.email,
                    replyToEmail: "sarah@example.com",
                    toEmails: ["sarah@example.com"],
                    subject: "Re: Q4 Launch Checklist",
                    snippet: "Reviewing the final build metrics right now.",
                    body: "Reviewing the final build metrics right now — everything looks green.",
                    at: now.addingTimeInterval(-420),
                    unread: false,
                    outgoing: true,
                    isBulk: false
                ),
                MessageItem(
                    id: "debug-work:103",
                    uid: 103,
                    accountId: workAcct.id,
                    accountLabel: workAcct.label,
                    accountEmail: workAcct.email,
                    threadKey: "<q4-launch-1@example.com>",
                    messageIdHeader: "<q4-launch-3@example.com>",
                    inReplyTo: "<q4-launch-2@example.com>",
                    references: ["<q4-launch-1@example.com>", "<q4-launch-2@example.com>"],
                    fromName: "Sarah Chen",
                    fromEmail: "sarah@example.com",
                    replyToEmail: "sarah@example.com",
                    toEmails: [workAcct.email],
                    subject: "Re: Q4 Launch Checklist",
                    snippet: "Awesome — reply here once TestFlight upload finishes and we'll ship.",
                    body: "Awesome — reply here once TestFlight upload finishes and we'll ship.",
                    at: now.addingTimeInterval(-120),
                    unread: true,
                    outgoing: false,
                    isBulk: false
                ),
            ],
            personalAcct.id: [
                MessageItem(
                    id: "debug-personal:201",
                    uid: 201,
                    accountId: personalAcct.id,
                    accountLabel: personalAcct.label,
                    accountEmail: personalAcct.email,
                    threadKey: "<dinner-friday@example.com>",
                    messageIdHeader: "<dinner-friday@example.com>",
                    inReplyTo: nil,
                    references: [],
                    fromName: "Alex Rivera",
                    fromEmail: "alex@example.com",
                    replyToEmail: "alex@example.com",
                    toEmails: [personalAcct.email],
                    subject: "Dinner Friday at 7?",
                    snippet: "Booked the patio table for 7:00 PM — let me know if that works!",
                    body: "Hey! Booked the patio table for 7:00 PM on Friday — let me know if that works for you.",
                    at: now.addingTimeInterval(-1080),
                    unread: true,
                    outgoing: false,
                    isBulk: false
                ),
            ],
        ]
        lastSyncAt = now
        rebuildThreads(notify: true)
    }
    #endif
}

private extension String {
    var nonEmpty: String? {
        let t = trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }
}
