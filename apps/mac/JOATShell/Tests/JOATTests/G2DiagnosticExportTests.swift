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
        XCTAssertEqual(archive?["schema"] as? String, "joat.g2-diagnostic-export.v1")
        XCTAssertEqual(event?["gesture"] as? String, "long_press")
        XCTAssertNil(event?["pixels"])
    }
}
