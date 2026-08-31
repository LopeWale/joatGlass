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
        guard (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) == nil else {
            throw ConfigurationError.invalidFile
        }
        guard fileManager.fileExists(atPath: url.path) else { return nil }
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
              let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(raw.keys) == ["gateway_url", "agent_id"],
              let gatewayURL = raw["gateway_url"] as? String,
              let agentID = raw["agent_id"] as? String,
              agentID == Self.hermesAgentID
        else {
            throw ConfigurationError.invalidFile
        }
        return try OpenClawRuntimeConfiguration(gatewayURL: gatewayURL)
    }

    /// Save by replacement so the Python process observes either the previous
    /// valid config or this complete, owner-only config—never a partial write.
    func save(
        to url: URL = defaultURL,
        fileManager: FileManager = .default
    ) throws {
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

        let payload: [String: String] = [
            "gateway_url": gatewayURL,
            "agent_id": agentID,
        ]
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
