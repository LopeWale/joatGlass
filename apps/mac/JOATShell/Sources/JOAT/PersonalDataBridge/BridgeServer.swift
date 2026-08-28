import Darwin
import Foundation

enum BridgeServerError: Error, LocalizedError {
    case pathTooLong
    case unsafeExistingPath
    case systemCall(String, Int32)

    var errorDescription: String? {
        switch self {
        case .pathTooLong: "native bridge socket path is too long"
        case .unsafeExistingPath: "native bridge path exists but is not an app-owned socket"
        case .systemCall(let name, let code): "\(name) failed with errno \(code)"
        }
    }
}

/// Private, framed IPC from the Python engine to the app-owned EventKit actor.
/// Only the exact app-launched engine PID is accepted; replacing or revoking that
/// authorization closes the prior connection.
final class NativeBridgeServer: @unchecked Sendable {
    private struct Listener {
        let id: UInt64
        let fd: Int32
    }

    private struct ActiveConnection {
        let id: UInt64
        let connection: BridgeConnection
    }

    static let shared = NativeBridgeServer()

    let socketURL: URL

    private let router: BridgeRouter
    private let acceptQueue = DispatchQueue(label: "com.joat.native-bridge.accept")
    private let connectionQueue = DispatchQueue(
        label: "com.joat.native-bridge.connection",
        attributes: .concurrent
    )
    private let lifecycleLock = NSLock()
    private let stateLock = NSLock()
    private var listener: Listener?
    private var activeConnection: ActiveConnection?
    private var authorizedPeerPID: pid_t?
    private var nextIdentity: UInt64 = 0

    init(
        socketURL: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT/native-bridge.sock"),
        router: BridgeRouter = BridgeRouter(),
        authorizedPeerPID: pid_t? = nil
    ) {
        self.socketURL = socketURL
        self.router = router
        self.authorizedPeerPID = authorizedPeerPID.flatMap { $0 > 0 ? $0 : nil }
    }

    var isRunning: Bool {
        stateLock.withLock { listener != nil }
    }

    func authorizationSnapshot() async -> EventKitAuthorizationSnapshot {
        await router.authorizationSnapshot()
    }

    func requestFullAccess(entity: String) async throws -> EventKitAuthorizationSnapshot {
        try await router.requestFullAccessFromMacUI(entity: entity)
    }

    /// Admit only the exact app-launched engine process. A same-UID process
    /// cannot replace the engine merely by discovering the owner-only path.
    /// Changing or revoking the PID also invalidates the existing connection.
    func authorizePeerProcessID(_ pid: pid_t?) {
        let revoked = stateLock.withLock { () -> ActiveConnection? in
            authorizedPeerPID = pid.flatMap { $0 > 0 ? $0 : nil }
            let previous = activeConnection
            activeConnection = nil
            return previous
        }
        revoked?.connection.cancel()
    }

    func start() throws {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        if stateLock.withLock({ listener != nil }) { return }

        let directory = socketURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: directory.path
        )
        try removeOwnedStaleSocket()

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw BridgeServerError.systemCall("socket", errno) }
        var didBind = false
        do {
            try bindSocket(fd)
            didBind = true
            guard Darwin.chmod(socketURL.path, 0o600) == 0 else {
                throw BridgeServerError.systemCall("chmod", errno)
            }
            guard Darwin.listen(fd, 1) == 0 else {
                throw BridgeServerError.systemCall("listen", errno)
            }
        } catch {
            Darwin.close(fd)
            if didBind { Darwin.unlink(socketURL.path) }
            throw error
        }
        let newListener = stateLock.withLock { () -> Listener in
            nextIdentity &+= 1
            let value = Listener(id: nextIdentity, fd: fd)
            listener = value
            return value
        }
        Task { await router.start() }
        acceptQueue.async { [weak self] in self?.acceptLoop(newListener) }
    }

    func stop() {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }

        let stopped = stateLock.withLock { () -> (Listener?, ActiveConnection?) in
            let result = (listener, activeConnection)
            listener = nil
            activeConnection = nil
            return result
        }
        stopped.1?.connection.cancel()
        if let stoppedListener = stopped.0 {
            Darwin.shutdown(stoppedListener.fd, SHUT_RDWR)
            Darwin.close(stoppedListener.fd)
        }
        Darwin.unlink(socketURL.path)
    }

    private func acceptLoop(_ acceptedListener: Listener) {
        while isCurrent(acceptedListener) {
            let client = Darwin.accept(acceptedListener.fd, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                return
            }
            var peerUID: uid_t = 0
            var peerGID: gid_t = 0
            var peerPID: pid_t = 0
            var peerPIDSize = socklen_t(MemoryLayout<pid_t>.size)
            guard getpeereid(client, &peerUID, &peerGID) == 0,
                  peerUID == geteuid(),
                  getsockopt(
                      client,
                      SOL_LOCAL,
                      LOCAL_PEERPID,
                      &peerPID,
                      &peerPIDSize
                  ) == 0,
                  peerPID > 0
            else {
                Darwin.close(client)
                continue
            }

            var noSigPipe: Int32 = 1
            guard setsockopt(
                client,
                SOL_SOCKET,
                SO_NOSIGPIPE,
                &noSigPipe,
                socklen_t(MemoryLayout<Int32>.size)
            ) == 0 else {
                Darwin.close(client)
                continue
            }

            let installed = stateLock.withLock { () -> (
                connection: BridgeConnection,
                replaced: ActiveConnection?
            )? in
                guard listener?.id == acceptedListener.id,
                      authorizedPeerPID == peerPID
                else { return nil }
                nextIdentity &+= 1
                let identity = nextIdentity
                let connection = BridgeConnection(
                    identity: identity,
                    fd: client,
                    router: router
                ) { [weak self] closedIdentity in
                    self?.connectionDidClose(identity: closedIdentity)
                }
                let replaced = activeConnection
                activeConnection = ActiveConnection(id: identity, connection: connection)
                return (connection, replaced)
            }
            guard let installed else {
                Darwin.close(client)
                if !isCurrent(acceptedListener) { return }
                continue
            }
            installed.replaced?.connection.cancel()
            connectionQueue.async { installed.connection.run() }
        }
    }

    private func isCurrent(_ candidate: Listener) -> Bool {
        stateLock.withLock { listener?.id == candidate.id }
    }

    private func connectionDidClose(identity: UInt64) {
        stateLock.withLock {
            if activeConnection?.id == identity { activeConnection = nil }
        }
    }

    private func bindSocket(_ fd: Int32) throws {
        let bytes = Array(socketURL.path.utf8CString)
        var address = sockaddr_un()
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw BridgeServerError.pathTooLong
        }
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: bytes.count) { chars in
                for index in bytes.indices { chars[index] = bytes[index] }
            }
        }
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard result == 0 else { throw BridgeServerError.systemCall("bind", errno) }
    }

    private func removeOwnedStaleSocket() throws {
        var info = stat()
        guard lstat(socketURL.path, &info) == 0 else {
            if errno == ENOENT { return }
            throw BridgeServerError.systemCall("lstat", errno)
        }
        guard (info.st_mode & S_IFMT) == S_IFSOCK,
              info.st_uid == geteuid()
        else { throw BridgeServerError.unsafeExistingPath }
        guard Darwin.unlink(socketURL.path) == 0 else {
            throw BridgeServerError.systemCall("unlink", errno)
        }
    }
}

private final class BridgeConnection: @unchecked Sendable {
    private let identity: UInt64
    private let fd: Int32
    private let router: BridgeRouter
    private let stateLock = NSLock()
    private var isCancelled = false
    private var currentTask: Task<Void, Never>?
    private let onClose: @Sendable (UInt64) -> Void

    init(
        identity: UInt64,
        fd: Int32,
        router: BridgeRouter,
        onClose: @escaping @Sendable (UInt64) -> Void
    ) {
        self.identity = identity
        self.fd = fd
        self.router = router
        self.onClose = onClose
    }

    func cancel() {
        let task = stateLock.withLock { () -> Task<Void, Never>? in
            isCancelled = true
            return currentTask
        }
        task?.cancel()
        Darwin.shutdown(fd, SHUT_RDWR)
    }

    func run() {
        defer {
            Darwin.shutdown(fd, SHUT_RDWR)
            Darwin.close(fd)
            onClose(identity)
        }
        while !stateLock.withLock({ isCancelled }), let header = readExactly(4) {
            let length = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            guard length > 0, length <= BridgeContract.maximumFrameBytes,
                  let payload = readExactly(Int(length))
            else { return }
            let request: BridgeRequest
            do {
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                request = try decoder.decode(BridgeRequest.self, from: payload)
            } catch {
                return
            }
            let semaphore = DispatchSemaphore(value: 0)
            let result = LockedValue<BridgeResponse>()
            let task = Task {
                defer { semaphore.signal() }
                guard !Task.isCancelled else { return }
                let response = await router.handle(request)
                result.set(response)
            }
            let shouldRun = stateLock.withLock { () -> Bool in
                guard !isCancelled else { return false }
                currentTask = task
                return true
            }
            if !shouldRun { task.cancel() }
            semaphore.wait()
            stateLock.withLock { currentTask = nil }
            guard shouldRun else { return }
            guard let response = result.get(),
                  writeFrame(response)
            else { return }
        }
    }

    private func readExactly(_ count: Int) -> Data? {
        var data = Data(count: count)
        var offset = 0
        let success = data.withUnsafeMutableBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            while offset < count {
                let received = Darwin.read(fd, base.advanced(by: offset), count - offset)
                if received == 0 { return false }
                if received < 0 {
                    if errno == EINTR { continue }
                    return false
                }
                offset += received
            }
            return true
        }
        return success ? data : nil
    }

    private func writeFrame(_ response: BridgeResponse) -> Bool {
        do {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            let payload = try encoder.encode(response)
            guard payload.count <= BridgeContract.maximumFrameBytes else { return false }
            var length = UInt32(payload.count).bigEndian
            var frame = Data(bytes: &length, count: 4)
            frame.append(payload)
            return writeAll(frame)
        } catch {
            return false
        }
    }

    private func writeAll(_ data: Data) -> Bool {
        var offset = 0
        return data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            while offset < data.count {
                let sent = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
                if sent <= 0 {
                    if errno == EINTR { continue }
                    return false
                }
                offset += sent
            }
            return true
        }
    }
}

private final class LockedValue<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value?

    func set(_ newValue: Value) {
        lock.withLock { value = newValue }
    }

    func get() -> Value? {
        lock.withLock { value }
    }
}
