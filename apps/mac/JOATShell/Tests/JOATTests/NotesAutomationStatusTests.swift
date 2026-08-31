import XCTest
@testable import JOAT

final class NotesAutomationStatusTests: XCTestCase {
    func testMapsAppleEventDeterminationOntoWearerStates() {
        XCTAssertEqual(NotesAutomationProbe.state(for: 0), .granted)
        XCTAssertEqual(NotesAutomationProbe.state(for: -1743), .denied)
        XCTAssertEqual(NotesAutomationProbe.state(for: -1744), .notDetermined)
        XCTAssertEqual(NotesAutomationProbe.state(for: -600), .notesNotRunning)
        XCTAssertEqual(NotesAutomationProbe.state(for: -1), .unavailable)
    }

    func testInjectedDeterminerSeesTheAskFlagItWasGiven() {
        let probe = NotesAutomationProbe(determine: { askUserIfNeeded in
            askUserIfNeeded ? 0 : OSStatus(-1744)
        })
        XCTAssertEqual(probe.state(), .notDetermined)
        XCTAssertEqual(probe.state(askUserIfNeeded: true), .granted)
    }

    func testLabelsStayWearerReadable() {
        XCTAssertEqual(NotesAutomationState.granted.label, "Granted")
        XCTAssertEqual(NotesAutomationState.notesNotRunning.label, "Open Notes to check")
    }
}
