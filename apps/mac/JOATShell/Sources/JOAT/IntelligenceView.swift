import AppKit
import SwiftUI

struct IntelligenceView: View {
    @ObservedObject var engine: EngineController
    let readiness: MacReadiness
    private var status: EngineStatus { readiness.status }

    var body: some View {
        WorkspacePage(title: "Intelligence", subtitle: "The services behind your conversations and connected agents.") {
            VStack(spacing: 0) {
                ServiceRow(title: "Speech recognition", symbol: "waveform",
                           value: readiness.value(status.stt), detail: "Transcribes speech on this Mac",
                           healthy: readiness.isLive && status.stt == "ready")
                Divider()
                ServiceRow(title: "Language model", symbol: "sparkle",
                           value: readiness.value(status.llm), detail: "Produces JOAT’s answers",
                           healthy: readiness.isLive && status.llm == "ready")
                Divider()
                ServiceRow(title: "Brainstorm", symbol: "person.2",
                           value: brainstormValue,
                           detail: "Collaborators work together when you start Brainstorm on the glasses.",
                           healthy: readiness.isLive && ["ready", "running", "settled"].contains(status.brainstorm.state))
            }
            GroupBox("Agent Terminal · OpenClaw and Hermes") {
                A2AConnectionSettings(engine: engine).padding(10)
            }
            GroupBox("OpenClaw session Gateway") {
                VStack(alignment: .leading, spacing: 0) {
                    ServiceRow(title: "Gateway", symbol: "network", value: gatewayValue,
                               healthy: readiness.isLive && ["connected_read_only", "connected_operator"].contains(status.openClaw.state))
                    Divider()
                    ServiceRow(title: "Agent Terminal", symbol: "terminal", value: terminalValue,
                               healthy: readiness.isLive && ["ready", "running"].contains(status.agentTerminal.state))
                    if readiness.isLive && !status.openClaw.endpointHost.isEmpty {
                        Text(status.openClaw.endpointHost).font(.caption.monospaced())
                            .foregroundStyle(.secondary).textSelection(.enabled).padding(.bottom, 12)
                    }
                    if let help = connectionHelp { WorkspaceNotice(text: help).padding(.bottom, 12) }
                    LocalCommandView(commands: engine.commands, action: .serviceRetry,
                                     title: "Retry Service", detail: "Check the saved Hermes / OpenClaw session.")
                        .disabled(engine.isPreview || engine.isTransitioning)
                        .padding(.bottom, 16)
                    Divider().padding(.bottom, 16)
                    HermesConnectionSettings(engine: engine).disabled(engine.isPreview)
                }.padding(10)
            }
        }
    }

    private var brainstormValue: String {
        guard readiness.isLive else { return "Not reporting" }
        if status.brainstorm.state == "running" {
            let phase = ["opening": "Opening", "independent": "Independent work", "cross_review": "Cross-review",
                         "meta_review": "Reviewing", "synthesis": "Synthesizing", "closing": "Closing", "turn": "Collaborating"]
            return phase[status.brainstorm.phase] ?? "Working"
        }
        if ["ready", "settled"].contains(status.brainstorm.state) {
            return "\(status.brainstorm.state.capitalized) · \(status.brainstorm.seatCount) collaborators"
        }
        return status.brainstorm.state.capitalized
    }

    private var gatewayValue: String {
        guard readiness.isLive else { return "Not reporting" }
        return ["checking": "Checking pairing", "approval_required": "Approval required",
                "connected_read_only": "Connected · read only", "connected_operator": "Connected · read and write",
                "disconnected": "Disconnected", "failed": "Connection failed"][status.openClaw.state] ?? "Not configured"
    }

    private var terminalValue: String {
        guard readiness.isLive else { return "Not reporting" }
        return ["approval_required": "Approval required", "ready": "Ready", "running": "Running",
                "blocked": "Blocked"][status.agentTerminal.state] ?? "Unavailable"
    }

    private var connectionHelp: String? {
        guard readiness.isLive else { return nil }
        switch status.agentTerminal.reason {
        case "write_scope_required": return "Approve operator write access at your Gateway before using Agent Terminal."
        case "dedicated_session_not_proven": return "JOAT is checking its dedicated session before Agent Terminal can send."
        case "tools_enabled": return "This remote session exposes tools. JOAT requires its dedicated session with tools disabled."
        case "history_recovery_required": return "Agent Terminal is paused while its session history is reconciled."
        case "connection_closed": return "The Gateway connection closed. Check the remote Mac, then retry the service."
        case "protocol_rejected": return "The Gateway returned an unexpected response. Check the connection in Diagnostics."
        default:
            if status.openClaw.reason == "endpoint_refused" { return "The last connection attempt was refused. If the Gateway is available now, retry the service to check again." }
            if status.openClaw.reason == "host_unreachable" { return "The remote Mac cannot be reached. Check that it is awake and connected to Tailscale." }
            return "Agent Terminal uses JOAT’s dedicated session. Start and control conversations from the glasses."
        }
    }
}

/// Reused in the workspace and Settings so configuration has one UI owner.
struct HermesConnectionSettings: View {
    @ObservedObject var engine: EngineController
    @State private var model = HermesConnectionsModel.shared
    @State private var confirmingReset = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Agent Gateways").font(.headline)
            Text("These agent services are used by this Mac. The phone’s Mac selector lists JOAT hosts separately.")
                .font(.caption).foregroundStyle(.secondary)
            ForEach($model.drafts) { $draft in
                HStack {
                    TextField("OpenClaw Gateway URL", text: $draft.gatewayURL,
                              prompt: Text("wss://your-mac-mini.tailnet.ts.net"))
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("OpenClaw Gateway URL")
                        .onSubmit { model.save() }
                    if model.drafts.count > 1 {
                        Button("Remove Gateway", systemImage: "minus.circle") { model.removeHost(id: draft.id) }
                            .labelStyle(.iconOnly).buttonStyle(.borderless)
                            .help("Remove this Gateway from the draft. Save to apply.")
                    }
                }
            }
            Text("Connect to the OpenClaw Gateway for JOAT’s dedicated Hermes agent session. The first Gateway supplies the status above; additional Gateways are used when their Agent Terminal lanes are opened.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Add Gateway", systemImage: "plus") { model.addHost() }
                    .disabled(model.drafts.count >= 16 || model.loadFailed)
                Spacer()
                Button("Reload Saved") { model.reload() }
                Button("Save Connections") { model.save() }.disabled(!model.canSave)
            }
            if model.configurations == nil && model.drafts.contains(where: { !$0.gatewayURL.isEmpty }) {
                WorkspaceNotice(text: "Use distinct wss:// Gateway addresses without a path, password, or token. Port 9443 is the Hermes dashboard, not its Gateway.", isError: true)
            }
            if let notice = model.notice { WorkspaceNotice(text: notice, isError: model.failed) }
            if model.loadFailed {
                HStack {
                    Button("Back Up and Reset…") { confirmingReset = true }
                    Button("Show Saved File") {
                        NSWorkspace.shared.activateFileViewerSelecting([OpenClawRuntimeConfiguration.defaultURL])
                    }
                }
            }
            if let backup = model.recoveryBackupURL {
                Button("Show Backup") { NSWorkspace.shared.activateFileViewerSelecting([backup]) }
            }
            if model.requiresRestart(after: engine.lastStartedAt) && engine.state == .running {
                Button("Restart to Apply") { engine.restart() }.disabled(engine.isTransitioning)
            }
            Text("No token is stored here. A new Gateway may require device approval on the remote Mac.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .confirmationDialog("Back up the unreadable connections file and start again?", isPresented: $confirmingReset) {
            Button("Back Up and Reset") { model.backUpAndReset() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The existing file will be kept as a backup. You’ll need to enter and save your Gateway addresses again. Current engine connections continue until the engine restarts.")
        }
    }
}
