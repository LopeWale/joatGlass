import SwiftUI
import UIKit

/// Multi-account Gmail screen for JOAT Brain, adapting between a two-column
/// `NavigationSplitView` on the unfolded iPhone Duo (`horizontalSizeClass == .regular`)
/// and a `NavigationStack` on the outer cover screen.
struct MailView: View {
    var body: some View {
        if let mail = JOATRuntime.shared.brain?.mail {
            MailAdaptiveView(mail: mail)
        } else {
            NavigationStack {
                ContentUnavailableView(
                    "Starting Mail…",
                    systemImage: "envelope.badge",
                    description: Text("Initializing standalone Gmail engine on this iPhone.")
                )
                .navigationTitle("Mail")
                .onAppear {
                    JOATRuntime.shared.start(reason: "mail-tab")
                }
            }
        }
    }
}

private struct MailAdaptiveView: View {
    @ObservedObject var mail: Mail
    @ObservedObject private var accountStore = GmailAccountStore.shared
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    @State private var columnVisibility: NavigationSplitViewVisibility = .all
    @State private var accountFilter: String? = nil // nil == All
    @State private var searchQuery = ""
    @State private var selectedThreadId: String?
    @State private var compactPath: [String] = []
    @State private var showingCompose = false
    @State private var showingAccounts = false
    @State private var showingAddAccount = false

    private var filteredThreads: [Mail.ThreadSummary] {
        let base: [Mail.ThreadSummary]
        if let acctId = accountFilter {
            base = mail.threads.filter { $0.accountId == acctId }
        } else {
            base = mail.threads
        }
        let q = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return base }
        return base.filter { thread in
            thread.participantLabel.lowercased().contains(q)
                || thread.replyEmail.lowercased().contains(q)
                || thread.subject.lowercased().contains(q)
                || thread.preview.lowercased().contains(q)
                || thread.accountLabel.lowercased().contains(q)
                || thread.messages.contains(where: {
                    $0.fromName.lowercased().contains(q)
                        || $0.fromEmail.lowercased().contains(q)
                        || $0.body.lowercased().contains(q)
                })
        }
    }

    private func unreadCount(for accountId: String?) -> Int {
        if let accountId {
            return mail.threads.filter { $0.accountId == accountId && $0.unread }.count
        }
        return mail.threads.filter(\.unread).count
    }

    var body: some View {
        Group {
            if horizontalSizeClass == .regular {
                splitLayout
            } else {
                compactLayout
            }
        }
        .sheet(isPresented: $showingCompose) {
            ComposeEmailSheet(
                mail: mail,
                accountStore: accountStore,
                initialAccountId: accountFilter ?? accountStore.defaultAccount?.id
            )
        }
        .sheet(isPresented: $showingAccounts) {
            GmailAccountsSheet(mail: mail, accountStore: accountStore)
        }
        .sheet(isPresented: $showingAddAccount) {
            AddGmailAccountSheet(mail: mail, accountStore: accountStore)
        }
        .onChange(of: accountStore.accounts) { _, accounts in
            if let acctId = accountFilter, !accounts.contains(where: { $0.id == acctId }) {
                accountFilter = nil
            }
            syncRegularSelectionIfNeeded()
        }
        .onChange(of: mail.threads) { _, _ in
            syncRegularSelectionIfNeeded()
        }
        .onChange(of: horizontalSizeClass) { _, newSize in
            if newSize == .regular {
                if selectedThreadId == nil, let pushed = compactPath.last {
                    selectedThreadId = pushed
                }
                syncRegularSelectionIfNeeded()
            } else if let selected = selectedThreadId, compactPath.isEmpty {
                compactPath = [selected]
            }
        }
        .onAppear {
            syncRegularSelectionIfNeeded()
        }
    }

    private func syncRegularSelectionIfNeeded() {
        guard horizontalSizeClass == .regular else { return }
        if let current = selectedThreadId,
           filteredThreads.contains(where: { $0.id == current }) {
            return
        }
        selectedThreadId = filteredThreads.first?.id
    }

    // MARK: - Unfolded Duo Split View

    private var splitLayout: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            inboxSidebar(isSplit: true)
                .navigationTitle("Mail")
                .toolbar { inboxToolbar }
                .modifySearchable(enabled: !accountStore.accounts.isEmpty, text: $searchQuery)
                .refreshable { await mail.syncNow() }
        } detail: {
            if let threadId = selectedThreadId,
               mail.threads.contains(where: { $0.id == threadId }) {
                MailThreadDetailView(threadId: threadId, mail: mail)
                    .id(threadId)
            } else if accountStore.accounts.isEmpty {
                ContentUnavailableView {
                    Label("No Gmail Connected", systemImage: "envelope.badge.shield.half.filled")
                } description: {
                    Text("Connect one or more Gmail or Google Workspace accounts to read and reply on your glasses and iPhone Duo.")
                } actions: {
                    Button {
                        showingAddAccount = true
                    } label: {
                        Label("Connect Gmail", systemImage: "plus.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(JoatTheme.accent)
                }
            } else {
                ContentUnavailableView {
                    Label("Select an Email", systemImage: "envelope.open")
                } description: {
                    Text("Choose a thread from your inbox on the left, or compose a new message.")
                } actions: {
                    if !accountStore.enabledAccounts.isEmpty {
                        Button {
                            showingCompose = true
                        } label: {
                            Label("New Email", systemImage: "square.and.pencil")
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(JoatTheme.accent)
                    }
                }
            }
        }
    }

    // MARK: - Compact Cover Stack

    private var compactLayout: some View {
        NavigationStack(path: $compactPath) {
            inboxSidebar(isSplit: false)
                .navigationTitle("Mail")
                .toolbar { inboxToolbar }
                .modifySearchable(enabled: !accountStore.accounts.isEmpty, text: $searchQuery)
                .refreshable { await mail.syncNow() }
                .navigationDestination(for: String.self) { threadId in
                    MailThreadDetailView(threadId: threadId, mail: mail)
                }
        }
        .onChange(of: compactPath) { _, path in
            if let last = path.last {
                selectedThreadId = last
            }
        }
    }

    // MARK: - Inbox List & Onboarding

    @ViewBuilder
    private func inboxSidebar(isSplit: Bool) -> some View {
        if accountStore.accounts.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    JoatHeroCard(
                        eyebrow: "Standalone iPhone Mail",
                        title: "Connect Gmail to JOAT Brain",
                        subtitle: "Direct TLS IMAP & SMTP on your iPhone—zero Mac mini dependency, unlimited Gmail or Workspace accounts, and unread-safe syncing.",
                        icon: "envelope.badge.shield.half.filled",
                        statusColor: JoatTheme.accent
                    ) {
                        VStack(alignment: .leading, spacing: 12) {
                            HStack(spacing: 8) {
                                JoatStatusPill(label: "On-Phone TLS", color: JoatTheme.accent, icon: "lock.shield.fill")
                                JoatStatusPill(label: "Unread Safe", color: JoatTheme.accent, icon: "eye.slash.fill")
                            }
                            Button {
                                showingAddAccount = true
                            } label: {
                                Label("Connect Gmail", systemImage: "person.crop.circle.badge.plus")
                                    .font(.subheadline.weight(.semibold))
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(JoatTheme.accent)
                            .controlSize(.regular)
                        }
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Label("Incoming cards on Even G2 with newsletter filtering", systemImage: "eyeglasses")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Label("Tap-to-reply from the exact account that received the thread", systemImage: "arrowshape.turn.up.left.fill")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        Label("16-character App Password stored only in iOS Keychain", systemImage: "key.fill")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .joatCardContainer(cornerRadius: 16, padding: 14)
                }
                .padding(16)
            }
            .background(JoatTheme.surface.ignoresSafeArea())
        } else {
            List {
                Section {
                    accountFilterStrip
                        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                }

                let activeErrors = accountStore.enabledAccounts.compactMap { acct -> (GmailAccount, String)? in
                    guard let err = mail.accountErrors[acct.id], !err.isEmpty else { return nil }
                    return (acct, err)
                }
                if !activeErrors.isEmpty {
                    Section {
                        ForEach(activeErrors, id: \.0.id) { acct, err in
                            HStack(alignment: .top, spacing: 10) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .foregroundStyle(JoatTheme.amber)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("\(acct.label) needs attention")
                                        .font(.subheadline.weight(.semibold))
                                    Text(err)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Manage") {
                                    showingAccounts = true
                                }
                                .font(.caption.weight(.semibold))
                                .buttonStyle(.bordered)
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }

                if filteredThreads.isEmpty {
                    Section {
                        VStack(spacing: 10) {
                            if mail.syncing {
                                ProgressView()
                                    .tint(JoatTheme.accent)
                                Text("Syncing your Gmail inbox…")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            } else if accountStore.enabledAccounts.isEmpty {
                                Image(systemName: "pause.circle")
                                    .font(.title2)
                                    .foregroundStyle(.secondary)
                                Text("All Gmail accounts are paused")
                                    .font(.subheadline.weight(.semibold))
                                Button("Manage Accounts") {
                                    showingAccounts = true
                                }
                                .buttonStyle(.bordered)
                            } else if !searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                Image(systemName: "magnifyingglass")
                                    .font(.title2)
                                    .foregroundStyle(.secondary)
                                Text("No emails match “\(searchQuery)”")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            } else {
                                Image(systemName: "tray")
                                    .font(.title2)
                                    .foregroundStyle(.secondary)
                                Text("Your inbox is clear")
                                    .font(.subheadline.weight(.semibold))
                                Text("Pull down to sync new messages.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 28)
                        .listRowBackground(Color.clear)
                    }
                } else {
                    Section {
                        ForEach(filteredThreads) { thread in
                            if isSplit {
                                Button {
                                    selectedThreadId = thread.id
                                } label: {
                                    MailThreadRowView(
                                        thread: thread,
                                        showAccountBadge: accountStore.accounts.count > 1 && accountFilter == nil,
                                        isSelected: selectedThreadId == thread.id
                                    )
                                }
                                .buttonStyle(.plain)
                                .listRowBackground(
                                    selectedThreadId == thread.id
                                        ? JoatTheme.accent.opacity(0.12)
                                        : JoatTheme.card
                                )
                            } else {
                                NavigationLink(value: thread.id) {
                                    MailThreadRowView(
                                        thread: thread,
                                        showAccountBadge: accountStore.accounts.count > 1 && accountFilter == nil,
                                        isSelected: false
                                    )
                                }
                                .listRowBackground(JoatTheme.card)
                            }
                        }
                    } header: {
                        HStack {
                            Text(accountFilterLabel)
                            Spacer()
                            if mail.syncing {
                                HStack(spacing: 4) {
                                    ProgressView().controlSize(.mini)
                                    Text("Syncing")
                                }
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            } else if let synced = mail.lastSyncAt {
                                Text("Updated \(synced.formatted(date: .omitted, time: .shortened))")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(JoatTheme.surface.ignoresSafeArea())
        }
    }

    private var accountFilterLabel: String {
        guard let acctId = accountFilter,
              let acct = accountStore.accounts.first(where: { $0.id == acctId }) else {
            return "Unified Inbox"
        }
        return "\(acct.label) · \(acct.email)"
    }

    private var accountFilterStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                AccountFilterPill(
                    title: "All",
                    unread: unreadCount(for: nil),
                    isSelected: accountFilter == nil,
                    isDimmed: false
                ) {
                    accountFilter = nil
                }

                ForEach(accountStore.accounts) { account in
                    AccountFilterPill(
                        title: account.label,
                        unread: unreadCount(for: account.id),
                        isSelected: accountFilter == account.id,
                        isDimmed: !account.enabled
                    ) {
                        accountFilter = (accountFilter == account.id) ? nil : account.id
                    }
                }
            }
            .padding(.vertical, 2)
        }
    }

    @ToolbarContentBuilder
    private var inboxToolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Button {
                showingAccounts = true
            } label: {
                Label("Gmail Accounts", systemImage: "person.crop.circle.badge.plus")
            }
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                if accountStore.enabledAccounts.isEmpty {
                    showingAddAccount = true
                } else {
                    showingCompose = true
                }
            } label: {
                Label("Compose", systemImage: "square.and.pencil")
            }
        }
    }
}

private extension View {
    @ViewBuilder
    func modifySearchable(enabled: Bool, text: Binding<String>) -> some View {
        if enabled {
            self.searchable(text: text, prompt: "Search sender, subject, or message")
        } else {
            self
        }
    }
}

// MARK: - Filter Pill & Inbox Row

private struct AccountFilterPill: View {
    let title: String
    let unread: Int
    let isSelected: Bool
    let isDimmed: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Text(title)
                    .font(.subheadline.weight(isSelected ? .semibold : .medium))
                if unread > 0 {
                    Text("\(unread)")
                        .font(.caption2.weight(.bold).monospacedDigit())
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1.5)
                        .background(
                            isSelected ? Color.white.opacity(0.24) : JoatTheme.accent.opacity(0.16),
                            in: Capsule()
                        )
                        .foregroundStyle(isSelected ? Color.white : JoatTheme.accent)
                }
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 7)
            .foregroundStyle(isSelected ? Color.white : (isDimmed ? .secondary : .primary))
            .background(
                Capsule()
                    .fill(isSelected ? JoatTheme.accent : JoatTheme.card)
            )
            .overlay(
                Capsule()
                    .strokeBorder(isSelected ? JoatTheme.accent : JoatTheme.cardBorder, lineWidth: 1)
            )
            .opacity(isDimmed && !isSelected ? 0.65 : 1.0)
        }
        .buttonStyle(.plain)
    }
}

private struct MailThreadRowView: View {
    let thread: Mail.ThreadSummary
    let showAccountBadge: Bool
    let isSelected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(thread.unread ? JoatTheme.accent : Color.clear)
                .frame(width: 8, height: 8)
                .padding(.top, 6)

            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(thread.participantLabel)
                        .font(.subheadline.weight(thread.unread ? .bold : .semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)

                    if showAccountBadge {
                        Text(thread.accountLabel)
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(JoatTheme.accent)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1.5)
                            .background(JoatTheme.accent.opacity(0.12), in: Capsule())
                    }

                    if thread.messages.count > 1 {
                        Text("\(thread.messages.count)")
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.tertiary)
                    }

                    Spacer(minLength: 4)

                    Text(Self.shortTimestamp(thread.at))
                        .font(.caption2)
                        .foregroundStyle(thread.unread ? JoatTheme.accent : .secondary)
                }

                Text(thread.subject)
                    .font(.footnote.weight(thread.unread ? .semibold : .medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)

                if !thread.preview.isEmpty {
                    Text(thread.preview)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }

    private static func shortTimestamp(_ date: Date) -> String {
        if Calendar.current.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        if Calendar.current.isDateInYesterday(date) {
            return "Yesterday"
        }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }
}

// MARK: - Thread Detail & Inline Quick Reply

struct MailThreadDetailView: View {
    let threadId: String
    @ObservedObject var mail: Mail

    @State private var expandedMessageIds: Set<String> = []
    @State private var didSeedExpansion = false
    @State private var replyText = ""
    @State private var sendingReply = false
    @State private var replyError: String?
    @FocusState private var replyFocused: Bool

    private var summary: Mail.ThreadSummary? {
        mail.threads.first { $0.id == threadId }
    }

    var body: some View {
        if let summary {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        threadHeader(summary)

                        ForEach(summary.messages) { msg in
                            MailMessageCardView(
                                message: msg,
                                isExpanded: expandedMessageIds.contains(msg.id),
                                canCollapse: summary.messages.count > 1
                            ) {
                                guard summary.messages.count > 1 else { return }
                                withAnimation(.snappy(duration: 0.2)) {
                                    if expandedMessageIds.contains(msg.id) {
                                        expandedMessageIds.remove(msg.id)
                                    } else {
                                        expandedMessageIds.insert(msg.id)
                                    }
                                }
                            }
                            .id(msg.id)
                        }
                    }
                    .padding(16)
                }
                .background(JoatTheme.surface.ignoresSafeArea())
                .navigationTitle(summary.participantLabel)
                .navigationBarTitleDisplayMode(.inline)
                .safeAreaInset(edge: .bottom) {
                    if !summary.replyEmail.isEmpty {
                        quickReplyBar(summary, scrollProxy: proxy)
                    }
                }
                .onAppear {
                    seedExpandedMessages(for: summary)
                    mail.markThreadRead(summary.id)
                }
                .onChange(of: threadId) { _, newId in
                    mail.markThreadRead(newId)
                }
                .onChange(of: summary.messages.count) { _, _ in
                    if let newest = summary.messages.last {
                        expandedMessageIds.insert(newest.id)
                    }
                }
            }
        } else {
            ContentUnavailableView(
                "Conversation Unavailable",
                systemImage: "envelope",
                description: Text("This thread is no longer in your active inbox.")
            )
        }
    }

    private func seedExpandedMessages(for summary: Mail.ThreadSummary) {
        guard !didSeedExpansion else { return }
        didSeedExpansion = true
        if summary.messages.count <= 2 {
            expandedMessageIds = Set(summary.messages.map(\.id))
        } else {
            expandedMessageIds = Set(summary.messages.suffix(2).map(\.id))
        }
    }

    private func threadHeader(_ summary: Mail.ThreadSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(summary.subject)
                .font(.title3.weight(.bold))
                .foregroundStyle(.primary)
                .textSelection(.enabled)

            HStack(spacing: 8) {
                JoatStatusPill(
                    label: "via \(summary.accountLabel) · \(summary.accountEmail)",
                    color: JoatTheme.accent,
                    icon: "envelope.fill"
                )
                if summary.messages.count > 1 {
                    Text("\(summary.messages.count) messages")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, 4)
    }

    private func quickReplyBar(_ summary: Mail.ThreadSummary, scrollProxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let err = replyError, !err.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(JoatTheme.amber)
                    Text(err)
                        .font(.caption)
                        .foregroundStyle(.primary)
                    Spacer()
                    Button {
                        replyError = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 4)
            }

            HStack(spacing: 4) {
                Image(systemName: "arrowshape.turn.up.left.fill")
                    .font(.system(size: 10))
                Text("Replying from \(summary.accountLabel) (\(summary.accountEmail)) to \(summary.replyEmail)")
                    .lineLimit(1)
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 4)

            HStack(alignment: .bottom, spacing: 10) {
                TextField("Reply to \(summary.participantLabel)…", text: $replyText, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($replyFocused)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(JoatTheme.surface)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
                    )

                Button {
                    sendQuickReply(summary, scrollProxy: scrollProxy)
                } label: {
                    if sendingReply {
                        ProgressView()
                            .frame(width: 34, height: 34)
                    } else {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 32))
                            .foregroundStyle(
                                canSendReply ? JoatTheme.accent : Color.secondary.opacity(0.35)
                            )
                    }
                }
                .disabled(!canSendReply || sendingReply)
                .buttonStyle(.plain)
                .accessibilityLabel("Send reply")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Divider()
        }
    }

    private var canSendReply: Bool {
        !replyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func sendQuickReply(_ summary: Mail.ThreadSummary, scrollProxy: ScrollViewProxy) {
        let body = replyText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return }
        sendingReply = true
        replyError = nil
        Task { @MainActor in
            do {
                try await mail.sendFromApp(
                    accountId: summary.accountId,
                    to: summary.replyEmail,
                    subject: Mail.replySubject(for: summary.subject),
                    body: body,
                    replyToThreadId: summary.id
                )
                replyText = ""
                replyFocused = false
                if let updated = mail.threads.first(where: { $0.id == summary.id }),
                   let lastId = updated.messages.last?.id {
                    expandedMessageIds.insert(lastId)
                    withAnimation {
                        scrollProxy.scrollTo(lastId, anchor: .bottom)
                    }
                }
            } catch {
                replyError = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            }
            sendingReply = false
        }
    }
}

private struct MailMessageCardView: View {
    let message: Mail.MessageItem
    let isExpanded: Bool
    let canCollapse: Bool
    let onToggle: () -> Void

    private var senderTitle: String {
        if message.outgoing {
            return "You (\(message.accountLabel))"
        }
        return message.fromName.isEmpty ? message.fromEmail : message.fromName
    }

    private var addressLine: String {
        if message.outgoing {
            let recipients = message.toEmails.joined(separator: ", ")
            return recipients.isEmpty ? message.accountEmail : "to \(recipients)"
        }
        return message.fromEmail
    }

    private var prose: String {
        let content = message.body.trimmingCharacters(in: .whitespacesAndNewlines)
        if !content.isEmpty { return content }
        let snip = message.snippet.trimmingCharacters(in: .whitespacesAndNewlines)
        return snip.isEmpty ? "(No content)" : snip
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: onToggle) {
                HStack(alignment: .top, spacing: 10) {
                    ZStack {
                        Circle()
                            .fill(message.outgoing ? JoatTheme.accent.opacity(0.18) : Color.secondary.opacity(0.14))
                            .frame(width: 34, height: 34)
                        Text(String(senderTitle.prefix(1)).uppercased())
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(message.outgoing ? JoatTheme.accent : .primary)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(senderTitle)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            if message.outgoing {
                                Text("Sent")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(JoatTheme.accent)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 1)
                                    .background(JoatTheme.accent.opacity(0.12), in: Capsule())
                            }
                        }

                        if isExpanded {
                            Text(addressLine)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        } else {
                            Text(message.snippet.isEmpty ? prose : message.snippet)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }

                    Spacer(minLength: 4)

                    VStack(alignment: .trailing, spacing: 4) {
                        Text(message.at.formatted(date: .abbreviated, time: .shortened))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        if canCollapse {
                            Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider()
                Text(prose)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(JoatTheme.card)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(
                    message.outgoing ? JoatTheme.accent.opacity(0.28) : JoatTheme.cardBorder,
                    lineWidth: 1
                )
        )
    }
}

// MARK: - Compose Email Sheet

struct ComposeEmailSheet: View {
    @ObservedObject var mail: Mail
    @ObservedObject var accountStore: GmailAccountStore

    @State private var fromAccountId: String
    @State private var to = ""
    @State private var subject = ""
    @State private var messageBody = ""
    @State private var sending = false
    @State private var errorMessage: String?
    @Environment(\.dismiss) private var dismiss

    init(mail: Mail, accountStore: GmailAccountStore, initialAccountId: String?) {
        self.mail = mail
        self.accountStore = accountStore
        let resolved = initialAccountId.flatMap { id in
            accountStore.enabledAccounts.contains(where: { $0.id == id }) ? id : nil
        } ?? accountStore.defaultAccount?.id ?? ""
        _fromAccountId = State(initialValue: resolved)
    }

    private var canSend: Bool {
        !fromAccountId.isEmpty
            && !to.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !messageBody.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if accountStore.enabledAccounts.count > 1 {
                        Picker("From", selection: $fromAccountId) {
                            ForEach(accountStore.enabledAccounts) { acct in
                                Text("\(acct.label) · \(acct.email)").tag(acct.id)
                            }
                        }
                    } else if let acct = accountStore.enabledAccounts.first {
                        LabeledContent("From") {
                            Text("\(acct.label) · \(acct.email)")
                                .foregroundStyle(.secondary)
                        }
                    }

                    TextField("To (recipient@example.com)", text: $to)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    TextField("Subject", text: $subject)
                }

                Section("Message") {
                    TextEditor(text: $messageBody)
                        .frame(minHeight: 190)
                }

                if let err = errorMessage, !err.isEmpty {
                    Section {
                        Label(err, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle("New Email")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(sending)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "Sending…" : "Send") {
                        sendEmail()
                    }
                    .fontWeight(.semibold)
                    .disabled(!canSend || sending)
                }
            }
        }
        .tint(JoatTheme.accent)
    }

    private func sendEmail() {
        sending = true
        errorMessage = nil
        Task { @MainActor in
            do {
                try await mail.sendFromApp(
                    accountId: fromAccountId,
                    to: to,
                    subject: subject,
                    body: messageBody,
                    replyToThreadId: nil
                )
                sending = false
                dismiss()
            } catch {
                errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                sending = false
            }
        }
    }
}

// MARK: - Gmail Accounts & Glasses Settings Sheet

struct GmailAccountsSheet: View {
    @ObservedObject var mail: Mail
    @ObservedObject var accountStore: GmailAccountStore

    @State private var showingAddSheet = false
    @State private var editingAccount: GmailAccount?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if accountStore.accounts.isEmpty {
                        Text("No Gmail accounts connected yet.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(accountStore.accounts) { account in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(alignment: .center, spacing: 10) {
                                    Button {
                                        accountStore.setDefault(account.id)
                                    } label: {
                                        Image(systemName: account.isDefault ? "star.fill" : "star")
                                            .foregroundStyle(account.isDefault ? JoatTheme.accent : .secondary)
                                            .font(.body)
                                    }
                                    .buttonStyle(.borderless)
                                    .accessibilityLabel(account.isDefault ? "Default account" : "Make \(account.label) default")

                                    VStack(alignment: .leading, spacing: 2) {
                                        HStack(spacing: 6) {
                                            Text(account.label)
                                                .font(.body.weight(.semibold))
                                            if account.isDefault {
                                                JoatStatusPill(label: "Default", color: JoatTheme.accent, icon: "star.fill")
                                            }
                                        }
                                        Text(account.email)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }

                                    Spacer()

                                    Toggle(
                                        "",
                                        isOn: Binding(
                                            get: { account.enabled },
                                            set: { accountStore.setEnabled(account.id, $0) }
                                        )
                                    )
                                    .labelsHidden()
                                }

                                HStack(spacing: 8) {
                                    accountStatusView(account)
                                    Spacer()
                                    Button("Update Password") {
                                        editingAccount = account
                                    }
                                    .font(.caption.weight(.medium))
                                    .buttonStyle(.borderless)
                                }
                            }
                            .padding(.vertical, 4)
                            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                Button(role: .destructive) {
                                    accountStore.remove(account.id)
                                } label: {
                                    Label("Remove", systemImage: "trash")
                                }
                            }
                        }
                        .onDelete { offsets in
                            let ids = offsets.map { accountStore.accounts[$0].id }
                            ids.forEach { accountStore.remove($0) }
                        }
                    }

                    Button {
                        showingAddSheet = true
                    } label: {
                        Label("Add Gmail Account…", systemImage: "plus.circle.fill")
                            .fontWeight(.medium)
                    }
                } header: {
                    Text("Connected Gmail Accounts")
                } footer: {
                    Text("Tap the star to choose the default sending account for voice emails on the glasses. Swipe left on an account to remove its Keychain App Password.")
                }

                Section {
                    Toggle("Only show direct emails on glasses", isOn: $mail.directOnlyOnGlasses)
                    if !accountStore.enabledAccounts.isEmpty {
                        Button {
                            Task { await mail.syncNow() }
                        } label: {
                            HStack {
                                Label(mail.syncing ? "Syncing Now…" : "Sync Inbox Now", systemImage: "arrow.clockwise")
                                Spacer()
                                if mail.syncing {
                                    ProgressView().controlSize(.small)
                                }
                            }
                        }
                        .disabled(mail.syncing)
                    }
                } header: {
                    Text("Even G2 Glasses Behavior")
                } footer: {
                    Text("Keeps newsletters, automated alerts, and bulk mailing lists off the glasses HUD while leaving them available in Mail.")
                }
            }
            .navigationTitle("Gmail Accounts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .sheet(isPresented: $showingAddSheet) {
                AddGmailAccountSheet(mail: mail, accountStore: accountStore)
            }
            .sheet(item: $editingAccount) { acct in
                AddGmailAccountSheet(mail: mail, accountStore: accountStore, existingAccount: acct)
            }
        }
        .tint(JoatTheme.accent)
    }

    @ViewBuilder
    private func accountStatusView(_ account: GmailAccount) -> some View {
        if !account.enabled {
            Label("Sync paused", systemImage: "pause.circle")
                .font(.caption2)
                .foregroundStyle(.secondary)
        } else if let err = mail.accountErrors[account.id], !err.isEmpty {
            Label(err, systemImage: "exclamationmark.triangle.fill")
                .font(.caption2)
                .foregroundStyle(JoatTheme.amber)
                .lineLimit(2)
        } else if mail.syncing {
            Label("Syncing…", systemImage: "arrow.triangle.2.circlepath")
                .font(.caption2)
                .foregroundStyle(.secondary)
        } else if let syncedAt = mail.lastSyncAt {
            Label("Synced \(syncedAt.formatted(.relative(presentation: .named)))", systemImage: "checkmark.circle.fill")
                .font(.caption2)
                .foregroundStyle(JoatTheme.accent)
        } else {
            Label("Ready", systemImage: "checkmark.circle")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Add / Update Gmail Account Sheet

struct AddGmailAccountSheet: View {
    @ObservedObject var mail: Mail
    @ObservedObject var accountStore: GmailAccountStore
    var existingAccount: GmailAccount? = nil

    @State private var label: String
    @State private var displayName: String
    @State private var email: String
    @State private var appPassword = ""
    @State private var makeDefault: Bool
    @State private var verifying = false
    @State private var errorMessage: String?
    @Environment(\.dismiss) private var dismiss

    private static let appPasswordsURL = URL(string: "https://myaccount.google.com/apppasswords")!

    init(mail: Mail, accountStore: GmailAccountStore, existingAccount: GmailAccount? = nil) {
        self.mail = mail
        self.accountStore = accountStore
        self.existingAccount = existingAccount
        _label = State(initialValue: existingAccount?.label ?? "")
        _displayName = State(initialValue: existingAccount?.displayName ?? "")
        _email = State(initialValue: existingAccount?.email ?? "")
        _makeDefault = State(initialValue: existingAccount?.isDefault ?? accountStore.accounts.isEmpty)
    }

    private var normalizedPasswordLength: Int {
        GmailAccountStore.normalizeAppPassword(appPassword).count
    }

    private var canSubmit: Bool {
        GmailAccountStore.isValidEmail(email) && normalizedPasswordLength >= 16 && !verifying
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Account Details") {
                    TextField("Label (e.g. Personal, Work)", text: $label)
                    TextField("Display Name (optional)", text: $displayName)
                    TextField("Gmail Address (name@example.com)", text: $email)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .disabled(existingAccount != nil)

                    SecureField("16-character App Password", text: $appPassword)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    Toggle("Default sending account", isOn: $makeDefault)
                }

                Section {
                    Button {
                        UIApplication.shared.open(Self.appPasswordsURL)
                    } label: {
                        Label("Open Google App Passwords in Safari", systemImage: "safari")
                            .fontWeight(.medium)
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        Text("1. Sign in with 2-Step Verification enabled and generate a new App Password for “JOAT Brain”.")
                        Text("2. Paste the 16-character password above—spaces and dashes are stripped automatically and saved only in iOS Keychain.")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 2)
                } header: {
                    Text("Google App Password")
                }

                if let err = errorMessage, !err.isEmpty {
                    Section {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(JoatTheme.amber)
                            Text(err)
                                .font(.footnote)
                        }
                    }
                }
            }
            .navigationTitle(existingAccount == nil ? "Connect Gmail" : "Update Gmail")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(verifying)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        verifyAndSave()
                    } label: {
                        if verifying {
                            HStack(spacing: 6) {
                                ProgressView().controlSize(.small)
                                Text("Verifying…")
                            }
                        } else {
                            Text("Save")
                                .fontWeight(.semibold)
                        }
                    }
                    .disabled(!canSubmit)
                }
            }
        }
        .tint(JoatTheme.accent)
    }

    private func verifyAndSave() {
        errorMessage = nil
        verifying = true
        let targetEmail = email
        let enteredPassword = appPassword
        let enteredLabel = label
        let enteredName = displayName
        let shouldDefault = makeDefault

        Task { @MainActor in
            do {
                try await Mail.verifyAccount(email: targetEmail, appPassword: enteredPassword)
                _ = try accountStore.addOrUpdate(
                    email: targetEmail,
                    label: enteredLabel,
                    displayName: enteredName,
                    appPassword: enteredPassword,
                    makeDefault: shouldDefault
                )
                appPassword = ""
                verifying = false
                dismiss()
                await mail.syncNow()
            } catch {
                errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                verifying = false
            }
        }
    }
}
