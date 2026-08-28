import Darwin
import Foundation
import XCTest
@testable import JOAT

final class NativeBridgeServerTests: XCTestCase {
    func testOwnerOnlySocketAnswersFramedCapabilitiesRequest() throws {
        let directory = URL(fileURLWithPath: "/tmp/jb-\(UUID().uuidString)", isDirectory: true)
        let path = directory.appendingPathComponent("bridge.sock")
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = NativeBridgeServer(socketURL: path)
        try server.start()
        defer { server.stop() }

        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(fd, 0)
        defer { Darwin.close(fd) }
        try connect(fd, path: path.path)

        let response = try exchange(
            makeRequest(requestID: "request-1", method: "capabilities"),
            on: fd
        )

        XCTAssertEqual(response.requestID, "request-1")
        XCTAssertEqual(response.commitPhase, "read_only")
        XCTAssertNil(response.error)
        XCTAssertNotNil(response.result)
        XCTAssertFalse(response.storeInstanceID.isEmpty)
    }

    func testIPCRequesterCannotTriggerEventKitAuthorization() throws {
        let directory = URL(fileURLWithPath: "/tmp/jb-\(UUID().uuidString)", isDirectory: true)
        let path = directory.appendingPathComponent("bridge.sock")
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = NativeBridgeServer(socketURL: path)
        try server.start()
        defer { server.stop() }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(fd, 0)
        defer { Darwin.close(fd) }
        try connect(fd, path: path.path)

        let response = try exchange(
            makeRequest(
                requestID: "permission-request",
                method: "authorization.requestFullAccess",
                body: [
                    "entity": .string("calendar"),
                    "user_initiated": .bool(true),
                ]
            ),
            on: fd
        )

        XCTAssertEqual(response.requestID, "permission-request")
        XCTAssertEqual(response.commitPhase, "none")
        XCTAssertEqual(response.error?.code, "permission_request_ui_only")
        XCTAssertNil(response.result)
    }

    func testReplacementConnectionIsServedBeforeFirstClientDisconnects() throws {
        let directory = URL(fileURLWithPath: "/tmp/jb-\(UUID().uuidString)", isDirectory: true)
        let path = directory.appendingPathComponent("bridge.sock")
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = NativeBridgeServer(socketURL: path)
        try server.start()
        defer { server.stop() }

        let first = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(first, 0)
        defer { Darwin.close(first) }
        try connect(first, path: path.path)
        XCTAssertNil(try exchange(
            makeRequest(requestID: "first", method: "capabilities"),
            on: first
        ).error)

        let second = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(second, 0)
        defer { Darwin.close(second) }
        try setReceiveTimeout(second, seconds: 1)
        try connect(second, path: path.path)

        let replacementResponse = try exchange(
            makeRequest(requestID: "replacement", method: "capabilities"),
            on: second
        )
        XCTAssertEqual(replacementResponse.requestID, "replacement")
        XCTAssertNil(replacementResponse.error)

        try setReceiveTimeout(first, seconds: 1)
        var byte: UInt8 = 0
        XCTAssertEqual(Darwin.recv(first, &byte, 1, MSG_PEEK), 0)
    }

    private func makeRequest(
        requestID: String,
        method: String,
        body: [String: JSONValue] = [:]
    ) -> BridgeRequest {
        BridgeRequest(
            version: BridgeContract.version,
            requestID: requestID,
            method: method,
            deadlineUnixMS: Int64(Date().timeIntervalSince1970 * 1_000) + 5_000,
            attemptID: nil,
            payloadDigest: nil,
            body: body
        )
    }

    private func exchange(_ request: BridgeRequest, on fd: Int32) throws -> BridgeResponse {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let payload = try encoder.encode(request)
        var length = UInt32(payload.count).bigEndian
        var frame = Data(bytes: &length, count: 4)
        frame.append(payload)
        XCTAssertTrue(writeAll(fd, frame))

        let header = try XCTUnwrap(readExactly(fd, count: 4))
        let count = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        let responseData = try XCTUnwrap(readExactly(fd, count: Int(count)))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(BridgeResponse.self, from: responseData)
    }

    private func setReceiveTimeout(_ fd: Int32, seconds: Int) throws {
        var timeout = timeval(tv_sec: seconds, tv_usec: 0)
        let result = withUnsafePointer(to: &timeout) { pointer in
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, pointer, socklen_t(MemoryLayout<timeval>.size))
        }
        if result != 0 {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    private func connect(_ fd: Int32, path: String) throws {
        let bytes = Array(path.utf8CString)
        var address = sockaddr_un()
        XCTAssertLessThanOrEqual(bytes.count, MemoryLayout.size(ofValue: address.sun_path))
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: bytes.count) { chars in
                for index in bytes.indices { chars[index] = bytes[index] }
            }
        }
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if result != 0 {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    private func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        var offset = 0
        return data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            while offset < data.count {
                let sent = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
                if sent <= 0 { return false }
                offset += sent
            }
            return true
        }
    }

    private func readExactly(_ fd: Int32, count: Int) -> Data? {
        var data = Data(count: count)
        var offset = 0
        let success = data.withUnsafeMutableBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            while offset < count {
                let received = Darwin.read(fd, base.advanced(by: offset), count - offset)
                if received <= 0 { return false }
                offset += received
            }
            return true
        }
        return success ? data : nil
    }
}
