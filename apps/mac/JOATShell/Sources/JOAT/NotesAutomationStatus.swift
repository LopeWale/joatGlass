import AppKit
import ApplicationServices

/// Automation (Apple Events → Notes) readiness for QuickNote export.
///
/// "Send the last note to my notes" runs `osascript` from the engine, which
/// is a child `Process` of JOAT.app — macOS attributes its Apple events to
/// this app as the responsible process. Preflighting the SAME decision here
/// means the wearer grants once, from a visible button, instead of being
/// surprised by a mid-export prompt triggered from the glasses.
enum NotesAutomationState: Equatable, Sendable {
    case granted
    case denied
    case notDetermined
    case notesNotRunning
    case unavailable

    var label: String {
        switch self {
        case .granted: "Granted"
        case .denied: "Denied"
        case .notDetermined: "Not determined"
        case .notesNotRunning: "Open Notes to check"
        case .unavailable: "Unavailable"
        }
    }
}

struct NotesAutomationProbe: Sendable {
    static let notesBundleID = "com.apple.Notes"

    /// Injectable for tests; production sends one permission-determination
    /// Apple event and never a content-bearing one.
    var determine: @Sendable (_ askUserIfNeeded: Bool) -> OSStatus = systemDetermine(askUserIfNeeded:)

    func state(askUserIfNeeded: Bool = false) -> NotesAutomationState {
        Self.state(for: determine(askUserIfNeeded))
    }

    static func state(for status: OSStatus) -> NotesAutomationState {
        switch Int(status) {
        case 0: .granted
        case -1743: .denied            // errAEEventNotPermitted
        case -1744: .notDetermined     // errAEEventWouldRequireUserConsent
        case -600: .notesNotRunning    // procNotFound — determination needs a live target
        default: .unavailable
        }
    }

    @Sendable static func systemDetermine(askUserIfNeeded: Bool) -> OSStatus {
        var target = AEAddressDesc()
        let created = notesBundleID.utf8CString.withUnsafeBufferPointer { buffer in
            AECreateDesc(
                typeApplicationBundleID,
                buffer.baseAddress,
                buffer.count - 1,  // exclude the trailing NUL
                &target
            )
        }
        guard created == noErr else { return OSStatus(created) }
        defer { AEDisposeDesc(&target) }
        return AEDeterminePermissionToAutomateTarget(
            &target,
            AEEventClass(typeWildCard),
            AEEventID(typeWildCard),
            askUserIfNeeded
        )
    }

    /// The determination API needs Notes RUNNING. Launch it quietly in the
    /// background, then ask — the consent prompt is attributed to JOAT.
    static func requestAccess(
        probe: NotesAutomationProbe = NotesAutomationProbe()
    ) async -> NotesAutomationState {
        guard let url = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: notesBundleID
        ) else { return .unavailable }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: configuration)
        // Give a cold-launched Notes a moment to register with the Apple
        // Event system before determination; a too-early ask reports -600.
        try? await Task.sleep(nanoseconds: 700_000_000)
        let ask = probe
        return await Task.detached(priority: .userInitiated) {
            ask.state(askUserIfNeeded: true)
        }.value
    }
}
