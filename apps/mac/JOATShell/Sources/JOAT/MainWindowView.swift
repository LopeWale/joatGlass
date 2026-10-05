import AppKit
import SwiftUI

/// Navigation never creates a second engine or glasses session.
struct MainWindowView: View {
    @ObservedObject var engine: EngineController
    @Environment(\.openSettings) private var openSettings
    @Environment(\.scenePhase) private var scenePhase
    @SceneStorage("macWorkspaceDestination") private var storedDestination = MacDestination.overview.rawValue
    @State private var status = EngineStatus()
    @State private var logTail = ""
    @State private var newsModel = NewsSettingsModel()
    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    private var destination: MacDestination { MacDestination(rawValue: storedDestination) ?? .overview }
    private var selection: Binding<MacDestination?> {
        Binding(get: { destination }, set: { storedDestination = ($0 ?? .overview).rawValue })
    }
    private var readiness: MacReadiness {
        MacReadiness(status: status, engineRunning: engine.state == .running || engine.isPreview)
    }

    var body: some View {
        NavigationSplitView {
            List(selection: selection) {
                Section("Workspace") {
                    ForEach([MacDestination.overview, .glasses, .intelligence, .news]) { item in
                        Label(item.title, systemImage: item.symbol).tag(item)
                            .padding(.vertical, 3)
                    }
                }
                Section("Manage") {
                    ForEach([MacDestination.access, .diagnostics]) { item in
                        Label(item.title, systemImage: item.symbol).tag(item)
                            .padding(.vertical, 3)
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 260)
            .safeAreaInset(edge: .bottom) {
                VStack(alignment: .leading, spacing: 5) {
                    Label(engine.isTransitioning ? "Updating engine…" : engineStateLabel,
                          systemImage: readiness.isLive ? "circle.fill" : "circle.dotted")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(readiness.isLive ? Color.green : Color.secondary)
                    Text("JOAT · Mac companion").font(.caption2).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
        } detail: {
            Group {
                switch destination {
                case .overview:
                    OverviewView(engine: engine, readiness: readiness) { storedDestination = $0.rawValue }
                case .glasses:
                    GlassesView(engine: engine, readiness: readiness)
                case .intelligence:
                    IntelligenceView(engine: engine, readiness: readiness)
                case .news:
                    NewsSettingsView(model: newsModel, commands: engine.commands).disabled(engine.isPreview)
                case .access:
                    AccessSettingsView(engine: engine, readiness: readiness)
                case .diagnostics:
                    diagnostics
                }
            }
            .navigationTitle(destination.title)
            .toolbar { workspaceToolbar }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if engine.isPreview || !readiness.isLive {
                    HStack(spacing: 8) {
                        Image(systemName: "info.circle")
                        Text(engine.isPreview ? "Design preview · Local engine status · Setup changes are disabled"
                             : engine.state == .running
                             ? "Waiting for a current engine update. Connection status is unavailable."
                             : "Engine offline. Any previous exchange shown is last known information.")
                        Spacer(minLength: 0)
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal, 24).padding(.vertical, 10)
                    .background(.bar)
                }
            }
        }
        .navigationSplitViewStyle(.balanced)
        .focusedSceneValue(\.workspaceDestination, $storedDestination)
        .frame(minWidth: 880, minHeight: 620)
        .onAppear(perform: refresh)
        .onReceive(tick) { _ in
            if scenePhase == .active { refresh() }
        }
        .onChange(of: scenePhase) { if scenePhase == .active { refresh() } }
        .onChange(of: destination) { refresh() }
    }

    @ToolbarContentBuilder
    private var workspaceToolbar: some ToolbarContent {
        ToolbarItemGroup {
            if engine.state == .running {
                Button("Stop Engine", systemImage: "stop.fill") { engine.stop() }
                    .help("Stop the engine and its glasses services")
                    .disabled(engine.isTransitioning || engine.isPreview)
                Button("Restart Engine", systemImage: "arrow.clockwise") { engine.restart() }
                    .help("Stop, then restart JOAT’s engine")
                    .disabled(engine.isTransitioning || engine.isPreview)
            } else {
                Button("Start Engine", systemImage: "play.fill") { engine.start() }
                    .disabled(engine.isTransitioning || engine.isPreview)
            }
        }
        if #available(macOS 26.0, *) { ToolbarSpacer(.fixed) }
        ToolbarItem {
            Button("Settings", systemImage: "gearshape") { openSettings() }
                .help("Launch at login and Mac connection settings")
        }
    }

    private var engineStateLabel: String {
        if engine.isPreview { return readiness.isLive ? "Live status preview" : "Offline status preview" }
        switch engine.state {
        case .running: return readiness.isLive ? "Engine online" : "Waiting for status"
        case .stopped: return "Engine stopped"
        case .crashed: return "Engine interrupted"
        case .failed: return "Engine needs attention"
        }
    }

    private var diagnostics: some View {
        WorkspacePage(title: "Diagnostics", subtitle: "Connection details and support tools when you need them.") {
            GroupBox("Engine") {
                VStack(spacing: 0) {
                    ServiceRow(title: "Process", symbol: "cpu", value: engineStateLabel,
                               healthy: engine.state == .running)
                    Divider()
                    LabeledContent("Last status update") {
                        if status.at > 0 {
                            Text(Date(timeIntervalSince1970: status.at), format: .dateTime.hour().minute().second())
                        } else { Text("No update received") }
                    }.padding(.vertical, 10)
                    LabeledContent("Questions this session", value: "\(status.counts.utterances)")
                        .padding(.vertical, 6)
                    LabeledContent("Cards this session", value: "\(status.counts.cards)")
                        .padding(.vertical, 6)
                    if case .failed(let reason) = engine.state {
                        WorkspaceNotice(text: reason, isError: true).padding(.top, 12)
                    }
                    if case .crashed(let code) = engine.state {
                        WorkspaceNotice(text: "The engine exited with code \(code). Restart it to retry.", isError: true)
                    }
                }.padding(8)
            }
            GroupBox("Glasses support capture") {
                VStack(alignment: .leading, spacing: 12) {
                    Toggle("Record G2 diagnostics", isOn: Binding(
                        get: { engine.g2DiagnosticsEnabled },
                        set: { engine.setG2DiagnosticsEnabled($0) }
                    )).disabled(engine.isTransitioning || engine.isPreview)
                    Text("Records page, gesture, rendering, and recovery metadata. Changing this setting restarts a running engine.")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("Export Redacted Trace…", systemImage: "square.and.arrow.up") { engine.exportG2Diagnostics() }
                        .disabled(engine.isPreview)
                    if let notice = engine.g2DiagnosticNotice {
                        WorkspaceNotice(text: notice,
                                        isError: notice.hasPrefix("G2 trace export failed") || notice.hasPrefix("Trace stayed off"))
                    }
                }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Recent log").font(.headline)
                    Spacer()
                    Button("Open Full Log", systemImage: "arrow.up.right.square") { engine.openLog() }
                }
                ScrollView([.horizontal, .vertical]) {
                    Text(logTail.isEmpty ? "No log entries available." : logTail)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                        .padding(12)
                }
                .frame(height: 230)
                .background(.background, in: .rect(cornerRadius: 10))
            }
        }
    }

    private func refresh() {
        if let snapshot = EngineStatus.load() { status = snapshot }
        else { status.at = 0 }
        if destination == .diagnostics { logTail = engine.logTail(lines: 60) }
    }
}
