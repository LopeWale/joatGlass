import SwiftUI

/// The app's canonical window: what the assistant IS DOING right now.
/// (The raw log lives one button away — it is a debugging aid, not the UI.)
struct MainWindowView: View {
    @ObservedObject var engine: EngineController
    @State private var status = EngineStatus()
    @State private var live = false
    @State private var showLog = false
    @State private var logTail = ""

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

                GroupBox("Calendar & Reminders") {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            LabeledContent("Calendar", value: engine.calendarAccess)
                            Button("Connect…") { engine.requestCalendarAccess() }
                                .disabled(engine.calendarAccess == "full access")
                        }
                        HStack {
                            LabeledContent("Reminders", value: engine.remindersAccess)
                            Button("Connect…") { engine.requestRemindersAccess() }
                                .disabled(engine.remindersAccess == "full access")
                        }
                        Text("Access is requested only from these buttons. You can revoke it in System Settings.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(4)
                }

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
        }
        .onReceive(tick) { _ in refresh() }
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
