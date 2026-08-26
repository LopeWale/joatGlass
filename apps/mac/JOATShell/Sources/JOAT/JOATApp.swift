import SwiftUI

/// Ensures the engine dies on EVERY termination path — menu Quit, Cmd-Q,
/// osascript 'quit app', logout — not just our own Quit button.
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var engine: EngineController?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppDelegate.engine?.stop()
        return .terminateNow
    }
}

@main
struct JOATApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var engine = EngineController()

    init() {}

    var body: some Scene {
        MenuBarExtra {
            MenuContent(engine: engine)
                .onAppear { AppDelegate.engine = engine }
        } label: {
            Image(systemName: engine.state == .running ? "brain.fill" : "brain")
        }
    }
}

struct MenuContent: View {
    @ObservedObject var engine: EngineController

    var body: some View {
        Group {
            Text(engine.statusLine)
            Divider()
            if engine.state == .running {
                Button("Stop Listening") { engine.stop() }
                Button("Restart Engine") { engine.restart() }
            } else {
                Button("Start Engine") { engine.start() }
            }
            Button("Open Log") { engine.openLog() }
            Divider()
            Toggle("Launch at Login", isOn: Binding(
                get: { engine.launchAtLogin },
                set: { engine.setLaunchAtLogin($0) }
            ))
            Button("Set Repo Folder…") { engine.chooseRepo() }
            Divider()
            Button("Quit JOAT (stops the engine)") {
                engine.stop()
                NSApplication.shared.terminate(nil)
            }
        }
    }
}
