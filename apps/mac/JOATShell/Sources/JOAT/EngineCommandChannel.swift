import Foundation
import Combine
import Darwin

/// A private inherited-pipe conversation with this shell's child engine. It has no
/// connection to the Hub WebSocket, its authentication, or its phone session.
@MainActor
final class EngineCommandChannel: ObservableObject {
    enum Action: String, Codable, CaseIterable {
        case newsRefresh = "news.refresh"
        case serviceRetry = "service.retry"
    }

    struct Capability: Codable, Equatable {
        let configured: Bool
        let available: Bool
        let active: Bool
        let denied: Bool
        let disconnected: Bool
        let pending: Bool

        static let disconnectedState = Capability(
            configured: false, available: false, active: false,
            denied: false, disconnected: true, pending: false
        )
        var canRequest: Bool { configured && available && !pending }
    }

    enum Outcome: String, Decodable {
        case pending, succeeded, refused, failed
        case timedOut = "timed_out"
        case cancelled, superseded, disconnected, engineRestarted
    }

    struct RequestState: Equatable {
        let requestID: String
        let outcome: Outcome
        let notice: String
        var isPending: Bool { outcome == .pending }
    }

    struct Launch {
        let epoch: String
        let input: Pipe
        let output: Pipe
    }

    @Published private(set) var isConnected = false
    @Published private(set) var capabilities: [Action: Capability] = [:]
    @Published private(set) var requests: [Action: RequestState] = [:]

    private struct Identity: Equatable {
        let epoch: String
        let enginePID: Int32
    }
    private struct Envelope: Decodable {
        let v: Int
        let epoch: String
        let engine_pid: Int32
        let parent_pid: Int32
        let kind: String
        let capabilities: [String: Capability]?
        let request_id: String?
        let action: Action?
        let outcome: Outcome?
        let reason: String?
    }
    private let parentPID: Int32
    private let now: () -> TimeInterval
    private let testWriter: ((Data) throws -> Void)?
    private let automaticTimers: Bool
    private var launch: Launch?
    private var identity: Identity?
    private var frameBuffer = Data()
    private var deadlines: [Action: TimeInterval] = [:]
    private var lastContact: TimeInterval = 0
    private var attachedAt: TimeInterval = 0
    private var heartbeatTask: Task<Void, Never>?
    private static let maximumFrameBytes = 16 * 1024
    private static let reasons: Set<String> = [
        "completed", "partial", "unavailable", "not_configured", "denied", "busy",
        "invalid_request", "duplicate", "timeout", "cancelled", "superseded",
        "owner_failed", "identity_mismatch"
    ]

    init(
        parentPID: Int32 = ProcessInfo.processInfo.processIdentifier,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        writer: ((Data) throws -> Void)? = nil,
        automaticTimers: Bool = true
    ) {
        self.parentPID = parentPID
        self.now = now
        self.testWriter = writer
        self.automaticTimers = automaticTimers
    }

    func capability(for action: Action) -> Capability {
        capabilities[action] ?? .disconnectedState
    }

    func state(for action: Action) -> RequestState? { requests[action] }

    func prepare() -> Launch {
        disconnect(outcome: .engineRestarted)
        let prepared = Launch(epoch: UUID().uuidString, input: Pipe(), output: Pipe())
        launch = prepared
        return prepared
    }

    /// Call only after Process.run succeeds. The pipe endpoints are tied to the
    /// Process launch; a matching envelope alone never establishes a connection.
    func attach(processIdentifier: Int32, epoch: String) {
        guard let launch, launch.epoch == epoch, processIdentifier > 0 else { return }
        identity = Identity(epoch: epoch, enginePID: processIdentifier)
        attachedAt = now()
        lastContact = attachedAt
        let writer = launch.input.fileHandleForWriting.fileDescriptor
        _ = fcntl(writer, F_SETFL, fcntl(writer, F_GETFL) | O_NONBLOCK)
        _ = fcntl(writer, F_SETNOSIGPIPE, 1)
        try? launch.input.fileHandleForReading.close()
        try? launch.output.fileHandleForWriting.close()
        if testWriter == nil {
            launch.output.fileHandleForReading.readabilityHandler = { [weak self] handle in
                var bytes = [UInt8](repeating: 0, count: 4096)
                let count = Darwin.read(handle.fileDescriptor, &bytes, bytes.count)
                let readError = errno
                let data = count > 0 ? Data(bytes.prefix(count)) : Data()
                if count == 0 || (count < 0 && readError != EINTR) {
                    handle.readabilityHandler = nil
                }
                Task { @MainActor [weak self] in
                    guard let self, self.identity?.epoch == epoch else { return }
                    if count > 0 { self.receive(data, epoch: epoch) }
                    else if count == 0 { self.disconnect() }
                    else if readError != EINTR { self.disconnect() }
                }
            }
        }
        if automaticTimers {
            heartbeatTask = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(250))
                    guard !Task.isCancelled, let self else { return }
                    self.checkDeadlines()
                }
            }
        }
    }

    /// Replaces only a request for the same action. Cancellation is best effort:
    /// the UI never implies that already performed owner work was rolled back.
    @discardableResult
    func send(_ action: Action, timeoutMilliseconds: Int = 20_000) -> String {
        if requests[action]?.isPending == true { cancel(action, outcome: .superseded) }
        let requestID = UUID().uuidString
        guard isConnected else {
            requests[action] = RequestState(requestID: requestID, outcome: .disconnected,
                notice: "Engine disconnected. No command was sent.")
            return requestID
        }
        let capability = capability(for: action)
        // A pending capability can describe the request being superseded. The
        // owner decides admission and returns an explicit refusal if still busy.
        guard capability.configured && capability.available else {
            requests[action] = RequestState(requestID: requestID, outcome: .refused,
                notice: capability.denied ? "Access denied. No command was sent." : "Action unavailable. No command was sent.")
            return requestID
        }
        let timeout = min(30_000, max(100, timeoutMilliseconds))
        requests[action] = RequestState(requestID: requestID, outcome: .pending,
            notice: "Waiting for engine acknowledgement…")
        deadlines[action] = now() + Double(timeout) / 1000
        do {
            try write(kind: "request", requestID: requestID, action: action, timeout: timeout)
        } catch { disconnect() }
        return requestID
    }

    func cancel(_ action: Action) { cancel(action, outcome: .cancelled) }

    private func cancel(_ action: Action, outcome: Outcome) {
        guard let request = requests[action], request.isPending else { return }
        do { try write(kind: "cancel", requestID: request.requestID) }
        catch { disconnect(); return }
        finish(action, outcome: outcome, notice: outcome == .superseded
            ? "Request superseded. Earlier work may already have occurred."
            : "Cancellation requested. Earlier work may already have occurred.")
    }

    func disconnect() { disconnect(outcome: .disconnected) }

    private func disconnect(outcome: Outcome) {
        heartbeatTask?.cancel()
        heartbeatTask = nil
        for action in Action.allCases where requests[action]?.isPending == true {
            finish(action, outcome: outcome, notice: outcome == .engineRestarted
                ? "Engine restarted before acknowledgement. Result unknown."
                : "Engine disconnected before acknowledgement. Result unknown.")
        }
        isConnected = false
        capabilities = [:]
        identity = nil
        deadlines = [:]
        frameBuffer.removeAll(keepingCapacity: false)
        if let launch {
            launch.output.fileHandleForReading.readabilityHandler = nil
            try? launch.input.fileHandleForReading.close()
            try? launch.output.fileHandleForWriting.close()
            try? launch.input.fileHandleForWriting.close()
            try? launch.output.fileHandleForReading.close()
        }
        launch = nil
    }

    /// Internal entry points make framing and monotonic deadlines deterministic
    /// in tests without opening a network listener or a live engine.
    func receive(_ data: Data, epoch: String) {
        guard identity?.epoch == epoch else { return }
        checkDeadlines()
        guard let identity, identity.epoch == epoch else { return }
        for byte in data {
            if byte == 10 {
                guard !frameBuffer.isEmpty else { disconnect(); return }
                let frame = frameBuffer
                frameBuffer.removeAll(keepingCapacity: true)
                guard let envelope = try? JSONDecoder().decode(Envelope.self, from: frame),
                      accept(envelope, identity: identity) else {
                    disconnect()
                    return
                }
            } else {
                guard frameBuffer.count < Self.maximumFrameBytes else { disconnect(); return }
                frameBuffer.append(byte)
            }
        }
    }

    func checkDeadlines() {
        guard identity != nil else { return }
        // Owners must publish hello before commands are admitted. Startup has a
        // separate grace period; a live channel requires a heartbeat within 8s.
        if isConnected ? now() - lastContact >= 8 : now() - attachedAt >= 30 {
            disconnect()
            return
        }
        for action in Action.allCases {
            guard let deadline = deadlines[action], now() >= deadline,
                  let request = requests[action], request.isPending else { continue }
            do { try write(kind: "cancel", requestID: request.requestID) }
            catch { disconnect(); return }
            finish(action, outcome: .timedOut,
                notice: "Engine acknowledgement timed out. Result unknown; cancellation requested.")
        }
    }

    private func accept(_ envelope: Envelope, identity: Identity) -> Bool {
        guard envelope.v == 1, envelope.epoch == identity.epoch,
              envelope.engine_pid == identity.enginePID, envelope.parent_pid == parentPID else { return false }
        guard ["hello", "capabilities", "ack"].contains(envelope.kind) else { return false }
        if envelope.kind == "hello" {
            guard !isConnected, let projection = envelope.capabilities,
                  Action.allCases.allSatisfy({ projection[$0.rawValue] != nil }) else { return false }
            isConnected = true
        } else if !isConnected { return false }
        if envelope.kind == "capabilities" && envelope.capabilities == nil { return false }
        if envelope.kind == "ack" {
            guard let id = envelope.request_id, UUID(uuidString: id) != nil,
                  let action = envelope.action, let outcome = envelope.outcome,
                  ![.disconnected, .engineRestarted].contains(outcome),
                  let reason = envelope.reason, Self.reasons.contains(reason) else { return false }
            // Late completions never replace a superseding request's state or
            // capability projection. Only a fresh heartbeat may update it.
            guard let request = requests[action], request.requestID == id, request.isPending else { return true }
            if outcome == .pending {
                requests[action] = RequestState(requestID: id, outcome: .pending,
                    notice: "Engine accepted the request. Waiting for completion…")
            } else {
                finish(action, outcome: outcome, notice: notice(outcome, reason: reason))
            }
        }
        if let projection = envelope.capabilities {
            guard Action.allCases.allSatisfy({ projection[$0.rawValue] != nil }) else { return false }
            capabilities = Dictionary(uniqueKeysWithValues: Action.allCases.map { ($0, projection[$0.rawValue]!) })
        }
        lastContact = now()
        return true
    }

    private func finish(_ action: Action, outcome: Outcome, notice: String) {
        guard let request = requests[action] else { return }
        requests[action] = RequestState(requestID: request.requestID, outcome: outcome, notice: notice)
        deadlines.removeValue(forKey: action)
    }

    private func notice(_ outcome: Outcome, reason: String) -> String {
        switch outcome {
        case .succeeded: return reason == "partial" ? "Engine acknowledged partial completion." : "Engine acknowledged completion."
        case .refused: return reason == "denied" ? "Engine refused: access denied." : "Engine refused the action (\(reason.replacingOccurrences(of: "_", with: " ")))."
        case .failed: return "Engine reported failure."
        case .timedOut: return "Engine reported a timeout. Earlier work may already have occurred."
        case .cancelled: return "Engine acknowledged cancellation. Earlier work may already have occurred."
        case .superseded: return "Engine acknowledged supersession. Earlier work may already have occurred."
        default: return "Waiting for engine acknowledgement…"
        }
    }

    private func write(kind: String, requestID: String, action: Action? = nil, timeout: Int? = nil) throws {
        guard let identity else { throw CocoaError(.fileWriteUnknown) }
        var payload: [String: Any] = ["v": 1, "epoch": identity.epoch,
            "engine_pid": identity.enginePID, "parent_pid": parentPID,
            "kind": kind, "request_id": requestID]
        if let action { payload["action"] = action.rawValue }
        if let timeout { payload["timeout_ms"] = timeout }
        var data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        data.append(10)
        if let testWriter { try testWriter(data); return }
        guard let launch else { throw CocoaError(.fileWriteUnknown) }
        // Each bounded request fits PIPE_BUF. Nonblocking writes cannot freeze
        // the app if a child has stopped reading; failure closes this channel.
        let written = data.withUnsafeBytes { bytes in
            Darwin.write(launch.input.fileHandleForWriting.fileDescriptor, bytes.baseAddress!, bytes.count)
        }
        guard written == data.count else { throw CocoaError(.fileWriteUnknown) }
    }
}
