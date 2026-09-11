import AppKit
import SwiftUI

/// Termination correctness per Apple's lifecycle contract: the engine must
/// die on EVERY quit path (menu Quit, Cmd-Q, osascript, logout), and the
/// app must WAIT for the child before letting macOS tear it down —
/// `.terminateLater` + reply, not fire-and-forget SIGTERM.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        guard EngineController.shouldRunAppServices(
            environment: ProcessInfo.processInfo.environment,
            previewBundle: EngineController.isDesignPreviewBundle
        ) else { return }
        // Reference established unconditionally at launch — never in a lazy
        // menu's onAppear, which may not run before logout (audit §C1).
        _ = EngineController.shared
        // Runloop is up by now, so AX queries can safely block a moment.
        NotificationWatcher.shared.start()
        // The app must always get a chance to stop its child (audit §A).
        ProcessInfo.processInfo.disableSuddenTermination()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let engine = EngineController.shared
        if engine.isPreview { return .terminateNow }
        guard engine.state == .running else {
            NativeBridgeServer.shared.stop()
            return .terminateNow
        }
        engine.stop {
            NativeBridgeServer.shared.stop()
            NSApplication.shared.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false  // closing the window must not stop the engine
    }
}

@main
struct JOATApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @ObservedObject private var engine = EngineController.shared

    var body: some Scene {
        // A real Mac app: Dock icon, Cmd-Tab, one canonical main window —
        // plus the menu-bar extra as the always-there status surface.
        Window("JOAT", id: "main") {
            MainWindowView(engine: engine)
        }
        .defaultSize(width: 1080, height: 780)
        .windowResizability(.contentMinSize)
        .windowToolbarStyle(.unified)
        .commands { MacNavigationCommands() }

        MenuBarExtra {
            MenuContent(engine: engine)
        } label: {
            Image(systemName: engine.state == .running ? "brain.fill" : "brain")
        }

        Settings {
            SettingsView(engine: engine)
        }
    }
}

struct MenuContent: View {
    @ObservedObject var engine: EngineController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Group {
            Text(engine.statusLine)
            Divider()
            Button("Open JOAT") {
                openWindow(id: "main")
                NSApp.activate()
            }
            if engine.isPreview {
                Text("Read-only design preview")
            } else if engine.state == .running {
                Button("Stop Engine") { engine.stop() }.disabled(engine.isTransitioning)
                Button("Restart Engine") { engine.restart() }.disabled(engine.isTransitioning)
            } else {
                Button("Start Engine") { engine.start() }.disabled(engine.isTransitioning)
            }
            Button("Open Log") { engine.openLog() }
            Divider()
            Button("Quit JOAT (stops the engine)") {
                NSApplication.shared.terminate(nil)
            }
        }
        .onAppear { engine.refreshLaunchAtLogin() }
    }
}

struct SettingsView: View {
    @ObservedObject var engine: EngineController

    var body: some View {
        Form {
            Section("General") {
                Toggle("Launch at Login", isOn: Binding(
                    get: { engine.launchAtLogin },
                    set: { engine.setLaunchAtLogin($0) }
                ))
                if let notice = engine.launchAtLoginNotice {
                    Text(notice).font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Home Mac") {
                Toggle("Private Tailnet home brain", isOn: Binding(
                    get: { engine.privateTailnetGateway },
                    set: { engine.setPrivateTailnetGateway($0) }
                ))
                Text("Use this for a home Mac behind private Tailscale Serve. The engine listens locally and still requires glasses pairing. Restart the engine after changing this setting.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Restart Engine") { engine.restart() }
                    .disabled(engine.state != .running || engine.isTransitioning)
            }
            Section("Agent Terminal · OpenClaw and Hermes") {
                A2AConnectionSettings(engine: engine)
            }
            Section("OpenClaw session Gateway") {
                HermesConnectionSettings(engine: engine)
            }
            Section("Advanced") {
                LabeledContent("Engine folder") {
                    Text(engine.repoURL()?.path ?? "Not selected")
                        .truncationMode(.middle).lineLimit(1)
                }
                Button("Change Engine Folder…") { engine.chooseRepo() }
                    .disabled(engine.isTransitioning)
            }
        }
        .formStyle(.grouped)
        .disabled(engine.isPreview)
        .frame(width: 560, height: 630)
        .onAppear { engine.refreshLaunchAtLogin() }
    }
}
