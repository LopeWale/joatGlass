import SwiftUI

/// Displays only child-channel facts. Saving settings does not acknowledge work.
struct LocalCommandView: View {
    @ObservedObject var commands: EngineCommandChannel
    let action: EngineCommandChannel.Action
    let title: String
    let detail: String

    private var capability: EngineCommandChannel.Capability { commands.capability(for: action) }
    private var pending: Bool { commands.state(for: action)?.isPending == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.headline)
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if pending {
                    ProgressView().controlSize(.small)
                    Button("Cancel") { commands.cancel(action) }
                } else {
                    Button(title, systemImage: "arrow.clockwise") { commands.send(action) }
                        .disabled(!commands.isConnected || !capability.canRequest)
                }
            }
            Text(capabilitySummary).font(.caption).foregroundStyle(.secondary)
            if let request = commands.state(for: action) {
                WorkspaceNotice(text: request.notice,
                                isError: ![.pending, .succeeded, .cancelled, .superseded].contains(request.outcome))
            }
        }
        .accessibilityElement(children: .contain)
    }

    private var capabilitySummary: String {
        guard commands.isConnected else { return "Engine command channel disconnected" }
        var facts = [capability.configured ? "Configured" : "Not configured",
                     capability.available ? "Available" : "Unavailable"]
        if capability.active { facts.append("Active") }
        if capability.denied { facts.append("Access denied") }
        if capability.disconnected { facts.append("Service disconnected") }
        if capability.pending || pending { facts.append("Pending") }
        return facts.joined(separator: " · ")
    }
}
