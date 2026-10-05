import XCTest
@testable import JOAT

@MainActor
final class EngineCommandChannelTests: XCTestCase {
    @MainActor
    private final class Fixture {
        var clock: TimeInterval = 100
        var writes: [[String: Any]] = []
        var channel: EngineCommandChannel!
        var epoch = ""
        let capability: [String: Bool] = [
            "configured": true, "available": true, "active": false,
            "denied": false, "disconnected": false, "pending": false
        ]

        init() {
            channel = EngineCommandChannel(parentPID: 123, now: { [unowned self] in self.clock },
                writer: { [unowned self] data in
                    self.writes.append(try JSONSerialization.jsonObject(with: data) as! [String: Any])
                }, automaticTimers: false)
            start()
        }

        func start() {
            let launch = channel.prepare()
            epoch = launch.epoch
            channel.attach(processIdentifier: 456, epoch: epoch)
        }

        func envelope(_ kind: String, additions: [String: Any] = [:]) -> [String: Any] {
            var value: [String: Any] = ["v": 1, "epoch": epoch, "engine_pid": 456,
                "parent_pid": 123, "kind": kind]
            value.merge(additions) { _, new in new }
            return value
        }

        func bytes(_ envelope: [String: Any]) -> Data {
            var data = try! JSONSerialization.data(withJSONObject: envelope)
            data.append(10)
            return data
        }

        func receive(_ kind: String, additions: [String: Any] = [:]) {
            channel.receive(bytes(envelope(kind, additions: additions)), epoch: epoch)
        }

        func hello() {
            receive("hello", additions: ["capabilities": ["news.refresh": capability, "service.retry": capability]])
        }

        func ack(_ id: String, outcome: String, action: String = "news.refresh", reason: String = "completed") {
            receive("ack", additions: ["request_id": id, "action": action, "outcome": outcome, "reason": reason])
        }
    }

    func testSuccessRequiresExplicitTerminalAcknowledgement() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .pending)
        XCTAssertEqual(f.writes.last?["engine_pid"] as? Int, 456)
        XCTAssertEqual(f.writes.last?["parent_pid"] as? Int, 123)
        XCTAssertEqual(f.writes.last?["epoch"] as? String, f.epoch)
        XCTAssertEqual(f.writes.last?["request_id"] as? String, id)
        f.ack(id, outcome: "pending", reason: "busy")
        XCTAssertTrue(f.channel.state(for: .newsRefresh)!.isPending)
        f.ack(id, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .succeeded)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.notice, "Engine acknowledged completion.")
    }

    func testEngineRefusalIsNotSuccess() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.serviceRetry)
        f.ack(id, outcome: "refused", action: "service.retry", reason: "denied")
        XCTAssertEqual(f.channel.state(for: .serviceRetry)?.outcome, .refused)
        XCTAssertEqual(f.channel.state(for: .serviceRetry)?.notice, "Engine refused: access denied.")
    }

    func testUnreadyAndUnavailableCommandsNeverReachThePipe() {
        let f = Fixture()
        f.channel.send(.newsRefresh)
        XCTAssertTrue(f.writes.isEmpty)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .disconnected)
        var denied = f.capability
        denied["denied"] = true
        denied["available"] = false
        f.receive("hello", additions: ["capabilities": ["news.refresh": denied, "service.retry": f.capability]])
        XCTAssertTrue(f.channel.capability(for: .newsRefresh).denied)
        XCTAssertFalse(f.channel.capability(for: .newsRefresh).canRequest)
        f.channel.send(.newsRefresh)
        XCTAssertTrue(f.writes.isEmpty)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .refused)
    }

    func testTimeoutRequestsCancellationAndIgnoresLateSuccess() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh, timeoutMilliseconds: 1000)
        f.clock += 1.1
        f.channel.checkDeadlines()
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .timedOut)
        XCTAssertTrue(f.channel.state(for: .newsRefresh)!.notice.contains("Result unknown"))
        XCTAssertEqual(f.writes.last?["kind"] as? String, "cancel")
        XCTAssertEqual(f.writes.last?["request_id"] as? String, id)
        f.ack(id, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .timedOut)
    }

    func testRestartInvalidatesOutstandingRequestsAndOldEpoch() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh)
        let oldEpoch = f.epoch
        let oldAck = f.bytes(f.envelope("ack", additions: ["request_id": id,
            "action": "news.refresh", "outcome": "succeeded", "reason": "completed"]))
        f.start()
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .engineRestarted)
        XCTAssertTrue(f.channel.capabilities.isEmpty)
        f.channel.receive(oldAck, epoch: oldEpoch)
        XCTAssertFalse(f.channel.isConnected)
        f.hello()
        XCTAssertTrue(f.channel.isConnected)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .engineRestarted)
    }

    func testDisconnectTerminalizesBothActionsAndClearsLiveProjection() {
        let f = Fixture()
        f.hello()
        f.channel.send(.newsRefresh)
        f.channel.send(.serviceRetry)
        f.channel.disconnect()
        XCTAssertFalse(f.channel.isConnected)
        XCTAssertTrue(f.channel.capabilities.isEmpty)
        XCTAssertTrue(f.channel.capability(for: .serviceRetry).disconnected)
        for action in EngineCommandChannel.Action.allCases {
            XCTAssertEqual(f.channel.state(for: action)?.outcome, .disconnected)
            XCTAssertTrue(f.channel.state(for: action)!.notice.contains("Result unknown"))
        }
    }

    func testSupersededAcknowledgementCannotOverwriteReplacement() {
        let f = Fixture()
        f.hello()
        let first = f.channel.send(.newsRefresh)
        let second = f.channel.send(.newsRefresh)
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(f.writes.count, 3)
        XCTAssertEqual(f.writes[1]["kind"] as? String, "cancel")
        XCTAssertEqual(f.writes[1]["request_id"] as? String, first)
        f.ack(first, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.requestID, second)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .pending)
        f.ack(second, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .succeeded)
    }

    func testCancellationDoesNotClaimOwnerRollback() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh)
        f.channel.cancel(.newsRefresh)
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .cancelled)
        XCTAssertTrue(f.channel.state(for: .newsRefresh)!.notice.contains("may already have occurred"))
        f.ack(id, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .cancelled)
    }

    func testHeartbeatExpiryAndRenewal() {
        let f = Fixture()
        f.hello()
        f.clock += 7
        f.receive("capabilities", additions: ["capabilities": ["news.refresh": f.capability, "service.retry": f.capability]])
        f.clock += 7
        f.channel.checkDeadlines()
        XCTAssertTrue(f.channel.isConnected)
        f.clock += 1
        f.channel.checkDeadlines()
        XCTAssertFalse(f.channel.isConnected)
        XCTAssertTrue(f.channel.capabilities.isEmpty)
    }

    func testFragmentedHelloAndBoundedFrame() {
        let f = Fixture()
        let bytes = f.bytes(f.envelope("hello", additions: ["capabilities": ["news.refresh": f.capability, "service.retry": f.capability]]))
        for byte in bytes.dropLast() { f.channel.receive(Data([byte]), epoch: f.epoch) }
        XCTAssertFalse(f.channel.isConnected)
        f.channel.receive(Data([10]), epoch: f.epoch)
        XCTAssertTrue(f.channel.isConnected)
        f.channel.receive(Data(repeating: 65, count: 16 * 1024 + 1), epoch: f.epoch)
        XCTAssertFalse(f.channel.isConnected)
    }

    func testWrongIdentityFailsClosed() {
        for override in [["engine_pid": 999], ["parent_pid": 999], ["v": 2]] {
            let f = Fixture()
            f.hello()
            f.channel.send(.newsRefresh)
            f.receive("capabilities", additions: override)
            XCTAssertFalse(f.channel.isConnected)
            XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .disconnected)
        }
    }

    func testUnsanitizedAckReasonFailsClosedWithoutDisplayingIt() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh)
        f.ack(id, outcome: "failed", reason: "secret-token-or-user-data")
        XCTAssertFalse(f.channel.isConnected)
        XCTAssertFalse(f.channel.state(for: .newsRefresh)!.notice.contains("secret"))
    }

    func testRetryCanBeAvailableWhileUnderlyingServiceIsDeniedAndDisconnected() {
        let f = Fixture()
        var remoteUnavailable = f.capability
        remoteUnavailable["denied"] = true
        remoteUnavailable["disconnected"] = true
        f.receive("hello", additions: ["capabilities": ["news.refresh": f.capability, "service.retry": remoteUnavailable]])
        XCTAssertTrue(f.channel.capability(for: .serviceRetry).canRequest)
        f.channel.send(.serviceRetry)
        XCTAssertEqual(f.writes.last?["action"] as? String, "service.retry")
        XCTAssertEqual(f.channel.state(for: .serviceRetry)?.outcome, .pending)
    }

    func testActualChildPipeHandshakeAcknowledgementAndEOF() async throws {
        let channel = EngineCommandChannel()
        let launch = channel.prepare()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-u", "-c", """
        import json, os, sys
        identity = dict(v=1, epoch=os.environ["JOAT_COMMAND_EPOCH"], engine_pid=os.getpid(), parent_pid=os.getppid())
        cap = dict(configured=True, available=True, active=False, denied=False, disconnected=False, pending=False)
        print(json.dumps(dict(identity, kind="hello", capabilities={"news.refresh":cap,"service.retry":cap})), flush=True)
        request = json.loads(sys.stdin.readline())
        assert all(request[k] == v for k,v in identity.items())
        print(json.dumps(dict(identity, kind="ack", request_id=request["request_id"], action=request["action"], outcome="succeeded", reason="completed")), flush=True)
        """]
        var environment = ProcessInfo.processInfo.environment
        environment["JOAT_COMMAND_EPOCH"] = launch.epoch
        process.environment = environment
        process.standardInput = launch.input
        process.standardOutput = launch.output
        process.standardError = Pipe()
        defer {
            if process.isRunning { process.terminate() }
            channel.disconnect()
        }
        try process.run()
        channel.attach(processIdentifier: process.processIdentifier, epoch: launch.epoch)
        for _ in 0..<500 {
            if channel.isConnected { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(channel.isConnected)
        channel.send(.newsRefresh)
        for _ in 0..<500 {
            if !channel.isConnected { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(channel.state(for: .newsRefresh)?.outcome, .succeeded)
        XCTAssertFalse(channel.isConnected, "EOF must clear the live channel")
        XCTAssertTrue(channel.capabilities.isEmpty)
    }

    func testCompletionPastDeadlineCannotWinBeforeTimerTick() {
        let f = Fixture()
        f.hello()
        let id = f.channel.send(.newsRefresh, timeoutMilliseconds: 1000)
        f.clock += 1.01
        f.ack(id, outcome: "succeeded")
        XCTAssertEqual(f.channel.state(for: .newsRefresh)?.outcome, .timedOut)
        XCTAssertEqual(f.writes.last?["kind"] as? String, "cancel")
    }
}
