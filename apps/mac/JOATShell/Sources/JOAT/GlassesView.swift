import SwiftUI

struct GlassesView: View {
    @ObservedObject var engine: EngineController
    let readiness: MacReadiness

    var body: some View {
        WorkspacePage(title: "Glasses", subtitle: "The connection from this Mac, through your phone, to Even G2.") {
            GroupBox("Connection") {
                VStack(spacing: 0) {
                    ServiceRow(title: "This Mac", symbol: "desktopcomputer",
                               value: readiness.isLive ? "Online" : "Not reporting",
                               detail: "Runs JOAT and your connected services", healthy: readiness.isLive)
                    Divider()
                    ServiceRow(title: "Even app relay", symbol: "iphone",
                               value: readiness.isLive ? (readiness.relayConnected ? "Connected" : "Disconnected") : "Unknown",
                               detail: "Open the JOAT plugin in the Even app on your phone.", healthy: readiness.relayConnected)
                    Divider()
                    ServiceRow(title: "Microphone", symbol: "mic",
                               value: readiness.value(readiness.status.mic),
                               detail: "Controlled from the glasses and Even app", healthy: readiness.isLive && readiness.status.mic == "streaming")
                }.padding(8)
            }
            if readiness.isLive && !readiness.status.device.isEmpty {
                LabeledContent("Reported device", value: readiness.status.device)
                    .textSelection(.enabled)
            }
            WorkspaceNotice(text: "The Mac can confirm its phone relay connection. Check the Even app for the glasses’ Bluetooth and battery status.")
            GroupBox("Pair or replace glasses") {
                VStack(alignment: .leading, spacing: 14) {
                    Text("If the phone keeps reconnecting, or you’re using a new phone or pair of glasses, open a pairing window here and connect JOAT in the Even app.")
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Open Pairing Window", systemImage: "link.badge.plus") { engine.openPairingWindow() }
                        .primaryGlassAction()
                        .disabled(engine.state != .running || engine.isTransitioning)
                    Text("Allows the intended device to pair for 120 seconds.")
                        .font(.caption).foregroundStyle(.secondary)
                    if let notice = engine.pairingNotice {
                        WorkspaceNotice(text: notice, isError: notice.hasPrefix("Pairing stayed closed"))
                    }
                    if engine.state != .running {
                        WorkspaceNotice(text: "Start the engine before opening a pairing window.")
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
            }
        }
    }
}
