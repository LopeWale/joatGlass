import AppKit
import SwiftUI

struct AccessSettingsView: View {
    @ObservedObject var engine: EngineController
    let readiness: MacReadiness
    @Environment(\.scenePhase) private var scenePhase
    @State private var messages: MessagesAccessState = .blocked
    @State private var notes: NotesAutomationState = .notDetermined
    private let messagesProbe = MessagesAccessProbe()
    private let notesProbe = NotesAutomationProbe()

    var body: some View {
        WorkspacePage(title: "Access", subtitle: "Choose what JOAT can use on this Mac. You stay in control.") {
            if engine.isPreview { WorkspaceNotice(text: "Permission checks are paused in this design preview.") }
            VStack(spacing: 0) {
                AccessPermissionRow(title: "Calendar", symbol: "calendar", detail: "Read and create events when you ask.",
                                    value: engine.calendarAccess, granted: engine.calendarAccess == "full access") {
                    if ["denied", "restricted"].contains(engine.calendarAccess) {
                        Button("Open Privacy Settings…") { openPrivacy("Privacy_Calendars") }
                    } else {
                        Button("Grant Access…") { engine.requestCalendarAccess() }
                    }
                }
                Divider()
                AccessPermissionRow(title: "Reminders", symbol: "checklist", detail: "Read, create, and complete reminders when you ask.",
                                    value: engine.remindersAccess, granted: engine.remindersAccess == "full access") {
                    if ["denied", "restricted"].contains(engine.remindersAccess) {
                        Button("Open Privacy Settings…") { openPrivacy("Privacy_Reminders") }
                    } else {
                        Button("Grant Access…") { engine.requestRemindersAccess() }
                    }
                }
                Divider()
                AccessPermissionRow(title: "Messages", symbol: "message", detail: "Read message history through Full Disk Access.",
                                    value: messages.label, granted: messages == .granted) {
                    Button("Open Privacy Settings…") { openPrivacy("Privacy_AllFiles") }
                }
                Divider()
                AccessPermissionRow(title: "Apple Notes", symbol: "note.text", detail: "Export a QuickNote to your JOAT folder in Notes.",
                                    value: notes.label, granted: notes == .granted) {
                    if notes == .denied {
                        Button("Open Privacy Settings…") { openPrivacy("Privacy_Automation") }
                    } else {
                        Button("Grant Access…") {
                            Task { notes = await NotesAutomationProbe.requestAccess(probe: notesProbe) }
                        }
                    }
                }
                Divider()
                AccessPermissionRow(title: "Notification text", symbol: "bell", detail: "Read the content of visible notification banners.",
                                    value: engine.accessibilityTrusted ? "Granted" : "Accessibility required",
                                    granted: engine.accessibilityTrusted) {
                    Button("Grant Access…") { engine.requestAccessibility() }
                }
            }
            HStack {
                Button("Check Access", systemImage: "arrow.clockwise", action: refresh)
                Spacer()
                Button("Privacy & Security…") { openPrivacy(nil) }
            }
            GroupBox("Engine connection") {
                ServiceRow(title: "Personal data bridge", symbol: "link",
                           value: bridgeValue,
                           detail: "Permission on the Mac and a working engine connection are checked separately.",
                           healthy: readiness.isLive && readiness.status.personalData.state == "ready")
                    .padding(8)
            }
            WorkspaceNotice(text: "Access is requested only when you choose Grant Access. These checks read permission status, not your content. Revoke access in System Settings at any time.")
        }
        .disabled(engine.isPreview)
        .onAppear(perform: refresh)
        .onChange(of: scenePhase) { if scenePhase == .active { refresh() } }
    }

    private var bridgeValue: String {
        guard readiness.isLive else { return "Not reporting" }
        return ["checking": "Checking", "ready": "Ready", "authorization_required": "Full access required",
                "not_configured": "Start through JOAT.app", "unavailable": "Restart JOAT",
                "protocol_error": "Update JOAT"][readiness.status.personalData.state] ?? "Unavailable"
    }

    private func refresh() {
        guard !engine.isPreview else { return }
        engine.refreshPersonalDataAccess()
        messages = messagesProbe.state()
        notes = notesProbe.state()
    }

    private func openPrivacy(_ pane: String?) {
        let suffix = pane.map { "?\($0)" } ?? ""
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security\(suffix)") {
            NSWorkspace.shared.open(url)
        }
    }
}

private struct AccessPermissionRow<Actions: View>: View {
    let title: String
    let symbol: String
    let detail: String
    let value: String
    let granted: Bool
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: symbol).font(.title2).foregroundStyle(.secondary)
                .frame(width: 28).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !granted { Text(value.capitalized).font(.caption).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 10)
            if granted {
                Label("Granted", systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green)
            } else {
                actions().controlSize(.small)
            }
        }.padding(.vertical, 16)
    }
}
