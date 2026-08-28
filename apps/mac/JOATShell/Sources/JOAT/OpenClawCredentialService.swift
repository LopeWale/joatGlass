import CryptoKit
import Foundation
import Security

protocol OpenClawSecretStoring: Sendable {
    func load(account: String) throws -> Data?
    func save(_ data: Data, account: String) throws
}

struct KeychainOpenClawSecretStore: OpenClawSecretStoring {
    private let service = "com.joat.openclaw"

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
                "openclaw_keychain_unavailable",
                "OpenClaw credentials are unavailable in Keychain"
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
                "openclaw_keychain_unavailable",
                "OpenClaw credentials could not be updated in Keychain"
            )
        }
        var insert = lookup
        insert[kSecValueData] = data
        // A locked Mac must not keep an unattended agent credential usable.
        insert[kSecAttrAccessible] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        guard SecItemAdd(insert as CFDictionary, nil) == errSecSuccess else {
            throw NativeBridgeError(
                "openclaw_keychain_unavailable",
                "OpenClaw credentials could not be stored in Keychain"
            )
        }
    }
}

struct OpenClawIdentityDTO: Codable, Equatable, Sendable {
    let deviceID: String
    let publicKey: String
}

struct OpenClawTokenDTO: Codable, Equatable, Sendable {
    let token: String
    let scopes: [String]
}

protocol OpenClawCredentialServicing: Sendable {
    func identity(profileID: String) async throws -> OpenClawIdentityDTO
    func sign(profileID: String, payload: String) async throws -> String
    func loadToken(
        profileID: String,
        deviceID: String,
        role: String
    ) async throws -> OpenClawTokenDTO?
    func storeToken(
        profileID: String,
        deviceID: String,
        role: String,
        token: String,
        scopes: [String]
    ) async throws
}

struct OpenClawProfileRequest: Decodable, Sendable {
    let profileID: String

    enum CodingKeys: String, CodingKey {
        case profileID = "profileId"
    }
}

struct OpenClawSignRequest: Decodable, Sendable {
    let profileID: String
    let payload: String

    enum CodingKeys: String, CodingKey {
        case profileID = "profileId"
        case payload
    }
}

struct OpenClawSignatureDTO: Encodable, Sendable {
    let signature: String
}

struct OpenClawTokenRequest: Decodable, Sendable {
    let profileID: String
    let deviceID: String
    let role: String

    enum CodingKeys: String, CodingKey {
        case profileID = "profileId"
        case deviceID = "deviceId"
        case role
    }
}

struct OpenClawTokenStoreRequest: Decodable, Sendable {
    let profileID: String
    let deviceID: String
    let role: String
    let token: String
    let scopes: [String]

    enum CodingKeys: String, CodingKey {
        case profileID = "profileId"
        case deviceID = "deviceId"
        case role, token, scopes
    }
}

struct OpenClawStoredDTO: Encodable, Sendable {
    let stored: Bool
}

actor OpenClawCredentialService: OpenClawCredentialServicing {
    private static let operatorScopes = Set(["operator.read", "operator.write"])
    private static let maximumSignedPayloadBytes = 65_536
    private static let maximumTokenBytes = 8_192

    private let secretStore: any OpenClawSecretStoring

    init(secretStore: any OpenClawSecretStoring = KeychainOpenClawSecretStore()) {
        self.secretStore = secretStore
    }

    func identity(profileID: String) throws -> OpenClawIdentityDTO {
        let key = try privateKey(profileID: profileID)
        let publicData = key.publicKey.rawRepresentation
        return OpenClawIdentityDTO(
            deviceID: SHA256.hash(data: publicData)
                .map { String(format: "%02x", $0) }
                .joined(),
            publicKey: publicData.base64URLUnpadded
        )
    }

    func sign(profileID: String, payload: String) throws -> String {
        guard !payload.isEmpty,
              payload.lengthOfBytes(using: .utf8) <= Self.maximumSignedPayloadBytes
        else {
            throw NativeBridgeError(
                "openclaw_invalid_signing_payload",
                "OpenClaw signing payload is invalid"
            )
        }
        let key = try privateKey(profileID: profileID)
        return try key.signature(for: Data(payload.utf8)).base64URLUnpadded
    }

    func loadToken(
        profileID: String,
        deviceID: String,
        role: String
    ) throws -> OpenClawTokenDTO? {
        try validateBinding(profileID: profileID, deviceID: deviceID, role: role)
        guard let data = try secretStore.load(
            account: tokenAccount(profileID: profileID, deviceID: deviceID, role: role)
        ) else { return nil }
        guard let record = try? JSONDecoder().decode(OpenClawTokenDTO.self, from: data) else {
            throw NativeBridgeError(
                "openclaw_token_invalid",
                "Stored OpenClaw pairing data is invalid"
            )
        }
        try validateToken(record.token, scopes: record.scopes)
        return record
    }

    func storeToken(
        profileID: String,
        deviceID: String,
        role: String,
        token: String,
        scopes: [String]
    ) throws {
        try validateBinding(profileID: profileID, deviceID: deviceID, role: role)
        try validateToken(token, scopes: scopes)
        let data = try JSONEncoder().encode(OpenClawTokenDTO(token: token, scopes: scopes))
        try secretStore.save(
            data,
            account: tokenAccount(profileID: profileID, deviceID: deviceID, role: role)
        )
    }

    private func privateKey(profileID: String) throws -> Curve25519.Signing.PrivateKey {
        try validateProfileID(profileID)
        let account = "identity.v1.\(profileID)"
        if let stored = try secretStore.load(account: account) {
            do {
                return try Curve25519.Signing.PrivateKey(rawRepresentation: stored)
            } catch {
                throw NativeBridgeError(
                    "openclaw_identity_invalid",
                    "Stored OpenClaw device identity is invalid"
                )
            }
        }
        let key = Curve25519.Signing.PrivateKey()
        try secretStore.save(key.rawRepresentation, account: account)
        return key
    }

    private func validateBinding(
        profileID: String,
        deviceID: String,
        role: String
    ) throws {
        guard role == "operator",
              deviceID == (try identity(profileID: profileID)).deviceID
        else {
            throw NativeBridgeError(
                "openclaw_credential_binding_mismatch",
                "OpenClaw credential binding does not match this device"
            )
        }
    }

    private func validateProfileID(_ profileID: String) throws {
        guard profileID.count == 64, profileID.allSatisfy({ $0.isHexDigit }) else {
            throw NativeBridgeError(
                "openclaw_profile_invalid",
                "OpenClaw Gateway profile identifier is invalid"
            )
        }
    }

    private func validateToken(_ token: String, scopes: [String]) throws {
        guard !token.isEmpty,
              token.lengthOfBytes(using: .utf8) <= Self.maximumTokenBytes,
              !scopes.isEmpty,
              Set(scopes).count == scopes.count,
              scopes.allSatisfy(Self.operatorScopes.contains)
        else {
            throw NativeBridgeError(
                "openclaw_token_invalid",
                "OpenClaw device token record is invalid"
            )
        }
    }

    private func tokenAccount(profileID: String, deviceID: String, role: String) -> String {
        "token.v1.\(profileID).\(deviceID).\(role)"
    }
}

private extension Data {
    var base64URLUnpadded: String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
