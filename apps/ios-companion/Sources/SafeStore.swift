import Foundation

/// Saved data that no longer reads (an older format, a cut-off write) is put
/// aside before anything is written over it. Before, a file that failed to
/// decode loaded as empty and the next save replaced it: every note gone
/// (review, 2026-10-30).
enum SafeStore {
    /// `quicknotes.json` → `quicknotes.unreadable-20261030-091500.json`, beside it.
    static func quarantine(_ url: URL, _ error: Error) {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        let name = url.deletingPathExtension().lastPathComponent
            + ".unreadable-\(f.string(from: Date())).\(url.pathExtension)"
        let aside = url.deletingLastPathComponent().appendingPathComponent(name)
        do {
            try FileManager.default.moveItem(at: url, to: aside)
            Log.add("\(url.lastPathComponent) did not read (\(error)); kept as \(name)")
        } catch {
            Log.add("\(url.lastPathComponent) did not read and could not be kept aside: \(error)")
        }
    }

    /// The same for a value kept in UserDefaults: copied to `<key>.unreadable`.
    static func quarantine(defaultsKey key: String, _ data: Data, _ error: Error) {
        UserDefaults.standard.set(data, forKey: key + ".unreadable")
        Log.add("\(key) did not read (\(error)); kept as \(key).unreadable")
    }
}
