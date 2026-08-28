import Foundation
import XCTest
@testable import JOAT

final class G2DiagnosticExportTests: XCTestCase {
    func testExportKeepsOnlyTheAllowlistedTraceVocabulary() throws {
        let source = try JSONSerialization.data(withJSONObject: [
            "schema": "joat.g2-diagnostic.v1",
            "events": [[
                "sequence": 1,
                "recordedAtMs": 100,
                "type": "tile_upload",
                "frame": 1,
                "order": 0,
                "containerId": 4,
                "outcome": "success",
                "pixels": "must-never-export",
            ]],
        ])

        XCTAssertThrowsError(try G2DiagnosticExport.redactedArchive(
            from: source, exportedAtMs: 200
        ))
    }

    func testExportWritesAContentFreeArchive() throws {
        let source = try JSONSerialization.data(withJSONObject: [
            "schema": "joat.g2-diagnostic.v2",
            "events": [[
                "sequence": 1,
                "recordedAtMs": 90,
                "type": "location_probe_state",
                "requested": "high",
                "outcome": "success",
            ], [
                "sequence": 2,
                "recordedAtMs": 100,
                "type": "location_probe",
                "sampleSequence": 1,
                "profile": "high",
                "lifecycle": "foreground",
                "fields": 7,
                "cadenceMs": 1002,
                "accuracy": "lte10",
                "timestampAge": "fresh",
                "displacement": "5_20",
                "headingDelta": "0_30",
            ]],
        ])

        let result = try G2DiagnosticExport.redactedArchive(from: source, exportedAtMs: 200)
        let archive = try JSONSerialization.jsonObject(with: result) as? [String: Any]
        let events = archive?["events"] as? [[String: Any]]
        let state = events?.first
        let event = events?.last
        XCTAssertEqual(archive?["schema"] as? String, "joat.g2-diagnostic-export.v2")
        XCTAssertEqual(state?["outcome"] as? String, "success")
        XCTAssertEqual(event?["accuracy"] as? String, "lte10")
        XCTAssertNil(event?["latitude"])
        XCTAssertNil(event?["longitude"])
    }

    func testExportStillAcceptsLegacyV1Receipts() throws {
        let source = try JSONSerialization.data(withJSONObject: [
            "schema": "joat.g2-diagnostic.v1",
            "events": [[
                "sequence": 1,
                "recordedAtMs": 100,
                "type": "gesture",
                "gesture": "long_press",
                "delivery": "sys",
            ]],
        ])

        let result = try G2DiagnosticExport.redactedArchive(from: source, exportedAtMs: 200)
        let archive = try JSONSerialization.jsonObject(with: result) as? [String: Any]
        let event = (archive?["events"] as? [[String: Any]])?.first
        XCTAssertEqual(archive?["schema"] as? String, "joat.g2-diagnostic-export.v2")
        XCTAssertEqual(event?["gesture"] as? String, "long_press")
    }
}
