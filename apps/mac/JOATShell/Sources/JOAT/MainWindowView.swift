import SwiftUI

/// The app's canonical window: what the assistant IS DOING right now.
/// (The raw log lives one button away — it is a debugging aid, not the UI.)
struct MainWindowView: View {
    @ObservedObject var engine: EngineController
    @State private var status = EngineStatus()
    @State private var live = false
    @State private var showLog = false
    @State private var logTail = ""

    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header

            HStack(spacing: 10) {
                Pill("Glasses", value: live ? status.glasses : "—",
                     ok: live && status.glasses == "connected")
                Pill("Mic", value: live ? status.mic : "—",
                     ok: live && status.mic == "streaming")
                Pill("Notifications", value: live ? status.notifications : "—",
                     ok: live && status.notifications == "live")
            }
            HStack(spacing: 10) {
                Pill("Speech", value: live ? status.stt : "—", ok: live && status.stt == "ready")
                Pill("Model", value: live ? status.llm : "—", ok: live && status.llm == "ready")
                Pill("Debate", value: live ? status.debate : "—", ok: live && status.debate == "ready")
            }

            GroupBox("Last exchange") {
                VStack(alignment: .leading, spacing: 6) {
                    Row(icon: "quote.bubble", label: "You said",
                        text: status.lastTranscript.isEmpty ? "nothing yet — say “JOAT …”" : status.lastTranscript)
                    Row(icon: "eyeglasses", label: "On the HUD",
                        text: status.lastReply.isEmpty ? "—" : status.lastReply)
                    if !status.lastCard.isEmpty {
                        Row(icon: "bell.badge", label: "Last card", text: status.lastCard)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }

            if !engine.accessibilityTrusted {
                GroupBox {
                    HStack {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Notification text needs Accessibility").font(.callout.weight(.medium))
                            Text("Without it, cards can show which app notified you but not what it said.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Grant…") { engine.requestAccessibility() }
                    }
                    .padding(4)
                }
            }

            Spacer(minLength: 0)

            HStack {
                Button(showLog ? "Hide Log" : "Show Log") { showLog.toggle() }
                Button("Open Full Log") { engine.openLog() }
                Spacer()
                Text("\(status.counts.utterances) asked · \(status.counts.cards) cards")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if showLog {
                ScrollView {
                    Text(logTail).font(.system(size: 10, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                }
                .frame(height: 150)
            }
        }
        .padding(16)
        .frame(minWidth: 460, minHeight: 420)
        .onAppear(perform: refresh)
        .onReceive(tick) { _ in refresh() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Circle().fill(dotColor).frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 1) {
                Text(engine.statusLine).font(.title3.weight(.semibold))
                if live && !status.device.isEmpty {
                    Text(status.device).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if engine.state == .running {
                Button("Stop") { engine.stop() }
                Button("Restart") { engine.restart() }
            } else {
                Button("Start Engine") { engine.start() }.keyboardShortcut(.defaultAction)
            }
        }
    }

    private var dotColor: Color {
        switch engine.state {
        case .running: live ? .green : .yellow   // running but not yet reporting
        case .stopped: .gray
        case .crashed: .red
        case .failed: .orange
        }
    }

    private func refresh() {
        if let s = EngineStatus.load() {
            status = s
            live = s.isFresh && engine.state == .running
        } else {
            live = false
        }
        if showLog { logTail = engine.logTail(lines: 30) }
    }
}

private struct Pill: View {
    let label: String, value: String, ok: Bool
    init(_ label: String, value: String, ok: Bool) {
        self.label = label; self.value = value; self.ok = ok
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.medium))
                .foregroundStyle(ok ? Color.green : .secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct Row: View {
    let icon: String, label: String, text: String
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundStyle(.secondary).frame(width: 16)
            Text(label).font(.caption).foregroundStyle(.secondary).frame(width: 72, alignment: .leading)
            Text(text).font(.callout).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
