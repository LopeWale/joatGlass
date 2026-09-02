import AppKit
import SwiftUI

/// The app's canonical window: what the assistant IS DOING right now.
/// (The raw log lives one button away — it is a debugging aid, not the UI.)
struct MainWindowView: View {
    @ObservedObject var engine: EngineController
    @State private var status = EngineStatus()
    @State private var live = false
    @State private var showLog = false
    @State private var logTail = ""
    @State private var messagesAccess: MessagesAccessState = .blocked
    @State private var notesAutomation: NotesAutomationState = .notDetermined
    // The glasses' News tab: which free feeds the engine polls. Saved on
    // every change; the engine re-reads the file on its next pass.
    @State private var newsConfig = NewsConfigStore().load()
    @State private var newFeedURL = ""
    @State private var newsNotice: String?

    private let messagesAccessProbe = MessagesAccessProbe()
    private let notesAutomationProbe = NotesAutomationProbe()

    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header

                HStack(spacing: 10) {
                    Pill("Glasses", value: live ? status.glasses : "Status unavailable",
                         ok: live && status.glasses == "connected")
                    Pill("Mic", value: live ? status.mic : "Status unavailable",
                         ok: live && status.mic == "streaming")
                    Pill("Notifications", value: live ? status.notifications : "Status unavailable",
                         ok: live && status.notifications == "live")
                }
                HStack(spacing: 10) {
                    Pill("Speech", value: live ? status.stt : "Status unavailable",
                         ok: live && status.stt == "ready")
                    Pill("Model", value: live ? status.llm : "Status unavailable",
                         ok: live && status.llm == "ready")
                    Pill("Brainstorm", value: brainstormValue,
                         ok: live && ["ready", "running", "settled"].contains(status.brainstorm.state))
                }

                GroupBox("Last exchange") {
                    VStack(alignment: .leading, spacing: 6) {
                        Row(icon: "quote.bubble", label: "You said",
                            text: status.lastTranscript.isEmpty ? "nothing yet — say “JOAT …”" : status.lastTranscript)
                        Row(icon: "eyeglasses", label: "On the HUD",
                            text: status.lastReply.isEmpty ? "—" : status.lastReply)
                        if !status.lastCard.isEmpty {
                            Row(icon: "bell.badge", label: "Last card", text: status.lastCard)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(4)
                }

                openClawStatus

                if !engine.accessibilityTrusted {
                    GroupBox {
                        HStack {
                            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Notification text needs Accessibility").font(.callout.weight(.medium))
                                Text("Without it, cards can show which app notified you but not what it said.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Grant…") { engine.requestAccessibility() }
                        }
                        .padding(4)
                    }
                }

                // One dense access panel, not a card per permission: each grant
                // is a single row with its action inline, and explanatory copy
                // appears only while the corresponding row actually needs help.
                GroupBox("Access") {
                    VStack(alignment: .leading, spacing: 6) {
                        AccessRow(
                            label: "Calendar",
                            value: engine.calendarAccess,
                            granted: engine.calendarAccess == "full access"
                        ) {
                            Button("Grant…") { engine.requestCalendarAccess() }
                        }
                        AccessRow(
                            label: "Reminders",
                            value: engine.remindersAccess,
                            granted: engine.remindersAccess == "full access"
                        ) {
                            Button("Grant…") { engine.requestRemindersAccess() }
                        }
                        AccessRow(
                            label: "Engine bridge",
                            value: personalDataBridgeValue,
                            granted: live && status.personalData.state == "ready"
                        ) { EmptyView() }
                        AccessRow(
                            label: "Messages history",
                            value: messagesAccess.label,
                            granted: messagesAccess == .granted
                        ) {
                            Button("Full Disk Access…") { openMessagesPrivacySettings() }
                            Button("Check") { messagesAccess = messagesAccessProbe.state() }
                        }
                        AccessRow(
                            label: "Notes (QuickNote export)",
                            value: notesAutomation.label,
                            granted: notesAutomation == .granted
                        ) {
                            Button("Grant…") {
                                Task {
                                    notesAutomation = await NotesAutomationProbe.requestAccess(
                                        probe: notesAutomationProbe
                                    )
                                }
                            }
                            Button("Check") { notesAutomation = notesAutomationProbe.state() }
                        }
                        if let help = accessHelp {
                            Text(help).font(.caption).foregroundStyle(.secondary)
                        }
                        Text("Access is requested only from these buttons; revoke any of it in System Settings. Status checks read no content.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(4)
                }

                newsSection

                Divider()

                HStack {
                    Button(showLog ? "Hide Log" : "Show Log") { showLog.toggle() }
                    Button("Open Full Log") { engine.openLog() }
                    Toggle("Record G2 trace", isOn: Binding(
                        get: { engine.g2DiagnosticsEnabled },
                        set: { engine.setG2DiagnosticsEnabled($0) }
                    ))
                    .help("Records only page, gesture, tile, and recovery metadata. Enabling restarts the engine.")
                    Button("Export Redacted G2 Trace…") { engine.exportG2Diagnostics() }
                    // The only recovery a locked-out phone cannot perform itself:
                    // it holds a token this Mac no longer accepts (reinstall,
                    // cleared storage, a different phone), so it cannot
                    // authenticate to ask. The wearer just sees "reconnecting"
                    // forever. Authority here is physical access to the Mac.
                    Button("Pair / Replace Glasses") { engine.openPairingWindow() }
                        .help("Open a private 120-second pairing window. Use when "
                              + "the HUD is stuck on \"reconnecting\" or when replacing glasses.")
                    Spacer()
                    Text("\(status.counts.utterances) asked · \(status.counts.cards) cards")
                        .font(.caption).foregroundStyle(.secondary)
                }

                if let notice = engine.pairingNotice {
                    Text(notice)
                        .font(.caption)
                        .foregroundStyle(notice.hasPrefix("Pairing stayed closed") ? .red : .secondary)
                }

                if let notice = engine.g2DiagnosticNotice {
                    Text(notice)
                        .font(.caption)
                        .foregroundStyle(notice.hasPrefix("G2 trace export failed") || notice.hasPrefix("Trace stayed off") ? .red : .secondary)
                }

                if showLog {
                    ScrollView {
                        Text(logTail).font(.system(size: 10, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                    }
                    .frame(height: 150)
                }
            }
            .padding(16)
        }
        .frame(minWidth: 560, minHeight: 520)
        .onAppear {
            refresh()
            engine.refreshPersonalDataAccess()
            messagesAccess = messagesAccessProbe.state()
            notesAutomation = notesAutomationProbe.state()
        }
        .onReceive(tick) { _ in refresh() }
    }

    /// The News tab on the glasses reads free RSS feeds the engine polls.
    /// This is the one place the wearer chooses which: a toggle per
    /// category, any feed of their own, and how often to look.
    private var newsSection: some View {
        GroupBox("News on the glasses") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Free feeds only — BBC, NPR and Google News per category, plus any RSS or Atom link you add. Fetched on this Mac; the glasses never fetch.")
                    .font(.caption).foregroundStyle(.secondary)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .leading)], alignment: .leading, spacing: 4) {
                    ForEach(NewsConfig.categories) { category in
                        Toggle(category.label, isOn: Binding(
                            get: { newsConfig.categories.contains(category.id) },
                            set: { on in setCategory(category.id, enabled: on) }
                        ))
                        .toggleStyle(.checkbox)
                    }
                }
                if !newsConfig.customFeeds.isEmpty {
                    ForEach(newsConfig.customFeeds, id: \.self) { feed in
                        HStack(spacing: 6) {
                            Text(feed).font(.caption).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Button("Remove") { removeFeed(feed) }.font(.caption)
                        }
                    }
                }
                HStack(spacing: 6) {
                    TextField("https://example.com/feed.xml", text: $newFeedURL)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit(addFeed)
                    Button("Add feed", action: addFeed)
                        .disabled(!(newFeedURL.hasPrefix("https://") || newFeedURL.hasPrefix("http://")))
                    Stepper(
                        "Every \(newsConfig.refreshMinutes) min",
                        value: Binding(
                            get: { newsConfig.refreshMinutes },
                            set: { setRefresh($0) }
                        ),
                        in: NewsConfig.minimumRefreshMinutes...NewsConfig.maximumRefreshMinutes,
                        step: 5
                    )
                    .font(.caption)
                }
                if let notice = newsNotice {
                    Text(notice).font(.caption)
                        .foregroundStyle(notice.hasPrefix("Could not save") ? .red : .secondary)
                }
            }
            .padding(4)
        }
    }

    private func setCategory(_ id: String, enabled: Bool) {
        var next = newsConfig
        next.categories.removeAll { $0 == id }
        if enabled {
            // Keep the engine's listing order so the file reads the way the
            // toggles do, rather than the order the wearer clicked them.
            next.categories.append(id)
            let order = NewsConfig.categories.map(\.id)
            next.categories.sort { (order.firstIndex(of: $0) ?? 0) < (order.firstIndex(of: $1) ?? 0) }
        }
        saveNews(next)
    }

    private func addFeed() {
        let url = newFeedURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard url.hasPrefix("https://") || url.hasPrefix("http://") else { return }
        var next = newsConfig
        if !next.customFeeds.contains(url) { next.customFeeds.append(url) }
        newFeedURL = ""
        saveNews(next)
    }

    private func removeFeed(_ feed: String) {
        var next = newsConfig
        next.customFeeds.removeAll { $0 == feed }
        saveNews(next)
    }

    private func setRefresh(_ minutes: Int) {
        var next = newsConfig
        next.refreshMinutes = minutes
        saveNews(next)
    }

    private func saveNews(_ next: NewsConfig) {
        let normalized = next.normalized
        newsConfig = normalized
        do {
            try NewsConfigStore().save(normalized)
            let feeds = normalized.categories.count * 3 + normalized.customFeeds.count
            newsNotice = "Saved. The engine polls \(feeds) feeds every \(normalized.refreshMinutes) minutes; the glasses update on its next pass."
        } catch {
            newsNotice = "Could not save news.json: \(error.localizedDescription)"
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Circle().fill(dotColor).frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 1) {
                Text(engine.statusLine).font(.title3.weight(.semibold))
                if live && !status.device.isEmpty {
                    Text(status.device).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if engine.state == .running {
                Button("Stop") { engine.stop() }
                Button("Restart") { engine.restart() }
            } else {
                Button("Start Engine") { engine.start() }.keyboardShortcut(.defaultAction)
            }
        }
    }

    private var dotColor: Color {
        switch engine.state {
        case .running: live ? .green : .yellow   // running but not yet reporting
        case .stopped: .gray
        case .crashed: .red
        case .failed: .orange
        }
    }

    private var brainstormValue: String {
        guard live else { return "Status unavailable · stale" }
        switch status.brainstorm.state {
        case "ready": return "Ready · \(status.brainstorm.seatCount) collaborators"
        case "running":
            let phase = [
                "opening": "Opening",
                "turn": "Collaborating",
                "closing": "Closing",
            ][status.brainstorm.phase] ?? "Working"
            return "\(phase) · \(status.brainstorm.seatCount) collaborators"
        case "settled": return "Settled · \(status.brainstorm.seatCount) collaborators"
        case "cancelled": return "Cancelled"
        case "failed": return "Failed"
        default: return "Unavailable"
        }
    }

    private var openClawStatus: some View {
        GroupBox("OpenClaw") {
            VStack(alignment: .leading, spacing: 8) {
                StatusLine(
                    label: "Gateway",
                    value: gatewayValue,
                    healthy: live && ["connected_read_only", "connected_operator"].contains(status.openClaw.state)
                )
                StatusLine(
                    label: "Agent Terminal",
                    value: terminalValue,
                    healthy: live && ["ready", "running"].contains(status.agentTerminal.state)
                )
                if live && !status.openClaw.endpointHost.isEmpty {
                    Text(status.openClaw.endpointHost)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .accessibilityLabel("OpenClaw Gateway host")
                }
                if live, let explanation = openClawExplanation {
                    Text(explanation)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
        }
    }

    private var gatewayValue: String {
        guard live else { return "Status unavailable · stale" }
        switch status.openClaw.state {
        case "checking": return "Checking signed pairing"
        case "approval_required": return "Approval required"
        case "connected_read_only": return "Connected · read-only"
        case "connected_operator": return "Connected · read + write"
        case "disconnected": return "Disconnected"
        case "failed": return "Connection failed"
        default: return "Not configured"
        }
    }

    private var terminalValue: String {
        guard live else { return "Status unavailable · stale" }
        switch status.agentTerminal.state {
        case "approval_required": return "Approval required"
        case "ready": return "Ready · dedicated no-tool session"
        case "running": return "Running"
        case "blocked": return "Blocked · safety gate"
        default: return "Unavailable"
        }
    }

    private var personalDataBridgeValue: String {
        guard live else { return "Status unavailable · stale" }
        switch status.personalData.state {
        case "checking": return "Checking"
        case "ready": return "Ready"
        case "authorization_required": return "Full access required"
        case "not_configured": return "Start through JOAT.app"
        case "unavailable": return "Restart JOAT"
        case "protocol_error": return "Update JOAT"
        default: return "Unavailable"
        }
    }

    private var openClawExplanation: String? {
        switch status.agentTerminal.reason {
        case "write_scope_required":
            return "Operator write approval is required before Agent Terminal can send."
        case "dedicated_session_not_proven":
            return "A dedicated no-tool Agent Terminal session has not been proven."
        case "tools_enabled":
            return "Agent Terminal is blocked because the remote agent exposes tools."
        case "history_recovery_required":
            return "Agent Terminal is paused until its exact session history is reconciled."
        case "connection_closed":
            return "The private Gateway connection closed after bounded reconnect attempts."
        case "protocol_rejected":
            return "Agent Terminal rejected an unexpected Gateway response."
        case "gateway_not_ready":
            return "Agent Terminal remains unavailable until the Gateway connection is proven."
        default:
            guard ["ready", "running"].contains(status.agentTerminal.state) else {
                return nil
            }
            return "JOAT selects only its separate no-tool session; it does not address Telegram or Hermes’s main transcript."
        }
    }

    private func refresh() {
        if let s = EngineStatus.load() {
            status = s
            live = s.isFresh && engine.state == .running
        } else {
            live = false
        }
        if showLog { logTail = engine.logTail(lines: 30) }
    }

    /// The one contextual help line for the Access panel: only the first
    /// problem speaks, so the panel never grows a paragraph per permission.
    private var accessHelp: String? {
        if live && status.personalData.state == "unavailable" {
            return "EventKit access is granted; restart JOAT to reconnect its engine."
        }
        if messagesAccess != .granted {
            return "Messages: grant JOAT under Privacy & Security → Full Disk Access, then Check; restart JOAT if it still shows blocked."
        }
        if notesAutomation == .denied {
            return "Notes: re-enable JOAT → Notes under Privacy & Security → Automation, then Check. “Send the last note to my notes” lands in the JOAT folder; iCloud syncs it to your phone."
        }
        if notesAutomation != .granted {
            return "Notes powers “send the last note to my notes” — Apple Notes on this Mac, synced to your phone by iCloud."
        }
        return nil
    }

    private func openMessagesPrivacySettings() {
        guard let url = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
        ) else { return }
        NSWorkspace.shared.open(url)
    }
}

private struct Pill: View {
    let label: String, value: String, ok: Bool
    init(_ label: String, value: String, ok: Bool) {
        self.label = label; self.value = value; self.ok = ok
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.medium))
                .foregroundStyle(ok ? Color.green : .secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

/// One permission as one row: label, current value, and either the green
/// check or its inline actions — no per-permission card chrome.
private struct AccessRow<Actions: View>: View {
    let label: String
    let value: String
    let granted: Bool
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(spacing: 8) {
            Text(label).frame(width: 170, alignment: .leading)
            Text(value)
                .foregroundStyle(granted ? Color.green : .secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            if granted {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .accessibilityLabel("Granted")
            } else {
                actions().controlSize(.small)
            }
        }
        .font(.callout)
        .accessibilityElement(children: .combine)
    }
}

private struct StatusLine: View {
    let label: String
    let value: String
    let healthy: Bool

    var body: some View {
        LabeledContent {
            Text(value)
                .foregroundStyle(healthy ? Color.green : .secondary)
        } label: {
            Text(label)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

private struct Row: View {
    let icon: String, label: String, text: String
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundStyle(.secondary).frame(width: 16)
            Text(label).font(.caption).foregroundStyle(.secondary).frame(width: 72, alignment: .leading)
            Text(text).font(.callout).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
