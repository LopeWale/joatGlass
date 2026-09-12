import AppKit
import CryptoKit
import Foundation
import Security
import SwiftUI

enum A2AProvider: String, Codable, CaseIterable, Sendable {
    case openclaw, hermes, generic

    var label: String {
        switch self {
        case .openclaw: "OpenClaw"
        case .hermes: "Hermes"
        case .generic: "Other A2A agent"
        }
    }

    static func legacy(_ identity: String) -> Self {
        for provider in [Self.openclaw, .hermes] {
            if identity == provider.rawValue || (2...16).contains(where: { identity == "\(provider.rawValue)-\($0)" }) {
                return provider
            }
        }
        return .generic
    }

    var permissionsNotice: String {
        switch self {
        case .openclaw:
            "Tool access follows the remote OpenClaw agent. Approve tool requests in its Control UI; this peer token cannot grant operator approval."
        case .hermes:
            "Tool access follows the remote Hermes configuration and peer trust. Manage permissions on Hermes; A2A does not expose tool approval controls."
        case .generic:
            "Tool permissions are managed by the remote agent. This connection has no negotiated tool approval interface."
        }
    }
}

struct A2AConnectionProfile: Codable, Equatable, Sendable {
    let id: String
    let label: String
    let endpoint: String
    let enabled: Bool
    let provider: A2AProvider

    init(id: String, label: String, endpoint: String, enabled: Bool, provider: A2AProvider? = nil) throws {
        self.id = try A2AConnectionConfiguration.validateProfileID(id)
        self.label = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !self.label.isEmpty, self.label.count <= 32,
              !self.label.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw A2AConnectionConfiguration.ConfigurationError.invalidPayload
        }
        self.endpoint = try A2AConnectionConfiguration.canonicalEndpoint(endpoint)
        self.enabled = enabled
        self.provider = provider ?? A2AProvider.legacy(self.id)
    }

    enum CodingKeys: String, CodingKey {
        case id, label, endpoint, enabled, provider
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawID = try container.decode(String.self, forKey: .id)
        let rawLabel = try container.decode(String.self, forKey: .label)
        let rawEndpoint = try container.decode(String.self, forKey: .endpoint)
        let rawEnabled = try container.decode(Bool.self, forKey: .enabled)
        let provider = try container.decodeIfPresent(A2AProvider.self, forKey: .provider)
        try self.init(id: rawID, label: rawLabel, endpoint: rawEndpoint, enabled: rawEnabled, provider: provider)
    }
}

struct A2AConnectionsPayload: Codable, Sendable {
    let profiles: [A2AConnectionProfile]
}

struct A2ATokenLoadRequest: Decodable, Sendable {
    let profileID: String
    let endpoint: String

    enum CodingKeys: String, CodingKey {
        case profileID = "profileId"
        case endpoint
    }
}

struct A2ATokenDTO: Encodable, Sendable {
    let token: String?
}

protocol A2ASecretStoring: Sendable {
    func load(account: String) throws -> Data?
    func save(_ data: Data, account: String) throws
}

struct KeychainA2ASecretStore: A2ASecretStoring {
    private let service = "com.joat.a2a"

    func load(account: String) throws -> Data? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecMatchLimit: kSecMatchLimitOne,
            kSecReturnData: true,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw NativeBridgeError(
                "a2a_keychain_unavailable",
                "A2A credentials are unavailable in Keychain"
            )
        }
        return data
    }

    func save(_ data: Data, account: String) throws {
        let lookup: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
        ]
        let update: [CFString: Any] = [kSecValueData: data]
        let updateStatus = SecItemUpdate(lookup as CFDictionary, update as CFDictionary)
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw NativeBridgeError(
                "a2a_keychain_unavailable",
                "A2A credentials could not be updated in Keychain"
            )
        }

        var insert = lookup
        insert[kSecValueData] = data
        insert[kSecAttrAccessible] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        guard SecItemAdd(insert as CFDictionary, nil) == errSecSuccess else {
            throw NativeBridgeError(
                "a2a_keychain_unavailable",
                "A2A credentials could not be stored in Keychain"
            )
        }
    }
}

protocol A2ATokenServicing: Sendable {
    func loadToken(profileID: String, endpoint: String) throws -> String?
    func storeToken(profileID: String, endpoint: String, token: String) throws
}

struct A2AConnectionConfiguration {
    enum ConfigurationError: LocalizedError {
        case invalidProfileID
        case invalidEndpoint
        case profileCountInvalid
        case duplicateProfileID
        case duplicateEndpoint
        case invalidPayload
        case unsafeDestination
        case writeFailed

        var errorDescription: String? {
            switch self {
            case .invalidProfileID:
                "A2A connection IDs must use a letter followed by up to 31 letters, numbers, underscores or hyphens."
            case .invalidEndpoint:
                "A2A endpoints must be HTTPS URLs without user, query, or fragment."
            case .profileCountInvalid:
                "Configure between 0 and 16 A2A profiles."
            case .duplicateProfileID:
                "A2A profile IDs must be unique."
            case .duplicateEndpoint:
                "A2A endpoints must be unique."
            case .invalidPayload:
                "The existing A2A configuration is invalid."
            case .unsafeDestination:
                "JOAT will not overwrite a non-file or symbolic-link A2A configuration."
            case .writeFailed:
                "JOAT could not save the A2A configuration."
            }
        }
    }

    static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT", isDirectory: true)
            .appendingPathComponent("a2a.json", isDirectory: false)
    }

    static func validateProfileID(_ value: String) throws -> String {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let pattern = "^[a-z][a-z0-9_-]{0,31}$"
        guard normalized.count <= 32,
              (try? NSRegularExpression(pattern: pattern))?.firstMatch(
                in: normalized,
                range: NSRange(location: 0, length: normalized.count)
              ) != nil else {
            throw ConfigurationError.invalidProfileID
        }
        return normalized
    }

    static func canonicalEndpoint(_ value: String) throws -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.utf8.count <= 2048, !trimmed.contains("\\"),
              trimmed.unicodeScalars.allSatisfy({
                  !CharacterSet.whitespacesAndNewlines.contains($0) && !CharacterSet.controlCharacters.contains($0)
              }) else {
            throw ConfigurationError.invalidEndpoint
        }
        guard let components = URLComponents(string: trimmed),
              let rawScheme = components.scheme,
              let host = components.host,
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil else {
            throw ConfigurationError.invalidEndpoint
        }
        let scheme = rawScheme.lowercased()
        guard scheme == "https" else { throw ConfigurationError.invalidEndpoint }

        var hostname = host.lowercased()
        while hostname.hasSuffix(".") { hostname.removeLast() }
        guard !hostname.isEmpty else { throw ConfigurationError.invalidEndpoint }

        let authorityHost = hostname.contains(":") ? "[\(hostname)]" : hostname
        let authority = if let port = components.port, port != 443 {
            "\(authorityHost):\(port)"
        } else {
            authorityHost
        }

        if let port = components.port, !(1...65535).contains(port) { throw ConfigurationError.invalidEndpoint }
        // Preserve the API's encoded path and trailing slash. They can be
        // routing-significant, and redirects must not carry credentials.
        let normalizedPath = components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath
        return "\(scheme)://\(authority)\(normalizedPath)"
    }

    static func load(
        from url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws -> [A2AConnectionProfile] {
        guard (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) == nil else {
            throw ConfigurationError.unsafeDestination
        }
        guard fileManager.fileExists(atPath: url.path) else { return [] }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey])
        let attributes = try fileManager.attributesOfItem(atPath: url.path)
        guard values.isRegularFile == true,
              let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue,
              permissions & 0o077 == 0 else {
            throw ConfigurationError.unsafeDestination
        }

        let data = try Data(contentsOf: url)
        guard data.count >= 2, data.count <= 65_536,
              let container = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ConfigurationError.invalidPayload
        }
        guard Set(container.keys) == ["profiles"], let rawProfiles = container["profiles"] as? [[String: Any]] else {
            throw ConfigurationError.invalidPayload
        }
        let profiles = try rawProfiles.map { item -> A2AConnectionProfile in
            let required: Set<String> = ["id", "label", "endpoint", "enabled"]
            guard required.isSubset(of: Set(item.keys)),
                  Set(item.keys).subtracting(required).isSubset(of: ["provider"]),
                  let id = item["id"] as? String,
                  let label = item["label"] as? String,
                  let endpoint = item["endpoint"] as? String,
                  let flag = item["enabled"] as? NSNumber,
                  CFGetTypeID(flag) == CFBooleanGetTypeID() else {
                throw ConfigurationError.invalidPayload
            }
            let enabled = flag.boolValue
            var provider: A2AProvider?
            if let rawProvider = item["provider"] {
                guard let value = rawProvider as? String, let parsed = A2AProvider(rawValue: value) else {
                    throw ConfigurationError.invalidPayload
                }
                provider = parsed
            }
            return try A2AConnectionProfile(id: id, label: label, endpoint: endpoint, enabled: enabled, provider: provider)
        }
        try validate(profiles)
        return profiles
    }

    static func save(
        _ profiles: [A2AConnectionProfile],
        to url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws {
        try validate(profiles)

        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)

        if (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) != nil {
            throw ConfigurationError.unsafeDestination
        }
        let destinationExists = fileManager.fileExists(atPath: url.path)
        if destinationExists {
            let existing = try url.resourceValues(forKeys: [.isRegularFileKey])
            guard existing.isRegularFile == true else { throw ConfigurationError.unsafeDestination }
        }

        let data = try JSONEncoder().encode(A2AConnectionsPayload(profiles: profiles))
        let temporaryURL = directory.appendingPathComponent(".a2a.json.\(UUID().uuidString)", isDirectory: false)
        var temporaryExists = false
        defer {
            if temporaryExists { try? fileManager.removeItem(at: temporaryURL) }
        }
        guard fileManager.createFile(atPath: temporaryURL.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
            throw ConfigurationError.writeFailed
        }
        temporaryExists = true

        if destinationExists {
            _ = try fileManager.replaceItemAt(
                url,
                withItemAt: temporaryURL,
                backupItemName: nil,
                options: [.usingNewMetadataOnly]
            )
        } else {
            try fileManager.moveItem(at: temporaryURL, to: url)
        }
        temporaryExists = false
        try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func validate(_ profiles: [A2AConnectionProfile]) throws {
        guard (0...16).contains(profiles.count) else { throw ConfigurationError.profileCountInvalid }
        let ids = profiles.map(\.id)
        guard Set(ids).count == ids.count else { throw ConfigurationError.duplicateProfileID }
        let endpoints = profiles.map(\.endpoint)
        guard Set(endpoints).count == endpoints.count else { throw ConfigurationError.duplicateEndpoint }
        guard Set(profiles.map { $0.label.lowercased() }).count == profiles.count else { throw ConfigurationError.invalidPayload }
    }

    static func tokenAccount(endpoint: String) -> String {
        let digest = SHA256.hash(data: Data(endpoint.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}

struct A2ACredentialService: A2ATokenServicing {
    private static let maximumTokenBytes = 8_192

    private let secretStore: any A2ASecretStoring
    private let configURL: URL

    init(
        secretStore: any A2ASecretStoring = KeychainA2ASecretStore(),
        configURL: URL = A2AConnectionConfiguration.defaultURL
    ) {
        self.secretStore = secretStore
        self.configURL = configURL
    }

    func loadToken(profileID: String, endpoint: String) throws -> String? {
        let canonicalProfileID = try A2AConnectionConfiguration.validateProfileID(profileID)
        let canonicalEndpoint = try A2AConnectionConfiguration.canonicalEndpoint(endpoint)
        let profiles = try A2AConnectionConfiguration.load(from: configURL)
        guard profileID == canonicalProfileID, endpoint == canonicalEndpoint,
              profiles.contains(where: { $0.id == profileID && $0.endpoint == endpoint && $0.enabled }) else {
            return nil
        }

        guard let data = try secretStore.load(account: A2AConnectionConfiguration.tokenAccount(endpoint: canonicalEndpoint)) else {
            return nil
        }
        guard let token = String(data: data, encoding: .utf8),
              !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              token.utf8.count <= Self.maximumTokenBytes,
              !token.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) || CharacterSet.controlCharacters.contains($0) }) else {
            throw NativeBridgeError(
                "a2a_token_invalid",
                "Stored A2A credential data is invalid"
            )
        }
        return token
    }

    func storeToken(profileID: String, endpoint: String, token: String) throws {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.utf8.count <= Self.maximumTokenBytes,
              !trimmed.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) || CharacterSet.controlCharacters.contains($0) }) else {
            throw NativeBridgeError("a2a_token_invalid", "A2A token cannot be empty or too large")
        }

        let canonicalProfileID = try A2AConnectionConfiguration.validateProfileID(profileID)
        let canonicalEndpoint = try A2AConnectionConfiguration.canonicalEndpoint(endpoint)
        let profiles = try A2AConnectionConfiguration.load(from: configURL)
        guard profiles.contains(where: { $0.id == canonicalProfileID && $0.endpoint == canonicalEndpoint && $0.enabled }) else {
            throw NativeBridgeError("a2a_profile_not_configured", "A2A profile is not enabled and saved")
        }

        try secretStore.save(Data(trimmed.utf8), account: A2AConnectionConfiguration.tokenAccount(endpoint: canonicalEndpoint))
    }
}

@MainActor
@Observable
final class A2AConnectionsModel {
    struct Draft: Identifiable, Equatable {
        let id: UUID
        var profileID: String
        var label: String
        var endpoint: String
        var enabled: Bool
        var provider: A2AProvider
        var token = ""
        init(profileID: String, label: String, endpoint: String, enabled: Bool = true, provider: A2AProvider? = nil) {
            id = UUID(); self.profileID = profileID; self.label = label
            self.endpoint = endpoint; self.enabled = enabled
            self.provider = provider ?? A2AProvider.legacy(profileID)
        }
    }

    static let shared = A2AConnectionsModel()
    var drafts: [Draft] = []
    private(set) var notice: String?
    private(set) var failed = false
    private(set) var loadFailed = false
    private(set) var savedAt: Date?
    private var savedProfiles: [A2AConnectionProfile] = []
    private let url: URL
    private let credentials: any A2ATokenServicing

    init(url: URL = A2AConnectionConfiguration.defaultURL, credentials: (any A2ATokenServicing)? = nil) {
        self.url = url
        self.credentials = credentials ?? A2ACredentialService(configURL: url)
        reload()
    }

    var parsed: [A2AConnectionProfile]? {
        guard let profiles = try? drafts.map({ try A2AConnectionProfile(
            id: $0.profileID, label: $0.label, endpoint: $0.endpoint, enabled: $0.enabled, provider: $0.provider
        ) }), (try? A2AConnectionConfiguration.validate(profiles)) != nil else { return nil }
        return profiles
    }
    var canPersist: Bool {
        !loadFailed && parsed != nil && (parsed != savedProfiles || drafts.contains { !$0.token.isEmpty })
    }
    func requiresRestart(after lastStart: Date?) -> Bool {
        guard let savedAt else { return false }
        return lastStart == nil || lastStart! < savedAt
    }
    func add(_ kind: String) {
        guard drafts.count < 16, !loadFailed else { return }
        let id = (1...16).map { $0 == 1 ? kind : "\(kind)-\($0)" }
            .first { candidate in !drafts.contains { $0.profileID == candidate } }!
        let name = kind == "openclaw" ? "OpenClaw" : "Hermes"
        let label = id == kind ? name : "\(name) \(id.split(separator: "-").last!)"
        let endpoint = kind == "openclaw"
            ? "https://admins-mac-mini.tail3a3363.ts.net:8443/a2a/v1"
            : "https://admins-mac-mini.tail3a3363.ts.net:9900/"
        drafts.append(Draft(profileID: id, label: label, endpoint: endpoint))
    }
    func remove(_ id: UUID) { drafts.removeAll { $0.id == id } }
    func reload() {
        do {
            savedProfiles = try A2AConnectionConfiguration.load(from: url)
            drafts = savedProfiles.map { Draft(profileID: $0.id, label: $0.label, endpoint: $0.endpoint, enabled: $0.enabled, provider: $0.provider) }
            loadFailed = false; failed = false; notice = nil
        } catch {
            loadFailed = true; failed = true
            notice = "Could not read saved connections. Repair the saved file, then Reload Saved."
        }
    }
    func save() {
        guard canPersist, let profiles = parsed else { return }
        do {
            guard try A2AConnectionConfiguration.load(from: url) == savedProfiles else {
                failed = true; notice = "Connections changed elsewhere. Reload Saved before saving."; return
            }
            // Validate every pending token before changing either persistent owner.
            for draft in drafts where !draft.token.isEmpty {
                guard draft.enabled, draft.token.utf8.count <= 8192,
                      !draft.token.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) || CharacterSet.controlCharacters.contains($0) }) else {
                    failed = true; notice = "Enable the connection and enter a token without spaces or line breaks."; return
                }
            }
            try A2AConnectionConfiguration.save(profiles, to: url)
            savedProfiles = profiles
            savedAt = Date()
            for index in drafts.indices where !drafts[index].token.isEmpty {
                try credentials.storeToken(profileID: profiles[index].id, endpoint: profiles[index].endpoint, token: drafts[index].token)
                drafts[index].token = ""
            }
            failed = false
            notice = "Connection settings saved."
        } catch {
            failed = true
            notice = "Connection or credential save failed. Unsaved tokens remain in the fields; correct the problem and retry."
        }
    }
}

struct A2AConnectionSettings: View {
    @ObservedObject var engine: EngineController
    @State private var model = A2AConnectionsModel.shared
    @State private var status = EngineStatus()
    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Choose OpenClaw or Hermes in Agent Terminal to send instructions and read replies. Credentials stay in this Mac’s Keychain.")
                .font(.caption).foregroundStyle(.secondary)
            if model.drafts.isEmpty { Text("Add an agent connection to get started.").foregroundStyle(.secondary) }
            ForEach($model.drafts) { $draft in
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        TextField("Connection name", text: $draft.label).textFieldStyle(.roundedBorder)
                            .accessibilityLabel("A2A connection name")
                        Toggle("Enabled", isOn: $draft.enabled).toggleStyle(.switch).controlSize(.small)
                        Button("Remove connection", systemImage: "minus.circle") { model.remove(draft.id) }
                            .labelStyle(.iconOnly).buttonStyle(.borderless)
                    }
                    Picker("Provider", selection: $draft.provider) {
                        ForEach(A2AProvider.allCases, id: \.self) { provider in
                            Text(provider.label).tag(provider)
                        }
                    }
                    Text(draft.provider.permissionsNotice).font(.caption).foregroundStyle(.secondary)
                    TextField("HTTPS endpoint", text: $draft.endpoint).textFieldStyle(.roundedBorder)
                        .accessibilityLabel("A2A endpoint")
                    SecureField("Token — leave blank to keep saved credential", text: $draft.token)
                        .textFieldStyle(.roundedBorder).accessibilityLabel("A2A bearer token")
                    Text(connectionStatus(draft)).font(.caption).foregroundStyle(.secondary)
                }.padding(.vertical, 6)
                Divider()
            }
            HStack {
                Menu("Add Agent") {
                    Button("OpenClaw") { model.add("openclaw") }
                    Button("Hermes") { model.add("hermes") }
                }.disabled(model.drafts.count >= 16 || model.loadFailed)
                Spacer()
                Button("Reload Saved") { model.reload() }
                Button("Save Agent Connections") { model.save() }.disabled(!model.canPersist)
            }
            if model.parsed == nil { WorkspaceNotice(text: "Use unique names and HTTPS endpoints without credentials, query strings or fragments.", isError: true) }
            if let notice = model.notice { WorkspaceNotice(text: notice, isError: model.failed) }
            if model.requiresRestart(after: engine.lastStartedAt) && engine.state == .running {
                Button("Restart to Apply Agent Connections") { engine.restart() }.disabled(engine.isTransitioning)
            }
        }.disabled(engine.isPreview)
            .onAppear { status = EngineStatus.load() ?? EngineStatus() }
            .onReceive(tick) { _ in status = EngineStatus.load() ?? EngineStatus() }
    }

    private func connectionStatus(_ draft: A2AConnectionsModel.Draft) -> String {
        guard draft.enabled else { return "Disabled — enable and save to use this agent." }
        guard model.parsed != nil, !model.canPersist, !model.requiresRestart(after: engine.lastStartedAt) else {
            return "Save changes and restart to check this connection."
        }
        guard status.isFresh, engine.state == .running else { return "Engine not reporting." }
        guard let connection = status.a2aConnections[draft.profileID] else { return "Save and restart to make this connection available." }
        switch connection.state {
        case "ready": return "Connected — ready for an instruction."
        case "checking": return "Checking authentication…"
        case "running": return "Working — waiting for the agent’s reply."
        case "input_required": return "The agent needs more input. Continue in Agent Terminal."
        case "auth_required":
            if connection.reason == "auth_required" {
                return "The agent needs additional authorization. " + draft.provider.permissionsNotice
            }
            return "Credential required or rejected. Update its token and retry."
        case "unknown": return "The request’s outcome is unknown. Check the agent before sending again."
        case "cancelled": return "Cancellation confirmed by the agent."
        case "failed": return "Connection or task failed. Open Agent Terminal for recovery details."
        case "closed": return "Connection closed. Open Agent Terminal to reconnect."
        default: return "Select this connection in Agent Terminal to connect."
        }
    }
}
