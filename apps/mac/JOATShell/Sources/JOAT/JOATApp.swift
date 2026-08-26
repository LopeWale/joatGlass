import SwiftUI

@main
struct JOATApp: App {
    @StateObject private var engine = EngineController()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(engine: engine)
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
