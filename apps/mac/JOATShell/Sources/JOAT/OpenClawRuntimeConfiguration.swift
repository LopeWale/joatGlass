import Darwin
import Foundation

/// The small, secret-free binding the Python engine reads before it opens an
/// authenticated Agent Terminal. Pairing material never belongs in this file:
/// the app-owned Keychain bridge binds it to the canonical gateway profile.
struct OpenClawRuntimeConfiguration: Equatable, Sendable {
    enum ConfigurationError: LocalizedError {
        case invalidGatewayOrigin
        case remoteGatewayRequiresTLS
        case hermesDashboard
        case invalidFile
        case unsafeDestination
        case writeFailed
        case invalidHostList

        var errorDescription: String? {
            switch self {
            case .invalidGatewayOrigin:
                "Enter a WebSocket Gateway origin with no path, query, or credentials."
            case .remoteGatewayRequiresTLS:
                "A remote Hermes Gateway must use wss://."
            case .hermesDashboard:
                "Port 9443 is Hermes’s control dashboard, not the authenticated Gateway."
            case .invalidFile:
                "The existing Hermes Gateway configuration is invalid."
            case .unsafeDestination:
                "JOAT will not overwrite a non-file or symbolic-link configuration."
            case .writeFailed:
                "JOAT could not save the Hermes Gateway configuration."
            case .invalidHostList:
                "Configure between one and sixteen distinct Gateways."
            }
        }
    }

    static let hermesAgentID = "hermes"
    static let hermesControlDashboardPort = 9443

    static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/JOAT", isDirectory: true)
            .appendingPathComponent("openclaw.json", isDirectory: false)
    }

    let gatewayURL: String
    let agentID: String

    init(gatewayURL: String) throws {
        self.gatewayURL = try Self.canonicalGatewayURL(gatewayURL)
        self.agentID = Self.hermesAgentID
    }

    static func load(
        from url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws -> OpenClawRuntimeConfiguration? {
        try loadAll(from: url, fileManager: fileManager).first
    }

    /// Matches OpenClawRuntimeConfig.load_all on the engine. A Mac editor
    /// must retain the complete roster, not overwrite it with its first host.
    static func loadAll(
        from url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws -> [OpenClawRuntimeConfiguration] {
        guard (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) == nil else {
            throw ConfigurationError.invalidFile
        }
        guard fileManager.fileExists(atPath: url.path) else { return [] }
        let resourceValues = try url.resourceValues(forKeys: [.isRegularFileKey])
        let attributes = try fileManager.attributesOfItem(atPath: url.path)
        let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue
        let ownerID = (attributes[.ownerAccountID] as? NSNumber)?.uint32Value
        guard resourceValues.isRegularFile == true,
              let permissions,
              permissions & 0o077 == 0,
              ownerID == getuid()
        else {
            throw ConfigurationError.invalidFile
        }
        let data = try Data(contentsOf: url)
        guard data.count >= 2, data.count <= 65_536,
              let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            throw ConfigurationError.invalidFile
        }
        let entries: [[String: Any]]
        if Set(raw.keys) == ["hosts"], let hosts = raw["hosts"] as? [[String: Any]] {
            entries = hosts
        } else if Set(raw.keys) == ["gateway_url", "agent_id"] {
            entries = [raw]
        } else { throw ConfigurationError.invalidFile }
        let configurations = try entries.map { entry in
            guard Set(entry.keys) == ["gateway_url", "agent_id"],
                  entry["agent_id"] as? String == hermesAgentID,
                  let gatewayURL = entry["gateway_url"] as? String else { throw ConfigurationError.invalidFile }
            return try OpenClawRuntimeConfiguration(gatewayURL: gatewayURL)
        }
        try validateHosts(configurations)
        return configurations
    }

    /// Save by replacement so the Python process observes either the previous
    /// valid config or this complete, owner-only config—never a partial write.
    func save(
        to url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws {
        // Legacy single-host callers may update a single host, but cannot
        // silently collapse a multi-Mac roster.
        if let existing = try? Self.loadAll(from: url, fileManager: fileManager), existing.count > 1 {
            throw ConfigurationError.invalidHostList
        }
        try Self.saveAll([self], to: url, fileManager: fileManager)
    }

    static func saveAll(
        _ configurations: [OpenClawRuntimeConfiguration],
        to url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws {
        try validateHosts(configurations)
        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fileManager.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: directory.path
        )

        if (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) != nil {
            throw ConfigurationError.unsafeDestination
        }
        let destinationExists = fileManager.fileExists(atPath: url.path)
        if destinationExists {
            let values = try url.resourceValues(forKeys: [.isRegularFileKey])
            guard values.isRegularFile == true else {
                throw ConfigurationError.unsafeDestination
            }
        }

        let entries = configurations.map { ["gateway_url": $0.gatewayURL, "agent_id": $0.agentID] }
        let payload: [String: Any] = entries.count == 1 ? entries[0] : ["hosts": entries]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        let temporaryURL = directory.appendingPathComponent(
            ".openclaw.json.\(UUID().uuidString)",
            isDirectory: false
        )
        var temporaryFileExists = false
        defer {
            if temporaryFileExists {
                try? fileManager.removeItem(at: temporaryURL)
            }
        }
        guard fileManager.createFile(
            atPath: temporaryURL.path,
            contents: data,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw ConfigurationError.writeFailed
        }
        temporaryFileExists = true
        try fileManager.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: temporaryURL.path
        )

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
        temporaryFileExists = false
        try fileManager.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: url.path
        )
    }

    private static func validateHosts(_ configurations: [OpenClawRuntimeConfiguration]) throws {
        guard (1...16).contains(configurations.count),
              Set(configurations.map(\.gatewayURL)).count == configurations.count else {
            throw ConfigurationError.invalidHostList
        }
    }

    private static func canonicalGatewayURL(_ rawValue: String) throws -> String {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: value),
              let rawScheme = components.scheme,
              let rawHost = components.host,
              components.user == nil,
              components.password == nil,
              components.percentEncodedPath.isEmpty || components.percentEncodedPath == "/",
              components.query == nil,
              components.fragment == nil
        else {
            throw ConfigurationError.invalidGatewayOrigin
        }

        let scheme = rawScheme.lowercased()
        var hostname = rawHost.lowercased()
        while hostname.hasSuffix(".") {
            hostname.removeLast()
        }
        guard !hostname.isEmpty, scheme == "ws" || scheme == "wss" else {
            throw ConfigurationError.invalidGatewayOrigin
        }
        let loopback = ["127.0.0.1", "::1", "localhost"].contains(hostname)
        guard scheme == "wss" || loopback else {
            throw ConfigurationError.remoteGatewayRequiresTLS
        }
        if components.port == hermesControlDashboardPort {
            throw ConfigurationError.hermesDashboard
        }

        let defaultPort = scheme == "wss" ? 443 : 80
        let host = hostname.contains(":") ? "[\(hostname)]" : hostname
        let authority = components.port == nil || components.port == defaultPort
            ? host
            : "\(host):\(components.port!)"
        return "\(scheme)://\(authority)"
    }
}
