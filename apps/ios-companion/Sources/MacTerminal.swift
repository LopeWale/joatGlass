import Foundation

/// The phone configuring the wearer's home Mac Agent Terminal over Tailscale.
///
/// Modeled on `MacMessages.swift`: nothing personal is hardcoded here.
/// `scripts/build_ios.sh` writes `Generated/mac-terminal.json` from
/// `~/Library/Application Support/JOAT/terminal-token` and Tailscale's DNS
/// name; `Documents/mac-terminal.json` overrides the bundled copy if present.
///
/// Endpoints on `services/terminal_endpoint.py` (exposed via Tailscale Serve
/// at `https://<mac>.<tailnet>.ts.net/terminal`):
/// - `GET  /v1/state`  -> agent readiness, models, workspaces, accounts, login
/// - `POST /v1/config` -> model/cwd changes, Gemini key rotation, sessions, CLI sign-in
@MainActor
final class MacTerminal: ObservableObject {
    struct Config: Decodable {
        /// https://<mac>.<tailnet>.ts.net/terminal
        let url: String
        let token: String
    }

    struct RecentMessage: Equatable, Decodable {
        let role: String
        let text: String

        init(role: String, text: String) {
            self.role = role
            self.text = text
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            self.role = c.nonEmptyString(["role", "sender", "author"]) ?? "assistant"
            self.text = c.string(["text", "content", "message"]) ?? ""
        }
    }

    struct PendingApprovalItem: Identifiable, Equatable, Decodable {
        let id: String
        let kind: String
        let summary: String

        init(id: String, kind: String, summary: String) {
            self.id = id
            self.kind = kind
            self.summary = summary
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            self.id = c.nonEmptyString(["id", "approvalId", "approval_id"]) ?? UUID().uuidString
            self.kind = c.nonEmptyString(["kind", "type"]) ?? "tool"
            self.summary = c.string(["summary", "description", "detail", "title"]) ?? ""
        }
    }

    struct SessionItem: Identifiable, Equatable, Decodable {
        let key: String
        let label: String
        var id: String { key }

        init(key: String, label: String) {
            self.key = key
            self.label = label
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            guard let k = c.nonEmptyString(["key", "id", "sessionKey", "session_key"]) else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "Missing session key")
                )
            }
            self.key = k
            self.label = c.nonEmptyString(["label", "title", "name"]) ?? k
        }
    }

    struct AgentItem: Identifiable, Equatable, Decodable {
        typealias RecentMessage = MacTerminal.RecentMessage
        typealias PendingApprovalItem = MacTerminal.PendingApprovalItem
        typealias SessionItem = MacTerminal.SessionItem

        let name: String
        let provider: String
        let title: String?
        let installed: Bool
        let loggedIn: Bool
        let statusDetail: String
        let cwd: String
        let currentModel: String
        let modelOptions: [String]
        let busy: Bool
        let activeSessionTitle: String?
        let pendingApprovals: Int
        let recentMessages: [RecentMessage]
        let pendingApprovalItems: [PendingApprovalItem]
        let sessions: [SessionItem]

        var id: String { name.isEmpty ? provider : name }

        var displayTitle: String {
            let lowerProvider = provider.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let lowerName = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let lowerTitle = title?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
            if ["claude", "claude-code", "claude code"].contains(lowerProvider)
                || ["claude", "claude-code", "claude code"].contains(lowerName)
                || lowerTitle == "claude" {
                return "Claude Code"
            }
            if let title, !title.isEmpty { return title }
            switch lowerProvider {
            case "codex": return "Codex"
            case "antigravity": return "Antigravity"
            default:
                if name.isEmpty { return provider.capitalized }
                return name.prefix(1).uppercased() + name.dropFirst()
            }
        }

        init(
            name: String = "",
            provider: String = "",
            title: String? = nil,
            installed: Bool = false,
            loggedIn: Bool = false,
            statusDetail: String = "",
            cwd: String = "",
            currentModel: String = "",
            modelOptions: [String] = [],
            busy: Bool = false,
            activeSessionTitle: String? = nil,
            pendingApprovals: Int = 0,
            recentMessages: [RecentMessage] = [],
            pendingApprovalItems: [PendingApprovalItem] = [],
            sessions: [SessionItem] = []
        ) {
            self.name = name
            self.provider = provider
            self.title = title
            self.installed = installed
            self.loggedIn = loggedIn
            self.statusDetail = statusDetail
            self.cwd = cwd
            self.currentModel = currentModel
            self.modelOptions = modelOptions
            self.busy = busy
            self.activeSessionTitle = activeSessionTitle
            self.pendingApprovals = pendingApprovals
            self.recentMessages = recentMessages
            self.pendingApprovalItems = pendingApprovalItems
            self.sessions = sessions
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            let decodedName = c.string(["name", "lane"]) ?? ""
            let decodedProvider = c.string(["provider"]) ?? decodedName.lowercased()
            self.name = decodedName.isEmpty ? decodedProvider : decodedName
            self.provider = decodedProvider
            self.title = c.nonEmptyString(["title", "displayName", "display_name"])
            self.installed = c.bool(["installed"], default: false)
            self.loggedIn = c.bool(["loggedIn", "logged_in"], default: false)
            self.statusDetail = c.string(["statusDetail", "status_detail", "detail"]) ?? ""
            self.cwd = c.string(["cwd", "workspace"]) ?? ""
            self.currentModel = c.string(["currentModel", "current_model", "model"]) ?? ""
            self.modelOptions = c.stringList(["modelOptions", "model_options", "options"])
            self.busy = c.bool(["busy", "running"], default: false)
            self.activeSessionTitle = c.nonEmptyString(["activeSessionTitle", "active_session_title", "sessionTitle"])
            self.pendingApprovals = c.int(["pendingApprovals", "pending_approvals", "approvals"], default: 0)
            self.recentMessages = c.LossyArray(
                RecentMessage.self,
                forKeys: ["recentMessages", "recent_messages", "messages"]
            ).filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            self.pendingApprovalItems = c.LossyArray(
                PendingApprovalItem.self,
                forKeys: ["pendingApprovalItems", "pending_approval_items", "approvalItems"]
            )
            self.sessions = c.LossyArray(
                SessionItem.self,
                forKeys: ["sessions", "pastSessions", "past_sessions"]
            )
        }
    }

    struct AccountProfileItem: Identifiable, Equatable, Decodable {
        let profileId: String
        let label: String
        let state: String
        let isDefault: Bool
        var active: Bool
        let email: String?
        let plan: String?

        var id: String { profileId }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            let pid = c.nonEmptyString(["profileId", "profile_id", "id"]) ?? "default"
            self.profileId = pid
            self.label = c.nonEmptyString(["label", "name"]) ?? (pid == "default" ? "This Mac's login" : pid)
            if let explicitState = c.nonEmptyString(["state", "status"]) {
                self.state = explicitState
            } else if let logged = c.optionalBool(["loggedIn", "logged_in"]) {
                self.state = logged ? "logged_in" : "logged_out"
            } else {
                self.state = "unknown"
            }
            self.isDefault = c.bool(["isDefault", "is_default"], default: pid == "default")
            self.active = c.bool(["active", "isActive", "is_active"], default: false)
            self.email = c.nonEmptyString(["email"])
            self.plan = c.nonEmptyString(["plan", "subscriptionType", "subscription_type"])
        }
    }

    struct ProviderAccountGroup: Identifiable, Equatable, Decodable {
        let provider: String
        let title: String
        let installed: Bool
        let activeProfileId: String?
        let profiles: [AccountProfileItem]

        var id: String { provider }

        init(provider: String, title: String, installed: Bool, activeProfileId: String?, profiles: [AccountProfileItem]) {
            self.provider = provider
            self.title = title
            self.installed = installed
            self.activeProfileId = activeProfileId
            self.profiles = profiles
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            let prov = c.nonEmptyString(["provider", "name"]) ?? "unknown"
            self.provider = prov
            self.title = c.nonEmptyString(["title", "label"]) ?? Self.defaultTitle(for: prov)
            self.installed = c.bool(["installed"], default: true)
            let activeId = c.nonEmptyString(["activeProfileId", "active_profile_id", "active"])
            var decodedProfiles = c.LossyArray(AccountProfileItem.self, forKeys: ["profiles", "items"])
            if let activeId, !decodedProfiles.contains(where: \.active) {
                decodedProfiles = decodedProfiles.map { item in
                    var copy = item
                    copy.active = (item.profileId == activeId)
                    return copy
                }
            }
            self.activeProfileId = activeId ?? decodedProfiles.first(where: \.active)?.profileId
            self.profiles = decodedProfiles
        }

        static func defaultTitle(for provider: String) -> String {
            switch provider.lowercased() {
            case "claude": return "Claude"
            case "codex": return "Codex"
            case "antigravity": return "Antigravity"
            default: return provider.capitalized
            }
        }
    }

    struct LoginProgress: Equatable, Decodable {
        let provider: String
        let profileId: String
        let stage: String
        let message: String
        let url: String?
        let code: String?
        let needsCode: Bool

        var isFinished: Bool { ["done", "failed", "cancelled"].contains(stage) }
        var isActive: Bool { !stage.isEmpty && !isFinished }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            self.provider = c.string(["provider"]) ?? ""
            self.profileId = c.nonEmptyString(["profileId", "profile_id"]) ?? "default"
            let stg = c.string(["stage", "state"]) ?? ""
            self.stage = stg
            self.message = c.string(["message", "detail"]) ?? ""
            self.url = c.nonEmptyString(["url", "link"])
            self.code = c.nonEmptyString(["code", "deviceCode", "device_code"])
            self.needsCode = c.bool(["needsCode", "needs_code"], default: stg == "enter_code")
        }
    }

    struct StateSnapshot: Equatable, Decodable {
        var agents: [AgentItem]
        var assistantSeat: String?
        var assistantOptions: [String]?
        var accounts: [ProviderAccountGroup]?
        var login: LoginProgress?
        var notice: String?

        init(
            agents: [AgentItem] = [],
            assistantSeat: String? = nil,
            assistantOptions: [String]? = nil,
            accounts: [ProviderAccountGroup]? = nil,
            login: LoginProgress? = nil,
            notice: String? = nil
        ) {
            self.agents = agents
            self.assistantSeat = assistantSeat
            self.assistantOptions = assistantOptions
            self.accounts = accounts
            self.login = login
            self.notice = notice
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: FlexKey.self)
            self.agents = c.LossyArray(AgentItem.self, forKeys: ["agents", "lanes"])
            self.assistantSeat = c.nonEmptyString(["assistantSeat", "assistant_seat"])
            let opts = c.stringList(["assistantOptions", "assistant_options"])
            self.assistantOptions = opts.isEmpty ? nil : opts

            if let accountList = c.optionalLossyArray(ProviderAccountGroup.self, forKeys: ["accounts"]) {
                self.accounts = accountList
            } else if let keyed = c.optionalDictionary(ProviderAccountGroup.self, forKey: "accounts") {
                let ordered = keyed.keys.sorted().compactMap { key -> ProviderAccountGroup? in
                    guard let g = keyed[key] else { return nil }
                    if g.provider == "unknown" {
                        return ProviderAccountGroup(
                            provider: key,
                            title: ProviderAccountGroup.defaultTitle(for: key),
                            installed: g.installed,
                            activeProfileId: g.activeProfileId,
                            profiles: g.profiles
                        )
                    }
                    return g
                }
                self.accounts = ordered
            } else {
                self.accounts = nil
            }

            if let decodedLogin = try? c.decodeIfPresent(LoginProgress.self, forKey: FlexKey("login")),
               !decodedLogin.stage.isEmpty || !decodedLogin.provider.isEmpty {
                self.login = decodedLogin
            } else {
                self.login = nil
            }
            self.notice = c.nonEmptyString(["notice", "message"])
        }
    }

    enum ConnectionState: Equatable {
        case unconfigured
        case idle
        case loading
        case connected
        case failed(String)
    }

    private struct HTTPError: Error {
        let status: Int
        let message: String
    }

    private let explicitConfig: Config?
    private let usesExplicitConfig: Bool
    private(set) var config: Config?
    var configured: Bool { config != nil }
    var hostLabel: String? { config.flatMap { URL(string: $0.url)?.host } }

    @Published private(set) var connectionState: ConnectionState = .idle
    @Published private(set) var state = StateSnapshot()
    @Published private(set) var isPerformingAction = false
    @Published private(set) var lastUpdated: Date?
    @Published var notice: String?
    @Published var actionError: String?

    private var viewVisible = false
    private var loginPoller: Task<Void, Never>?

    private static func makeDefaultSession() -> URLSession {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 25
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }

    private let session: URLSession

    init() {
        self.explicitConfig = nil
        self.usesExplicitConfig = false
        self.session = Self.makeDefaultSession()
        reloadConfig()
    }

    init(config: Config?, session: URLSession? = nil) {
        let normalized = config.flatMap { raw -> Config? in
            let cleanURL = raw.url.trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: "/+$", with: "", options: .regularExpression)
            let cleanToken = raw.token.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleanURL.isEmpty, !cleanToken.isEmpty else { return nil }
            return Config(url: cleanURL, token: cleanToken)
        }
        self.explicitConfig = normalized
        self.usesExplicitConfig = true
        self.session = session ?? Self.makeDefaultSession()
        self.config = normalized
        if normalized == nil {
            self.connectionState = .unconfigured
            Log.add("mac terminal: not configured")
        } else {
            self.connectionState = .idle
            Log.add("mac terminal: \(URL(string: normalized!.url)?.host ?? "?")")
        }
    }

    func reloadConfig() {
        if usesExplicitConfig {
            config = explicitConfig
            if explicitConfig == nil {
                connectionState = .unconfigured
            } else if connectionState == .unconfigured {
                connectionState = .idle
            }
            return
        }
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("mac-terminal.json")
        let candidates = [docs, Bundle.main.url(forResource: "mac-terminal", withExtension: "json")].compactMap { $0 }
        let loaded = candidates.lazy.compactMap { url -> Config? in
            guard let data = try? Data(contentsOf: url),
                  let raw = try? JSONDecoder().decode(Config.self, from: data) else { return nil }
            let cleanURL = raw.url.trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: "/+$", with: "", options: .regularExpression)
            let cleanToken = raw.token.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleanURL.isEmpty, !cleanToken.isEmpty else { return nil }
            return Config(url: cleanURL, token: cleanToken)
        }.first
        config = loaded
        if loaded == nil {
            connectionState = .unconfigured
            Log.add("mac terminal: not configured")
        } else {
            if connectionState == .unconfigured { connectionState = .idle }
            Log.add("mac terminal: \(URL(string: loaded!.url)?.host ?? "?")")
        }
    }

    func setViewVisible(_ visible: Bool) {
        viewVisible = visible
        syncLoginPoller()
    }

    // MARK: - State & Config API

    func refresh(sendRefreshAction: Bool = false) async {
        reloadConfig()
        guard configured else { return }
        if sendRefreshAction {
            _ = await sendConfig(["action": "refresh"], logLabel: "refresh")
        } else {
            await fetchState()
        }
    }

    func fetchState(quiet: Bool = false) async {
        guard configured else {
            connectionState = .unconfigured
            return
        }
        if !quiet, state.agents.isEmpty {
            connectionState = .loading
        }
        do {
            let data = try await perform(path: "/v1/state", method: "GET")
            let decoded = try JSONDecoder().decode(StateSnapshot.self, from: data)
            applySnapshot(decoded)
            if !quiet { actionError = nil }
        } catch {
            let msg = Self.describe(error)
            Log.add("mac terminal state failed: \(msg)")
            if state.agents.isEmpty {
                connectionState = .failed(msg)
            } else if !quiet {
                actionError = msg
            }
        }
    }

    @discardableResult
    func setModel(provider: String, model: String) async -> Bool {
        await sendConfig(["action": "set_model", "provider": provider, "model": model],
                         logLabel: "set_model(\(provider))")
    }

    @discardableResult
    func setCwd(provider: String, cwd: String) async -> Bool {
        await sendConfig(["action": "set_cwd", "provider": provider, "cwd": cwd],
                         logLabel: "set_cwd(\(provider))")
    }

    /// Write-only Gemini API key update for Antigravity; never logged or echoed back.
    @discardableResult
    func setGeminiKey(_ key: String) async -> Bool {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            actionError = "Enter a Gemini API key."
            return false
        }
        return await sendConfig(["action": "set_gemini_key", "key": trimmed],
                                logLabel: "set_gemini_key")
    }

    @discardableResult
    func newSession(lane: String) async -> Bool {
        await sendConfig(["action": "new_session", "lane": lane],
                         logLabel: "new_session(\(lane))")
    }

    @discardableResult
    func resumeSession(provider: String, sessionKey: String) async -> Bool {
        let cleanProvider = provider.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanKey = sessionKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanProvider.isEmpty, !cleanKey.isEmpty else {
            actionError = "Choose a session to resume."
            return false
        }
        return await sendConfig(
            [
                "action": "resume_session",
                "provider": cleanProvider,
                "sessionKey": cleanKey,
            ],
            logLabel: "resume_session(\(cleanProvider))"
        )
    }

    @discardableResult
    func sendInstruction(provider: String, instruction: String, newSession: Bool = false) async -> Bool {
        let trimmed = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            actionError = "Enter an instruction to send."
            return false
        }
        return await sendConfig(
            [
                "action": "send_instruction",
                "provider": provider,
                "instruction": trimmed,
                "newSession": newSession,
            ],
            logLabel: "send_instruction(\(provider))"
        )
    }

    @discardableResult
    func cancelRun(provider: String? = nil) async -> Bool {
        var body: [String: Any] = ["action": "cancel_run"]
        let cleanProvider = provider?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let cleanProvider, !cleanProvider.isEmpty {
            body["provider"] = cleanProvider
        }
        let label = (cleanProvider?.isEmpty == false) ? "cancel_run(\(cleanProvider!))" : "cancel_run"
        return await sendConfig(body, logLabel: label)
    }

    @discardableResult
    func decideApproval(decision: String, provider: String? = nil) async -> Bool {
        let cleanDecision = decision.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard cleanDecision == "approve" || cleanDecision == "deny" else {
            actionError = "Decision must be 'approve' or 'deny'."
            return false
        }
        var body: [String: Any] = ["action": "decide_approval", "decision": cleanDecision]
        let cleanProvider = provider?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let cleanProvider, !cleanProvider.isEmpty {
            body["provider"] = cleanProvider
        }
        let label = (cleanProvider?.isEmpty == false)
            ? "decide_approval(\(cleanDecision),\(cleanProvider!))"
            : "decide_approval(\(cleanDecision))"
        return await sendConfig(body, logLabel: label)
    }

    @discardableResult
    func setAssistantSeat(_ seat: String) async -> Bool {
        await sendConfig(["action": "set_assistant_seat", "seat": seat],
                         logLabel: "set_assistant_seat(\(seat))")
    }

    @discardableResult
    func selectAccount(provider: String, profileId: String) async -> Bool {
        await sendConfig(["action": "account.select", "provider": provider, "profileId": profileId],
                         logLabel: "account.select(\(provider))")
    }

    @discardableResult
    func addAccount(provider: String, label: String) async -> Bool {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            actionError = "Give the account a label."
            return false
        }
        return await sendConfig(["action": "account.add", "provider": provider, "label": trimmed],
                                logLabel: "account.add(\(provider))")
    }

    @discardableResult
    func removeAccount(provider: String, profileId: String) async -> Bool {
        await sendConfig(["action": "account.remove", "provider": provider, "profileId": profileId],
                         logLabel: "account.remove(\(provider))")
    }

    @discardableResult
    func startLogin(provider: String) async -> Bool {
        await sendConfig(["action": "login.start", "provider": provider],
                         logLabel: "login.start(\(provider))")
    }

    /// Submits the one-time OAuth callback code; never logs the code itself.
    @discardableResult
    func submitLoginCode(_ code: String) async -> Bool {
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            actionError = "Paste the sign-in code first."
            return false
        }
        return await sendConfig(["action": "login.code", "code": trimmed],
                                logLabel: "login.code")
    }

    @discardableResult
    func cancelLogin() async -> Bool {
        await sendConfig(["action": "login.cancel"], logLabel: "login.cancel")
    }

    func dismissFinishedLogin() {
        guard let login = state.login, login.isFinished else { return }
        state.login = nil
        Task { _ = await sendConfig(["action": "login.cancel"], logLabel: "login.dismiss", quiet: true) }
    }

    // MARK: - Internals

    private func sendConfig(_ body: [String: Any], logLabel: String, quiet: Bool = false) async -> Bool {
        guard configured else {
            connectionState = .unconfigured
            return false
        }
        if !quiet {
            isPerformingAction = true
            actionError = nil
        }
        defer {
            if !quiet { isPerformingAction = false }
        }
        do {
            let payload = try JSONSerialization.data(withJSONObject: body)
            let data = try await perform(path: "/v1/config", method: "POST", body: payload)
            try await handleConfigResponse(data)
            Log.add("mac terminal: \(logLabel) ok")
            return true
        } catch {
            let msg = Self.describe(error)
            Log.add("mac terminal: \(logLabel) failed (\(msg))")
            if !quiet { actionError = msg }
            return false
        }
    }

    private func handleConfigResponse(_ data: Data) async throws {
        let obj = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
        let topNotice = (obj["notice"] as? String ?? obj["message"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines)

        if obj["agents"] != nil, let snapshot = try? JSONDecoder().decode(StateSnapshot.self, from: data) {
            applySnapshot(snapshot, overrideNotice: topNotice)
            return
        }
        if let nestedState = obj["state"] as? [String: Any],
           let nestedData = try? JSONSerialization.data(withJSONObject: nestedState),
           let snapshot = try? JSONDecoder().decode(StateSnapshot.self, from: nestedData) {
            applySnapshot(snapshot, overrideNotice: topNotice)
            return
        }
        if let topNotice, !topNotice.isEmpty {
            notice = topNotice
        }
        await fetchState(quiet: true)
    }

    private func applySnapshot(_ snapshot: StateSnapshot, overrideNotice: String? = nil) {
        state = snapshot
        connectionState = .connected
        lastUpdated = Date()
        if let overrideNotice, !overrideNotice.isEmpty {
            notice = overrideNotice
        } else if let snapNotice = snapshot.notice, !snapNotice.isEmpty {
            notice = snapNotice
        }
        syncLoginPoller()
    }

    private func syncLoginPoller() {
        let shouldPoll = viewVisible && (state.login?.isActive == true)
        if !shouldPoll {
            loginPoller?.cancel()
            loginPoller = nil
            return
        }
        guard loginPoller == nil else { return }
        loginPoller = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard !Task.isCancelled, let self else { return }
                guard self.viewVisible, self.state.login?.isActive == true else {
                    self.loginPoller = nil
                    return
                }
                await self.fetchState(quiet: true)
            }
        }
    }

    private func perform(path: String, method: String, body: Data? = nil) async throws -> Data {
        guard let config, let url = URL(string: config.url + path) else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let doc = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            let extracted = Self.extractErrorMessage(doc: doc, status: status)
            throw HTTPError(status: status, message: extracted)
        }
        return data
    }

    private static func extractErrorMessage(doc: [String: Any], status: Int) -> String {
        if status == 401 {
            return "The Mac refused this phone's terminal token. Rebuild JOAT Brain on the Mac."
        }
        if let errStr = doc["error"] as? String, !errStr.isEmpty {
            return errStr
        }
        if let errObj = doc["error"] as? [String: Any] {
            if let msg = errObj["message"] as? String, !msg.isEmpty { return msg }
            if let detail = errObj["detail"] as? String, !detail.isEmpty { return detail }
            if let type = errObj["type"] as? String, !type.isEmpty { return type }
        }
        if let detail = doc["detail"] as? String, !detail.isEmpty { return detail }
        if let msg = doc["message"] as? String, !msg.isEmpty { return msg }
        return "The Mac returned HTTP \(status)."
    }

    private static func describe(_ error: Error) -> String {
        if let http = error as? HTTPError {
            return http.message
        }
        if error is DecodingError {
            return "The Mac sent an unexpected response format."
        }
        return "Can't reach the Mac right now. Make sure your Mac is awake with JOAT running and Tailscale connected."
    }
}

// MARK: - Defensive Decoding Helpers

private struct FlexKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init(_ string: String) { self.stringValue = string }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private struct FlexibleOption: Decodable {
    let value: String?
    init(from decoder: Decoder) throws {
        if let single = try? decoder.singleValueContainer(),
           let str = try? single.decode(String.self) {
            self.value = str
            return
        }
        let c = try decoder.container(keyedBy: FlexKey.self)
        self.value = c.nonEmptyString(["label", "id", "name", "value"])
    }
}

private struct LossyElement<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws {
        self.value = try? T(from: decoder)
    }
}

private extension KeyedDecodingContainer where K == FlexKey {
    func string(_ keys: [String]) -> String? {
        for k in keys {
            if let v = try? decodeIfPresent(String.self, forKey: FlexKey(k)) {
                return v
            }
        }
        return nil
    }

    func nonEmptyString(_ keys: [String]) -> String? {
        guard let raw = string(keys)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else { return nil }
        return raw
    }

    func optionalBool(_ keys: [String]) -> Bool? {
        for k in keys {
            let fk = FlexKey(k)
            if let b = try? decodeIfPresent(Bool.self, forKey: fk) { return b }
            if let i = try? decodeIfPresent(Int.self, forKey: fk) { return i != 0 }
        }
        return nil
    }

    func bool(_ keys: [String], default fallback: Bool) -> Bool {
        optionalBool(keys) ?? fallback
    }

    func int(_ keys: [String], default fallback: Int) -> Int {
        for k in keys {
            let fk = FlexKey(k)
            if let i = try? decodeIfPresent(Int.self, forKey: fk) { return i }
            if let b = try? decodeIfPresent(Bool.self, forKey: fk) { return b ? 1 : 0 }
        }
        return fallback
    }

    func stringList(_ keys: [String]) -> [String] {
        for k in keys {
            let fk = FlexKey(k)
            if let list = try? decodeIfPresent([String].self, forKey: fk) {
                return list.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            }
            if let flex = try? decodeIfPresent([FlexibleOption].self, forKey: fk) {
                return flex.compactMap(\.value)
            }
        }
        return []
    }

    func optionalLossyArray<T: Decodable>(_ type: T.Type, forKeys keys: [String]) -> [T]? {
        for k in keys {
            let fk = FlexKey(k)
            if let wrapped = try? decodeIfPresent([LossyElement<T>].self, forKey: fk) {
                return wrapped.compactMap(\.value)
            }
        }
        return nil
    }

    func LossyArray<T: Decodable>(_ type: T.Type, forKeys keys: [String]) -> [T] {
        optionalLossyArray(type, forKeys: keys) ?? []
    }

    func optionalDictionary<T: Decodable>(_ type: T.Type, forKey key: String) -> [String: T]? {
        guard let wrapped = try? decodeIfPresent([String: LossyElement<T>].self, forKey: FlexKey(key)) else {
            return nil
        }
        var out: [String: T] = [:]
        for (k, v) in wrapped {
            if let item = v.value { out[k] = item }
        }
        return out
    }
}
