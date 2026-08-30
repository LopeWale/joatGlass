import Foundation

enum MessagesAccessState: Equatable {
    case granted
    case blocked
    case databaseMissing

    var label: String {
        switch self {
        case .granted: "Granted"
        case .blocked: "Blocked"
        case .databaseMissing: "Messages database not found"
        }
    }
}

/// Checks only whether JOAT's own signed process can open the Messages store.
/// It never queries a table or reads message bytes.
struct MessagesAccessProbe {
    let databaseURL: URL

    init(databaseURL: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Messages/chat.db")) {
        self.databaseURL = databaseURL
    }

    func state() -> MessagesAccessState {
        do {
            let handle = try FileHandle(forReadingFrom: databaseURL)
            try handle.close()
            return .granted
        } catch {
            return Self.state(for: error)
        }
    }

    static func state(for error: Error) -> MessagesAccessState {
        let value = error as NSError
        if value.domain == NSCocoaErrorDomain,
           [CocoaError.fileNoSuchFile.rawValue,
            CocoaError.fileReadNoSuchFile.rawValue].contains(value.code) {
            return .databaseMissing
        }
        return .blocked
    }
}
