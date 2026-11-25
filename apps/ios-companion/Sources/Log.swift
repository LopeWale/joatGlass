import Foundation
import os

/// In-memory log mirrored to the unified log (subsystem com.joatlabs.joatbrain, category spike).
/// Stream it from the Mac with:  log stream --device --predicate 'subsystem == "com.joatlabs.joatbrain"'
@MainActor
final class Log: ObservableObject {
    static let shared = Log()
    static let os = Logger(subsystem: "com.joatlabs.joatbrain", category: "spike")

    @Published private(set) var lines: [String] = []
    private let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    /// Documents/brain.log on the phone. Pull it with
    /// `xcrun devicectl device copy from --domain-type appDataContainer
    ///  --domain-identifier com.joat.brain --source Documents/brain.log ...`
    nonisolated static let fileURL = FileManager.default
        .urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("brain.log")
    nonisolated private static let fileQueue = DispatchQueue(label: "com.joatlabs.joatbrain.logfile")
    nonisolated private static let fileStamp: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    nonisolated private static func appendToFile(_ message: String) {
        fileQueue.async {
            let line = "\(fileStamp.string(from: Date())) \(message)\n"
            let fm = FileManager.default
            // Keep one run's worth: start over past 8 MB.
            if let size = (try? fm.attributesOfItem(atPath: fileURL.path))?[.size] as? Int, size > 8_000_000 {
                try? fm.removeItem(at: fileURL)
            }
            if !fm.fileExists(atPath: fileURL.path) { fm.createFile(atPath: fileURL.path, contents: nil) }
            // The throwing calls: the old seekToEndOfFile()/write(_:) raise
            // an Objective-C exception on a full disk, which crashes the app.
            if let h = try? FileHandle(forWritingTo: fileURL) {
                _ = try? h.seekToEnd()
                try? h.write(contentsOf: Data(line.utf8))
                try? h.close()
            }
        }
    }

    nonisolated static func add(_ message: String) {
        os.info("\(message, privacy: .public)")
        appendToFile(message)
        // stderr is unbuffered: `xcrun devicectl device process launch --console`
        // streams it to the Mac over the cable, no root needed.
        FileHandle.standardError.write(Data("[brain] \(message)\n".utf8))
        Task { @MainActor in
            let line = "\(shared.stamp.string(from: Date()))  \(message)"
            shared.lines.append(line)
            if shared.lines.count > 400 { shared.lines.removeFirst(shared.lines.count - 400) }
        }
    }

    /// Exports a `.txt` snapshot of `brain.log` (and in-memory lines) so iOS Share Sheet,
    /// AirDrop, and chat file pickers never reject the `.log` extension as "Invalid Media".
    static func exportShareURL() -> URL {
        fileQueue.sync {}
        let outURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("joat-brain-diagnostics.txt")
        let diskText = (try? String(contentsOf: fileURL, encoding: .utf8)) ?? ""
        let body: String
        if !diskText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            body = diskText
        } else {
            body = shared.lines.joined(separator: "\n") + "\n"
        }
        try? Data(body.utf8).write(to: outURL, options: .atomic)
        return outURL
    }

    /// Full text snapshot for 1-tap clipboard copy in `DiagnosticsView`.
    func snapshotText() -> String {
        Self.fileQueue.sync {}
        if let disk = try? String(contentsOf: Self.fileURL, encoding: .utf8),
           !disk.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            // Bound clipboard copy to the newest ~256 KB so paste stays instant.
            if disk.utf8.count > 262_144 {
                return String(disk.suffix(262_144))
            }
            return disk
        }
        return lines.joined(separator: "\n")
    }

    func clear() { lines.removeAll() }
}
