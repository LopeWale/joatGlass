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
/// Only one same-user connection is accepted; a replacement closes the old one.
final class NativeBridgeServer: @unchecked Sendable {
    static let shared = NativeBridgeServer()

    let socketURL: URL

    private let router: BridgeRouter
    private let acceptQueue = DispatchQueue(label: "com.joat.native-bridge.accept")
    private let stateLock = NSLock()
    private var listenerFD: Int32 = -1
    private var connectionFD: Int32 = -1

    init(
        socketURL: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT/native-bridge.sock"),
        router: BridgeRouter = BridgeRouter()
    ) {
        self.socketURL = socketURL
        self.router = router
    }

    var isRunning: Bool {
        stateLock.withLock { listenerFD >= 0 }
    }

    func authorizationSnapshot() async -> EventKitAuthorizationSnapshot {
        await router.authorizationSnapshot()
    }

    func requestFullAccess(entity: String) async throws -> EventKitAuthorizationSnapshot {
        try await router.requestFullAccessFromMacUI(entity: entity)
    }

    func start() throws {
        if isRunning { return }
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
        do {
            try bindSocket(fd)
            guard Darwin.chmod(socketURL.path, 0o600) == 0 else {
                throw BridgeServerError.systemCall("chmod", errno)
            }
            guard Darwin.listen(fd, 1) == 0 else {
                throw BridgeServerError.systemCall("listen", errno)
            }
        } catch {
            Darwin.close(fd)
            Darwin.unlink(socketURL.path)
            throw error
        }
        stateLock.withLock { listenerFD = fd }
        Task { await router.start() }
        acceptQueue.async { [weak self] in self?.acceptLoop(fd) }
    }

    func stop() {
        let descriptors = stateLock.withLock { () -> (Int32, Int32) in
            let result = (listenerFD, connectionFD)
            listenerFD = -1
            connectionFD = -1
            return result
        }
        if descriptors.1 >= 0 { Darwin.shutdown(descriptors.1, SHUT_RDWR) }
        if descriptors.0 >= 0 { Darwin.shutdown(descriptors.0, SHUT_RDWR) }
        if descriptors.1 >= 0 { Darwin.close(descriptors.1) }
        if descriptors.0 >= 0 { Darwin.close(descriptors.0) }
        Darwin.unlink(socketURL.path)
    }

    private func acceptLoop(_ listener: Int32) {
        while stateLock.withLock({ listenerFD == listener }) {
            let client = Darwin.accept(listener, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                return
            }
            var peerUID: uid_t = 0
            var peerGID: gid_t = 0
            guard getpeereid(client, &peerUID, &peerGID) == 0,
                  peerUID == geteuid()
            else {
                Darwin.close(client)
                continue
            }
            let old = stateLock.withLock { () -> Int32 in
                let old = connectionFD
                connectionFD = client
                return old
            }
            if old >= 0 {
                Darwin.shutdown(old, SHUT_RDWR)
                Darwin.close(old)
            }
            BridgeConnection(fd: client, router: router) { [weak self] closed in
                self?.stateLock.withLock {
                    if self?.connectionFD == closed { self?.connectionFD = -1 }
                }
            }.run()
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
    private let fd: Int32
    private let router: BridgeRouter
    private let onClose: @Sendable (Int32) -> Void

    init(
        fd: Int32,
        router: BridgeRouter,
        onClose: @escaping @Sendable (Int32) -> Void
    ) {
        self.fd = fd
        self.router = router
        self.onClose = onClose
    }

    func run() {
        defer {
            Darwin.shutdown(fd, SHUT_RDWR)
            Darwin.close(fd)
            onClose(fd)
        }
        while let header = readExactly(4) {
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
            Task {
                let response = await router.handle(request)
                result.set(response)
                semaphore.signal()
            }
            semaphore.wait()
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
                if sent < 0 {
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
