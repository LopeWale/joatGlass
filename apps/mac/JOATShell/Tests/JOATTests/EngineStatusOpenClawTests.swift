import Foundation
import XCTest
@testable import JOAT

final class EngineStatusOpenClawTests: XCTestCase {
    func testDecodesSanitizedOpenClawAndBrainstormProjection() throws {
        let data = Data(#"""
        {
          "at": 1777777777,
          "openclaw": {
            "state": "connected_read_only",
            "endpoint_host": "admins-mac-mini.tail3a3363.ts.net",
            "scopes": ["operator.read"],
            "reason": "write_scope_required"
          },
          "agent_terminal": {
            "state": "approval_required",
            "reason": "write_scope_required"
          },
          "brainstorm": {
            "state": "ready",
            "seat_count": 2,
            "phase": "idle"
          }
        }
        """#.utf8)

        let status = try JSONDecoder().decode(EngineStatus.self, from: data)

        XCTAssertEqual(status.openClaw.state, "connected_read_only")
        XCTAssertEqual(status.openClaw.endpointHost, "admins-mac-mini.tail3a3363.ts.net")
        XCTAssertEqual(status.openClaw.scopes, ["operator.read"])
        XCTAssertEqual(status.openClaw.reason, "write_scope_required")
        XCTAssertEqual(status.agentTerminal.state, "approval_required")
        XCTAssertEqual(status.brainstorm.state, "ready")
        XCTAssertEqual(status.brainstorm.seatCount, 2)
    }

    func testMissingOrMalformedProjectionStaysUnavailable() throws {
        let data = Data(#"""
        {
          "openclaw": {"state": 17, "scopes": "operator.write"},
          "agent_terminal": {"state": true},
          "brainstorm": {"seat_count": "many"}
        }
        """#.utf8)

        let status = try JSONDecoder().decode(EngineStatus.self, from: data)

        XCTAssertEqual(status.openClaw.state, "not_configured")
        XCTAssertEqual(status.openClaw.scopes, [])
        XCTAssertEqual(status.agentTerminal.state, "unavailable")
        XCTAssertEqual(status.brainstorm.state, "unavailable")
        XCTAssertEqual(status.brainstorm.seatCount, 0)
    }

    func testFutureDatedSnapshotIsNotFresh() {
        var status = EngineStatus()
        status.at = Date().addingTimeInterval(60).timeIntervalSince1970

        XCTAssertFalse(status.isFresh)
    }
}
