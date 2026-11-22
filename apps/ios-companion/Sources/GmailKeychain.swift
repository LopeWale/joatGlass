import Foundation
import Security
import Combine

/// Metadata for a connected Gmail or Google Workspace account.
/// Account metadata lives in UserDefaults (`joat.gmail.accounts.v1`), while
/// the 16-character normalized App Password lives exclusively in iOS Keychain
/// (`service: "com.joat.brain.gmail"`).
struct GmailAccount: Codable, Identifiable, Equatable {
    var id: String
    var label: String
    var email: String
    var displayName: String
    var enabled: Bool
    var isDefault: Bool
    var addedAt: Date
}

enum GmailKeychainError: LocalizedError, Equatable {
    case invalidEmail
    case invalidAppPassword
    case keychainFailure(OSStatus)

    var errorDescription: String? {
        switch self {
        case .invalidEmail:
            return "Enter a valid Gmail or Google Workspace email address."
        case .invalidAppPassword:
            return "Enter your 16-character Google App Password."
        case .keychainFailure(let status):
            return "Could not save the App Password in Keychain (error \(status))."
        }
    }
}

final class GmailAccountStore: ObservableObject {
    static let shared = GmailAccountStore()
    static let defaultsKey = "joat.gmail.accounts.v1"
    static let keychainService = "com.joat.brain.gmail"

    @Published private(set) var accounts: [GmailAccount] = []

    var enabledAccounts: [GmailAccount] {
        accounts.filter(\.enabled)
    }

    var defaultAccount: GmailAccount? {
        enabledAccounts.first(where: \.isDefault)
            ?? enabledAccounts.first
            ?? accounts.first(where: \.isDefault)
            ?? accounts.first
    }

    private let defaults: UserDefaults
    private let service: String

    init(defaults: UserDefaults = .standard, service: String = GmailAccountStore.keychainService) {
        self.defaults = defaults
        self.service = service
        if let data = defaults.data(forKey: Self.defaultsKey) {
            do {
                self.accounts = try JSONDecoder().decode([GmailAccount].self, from: data)
            } catch {
                defaults.set(data, forKey: Self.defaultsKey + ".unreadable")
                self.accounts = []
            }
        }
        ensureDefaultInvariant()
    }

    /// Strips all whitespace and `-` characters from a pasted Google App Password.
    static func normalizeAppPassword(_ raw: String) -> String {
        String(raw.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) && $0 != "-" })
    }

    /// Derives a concise default tag when the user leaves the label blank.
    static func defaultLabel(for email: String) -> String {
        let parts = email.lowercased().split(separator: "@", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return "Gmail" }
        let local = parts[0]
        let domain = parts[1]
        if domain == "gmail.com" || domain == "googlemail.com" {
            return local.isEmpty ? "Personal" : local
        }
        let stem = domain.split(separator: ".").first.map(String.init) ?? domain
        guard !stem.isEmpty else { return local.isEmpty ? "Work" : local }
        return stem.prefix(1).uppercased() + stem.dropFirst()
    }

    static func isValidEmail(_ email: String) -> Bool {
        let clean = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, !clean.contains(where: \.isWhitespace) else { return false }
        let parts = clean.split(separator: "@", omittingEmptySubsequences: false)
        guard parts.count == 2 else { return false }
        let local = parts[0], domain = parts[1]
        guard !local.isEmpty, domain.contains("."), !domain.hasPrefix("."), !domain.hasSuffix(".") else {
            return false
        }
        return true
    }

    func appPassword(for accountId: String) -> String? {
        guard !accountId.isEmpty else { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountId,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data,
              let password = String(data: data, encoding: .utf8), !password.isEmpty else {
            return nil
        }
        return password
    }

    @discardableResult
    func addOrUpdate(
        email: String,
        label: String,
        displayName: String,
        appPassword: String,
        makeDefault: Bool = false
    ) throws -> GmailAccount {
        let cleanEmail = email.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard Self.isValidEmail(cleanEmail) else {
            throw GmailKeychainError.invalidEmail
        }

        let normalizedPassword = Self.normalizeAppPassword(appPassword)
        let existingIndex = accounts.firstIndex(where: { $0.email.lowercased() == cleanEmail })
        let accountId = existingIndex.map { accounts[$0].id } ?? UUID().uuidString.lowercased()

        if normalizedPassword.isEmpty {
            guard existingIndex != nil, self.appPassword(for: accountId) != nil else {
                throw GmailKeychainError.invalidAppPassword
            }
        } else {
            try saveKeychainPassword(normalizedPassword, for: accountId)
        }

        let trimmedLabel = label.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedLabel: String
        if !trimmedLabel.isEmpty {
            resolvedLabel = trimmedLabel
        } else if let idx = existingIndex, !accounts[idx].label.isEmpty {
            resolvedLabel = accounts[idx].label
        } else {
            resolvedLabel = Self.defaultLabel(for: cleanEmail)
        }
        let cleanDisplayName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        let shouldBeDefault = makeDefault || accounts.isEmpty || (existingIndex.map { accounts[$0].isDefault } ?? false)

        if shouldBeDefault {
            for i in accounts.indices {
                accounts[i].isDefault = false
            }
        }

        let saved: GmailAccount
        if let idx = existingIndex {
            accounts[idx].label = resolvedLabel
            accounts[idx].email = cleanEmail
            accounts[idx].displayName = cleanDisplayName
            accounts[idx].enabled = true
            if shouldBeDefault { accounts[idx].isDefault = true }
            saved = accounts[idx]
        } else {
            let fresh = GmailAccount(
                id: accountId,
                label: resolvedLabel,
                email: cleanEmail,
                displayName: cleanDisplayName,
                enabled: true,
                isDefault: shouldBeDefault,
                addedAt: Date()
            )
            accounts.append(fresh)
            saved = fresh
        }

        ensureDefaultInvariant()
        persist()
        return accounts.first(where: { $0.id == saved.id }) ?? saved
    }

    func setEnabled(_ id: String, _ enabled: Bool) {
        guard let idx = accounts.firstIndex(where: { $0.id == id }) else { return }
        accounts[idx].enabled = enabled
        ensureDefaultInvariant()
        persist()
    }

    func setDefault(_ id: String) {
        guard accounts.contains(where: { $0.id == id }) else { return }
        for i in accounts.indices {
            accounts[i].isDefault = (accounts[i].id == id)
        }
        persist()
    }

    func remove(_ id: String) {
        accounts.removeAll { $0.id == id }
        deleteKeychainPassword(for: id)
        defaults.removeObject(forKey: "joat.gmail.sync.\(id)")
        ensureDefaultInvariant()
        persist()
    }

    // MARK: - Persistence & Keychain Helpers

    private func ensureDefaultInvariant() {
        guard !accounts.isEmpty else { return }
        let defaultsCount = accounts.filter(\.isDefault).count
        if defaultsCount == 0 {
            if let firstEnabled = accounts.firstIndex(where: \.enabled) {
                accounts[firstEnabled].isDefault = true
            } else {
                accounts[0].isDefault = true
            }
        } else if defaultsCount > 1 {
            var seen = false
            for i in accounts.indices where accounts[i].isDefault {
                if seen {
                    accounts[i].isDefault = false
                } else {
                    seen = true
                }
            }
        }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(accounts) {
            defaults.set(data, forKey: Self.defaultsKey)
        }
    }

    private func saveKeychainPassword(_ password: String, for accountId: String) throws {
        let data = Data(password.utf8)
        let matchQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountId,
        ]
        SecItemDelete(matchQuery as CFDictionary)

        var addQuery = matchQuery
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(addQuery as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw GmailKeychainError.keychainFailure(status)
        }
    }

    private func deleteKeychainPassword(for accountId: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountId,
        ]
        SecItemDelete(query as CFDictionary)
    }

    #if DEBUG
    func debugSetAccounts(_ seeded: [GmailAccount]) {
        self.accounts = seeded
    }
    #endif
}
