import CryptoKit
import Foundation
import XCTest
@testable import JOAT

final class BridgePayloadDigestTests: XCTestCase {
    func testCivilTimeEncodesExplicitNullRepeatedChoice() throws {
        let civil = CivilTime(
            year: 2026,
            month: 9,
            day: 1,
            hour: 18,
            minute: 0,
            second: 0,
            timeZoneID: "America/Chicago",
            repeatedTimeChoice: nil
        )

        let encoded = try BridgeContract.encode(civil)

        guard case .object(let value) = encoded else {
            return XCTFail("CivilTime did not encode as an object")
        }
        XCTAssertEqual(value["repeated_time_choice"], .null)
    }

    func testSwiftCanonicalWriteBodyMatchesPythonV1Vector() throws {
        let body = exampleBody()
        let canonical = try BridgeContract.canonicalJSONData(body)

        // Produced by Python's json.dumps(body, ensure_ascii=False,
        // separators=(",", ":"), sort_keys=True). This locks both sides to
        // one byte-level contract, including Unicode and unescaped slashes.
        XCTAssertEqual(
            String(decoding: canonical, as: UTF8.self),
            #"{"alarm_offsets_seconds":[0,900],"calendar_identifier":"cal/é","end_unix_ms":1788276900000,"is_all_day":false,"start_unix_ms":1788274200000,"store_generation":7,"store_instance_id":"store-A","time_zone_id":"America/Chicago","title":"Réview \"α\""}"#
        )
        XCTAssertEqual(
            hexDigest(canonical),
            "e32c3a352b330192db16cfe2dd2e7d0ec94768d70ea1d8d58c3df20d8711690c"
        )

        let reminderJSON = #"{"alarm_offsets_seconds":[0],"calendar_identifier":"list-personal","due":{"day":1,"hour":18,"minute":0,"month":9,"repeated_time_choice":null,"second":0,"time_zone_id":"America/Chicago","year":2026},"store_generation":7,"store_instance_id":"store-A","title":"Buy filters"}"#
        let reminderBody = try JSONDecoder().decode(
            [String: JSONValue].self,
            from: Data(reminderJSON.utf8)
        )
        let canonicalReminder = try BridgeContract.canonicalJSONData(reminderBody)
        XCTAssertEqual(String(decoding: canonicalReminder, as: UTF8.self), reminderJSON)
        XCTAssertEqual(
            hexDigest(canonicalReminder),
            "58ab9a243a192d307176f665030a575625845b813b9d53670d39ec9eefe99ffc"
        )
    }

    func testRouterRejectsDigestThatDoesNotHashCanonicalReceivedBody() async throws {
        let router = BridgeRouter()
        let body = exampleBody()
        let jsonBody = try XCTUnwrap(try BridgeContract.encode(body).objectValue)
        let deadline = Int64(Date().timeIntervalSince1970 * 1_000) + 5_000

        let rejected = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "mismatch",
            method: "calendar.create",
            deadlineUnixMS: deadline,
            attemptID: "attempt-mismatch",
            payloadDigest: String(repeating: "a", count: 64),
            body: jsonBody
        ))
        XCTAssertEqual(rejected.error?.code, "payload_digest_mismatch")
        XCTAssertEqual(rejected.commitPhase, "none")

        let acceptedBinding = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "matching",
            method: "calendar.create",
            deadlineUnixMS: deadline,
            attemptID: "attempt-matching",
            payloadDigest: hexDigest(try BridgeContract.canonicalJSONData(body)),
            body: jsonBody
        ))
        XCTAssertEqual(acceptedBinding.error?.code, "stale_store_instance")
        XCTAssertNotEqual(acceptedBinding.error?.code, "payload_digest_mismatch")
    }

    func testReminderCompletionPayloadBindsThePreviewedList() throws {
        let request = ReminderCompleteRequest(
            storeInstanceID: "store-A",
            storeGeneration: 7,
            calendarIdentifier: "list-personal",
            calendarItemIdentifier: "reminder-1"
        )

        let encoded = try XCTUnwrap(try BridgeContract.encode(request).objectValue)

        XCTAssertEqual(encoded["calendar_identifier"], .string("list-personal"))
        XCTAssertEqual(encoded["calendar_item_identifier"], .string("reminder-1"))
    }

    func testAcceptanceCleanupPayloadBindsExactNativeState() throws {
        let event = AcceptanceCalendarDeleteRequest(
            calendarIdentifier: "calendar-1",
            calendarItemIdentifier: "event-1",
            expectedTitle: "JOAT ACCEPTANCE a1b2c3d4 EVENT",
            expectedStartUnixMS: 1_788_274_200_000,
            expectedEndUnixMS: 1_788_274_260_000
        )
        let reminder = AcceptanceReminderDeleteRequest(
            calendarIdentifier: "list-1",
            calendarItemIdentifier: "reminder-1",
            expectedTitle: "JOAT ACCEPTANCE a1b2c3d4 REMINDER",
            expectedDueUnixMS: 1_788_274_200_000
        )

        let encodedEvent = try XCTUnwrap(try BridgeContract.encode(event).objectValue)
        let encodedReminder = try XCTUnwrap(try BridgeContract.encode(reminder).objectValue)

        XCTAssertEqual(encodedEvent["calendar_identifier"], .string("calendar-1"))
        XCTAssertEqual(encodedEvent["calendar_item_identifier"], .string("event-1"))
        XCTAssertEqual(encodedEvent["expected_title"], .string("JOAT ACCEPTANCE a1b2c3d4 EVENT"))
        XCTAssertEqual(encodedEvent["expected_start_unix_ms"], .number(1_788_274_200_000))
        XCTAssertEqual(encodedEvent["expected_end_unix_ms"], .number(1_788_274_260_000))
        XCTAssertEqual(encodedReminder["calendar_identifier"], .string("list-1"))
        XCTAssertEqual(encodedReminder["calendar_item_identifier"], .string("reminder-1"))
        XCTAssertEqual(encodedReminder["expected_title"], .string("JOAT ACCEPTANCE a1b2c3d4 REMINDER"))
        XCTAssertEqual(encodedReminder["expected_due_unix_ms"], .number(1_788_274_200_000))
    }

    func testAcceptanceCleanupRejectsNonSyntheticTitleBeforeEventKitAccess() async throws {
        let router = BridgeRouter()
        let request = AcceptanceCalendarDeleteRequest(
            calendarIdentifier: "calendar-1",
            calendarItemIdentifier: "event-1",
            expectedTitle: "JOAT ACCEPTANCE ١٢٣٤٥٦٧٨ EVENT",
            expectedStartUnixMS: 1_788_274_200_000,
            expectedEndUnixMS: 1_788_274_260_000
        )
        let body = try XCTUnwrap(try BridgeContract.encode(request).objectValue)
        let deadline = Int64(Date().timeIntervalSince1970 * 1_000) + 5_000
        let digest = hexDigest(try BridgeContract.canonicalJSONData(body))

        let response = await router.handle(BridgeRequest(
            version: BridgeContract.version,
            requestID: "unsafe-cleanup",
            method: "acceptance.calendar.delete",
            deadlineUnixMS: deadline,
            attemptID: "attempt-unsafe-cleanup",
            payloadDigest: digest,
            body: body
        ))

        XCTAssertEqual(response.error?.code, "acceptance_title_required")
        XCTAssertEqual(response.commitPhase, "none")
    }

    private func exampleBody() -> CalendarCreateRequest {
        CalendarCreateRequest(
            storeInstanceID: "store-A",
            storeGeneration: 7,
            calendarIdentifier: "cal/é",
            title: "Réview \"α\"",
            isAllDay: false,
            startUnixMS: 1_788_274_200_000,
            endUnixMS: 1_788_276_900_000,
            timeZoneID: "America/Chicago",
            alarmOffsetsSeconds: [0, 900]
        )
    }

    private func hexDigest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

private extension JSONValue {
    var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }
}
