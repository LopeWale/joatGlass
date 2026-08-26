import SwiftUI

/// The app's canonical window: engine state, controls, live log tail.
struct MainWindowView: View {
    @ObservedObject var engine: EngineController
    @State private var logTail: String = ""

    private let refresh = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 12, height: 12)
                Text(engine.statusLine)
                    .font(.title3.weight(.semibold))
                Spacer()
                if engine.state == .running {
                    Button("Stop") { engine.stop() }
                    Button("Restart") { engine.restart() }
                } else {
                    Button("Start Engine") { engine.start() }
                        .keyboardShortcut(.defaultAction)
                }
            }

            GroupBox("Engine log") {
                ScrollViewReader { proxy in
                    ScrollView {
                        Text(logTail.isEmpty ? "waiting for log output…" : logTail)
                            .font(.system(size: 11, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .id("tail")
                    }
                    .onChange(of: logTail) { proxy.scrollTo("tail", anchor: .bottom) }
                }
            }
            .frame(maxHeight: .infinity)

            HStack {
                Button("Open Full Log") { engine.openLog() }
                Spacer()
                Text("Quit stops the engine — a closed app is not listening.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .onAppear(perform: readTail)
        .onReceive(refresh) { _ in readTail() }
    }

    private var statusColor: Color {
        switch engine.state {
        case .running: .green
        case .stopped: .gray
        case .crashed: .red
        }
    }

    private func readTail() {
        logTail = engine.logTail(lines: 40)
    }
}