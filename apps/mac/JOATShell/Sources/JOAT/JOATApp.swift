import AppKit
import SwiftUI

/// Termination correctness per Apple's lifecycle contract: the engine must
/// die on EVERY quit path (menu Quit, Cmd-Q, osascript, logout), and the
/// app must WAIT for the child before letting macOS tear it down —
/// `.terminateLater` + reply, not fire-and-forget SIGTERM.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Reference established unconditionally at launch — never in a lazy
        // menu's onAppear, which may not run before logout (audit §C1).
        _ = EngineController.shared
        // The app must always get a chance to stop its child (audit §A).
        ProcessInfo.processInfo.disableSuddenTermination()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let engine = EngineController.shared
        guard engine.state == .running else { return .terminateNow }
        engine.stop {
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

    var body: some View {
        Form {
            Toggle("Launch at Login", isOn: Binding(
                get: { engine.launchAtLogin },
                set: { engine.setLaunchAtLogin($0) }
            ))
            LabeledContent("Engine repo") {
                Text(engine.repoURL()?.path ?? "not found")
                    .truncationMode(.middle)
                    .lineLimit(1)
            }
            Button("Change Repo Folder…") { engine.chooseRepo() }
        }
        .padding(20)
        .frame(width: 420)
        .onAppear { engine.refreshLaunchAtLogin() }
    }
}
