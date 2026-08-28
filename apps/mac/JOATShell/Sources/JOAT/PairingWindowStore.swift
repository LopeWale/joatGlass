import Foundation

/// Filesystem contract shared with the Python gateway for explicit pairing.
///
/// A marker is durable before the old token is removed. If any step fails,
/// the marker is removed so a partial operation cannot broaden network trust.
struct PairingWindowStore {
    enum StoreError: LocalizedError {
        case invalidDuration

        var errorDescription: String? {
            switch self {
            case .invalidDuration:
                "Pairing must be opened for between 1 and 300 seconds."
            }
        }
    }

    private let directory: URL
    private let now: () -> Date
    private let fileManager: FileManager

    init(
        directory: URL,
        now: @escaping () -> Date = Date.init,
        fileManager: FileManager = .default
    ) {
        self.directory = directory
        self.now = now
        self.fileManager = fileManager
    }

    @discardableResult
    func open(duration: TimeInterval = 120) throws -> Date {
        guard duration > 0, duration <= 300 else {
            throw StoreError.invalidDuration
        }

        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let markerURL = directory.appendingPathComponent("pairing-window.json")
        let tokenURL = directory.appendingPathComponent("pairing.json")
        let expiresAt = now().addingTimeInterval(duration)
        let payload = try JSONSerialization.data(
            withJSONObject: ["expiresAt": expiresAt.timeIntervalSince1970]
        )

        do {
            try payload.write(to: markerURL, options: .atomic)
            try fileManager.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: markerURL.path
            )
            if fileManager.fileExists(atPath: tokenURL.path) {
                try fileManager.removeItem(at: tokenURL)
            }
        } catch {
            try? fileManager.removeItem(at: markerURL)
            throw error
        }
        return expiresAt
    }
}
