import Foundation
import Observation

@MainActor @Observable
final class HermesConnectionsModel {
    static let shared = HermesConnectionsModel()

    struct Draft: Identifiable {
        let id = UUID()
        var gatewayURL: String
        var name: String
    }

    var drafts: [Draft] = []
    private(set) var notice: String?
    private(set) var failed = false
    private(set) var savedAt: Date?
    private(set) var loadFailed = false
    private(set) var recoveryBackupURL: URL?
    private var saved: [OpenClawRuntimeConfiguration] = []
    private let url: URL

    init(url: URL = OpenClawRuntimeConfiguration.defaultURL) {
        self.url = url
        reload()
    }

    var configurations: [OpenClawRuntimeConfiguration]? {
        let values = drafts.compactMap { try? OpenClawRuntimeConfiguration(gatewayURL: $0.gatewayURL, name: $0.name) }
        guard (1...16).contains(values.count), values.count == drafts.count,
              Set(values.map(\.gatewayURL)).count == values.count else { return nil }
        return values
    }

    var canSave: Bool {
        guard !loadFailed, let configurations else { return false }
        return configurations != saved
    }

    func requiresRestart(after lastStart: Date?) -> Bool {
        guard let savedAt else { return false }
        guard let lastStart else { return true }
        return lastStart < savedAt
    }

    func addHost() {
        guard drafts.count < 16 else { return }
        drafts.append(Draft(gatewayURL: "", name: ""))
    }

    func removeHost(id: UUID) {
        guard drafts.count > 1 else { return }
        drafts.removeAll { $0.id == id }
    }

    func reload() {
        do {
            let configurations = try OpenClawRuntimeConfiguration.loadAll(from: url)
            saved = configurations
            drafts = saved.map { Draft(gatewayURL: $0.gatewayURL, name: $0.name) }
            if drafts.isEmpty { drafts = [Draft(gatewayURL: "", name: "")] }
            failed = false
            loadFailed = false
            notice = saved.isEmpty ? "No Gateway configured yet." : nil
        } catch {
            loadFailed = true
            failed = true
            notice = "Could not read the saved Gateways: \(error.localizedDescription) Retry loading, or back up the unreadable file and start again."
        }
    }

    /// Explicit recovery keeps the unreadable file rather than silently
    /// replacing connections that the editor could not understand.
    func backUpAndReset() {
        guard loadFailed else { return }
        if (try? OpenClawRuntimeConfiguration.loadAll(from: url)) != nil {
            reload() // Another writer already repaired it; preserve that work.
            return
        }
        do {
            let manager = FileManager.default
            let attributes = try manager.attributesOfItem(atPath: url.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid()
            else { throw OpenClawRuntimeConfiguration.ConfigurationError.unsafeDestination }
            let backup = url.deletingLastPathComponent()
                .appendingPathComponent("openclaw-backup-\(UUID().uuidString).json")
            try manager.moveItem(at: url, to: backup)
            recoveryBackupURL = backup
            try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backup.path)
            reload()
            notice = "Previous file backed up. Enter your Gateway addresses, then save. The running engine keeps its current connections until it restarts."
        } catch {
            failed = true
            notice = "Could not reset the saved Gateways: \(error.localizedDescription)"
        }
    }

    func save() {
        guard canSave, let configurations else { return }
        do {
            let current = try OpenClawRuntimeConfiguration.loadAll(from: url)
            guard current == saved else {
                failed = true
                notice = "The saved Gateways changed elsewhere. Reload Saved before editing them again. Your draft is still shown."
                return
            }
            try OpenClawRuntimeConfiguration.saveAll(configurations, to: url)
            saved = configurations
            // Preserve each row's identity while normalizing its displayed URL and name.
            for index in drafts.indices {
                drafts[index].gatewayURL = configurations[index].gatewayURL
                drafts[index].name = configurations[index].name
            }
            savedAt = Date()
            failed = false
            notice = "Saved. These Gateways will be used on the next engine start."
        } catch {
            failed = true
            notice = "Could not save the Gateways: \(error.localizedDescription)"
        }
    }
}
