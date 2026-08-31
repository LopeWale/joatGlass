import AppKit
import SwiftUI

/// Termination correctness per Apple's lifecycle contract: the engine must
/// die on EVERY quit path (menu Quit, Cmd-Q, osascript, logout), and the
/// app must WAIT for the child before letting macOS tear it down —
/// `.terminateLater` + reply, not fire-and-forget SIGTERM.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        guard EngineController.shouldRunAppServices(
            environment: ProcessInfo.processInfo.environment
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
        .defaultSize(width: 560, height: 480)

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
            if engine.state == .running {
                Button("Stop Listening") { engine.stop() }
                Button("Restart Engine") { engine.restart() }
            } else {
                Button("Start Engine") { engine.start() }
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
    @State private var hermesGatewayURL = ""
    @State private var hermesConfigurationMessage: String?

    var body: some View {
        Form {
            Toggle("Launch at Login", isOn: Binding(
                get: { engine.launchAtLogin },
                set: { engine.setLaunchAtLogin($0) }
            ))
            Toggle("Private Tailnet home brain", isOn: Binding(
                get: { engine.privateTailnetGateway },
                set: { engine.setPrivateTailnetGateway($0) }
            ))
            Text(
                "For an unattended home Mac behind private Tailscale Serve. "
                + "JOAT listens only on loopback and still requires G2 pairing. "
                + "Restart JOAT after changing this setting."
            )
            .font(.footnote)
            .foregroundStyle(.secondary)

            Section("Hermes on your Mac mini") {
                TextField("Gateway URL", text: $hermesGatewayURL)
                    .textFieldStyle(.roundedBorder)
                    .fontDesign(.monospaced)
                    .accessibilityLabel("Hermes Gateway URL")
                LabeledContent("Agent Terminal agent", value: "Hermes (fixed)")
                Text(
                    "Use the authenticated Tailscale Gateway origin, for example "
                    + "wss://your-mac-mini.tailnet.ts.net. Port 9443 is the Hermes "
                    + "control dashboard, not this Gateway."
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
                HStack {
                    Button("Save Hermes Connection") {
                        saveHermesConnection()
                    }
                    .disabled(!hermesGatewayCanSave)
                    Button("Reload") {
                        loadHermesConnection()
                    }
                }
                if let hermesConfigurationMessage {
                    Text(hermesConfigurationMessage)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Text(
                    "Saving stores no token. Restart the engine to connect; a new "
                    + "Gateway endpoint uses its own signed device identity and may "
                    + "need a visible pairing approval."
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            }
            LabeledContent("Engine repo") {
                Text(engine.repoURL()?.path ?? "not found")
                    .truncationMode(.middle)
                    .lineLimit(1)
            }
            Button("Change Repo Folder…") { engine.chooseRepo() }
        }
        .padding(20)
        .frame(width: 520)
        .onAppear {
            engine.refreshLaunchAtLogin()
            loadHermesConnection()
        }
    }

    private var hermesGatewayCanSave: Bool {
        (try? OpenClawRuntimeConfiguration(gatewayURL: hermesGatewayURL)) != nil
    }

    private func loadHermesConnection() {
        do {
            guard let configuration = try OpenClawRuntimeConfiguration.load() else {
                hermesGatewayURL = ""
                hermesConfigurationMessage = "No Hermes Gateway is configured yet."
                return
            }
            hermesGatewayURL = configuration.gatewayURL
            hermesConfigurationMessage = "Saved Gateway loaded."
        } catch {
            hermesConfigurationMessage = error.localizedDescription
        }
    }

    private func saveHermesConnection() {
        do {
            let configuration = try OpenClawRuntimeConfiguration(gatewayURL: hermesGatewayURL)
            try configuration.save()
            hermesGatewayURL = configuration.gatewayURL
            hermesConfigurationMessage = "Gateway saved. Restart Engine to connect."
        } catch {
            hermesConfigurationMessage = error.localizedDescription
        }
    }
}
