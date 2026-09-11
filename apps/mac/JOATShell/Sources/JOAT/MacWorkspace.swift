import SwiftUI

private struct WorkspaceDestinationKey: FocusedValueKey {
    typealias Value = Binding<String>
}

extension FocusedValues {
    var workspaceDestination: Binding<String>? {
        get { self[WorkspaceDestinationKey.self] }
        set { self[WorkspaceDestinationKey.self] = newValue }
    }
}

struct MacNavigationCommands: Commands {
    @FocusedValue(\.workspaceDestination) private var destination

    var body: some Commands {
        CommandMenu("Go") {
            ForEach(Array(MacDestination.allCases.enumerated()), id: \.element) { index, page in
                Button(page.title) { destination?.wrappedValue = page.rawValue }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: .command)
                    .disabled(destination == nil)
            }
        }
    }
}

enum MacDestination: String, CaseIterable, Identifiable {
    case overview, glasses, intelligence, news, access, diagnostics

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .overview: "square.grid.2x2"
        case .glasses: "eyeglasses"
        case .intelligence: "brain"
        case .news: "newspaper"
        case .access: "hand.raised"
        case .diagnostics: "waveform.path.ecg"
        }
    }
}

/// Presentation of the existing engine snapshot. A fresh file alone cannot
/// prove that this shell owns a running engine, or that the phone reaches G2.
struct MacReadiness {
    let status: EngineStatus
    let engineRunning: Bool

    var isLive: Bool { engineRunning && status.isFresh }
    var relayConnected: Bool { isLive && status.glasses == "connected" }
    var ready: Bool { relayConnected && status.stt == "ready" && status.llm == "ready" }

    var title: String {
        if !engineRunning { return "Start JOAT to get connected" }
        if !isLive { return "Waiting for the engine" }
        if !relayConnected { return "Connect your glasses" }
        if !ready { return "Getting your assistant ready" }
        return "Your assistant is ready"
    }

    var detail: String {
        if !engineRunning { return "Start the engine to bring your assistant and glasses services online." }
        if !isLive { return "The engine is running, but a current status update has not arrived." }
        if !relayConnected { return "Open JOAT in the Even app on your phone to connect to this Mac." }
        if !ready { return "The phone relay is connected. Speech or the language model is still getting ready." }
        return status.mic == "streaming"
            ? "The microphone is streaming through the phone relay."
            : "The phone relay is connected. Use the glasses to begin speaking."
    }

    func value(_ value: String) -> String {
        isLive ? value.replacingOccurrences(of: "_", with: " ").capitalized : "Not reporting"
    }
}

struct WorkspacePage<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(.largeTitle.weight(.semibold))
                    Text(subtitle).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                content()
            }
            .frame(maxWidth: 900, alignment: .leading)
            .padding(28)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }
}

struct ServiceRow: View {
    let title: String
    let symbol: String
    let value: String
    var detail: String? = nil
    var healthy = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol).font(.title3).foregroundStyle(.secondary)
                .frame(width: 24, height: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                if let detail {
                    Text(detail).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            Label(value, systemImage: healthy ? "checkmark.circle.fill" : "circle.dotted")
                .font(.callout)
                .foregroundStyle(healthy ? Color.green : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
    }
}

struct WorkspaceNotice: View {
    let text: String
    var isError = false

    var body: some View {
        Label(text, systemImage: isError ? "exclamationmark.circle" : "info.circle")
            .font(.callout)
            .foregroundStyle(isError ? Color.orange : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}

struct PrimaryGlassAction: ViewModifier {
    func body(content: Content) -> some View {
        if #available(macOS 26.0, *) {
            content.buttonStyle(.glassProminent).controlSize(.large)
        } else {
            content.buttonStyle(.borderedProminent).controlSize(.large)
        }
    }
}

extension View {
    func primaryGlassAction() -> some View { modifier(PrimaryGlassAction()) }
}
