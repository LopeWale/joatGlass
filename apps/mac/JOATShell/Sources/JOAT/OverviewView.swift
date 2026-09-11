import SwiftUI

struct OverviewView: View {
    @ObservedObject var engine: EngineController
    let readiness: MacReadiness
    let navigate: (MacDestination) -> Void

    private var status: EngineStatus { readiness.status }

    var body: some View {
        WorkspacePage(title: "Overview", subtitle: "Your assistant, your glasses, and the latest exchange.") {
            HStack(alignment: .center, spacing: 24) {
                Image(systemName: "eyeglasses")
                    .font(.system(size: 46, weight: .light))
                    .foregroundStyle(readiness.relayConnected ? Color.accentColor : Color.secondary)
                    .frame(width: 92, height: 78)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 10) {
                    Text(readiness.title).font(.title2.weight(.semibold))
                    Text(readiness.detail).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if engine.state != .running && !engine.isPreview {
                        Button("Start Engine", systemImage: "play.fill") { engine.start() }
                            .primaryGlassAction().disabled(engine.isTransitioning)
                    } else {
                        Button("View Connection", systemImage: "arrow.up.right") { navigate(.glasses) }
                            .primaryGlassAction()
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 2)

            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Latest exchange").font(.headline)
                    Spacer()
                    Text(readiness.isLive ? "From the engine" : "Last known · offline")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if status.lastTranscript.isEmpty && status.lastReply.isEmpty {
                    HStack(alignment: .center, spacing: 14) {
                        Image(systemName: "quote.bubble").font(.title).foregroundStyle(.tertiary)
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Your conversation starts on the glasses").font(.headline)
                            Text("Your latest question and JOAT’s reply will appear here.")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 18)
                } else {
                    VStack(alignment: .leading, spacing: 18) {
                        if !status.lastTranscript.isEmpty {
                            exchange("You", text: status.lastTranscript, prominent: false)
                        }
                        if !status.lastReply.isEmpty {
                            exchange("JOAT", text: status.lastReply, prominent: true)
                        }
                    }
                    Text("This is the engine’s latest exchange, not a live image of the glasses.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(20)
            .background(.background, in: .rect(cornerRadius: 16))
            .overlay { RoundedRectangle(cornerRadius: 16).strokeBorder(.separator.opacity(0.45)) }

            VStack(spacing: 0) {
                ServiceRow(title: "Speech", symbol: "waveform", value: readiness.value(status.stt),
                           detail: "Transcription on this Mac", healthy: readiness.isLive && status.stt == "ready")
                Divider()
                ServiceRow(title: "Model", symbol: "sparkle", value: readiness.value(status.llm),
                           detail: "JOAT’s answer service", healthy: readiness.isLive && status.llm == "ready")
                Divider()
                Button { navigate(.access) } label: {
                    ServiceRow(title: "Notifications", symbol: "bell", value: readiness.value(status.notifications),
                               detail: "View access and delivery status", healthy: readiness.isLive && status.notifications == "live")
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }

            if !status.lastCard.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Latest notification", systemImage: "bell.badge").font(.headline)
                    Text(status.lastCard).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
        }
    }

    private func exchange(_ author: String, text: String, prominent: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(author).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(text).font(prominent ? .title3 : .body)
                .foregroundStyle(prominent ? Color.primary : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }
}
