import Foundation
import Network

/// Spike 1: the plugin's brain, on the phone. A WebSocket server bound to
/// 127.0.0.1 only (never the LAN), speaking just enough of the JOAT wire
/// protocol for MacLink to report "connected":
///
///   plugin → {kind:"auth", token}        → {kind:"auth.accepted"}
///   brain  → {kind:"link.ping", seq}     ← {kind:"link.pong", seq}   every 5 s
///   brain  → {kind:"assistant.overlay"}  one receipt on the glasses after auth
///
/// Every other uplink frame is counted and logged by kind. The handshake's
/// Origin header is logged too — that is the plugin page's own origin, which
/// decides whether a loopback ws:// is mixed content for WebKit.
final class LoopbackServer: ObservableObject, PluginLink {
    @Published private(set) var state = "stopped"
    @Published private(set) var clients = 0
    @Published private(set) var framesIn = 0
    @Published private(set) var bytesIn = 0
    @Published private(set) var lastOrigin = "-"
    @Published private(set) var authenticated = false
    @Published private(set) var lastPongMs = -1
    @Published private(set) var pongs = 0
    /// Spike 2: when the last pong arrived, the longest silence between two
    /// pongs on one connection, and how often the plugin (re)connected.
    @Published private(set) var lastPongAt: Date?
    @Published private(set) var maxPongGapMs = 0
    @Published private(set) var pluginSessions = 0

    /// Glasses mic frames (16 kHz mono Int16), delivered on the server queue.
    var onAudio: (([Int16]) -> Void)?
    /// Every other uplink (kind, decoded JSON object, raw text), on the server queue.
    var onUplink: ((String, [String: Any], String) -> Void)?
    /// A plugin authenticated and got the connect greeting.
    var onPluginConnected: (() -> Void)?
    @Published private(set) var audioFrames = 0

    let port: UInt16
    private var authed: Set<ObjectIdentifier> = []
    private var listener: NWListener?
    private var connections: [ObjectIdentifier: NWConnection] = [:]
    private var readyIds: Set<ObjectIdentifier> = []
    private var pingTimers: [ObjectIdentifier: DispatchSourceTimer] = [:]
    private var pingSentAt: [Int: Date] = [:]
    private var seq = 0
    private let queue = DispatchQueue(label: "com.joatlabs.joatbrain.loopback")
    private let meter = AudioMeter()

    /// Listener lifecycle, touched on the main queue only.
    private var wanted = false
    private var listening = false
    private var startedAt = Date.distantPast
    private var restartPending = false
    private var failures = 0
    private var watchdog: Timer?

    init(port: UInt16 = 8787) { self.port = port }

    /// iOS takes a listening socket back while the app is suspended (TN2277)
    /// and a listener can fail on its own; either way the plugin would dial
    /// a dead port forever (2026-10-29: "has not been able to connect back
    /// since"). Called when the app comes forward and every 10 s: a listener
    /// that is not listening is built again. Coming forward with no plugin
    /// connected rebuilds it anyway, because a reclaimed socket can still
    /// look ready. Open plugin connections are separate sockets and stay.
    func ensureListening(reason: String, rebuildIfIdle: Bool = false) {
        guard wanted, !restartPending else { return }
        if listener == nil { start(); return }
        if rebuildIfIdle && !authenticated && clients == 0 {
            restartListener(after: 0, reason: reason)
        } else if !listening && Date().timeIntervalSince(startedAt) > 5 {
            restartListener(after: 0, reason: "\(reason): not listening")
        }
    }

    private func restartListener(after seconds: Double, reason: String) {
        guard wanted, !restartPending else { return }
        restartPending = true
        Log.add("listener rebuild (\(reason))")
        if let old = listener {
            old.stateUpdateHandler = nil
            old.newConnectionHandler = nil
            old.cancel()
        }
        listener = nil
        listening = false
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self else { return }
            self.restartPending = false
            if self.wanted { self.start() }
        }
    }

    func start() {
        wanted = true
        if watchdog == nil {
            watchdog = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
                self?.ensureListening(reason: "watchdog")
            }
        }
        guard listener == nil else { return }
        startedAt = Date()
        do {
            let params = NWParameters.tcp
            params.allowLocalEndpointReuse = true
            // Bind to loopback only: nothing on Wi-Fi can reach this socket.
            params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
            let ws = NWProtocolWebSocket.Options()
            ws.autoReplyPing = true
            ws.setClientRequestHandler(queue) { [weak self] _, headers in
                var origin = "(no Origin header)"
                var lines: [String] = []
                for header in headers {
                    lines.append("\(header.name): \(header.value)")
                    if header.name.lowercased() == "origin" { origin = header.value }
                }
                Log.add("handshake headers: \(lines.joined(separator: " | "))")
                Log.add("plugin origin: \(origin)")
                DispatchQueue.main.async { self?.lastOrigin = origin }
                return NWProtocolWebSocket.Response(status: .accept, subprotocol: nil)
            }
            params.defaultProtocolStack.applicationProtocols.insert(ws, at: 0)

            let listener = try NWListener(using: params)
            listener.stateUpdateHandler = { [weak self, weak listener] st in
                let text: String
                var ready = false
                var failed = false
                switch st {
                case .ready: text = "listening ws://127.0.0.1:\(self?.port ?? 0)"; ready = true
                case .failed(let e): text = "failed: \(e)"; failed = true
                case .waiting(let e): text = "waiting: \(e)"
                case .cancelled: text = "stopped"
                default: text = "\(st)"
                }
                Log.add("listener \(text)")
                DispatchQueue.main.async {
                    guard let self, let listener, self.listener === listener else { return }
                    self.state = text
                    self.listening = ready
                    if ready { self.failures = 0 }
                    if failed {
                        // 1, 2, 4 … 30 s: a port another process holds must
                        // not turn into a rebuild every second.
                        self.failures += 1
                        let wait = min(30, pow(2, Double(self.failures - 1)))
                        self.restartListener(after: wait, reason: "listener failed")
                    }
                }
            }
            listener.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
            listener.start(queue: queue)
            self.listener = listener
        } catch {
            Log.add("listener could not start: \(error)")
            state = "error: \(error)"
        }
    }

    func stop() {
        wanted = false
        watchdog?.invalidate()
        watchdog = nil
        listening = false
        listener?.cancel()
        listener = nil
        connections.values.forEach { $0.cancel() }
        connections.removeAll()
        readyIds.removeAll()
        pingTimers.values.forEach { $0.cancel() }
        pingTimers.removeAll()
        DispatchQueue.main.async { self.clients = 0; self.state = "stopped"; self.authenticated = false }
    }

    private func accept(_ conn: NWConnection) {
        let id = ObjectIdentifier(conn)
        connections[id] = conn
        Log.add("client connecting from \(conn.endpoint)")
        conn.stateUpdateHandler = { [weak self] st in
            switch st {
            case .ready:
                Log.add("client ready; waiting for auth")
                self?.readyIds.insert(id)
                DispatchQueue.main.async { self?.clients += 1 }
                self?.receive(conn)
            case .failed(let e):
                Log.add("client failed: \(e)")
                self?.drop(conn)
                conn.cancel()
            case .cancelled:
                Log.add("client closed")
                self?.drop(conn)
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    private func drop(_ conn: NWConnection) {
        let id = ObjectIdentifier(conn)
        pingTimers.removeValue(forKey: id)?.cancel()
        authed.remove(id)
        // Only a connection that reached ready was counted; one that died in
        // the handshake must not take a live plugin's count (and its
        // Authenticated) down with it.
        let counted = readyIds.remove(id) != nil
        if connections.removeValue(forKey: id) != nil, counted {
            DispatchQueue.main.async {
                self.clients = max(0, self.clients - 1)
                if self.clients == 0 { self.authenticated = false }
            }
        }
    }

    private func receive(_ conn: NWConnection) {
        conn.receiveMessage { [weak self] data, context, isComplete, error in
            guard let self else { return }
            if let error {
                Log.add("receive error: \(error)")
                conn.cancel()
                return
            }
            // Nothing more will come (the page went away without a close
            // frame): re-arming here would spin on the queue (review, 2026-10-30).
            if data == nil, context == nil, isComplete {
                Log.add("client finished without a close frame")
                conn.cancel()
                return
            }
            if let data, let context,
               let meta = context.protocolMetadata(definition: NWProtocolWebSocket.definition) as? NWProtocolWebSocket.Metadata {
                DispatchQueue.main.async { self.framesIn += 1; self.bytesIn += data.count }
                switch meta.opcode {
                case .text:
                    self.handle(text: String(decoding: data, as: UTF8.self), bytes: data.count, on: conn)
                case .binary:
                    Log.add("rx binary \(data.count)B")
                case .close:
                    Log.add("rx close")
                    conn.cancel()
                    return
                default:
                    break
                }
            }
            self.receive(conn)
        }
    }

    private func handle(text: String, bytes: Int, on conn: NWConnection) {
        let kind = Self.kind(of: text)
        switch kind {
        case "auth":
            Log.add("rx auth (\(bytes)B) → auth.accepted")
            send(conn, text: #"{"kind":"auth.accepted"}"#)
            authed.insert(ObjectIdentifier(conn))
            DispatchQueue.main.async {
                self.authenticated = true
                self.pluginSessions += 1
                self.lastPongAt = nil   // a new connection starts its own gap clock
            }
            startPings(conn)
            // What the Mac's VoiceOrchestrator.on_connect sends: clear the
            // regions a previous brain may have left text in, then the status
            // word. Without it the plugin stays on its first-light page (the
            // image overview) until something else repaints Home.
            for container in [2, 5, 6] {   // Main, Context, Tray (types.ts ContainerId)
                send(conn, text: #"{"kind":"hud.text","containerId":\#(container),"text":""}"#)
            }
            send(conn, text: #"{"kind":"hud.text","containerId":1,"text":"JOAT · online — say 'JOAT ...'"}"#)
            Log.add("tx connect greeting (cleared Main/Context/Tray, status online)")
            // The Mac answers a connected plugin with AudioControl(open=True):
            // ask for the glasses mic the same way.
            send(conn, text: #"{"kind":"audio.control","open":true,"source":"glasses"}"#)
            Log.add("tx audio.control open (glasses mic)")
            onPluginConnected?()
        case "link.pong":
            let n = Self.int(field: "seq", in: text) ?? -1
            var rtt = -1
            if let sent = pingSentAt.removeValue(forKey: n) {
                rtt = Int(Date().timeIntervalSince(sent) * 1000)
            }
            Log.add("rx link.pong seq=\(n) rtt=\(rtt)ms")
            DispatchQueue.main.async {
                let now = Date()
                if let last = self.lastPongAt {
                    self.maxPongGapMs = max(self.maxPongGapMs, Int(now.timeIntervalSince(last) * 1000))
                }
                self.lastPongAt = now
                self.pongs += 1
                self.lastPongMs = rtt
            }
        case "selftest":
            Log.add("rx selftest → selftest.ok")
            send(conn, text: #"{"kind":"selftest.ok"}"#)
        case "audio":
            guard let frame = try? JSONDecoder().decode(AudioFrame.self, from: Data(text.utf8)) else {
                Log.add("audio frame did not decode (\(bytes)B)")
                return
            }
            DispatchQueue.main.async { self.audioFrames += 1 }
            if frame.seq % 250 == 0 { Log.add("rx audio seq=\(frame.seq) source=\(frame.source) samples=\(frame.pcm.count)") }
            meter.add(source: frame.source, speaker: frame.speaker, pcm: frame.pcm)
            onAudio?(frame.pcm)
        default:
            Log.add("rx \(kind) (\(bytes)B): \(text.prefix(160))")
            if let obj = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] {
                onUplink?(kind, obj, text)
            }
        }
    }

    /// PluginLink: the features' way back to the glasses.
    func send(_ object: [String: Any]) { broadcast(object) }

    /// Send one downlink JSON object to every authenticated plugin.
    func broadcast(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        queue.async {
            for (id, conn) in self.connections where self.authed.contains(id) {
                self.send(conn, text: text)
            }
        }
    }

    private struct AudioFrame: Decodable {
        let kind: String
        let source: String
        let seq: Int
        let pcm: [Int16]
        /// Even's own wearer/other call for this audio; the glasses mic only.
        let speaker: String?
    }

    private func startPings(_ conn: NWConnection) {
        let id = ObjectIdentifier(conn)
        pingTimers[id]?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 2, repeating: 5)
        timer.setEventHandler { [weak self, weak conn] in
            guard let self, let conn else { return }
            self.seq += 1
            self.pingSentAt[self.seq] = Date()
            self.send(conn, text: #"{"kind":"link.ping","seq":\#(self.seq)}"#)
        }
        timer.resume()
        pingTimers[id] = timer
    }

    private func send(_ conn: NWConnection, text: String) {
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "text", metadata: [meta])
        conn.send(content: text.data(using: .utf8), contentContext: ctx, isComplete: true,
                  completion: .contentProcessed { if let e = $0 { Log.add("send error: \(e)") } })
    }

    // Tiny JSON peeks: enough for the spike without a decoder per message kind.
    private static func kind(of text: String) -> String {
        guard let range = text.range(of: #""kind":""#) else { return "?" }
        let rest = text[range.upperBound...]
        return String(rest.prefix(while: { $0 != "\"" }))
    }

    private static func int(field: String, in text: String) -> Int? {
        guard let range = text.range(of: "\"\(field)\":") else { return nil }
        let rest = text[range.upperBound...].drop(while: { $0 == " " })
        return Int(rest.prefix(while: { $0.isNumber || $0 == "-" }))
    }
}

/// Which mic is JOAT hearing, and how well? Every 2 s of incoming plugin
/// audio becomes one log line: the source tag, Even's speaker tags (the
/// wearer/other call exists only for the glasses mic, so "wearer" is the
/// tell), how much audio arrived against real time, and its loudness.
/// Quiet windows are logged every 30 s only.
final class AudioMeter {
    private var started = Date()
    private var lastQuietLog = Date.distantPast
    private var samples = 0
    private var squares: Double = 0
    private var peak: Int32 = 0
    private var frames = 0
    private var sources: [String: Int] = [:]
    private var speakers: [String: Int] = [:]

    func add(source: String, speaker: String?, pcm: [Int16]) {
        frames += 1
        sources[source, default: 0] += 1
        speakers[speaker ?? "none", default: 0] += 1
        samples += pcm.count
        for v in pcm {
            let x = Int32(v)
            squares += Double(x * x)
            peak = max(peak, abs(x))
        }
        let now = Date()
        let span = now.timeIntervalSince(started)
        guard span >= 2 else { return }
        let rms = samples > 0 ? (squares / Double(samples)).squareRoot() : 0
        let level = Self.dbfs(rms), top = Self.dbfs(Double(peak))
        // -45 dBFS peak: about where speech starts on a head-worn mic.
        if top > -45 || now.timeIntervalSince(lastQuietLog) >= 30 {
            if top <= -45 { lastQuietLog = now }
            let tags = { (d: [String: Int]) in d.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: ",") }
            Log.add(String(format: "mic %.1fs: source[%@] speaker[%@] audio=%.2fx realtime level=%.0f dBFS peak=%.0f dBFS",
                           span, tags(sources), tags(speakers), Double(samples) / 16_000 / span, level, top))
        }
        started = now
        samples = 0; squares = 0; peak = 0; frames = 0
        sources = [:]; speakers = [:]
    }

    static func dbfs(_ v: Double) -> Double { v <= 0 ? -120 : 20 * log10(v / 32768) }
}
