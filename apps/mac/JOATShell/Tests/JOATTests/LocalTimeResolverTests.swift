import XCTest
@testable import JOAT

final class LocalTimeResolverTests: XCTestCase {
    func testRejectsChicagoSpringDSTGap() {
        let result = LocalTimeResolver.resolve(CivilTime(
            year: 2026, month: 3, day: 8,
            hour: 2, minute: 30, second: 0,
            timeZoneID: "America/Chicago",
            repeatedTimeChoice: nil
        ))

        XCTAssertEqual(result.status, .nonexistent)
        XCTAssertNil(result.chosenUnixMS)
    }

    func testRequiresChoiceForChicagoRepeatedHour() {
        let unresolved = LocalTimeResolver.resolve(CivilTime(
            year: 2026, month: 11, day: 1,
            hour: 1, minute: 30, second: 0,
            timeZoneID: "America/Chicago",
            repeatedTimeChoice: nil
        ))
        let first = LocalTimeResolver.resolve(CivilTime(
            year: 2026, month: 11, day: 1,
            hour: 1, minute: 30, second: 0,
            timeZoneID: "America/Chicago",
            repeatedTimeChoice: .first
        ))
        let last = LocalTimeResolver.resolve(CivilTime(
            year: 2026, month: 11, day: 1,
            hour: 1, minute: 30, second: 0,
            timeZoneID: "America/Chicago",
            repeatedTimeChoice: .last
        ))

        XCTAssertEqual(unresolved.status, .ambiguous)
        XCTAssertNil(unresolved.chosenUnixMS)
        XCTAssertEqual(last.chosenUnixMS! - first.chosenUnixMS!, 3_600_000)
        XCTAssertNotEqual(first.chosenUTCOffsetSeconds, last.chosenUTCOffsetSeconds)
    }

    func testRejectsUnknownTimeZoneInsteadOfFallingBackToMac() {
        let result = LocalTimeResolver.resolve(CivilTime(
            year: 2026, month: 1, day: 2,
            hour: 9, minute: 0, second: 0,
            timeZoneID: "Mars/Olympus",
            repeatedTimeChoice: nil
        ))

        XCTAssertEqual(result.status, .invalidTimeZone)
    }
}
