import Foundation
import Network

/// Spike 0 check: the app dials its own loopback server with the same
/// Network framework the server uses, sends {"kind":"selftest"} and expects
/// {"kind":"selftest.ok"}. Network.framework is not subject to App Transport
/// Security, so this tests the listener alone (URLSession to ws://127.0.0.1
/// is refused by ATS on iOS 17+ unless the IP is excepted in Info.plist).
/// It never sends `auth`, so it does not touch the plugin's link state.
final class SelfTest: ObservableObject {
    @Published private(set) var result = "not run"
    private var conn: NWConnection?
    private let queue = DispatchQueue(label: "com.joatlabs.joatbrain.selftest")

    func run(port: UInt16) {
        conn?.cancel()
        result = "running"
        let params = NWParameters.tcp
        let ws = NWProtocolWebSocket.Options()
        ws.autoReplyPing = true
        params.defaultProtocolStack.applicationProtocols.insert(ws, at: 0)
        let conn = NWConnection(to: .url(URL(string: "ws://127.0.0.1:\(port)/glasses")!), using: params)
        self.conn = conn
        conn.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                Log.add("selftest connected; sending selftest")
                self?.send(#"{"kind":"selftest"}"#, on: conn)
                self?.receive(on: conn)
            case .waiting(let e):
                Log.add("selftest waiting: \(e)")
                self?.finish("waiting: \(e)", conn)
            case .failed(let e):
                Log.add("selftest failed: \(e)")
                self?.finish("failed: \(e)", conn)
            default:
                break
            }
        }
        conn.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.conn === conn, conn.state != .cancelled { self?.finish("timed out after 5 s", conn) }
        }
    }

    private func send(_ text: String, on conn: NWConnection) {
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "selftest", metadata: [meta])
        conn.send(content: text.data(using: .utf8), contentContext: ctx, isComplete: true,
                  completion: .contentProcessed { [weak self] e in
                      if let e { Log.add("selftest send error: \(e)"); self?.finish("send error: \(e)", conn) }
                  })
    }

    private func receive(on conn: NWConnection) {
        conn.receiveMessage { [weak self] data, _, _, error in
            if let error { self?.finish("receive error: \(error)", conn); return }
            let text = data.map { String(decoding: $0, as: UTF8.self) } ?? ""
            Log.add("selftest rx: \(text.prefix(120))")
            if text.contains("\"selftest.ok\"") { self?.finish("PASS (server answered)", conn) }
            else { self?.receive(on: conn) }
        }
    }

    private func finish(_ text: String, _ conn: NWConnection) {
        conn.cancel()
        if self.conn === conn { self.conn = nil }
        DispatchQueue.main.async { self.result = text }
    }
}
