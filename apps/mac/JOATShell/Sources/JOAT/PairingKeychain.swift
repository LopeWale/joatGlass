import Foundation
import Security

/// Where the glasses pairing key is kept.
protocol PairingSecretStore {
    func read() throws -> String?
    func save(_ token: String) throws
    func delete() throws
}

/// The glasses pairing key in the login keychain (2026-10-13).
///
/// It used to be `pairing.json`, which any program running as the wearer
/// could read, and with it take the glasses. An item this app adds with no
/// explicit access object gets the system's default access: only the app
/// that created it may read it without macOS asking the wearer.
///
/// This is the file-based keychain, which the SecItem API targets by default
/// on macOS. The data protection keychain needs keychain access-group
/// entitlements authorized by a provisioning profile, and JOAT.app is signed
/// with Developer ID and no profile.
struct PairingKeychain: PairingSecretStore {
    enum KeychainError: LocalizedError {
        case status(OSStatus)
        case unreadable

        var errorDescription: String? {
            switch self {
            case .status(let status):
                let message = SecCopyErrorMessageString(status, nil) as String?
                return "Keychain error \(status)" + (message.map { ": \($0)" } ?? "")
            case .unreadable:
                return "The keychain item for the glasses key is unreadable."
            }
        }
    }

    let service: String
    let account: String

    init(service: String = "com.joat.shell.pairing", account: String = "glasses") {
        self.service = service
        self.account = account
    }

    private var identity: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    func read() throws -> String? {
        var query = identity
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.status(status) }
        guard let data = result as? Data, let token = String(data: data, encoding: .utf8),
              !token.isEmpty else { throw KeychainError.unreadable }
        return token
    }

    func save(_ token: String) throws {
        let data = Data(token.utf8)
        let updated = SecItemUpdate(
            identity as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw KeychainError.status(updated) }
        var item = identity
        item[kSecValueData as String] = data
        item[kSecAttrLabel as String] = "JOAT glasses pairing key"
        let added = SecItemAdd(item as CFDictionary, nil)
        guard added == errSecSuccess else { throw KeychainError.status(added) }
    }

    func delete() throws {
        let status = SecItemDelete(identity as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.status(status)
        }
    }
}

/// The key, the pairing window, and the one-time move out of pairing.json.
///
/// The engine is given the key over the private pipe this app launched it
/// with (EngineCommandChannel); only this app's Pair Glasses opens a window,
/// and a phone paired in that window comes back here to be kept.
@MainActor
final class PairingKeyHolder {
    enum HolderError: LocalizedError {
        case noWindow
        case notKept

        var errorDescription: String? {
            switch self {
            case .noWindow: "No pairing window was open for that key."
            case .notKept: "The keychain did not keep the glasses key."
            }
        }
    }

    /// The phone's acknowledgement may land just after the window closes.
    static let confirmGrace: TimeInterval = 30
    static let windowDuration: TimeInterval = 120

    private let secrets: PairingSecretStore
    private let directory: URL
    private let now: () -> Date
    private let fileManager: FileManager
    private(set) var windowExpiresAt: Date?

    init(
        secrets: PairingSecretStore = PairingKeychain(),
        directory: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT"),
        now: @escaping () -> Date = Date.init,
        fileManager: FileManager = .default
    ) {
        self.secrets = secrets
        self.directory = directory
        self.now = now
        self.fileManager = fileManager
    }

    /// Move pairing.json into the keychain, once, then remove the files.
    ///
    /// Only an empty keychain takes the file's key: once the keychain holds
    /// one, a pairing.json that reappears is never trusted, since any program
    /// running as the wearer can write it. The window marker is retired too;
    /// a window now opens only from this app.
    func migrateLegacyFile() throws {
        let tokenURL = directory.appendingPathComponent("pairing.json")
        let windowURL = directory.appendingPathComponent("pairing-window.json")
        if try secrets.read() == nil,
           let data = try? Data(contentsOf: tokenURL),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let token = object["token"] as? String, !token.isEmpty {
            try secrets.save(token)
            guard try secrets.read() == token else { throw HolderError.notKept }
        }
        if fileManager.fileExists(atPath: tokenURL.path) {
            try fileManager.removeItem(at: tokenURL)
        }
        if fileManager.fileExists(atPath: windowURL.path) {
            try fileManager.removeItem(at: windowURL)
        }
    }

    /// The key, or no key and the window if one is still open.
    func state() throws -> (token: String?, windowExpiresAt: Date?) {
        if let token = try secrets.read() {
            windowExpiresAt = nil
            return (token, nil)
        }
        if let expires = windowExpiresAt, expires <= now() {
            windowExpiresAt = nil
        }
        return (nil, windowExpiresAt)
    }

    /// Pair / Replace Glasses: forget the key and open a short window.
    func openWindow() throws -> Date {
        try secrets.delete()
        let expires = now().addingTimeInterval(Self.windowDuration)
        windowExpiresAt = expires
        return expires
    }

    /// Keep the key of the phone that paired during this app's window.
    func keep(_ token: String) throws {
        guard let expires = windowExpiresAt,
              now() <= expires.addingTimeInterval(Self.confirmGrace) else {
            throw HolderError.noWindow
        }
        try secrets.save(token)
        guard try secrets.read() == token else { throw HolderError.notKept }
        windowExpiresAt = nil
    }
}
