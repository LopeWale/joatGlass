import Foundation

/// Drives the 3-level Agent Terminal capability (`AGENT TERMINAL` ->
/// `AGENT TERMINAL · <LANE>` -> 5-container `agent_terminal.view`) directly
/// from `JOAT Brain` on the iPhone, relaying state and actions to `MacTerminal`
/// (`/terminal` over Tailscale) while keeping the glasses WebSocket, speech
/// recognition (`Transcriber` + `Prompts`), and JOAT Assistant on this iPhone.
final class GlassesTerminal: BrainFeature {
    let name = "glasses terminal"
    let uplinkKinds: Set<String> = ["terminal.action", "input"]

    private enum Screen: Equatable {
        case closed
        case agentPicker(requestId: String?)
        case sessionPicker(laneKey: String, requestId: String?)
        case liveSession(laneKey: String, requestId: String, selectionId: String)
    }

    private enum SessionRowAction: Equatable {
        case active
        case newSession
        case resume(key: String)
    }

    private struct OptimisticTurn: Equatable {
        let laneKey: String
        let userText: String
        var running: Bool
    }

    private let link: PluginLink
    private let macTerminal: MacTerminal
    /// Optional runtime hook so tests overriding `AssistantRuntimeHooks.sharedMacTerminal`
    /// transparently drive the same `MacTerminal` instance.
    var overrideMacTerminal: (() -> MacTerminal?)?
    private let prompts: Prompts
    weak var menu: CapabilityMenu?

    var busyPollSeconds: TimeInterval = 1.2
    var idlePollSeconds: TimeInterval = 3.0

    private var screen: Screen = .closed
    private var visitRevision: Int = 0
    private var agentRowKeys: [String] = []
    private var sessionRows: [SessionRowAction] = []
    private var lastRenderedPickerItems: [String] = []
    private var lastRenderedPickerSelectable: Bool = false
    private var lastEmittedPayloadJSON: String?
    private var optimisticTurn: OptimisticTurn?
    private var lastInlineError: String?
    private var ownsActivePrompt = false
    private var pollTask: Task<Void, Never>?

    @MainActor
    var activeTerminal: MacTerminal {
        overrideMacTerminal?() ?? macTerminal
    }

    var isOpen: Bool {
        screen != .closed
    }

    init(link: PluginLink, macTerminal: MacTerminal, prompts: Prompts, menu: CapabilityMenu? = nil) {
        self.link = link
        self.macTerminal = macTerminal
        self.prompts = prompts
        self.menu = menu
    }

    // MARK: - BrainFeature Lifecycle

    func pluginConnected() {
        retireInternal(notifyMenu: false)
    }

    /// Called by `CapabilityMenu` when the wearer navigates away from Agent Terminal
    /// (e.g., Home, another capability row, or menu close).
    func retireFromMenu() {
        retireInternal(notifyMenu: false)
    }

    private func retireInternal(notifyMenu: Bool) {
        visitRevision += 1
        stopPolling()
        if ownsActivePrompt {
            ownsActivePrompt = false
            prompts.cancel("left Agent Terminal")
        }
        screen = .closed
        agentRowKeys = []
        sessionRows = []
        lastRenderedPickerItems = []
        lastRenderedPickerSelectable = false
        lastEmittedPayloadJSON = nil
        optimisticTurn = nil
        lastInlineError = nil
        if notifyMenu {
            menu?.leaveTerminalIfOpen()
        }
    }

    // MARK: - Level 1: Agent Picker (`AGENT TERMINAL`)

    func openPicker(requestId: String?) {
        stopPolling()
        if ownsActivePrompt {
            ownsActivePrompt = false
            prompts.cancel("opened Agent Terminal picker")
        }
        visitRevision += 1
        let rev = visitRevision
        screen = .agentPicker(requestId: requestId)
        sessionRows = []
        lastEmittedPayloadJSON = nil
        optimisticTurn = nil
        lastInlineError = nil
        menu?.syncTerminalVisit(requestId: requestId)

        let performInitialPaintAndSync: @MainActor () -> Void = { [weak self] in
            guard let self, self.visitRevision == rev else { return }
            let terminal = self.activeTerminal
            terminal.reloadConfig()
            guard terminal.configured else {
                self.agentRowKeys = []
                self.sendHudList(
                    [
                        "title": "AGENT TERMINAL",
                        "items": [
                            "Mac Agent Terminal is not linked yet.",
                            "Pair your Mac over Tailscale in JOAT Brain.",
                        ],
                        "hint": "2x tap: back",
                        "selectable": false,
                    ],
                    requestId: requestId
                )
                return
            }

            let hadCached = !terminal.state.agents.isEmpty
            if hadCached {
                self.paintAgentPicker(agents: terminal.state.agents, requestId: requestId)
            } else {
                self.agentRowKeys = []
                self.lastRenderedPickerItems = ["Loading agents from your Mac…"]
                self.lastRenderedPickerSelectable = false
                self.sendHudList(
                    [
                        "title": "AGENT TERMINAL",
                        "items": ["Loading agents from your Mac…"],
                        "hint": "2x tap: back",
                        "selectable": false,
                    ],
                    requestId: requestId
                )
            }

            Task { @MainActor [weak self] in
                guard let self, self.visitRevision == rev else { return }
                await terminal.fetchState(quiet: hadCached)
                guard self.visitRevision == rev,
                      case .agentPicker(let curReq) = self.screen,
                      curReq == requestId else { return }

                if terminal.state.agents.isEmpty {
                    self.agentRowKeys = []
                    let offlineItems: [String]
                    if case .failed(let err) = terminal.connectionState {
                        offlineItems = [
                            "Can't reach your Mac over Tailscale.",
                            Self.compactText(err, maxChars: 60),
                        ]
                    } else {
                        offlineItems = [
                            "Add an agent in JOAT on your Mac.",
                            "Intelligence → Agent connections",
                        ]
                    }
                    self.lastRenderedPickerItems = offlineItems
                    self.lastRenderedPickerSelectable = false
                    self.sendHudList(
                        [
                            "title": "AGENT TERMINAL",
                            "items": offlineItems,
                            "hint": "2x tap: back",
                            "selectable": false,
                        ],
                        requestId: requestId
                    )
                } else {
                    let fresh = Self.buildAgentPickerModel(from: terminal.state.agents)
                    if !hadCached || fresh.rows != self.lastRenderedPickerItems || !self.lastRenderedPickerSelectable {
                        self.paintAgentPicker(agents: terminal.state.agents, requestId: requestId)
                    } else {
                        self.agentRowKeys = fresh.keys
                    }
                }
            }
        }

        if Thread.isMainThread {
            MainActor.assumeIsolated { performInitialPaintAndSync() }
        } else {
            Task { @MainActor in performInitialPaintAndSync() }
        }
    }

    @MainActor
    private func paintAgentPicker(agents: [MacTerminal.AgentItem], requestId: String?) {
        let built = Self.buildAgentPickerModel(from: agents)
        agentRowKeys = built.keys
        lastRenderedPickerItems = built.rows
        lastRenderedPickerSelectable = true
        sendHudList(
            [
                "title": "AGENT TERMINAL",
                "items": built.rows,
                "hint": "tap: choose agent · 2x tap: back",
                "selectable": true,
            ],
            requestId: requestId
        )
    }

    static func buildAgentPickerModel(from agents: [MacTerminal.AgentItem]) -> (keys: [String], rows: [String]) {
        var keys: [String] = []
        var rows: [String] = []
        for agent in agents.prefix(18) {
            let key = agent.name.isEmpty ? agent.provider : agent.name
            guard !key.isEmpty else { continue }
            keys.append(key)
            rows.append(formatAgentPickerRow(agent))
        }
        return (keys, rows)
    }

    static func formatAgentPickerRow(_ agent: MacTerminal.AgentItem) -> String {
        let title = agent.displayTitle
        if !agent.installed || !agent.loggedIn {
            return "\(title) · setup needed"
        }
        let approvals = max(agent.pendingApprovals, agent.pendingApprovalItems.count)
        if approvals > 0 {
            return "\(title) · approval needed"
        }
        if agent.busy {
            if let summary = activeSummary(for: agent), !summary.isEmpty {
                return "\(title) · running · \(summary)"
            }
            return "\(title) · running"
        }
        return "\(title) · ready"
    }

    // MARK: - Voice Entry Points

    /// Voice opens:
    /// - `"open claude"` / `"open codex"` / `"open antigravity"` -> jumps straight
    ///   into that agent's Level-2 session picker (`AGENT TERMINAL · <LANE>`).
    /// - `"open terminal"` / `"open agent terminal"` -> opens Level-1 `AGENT TERMINAL`.
    func openByVoice(agentHint: String) {
        let requestId = "voice-" + ConfirmGate.hex(6)
        guard let targetProvider = Self.resolveAgentHint(agentHint) else {
            openPicker(requestId: requestId)
            return
        }

        stopPolling()
        if ownsActivePrompt {
            ownsActivePrompt = false
            prompts.cancel("opened Agent Terminal by voice")
        }
        visitRevision += 1
        let rev = visitRevision
        screen = .sessionPicker(laneKey: targetProvider, requestId: requestId)
        lastEmittedPayloadJSON = nil
        optimisticTurn = nil
        lastInlineError = nil
        menu?.syncTerminalVisit(requestId: requestId)

        let performInitialPaintAndSync: @MainActor () -> Void = { [weak self] in
            guard let self, self.visitRevision == rev else { return }
            let terminal = self.activeTerminal
            terminal.reloadConfig()
            guard terminal.configured else {
                self.screen = .agentPicker(requestId: requestId)
                self.sendHudList(
                    [
                        "title": "AGENT TERMINAL",
                        "items": [
                            "Mac Agent Terminal is not linked yet.",
                            "Pair your Mac over Tailscale in JOAT Brain.",
                        ],
                        "hint": "2x tap: back",
                        "selectable": false,
                    ],
                    requestId: requestId
                )
                return
            }

            let hadCached = !terminal.state.agents.isEmpty
            if hadCached {
                let resolvedLane = Self.findAgent(matching: targetProvider, in: terminal.state.agents).map {
                    $0.name.isEmpty ? $0.provider : $0.name
                } ?? targetProvider
                self.screen = .sessionPicker(laneKey: resolvedLane, requestId: requestId)
                self.paintSessionPicker(laneKey: resolvedLane, agents: terminal.state.agents, requestId: requestId)
            } else {
                self.sessionRows = []
                self.lastRenderedPickerItems = ["Loading sessions from your Mac…"]
                self.lastRenderedPickerSelectable = false
                self.sendHudList(
                    [
                        "title": "AGENT TERMINAL · \(targetProvider.uppercased())",
                        "items": ["Loading sessions from your Mac…"],
                        "hint": "2x tap: back",
                        "selectable": false,
                    ],
                    requestId: requestId
                )
            }

            Task { @MainActor [weak self] in
                guard let self, self.visitRevision == rev else { return }
                await terminal.fetchState(quiet: hadCached)
                guard self.visitRevision == rev,
                      case .sessionPicker(_, let curReq) = self.screen,
                      curReq == requestId else { return }

                if terminal.state.agents.isEmpty {
                    if case .failed(let err) = terminal.connectionState {
                        self.sendHudList(
                            [
                                "title": "AGENT TERMINAL · \(targetProvider.uppercased())",
                                "items": [
                                    "Can't reach your Mac over Tailscale.",
                                    Self.compactText(err, maxChars: 60),
                                ],
                                "hint": "2x tap: back",
                                "selectable": false,
                            ],
                            requestId: requestId
                        )
                    } else {
                        self.paintSessionPicker(laneKey: targetProvider, agents: [], requestId: requestId)
                    }
                    return
                }

                let resolvedLane = Self.findAgent(matching: targetProvider, in: terminal.state.agents).map {
                    $0.name.isEmpty ? $0.provider : $0.name
                } ?? targetProvider
                self.screen = .sessionPicker(laneKey: resolvedLane, requestId: requestId)
                self.agentRowKeys = Self.buildAgentPickerModel(from: terminal.state.agents).keys
                let fresh = Self.buildSessionPickerModel(laneKey: resolvedLane, agents: terminal.state.agents)
                if !hadCached || fresh.rows != self.lastRenderedPickerItems || !self.lastRenderedPickerSelectable {
                    self.paintSessionPicker(laneKey: resolvedLane, agents: terminal.state.agents, requestId: requestId)
                } else {
                    self.sessionRows = fresh.actions
                }
            }
        }

        if Thread.isMainThread {
            MainActor.assumeIsolated { performInitialPaintAndSync() }
        } else {
            Task { @MainActor in performInitialPaintAndSync() }
        }
    }

    /// Opens a live Level-3 5-container `agent_terminal.view` directly for a voice tool
    /// action (e.g., `PhoneToolBelt` `agent_terminal_action` with `open_on_glasses: true`).
    /// Emits the voice prelude (`AGENT TERMINAL`, `selectable: false`) so `CapabilityMenu.ts`
    /// binds `terminalOpening = { requestId, selectionId: requestId }`, followed immediately
    /// by `agent_terminal.view`.
    @MainActor
    func openSessionByVoice(provider: String) {
        stopPolling()
        if ownsActivePrompt {
            ownsActivePrompt = false
            prompts.cancel("opened live Agent Terminal session")
        }
        let requestId = "voice-" + ConfirmGate.hex(6)
        let terminal = activeTerminal
        let resolvedLane = Self.findAgent(matching: provider, in: terminal.state.agents).map {
            $0.name.isEmpty ? $0.provider : $0.name
        } ?? (Self.resolveAgentHint(provider) ?? provider)
        let displayTitle = Self.findAgent(matching: resolvedLane, in: terminal.state.agents)?.displayTitle
            ?? Self.defaultDisplayTitle(for: resolvedLane)

        visitRevision += 1
        screen = .liveSession(laneKey: resolvedLane, requestId: requestId, selectionId: requestId)
        lastEmittedPayloadJSON = nil
        lastInlineError = nil
        menu?.syncTerminalVisit(requestId: requestId)

        sendHudList(
            [
                "title": "AGENT TERMINAL",
                "items": ["Opening \(displayTitle)…"],
                "hint": CapabilityMenu.backHint,
                "selectable": false,
            ],
            requestId: requestId
        )
        emitTerminalView(laneKey: resolvedLane, requestId: requestId, selectionId: requestId, force: true)
        startPolling()
    }

    static func resolveAgentHint(_ raw: String) -> String? {
        let cleaned = raw
            .lowercased()
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
        switch cleaned {
        case "claude", "claude code":
            return "claude"
        case "codex", "openai":
            return "codex"
        case "antigravity", "gemini":
            return "antigravity"
        default:
            return nil
        }
    }

    // MARK: - Level 2: Session Picker (`AGENT TERMINAL · <LANE_UPPER>`)

    @MainActor
    private func openSessionPickerFromAgentTap(laneKey: String, requestId: String?) {
        stopPolling()
        visitRevision += 1
        let rev = visitRevision
        screen = .sessionPicker(laneKey: laneKey, requestId: requestId)
        lastEmittedPayloadJSON = nil
        optimisticTurn = nil
        lastInlineError = nil

        let terminal = activeTerminal
        paintSessionPicker(laneKey: laneKey, agents: terminal.state.agents, requestId: requestId)

        Task { @MainActor [weak self] in
            guard let self, self.visitRevision == rev else { return }
            await terminal.fetchState(quiet: true)
            guard self.visitRevision == rev,
                  case .sessionPicker(let curLane, let curReq) = self.screen,
                  curLane == laneKey, curReq == requestId else { return }
            let fresh = Self.buildSessionPickerModel(laneKey: laneKey, agents: terminal.state.agents)
            if fresh.rows != self.lastRenderedPickerItems {
                self.paintSessionPicker(laneKey: laneKey, agents: terminal.state.agents, requestId: requestId)
            } else {
                self.sessionRows = fresh.actions
            }
        }
    }

    @MainActor
    private func paintSessionPicker(laneKey: String, agents: [MacTerminal.AgentItem], requestId: String?) {
        let built = Self.buildSessionPickerModel(laneKey: laneKey, agents: agents)
        sessionRows = built.actions
        lastRenderedPickerItems = built.rows
        lastRenderedPickerSelectable = true
        sendHudList(
            [
                "title": "AGENT TERMINAL · \(built.laneUpper)",
                "items": built.rows,
                "hint": "tap: open · 2x tap: agents",
                "selectable": true,
            ],
            requestId: requestId
        )
    }

    private static func buildSessionPickerModel(
        laneKey: String,
        agents: [MacTerminal.AgentItem]
    ) -> (laneUpper: String, rows: [String], actions: [SessionRowAction]) {
        let agent = findAgent(matching: laneKey, in: agents)
        let canonicalLane = agent.map { $0.name.isEmpty ? $0.provider : $0.name } ?? laneKey
        let laneUpper = canonicalLane.uppercased()

        var rows: [String] = []
        var actions: [SessionRowAction] = []

        if let agent {
            let trimmedTitle = agent.activeSessionTitle?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !trimmedTitle.isEmpty {
                rows.append("Active · \(compactText(trimmedTitle, maxChars: 48))")
                actions.append(.active)
            } else if !agent.recentMessages.isEmpty {
                let fallbackTitle = activeSummary(for: agent) ?? "Current session"
                rows.append("Active · \(fallbackTitle)")
                actions.append(.active)
            }
        }

        rows.append("+ New session")
        actions.append(.newSession)

        if let agent {
            for session in agent.sessions.prefix(max(0, 18 - rows.count)) {
                let key = session.key.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !key.isEmpty else { continue }
                let label = session.label.trimmingCharacters(in: .whitespacesAndNewlines)
                rows.append(label.isEmpty ? key : label)
                actions.append(.resume(key: key))
            }
        }

        return (laneUpper, rows, actions)
    }

    // MARK: - Level 3: Live 5-Container Terminal (`agent_terminal.view`)

    @MainActor
    private func openLiveSessionFromPicker(
        laneKey: String,
        action: SessionRowAction,
        requestId: String,
        selectionId: String
    ) {
        stopPolling()
        if ownsActivePrompt {
            ownsActivePrompt = false
            prompts.cancel("opened terminal session")
        }
        visitRevision += 1
        let rev = visitRevision
        screen = .liveSession(laneKey: laneKey, requestId: requestId, selectionId: selectionId)
        lastEmittedPayloadJSON = nil
        optimisticTurn = nil
        lastInlineError = nil

        Task { @MainActor [weak self] in
            guard let self, self.visitRevision == rev else { return }
            let terminal = self.activeTerminal
            let agent = Self.findAgent(matching: laneKey, in: terminal.state.agents)
            let targetName = agent.map { $0.name.isEmpty ? $0.provider : $0.name } ?? laneKey

            switch action {
            case .active:
                self.emitTerminalView(laneKey: targetName, requestId: requestId, selectionId: selectionId, force: true)
                await terminal.fetchState(quiet: true)
                guard self.visitRevision == rev,
                      case .liveSession(let curLane, let curReq, let curSel) = self.screen,
                      curLane == laneKey, curReq == requestId, curSel == selectionId else { return }
                self.emitTerminalView(laneKey: targetName, requestId: requestId, selectionId: selectionId, force: false)

            case .newSession:
                let ok = await terminal.newSession(lane: targetName)
                guard self.visitRevision == rev,
                      case .liveSession(let curLane, let curReq, let curSel) = self.screen,
                      curLane == laneKey, curReq == requestId, curSel == selectionId else { return }
                if !ok {
                    self.lastInlineError = terminal.actionError ?? "Could not start a new session."
                }
                self.emitTerminalView(laneKey: targetName, requestId: requestId, selectionId: selectionId, force: true)

            case .resume(let sessionKey):
                let ok = await terminal.resumeSession(provider: targetName, sessionKey: sessionKey)
                guard self.visitRevision == rev,
                      case .liveSession(let curLane, let curReq, let curSel) = self.screen,
                      curLane == laneKey, curReq == requestId, curSel == selectionId else { return }
                if !ok {
                    self.lastInlineError = terminal.actionError ?? "That session could not be resumed."
                }
                self.emitTerminalView(laneKey: targetName, requestId: requestId, selectionId: selectionId, force: true)
            }

            self.startPolling()
        }
    }

    @MainActor
    private func emitCurrentTerminalViewIfOpen(force: Bool) {
        guard case .liveSession(let laneKey, let reqId, let selId) = screen else { return }
        emitTerminalView(laneKey: laneKey, requestId: reqId, selectionId: selId, force: force)
    }

    @MainActor
    private func emitTerminalView(
        laneKey: String,
        requestId: String,
        selectionId: String,
        force: Bool
    ) {
        let terminal = activeTerminal
        let agents = terminal.state.agents
        let agent = Self.findAgent(matching: laneKey, in: agents)
        let laneName = agent.map { $0.name.isEmpty ? $0.provider : $0.name } ?? laneKey

        // Reconcile optimistic user turn once the Mac snapshot reflects it.
        if let opt = optimisticTurn, opt.laneKey == laneKey {
            if let agent, agent.recentMessages.contains(where: {
                $0.role.lowercased() == "user"
                    && $0.text.trimmingCharacters(in: .whitespacesAndNewlines) == opt.userText
            }) {
                optimisticTurn = nil
            }
        }

        let payload = Self.buildTerminalViewPayload(
            laneName: laneName,
            agent: agent,
            allAgents: agents,
            configured: terminal.configured,
            connectionState: terminal.connectionState,
            requestId: requestId,
            selectionId: selectionId,
            optimisticTurn: (optimisticTurn?.laneKey == laneKey) ? optimisticTurn : nil,
            inlineError: lastInlineError
        )

        let canonicalJSON: String? = {
            guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
                return nil
            }
            return String(data: data, encoding: .utf8)
        }()

        if !force, let canonicalJSON, canonicalJSON == lastEmittedPayloadJSON {
            return
        }
        lastEmittedPayloadJSON = canonicalJSON
        link.send(payload)
        let termObj = payload["terminal"] as? [String: Any]
        Log.add("tx agent_terminal.view \(termObj?["focus"] as? String ?? laneName) approvals=\(termObj?["approvals"] as? Int ?? 0)")
    }

    private static func buildTerminalViewPayload(
        laneName: String,
        agent: MacTerminal.AgentItem?,
        allAgents: [MacTerminal.AgentItem],
        configured: Bool,
        connectionState: MacTerminal.ConnectionState,
        requestId: String,
        selectionId: String,
        optimisticTurn: OptimisticTurn?,
        inlineError: String?
    ) -> [String: Any] {
        let approvalCount = agent.map { max($0.pendingApprovals, $0.pendingApprovalItems.count) } ?? 0
        let isBusy = (agent?.busy == true) || (optimisticTurn?.running == true)

        let state: String
        if !configured {
            state = "unavailable"
        } else if isBusy {
            state = "running"
        } else if let agent, (agent.installed && agent.loggedIn) || approvalCount > 0 {
            state = "ready"
        } else {
            state = "unavailable"
        }

        let focus = String("\(laneName) · \(state)".prefix(96))
        let lanes: [String]
        if allAgents.isEmpty {
            lanes = [String("\(laneName): \(isBusy ? "running" : "ready")".prefix(48))]
        } else {
            lanes = Array(allAgents.prefix(3).map { item -> String in
                let itemName = item.name.isEmpty ? item.provider : item.name
                let itemBusy = item.busy || (itemName.caseInsensitiveCompare(laneName) == .orderedSame && isBusy)
                return String("\(itemName): \(itemBusy ? "running" : "ready")".prefix(48))
            })
        }

        var rawMessages = agent?.recentMessages ?? []
        if let opt = optimisticTurn {
            let alreadyPresent = rawMessages.contains {
                $0.role.lowercased() == "user"
                    && $0.text.trimmingCharacters(in: .whitespacesAndNewlines) == opt.userText
            }
            if !alreadyPresent {
                rawMessages.append(MacTerminal.RecentMessage(role: "user", text: opt.userText))
            }
        }
        let retainedMessages = Array(rawMessages.suffix(48))
        let mappedMessages: [[String: String]] = retainedMessages.enumerated().map { idx, msg in
            [
                "id": "m-\(idx)",
                "role": wireRole(msg.role),
                "text": String(msg.text.prefix(4000)),
            ]
        }

        var reason = ""
        var activityLines: [String] = []

        if let inlineError, !inlineError.isEmpty {
            let cleanErr = compactText(inlineError, maxChars: 160)
            reason = String(cleanErr.prefix(64))
            activityLines.append("[!] \(cleanErr)")
        } else if !configured {
            reason = "Mac Agent Terminal is not linked yet."
            activityLines.append("Pair your Mac over Tailscale in JOAT Brain.")
        } else if case .failed(let connErr) = connectionState, agent == nil {
            let cleanErr = compactText(connErr, maxChars: 160)
            reason = String(cleanErr.prefix(64))
            activityLines.append(cleanErr)
        } else if let agent {
            if state == "unavailable" {
                let msg = !agent.installed
                    ? "\(agent.displayTitle) is not installed on your Mac."
                    : "Sign in to \(agent.displayTitle) in JOAT Brain › Agents."
                reason = String(msg.prefix(64))
                activityLines.append(msg)
            } else if !agent.statusDetail.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                activityLines.append(agent.statusDetail.trimmingCharacters(in: .whitespacesAndNewlines))
            } else {
                activityLines.append(isBusy ? "Working…" : "Ready")
            }
        } else {
            activityLines.append(isBusy ? "Working…" : "Ready")
        }

        if approvalCount > 0 {
            let firstApprovalSummary = firstApprovalSummaryText(for: agent)
            activityLines.append("[approval] \(firstApprovalSummary)")
            activityLines.append("[!] Approval needed: \(firstApprovalSummary)")
        }

        let activity = String(activityLines.joined(separator: "\n").prefix(1200))

        return [
            "kind": "agent_terminal.view",
            "terminal": [
                "openingRequestId": String(requestId.prefix(128)),
                "openingSelectionId": String(selectionId.prefix(128)),
                "state": state,
                "focus": focus,
                "lanes": lanes,
                "activity": activity,
                "terminalReason": String(reason.prefix(64)),
                "approvals": approvalCount,
                "conversationId": String("\(laneName)-session".prefix(128)),
                "messages": mappedMessages,
                "historyTruncated": false,
                "inputRequired": false,
                "taskRecovery": false,
            ] as [String: Any],
        ]
    }

    private static func wireRole(_ raw: String) -> String {
        switch raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "user":
            return "user"
        case "status", "tool", "system", "notice":
            return "status"
        default:
            return "assistant"
        }
    }

    private static func firstApprovalSummaryText(for agent: MacTerminal.AgentItem?) -> String {
        guard let item = agent?.pendingApprovalItems.first else {
            return "Tool execution requested"
        }
        let summary = item.summary.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        let kind = item.kind.trimmingCharacters(in: .whitespacesAndNewlines)
        if summary.isEmpty {
            return kind.isEmpty ? "Tool execution requested" : kind
        }
        if kind.isEmpty || summary.lowercased().hasPrefix(kind.lowercased() + ":") {
            return compactText(summary, maxChars: 180)
        }
        return compactText("\(kind): \(summary)", maxChars: 180)
    }

    // MARK: - Live Polling (Level 3 Only)

    @MainActor
    private func startPolling() {
        guard pollTask == nil else { return }
        let rev = visitRevision
        pollTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self, self.visitRevision == rev,
                      case .liveSession(let laneKey, let reqId, let selId) = self.screen else {
                    self?.pollTask = nil
                    return
                }
                let terminal = self.activeTerminal
                let agent = Self.findAgent(matching: laneKey, in: terminal.state.agents)
                let isBusy = (agent?.busy == true) || (self.optimisticTurn?.running == true)
                let delaySeconds = max(0.05, isBusy ? self.busyPollSeconds : self.idlePollSeconds)
                try? await Task.sleep(nanoseconds: UInt64(delaySeconds * 1_000_000_000))
                guard !Task.isCancelled, self.visitRevision == rev,
                      case .liveSession(let curLane, let curReq, let curSel) = self.screen,
                      curLane == laneKey, curReq == reqId, curSel == selId else {
                    self.pollTask = nil
                    return
                }
                guard terminal.configured else { continue }
                await terminal.fetchState(quiet: true)
                guard !Task.isCancelled, self.visitRevision == rev,
                      case .liveSession(let afterLane, let afterReq, let afterSel) = self.screen,
                      afterLane == laneKey, afterReq == reqId, afterSel == selId else {
                    self.pollTask = nil
                    return
                }
                if case .connected = terminal.connectionState {
                    self.lastInlineError = nil
                }
                self.emitTerminalView(laneKey: laneKey, requestId: reqId, selectionId: selId, force: false)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    // MARK: - Uplinks (`input` & `terminal.action`)

    func handle(kind: String, message: [String: Any]) {
        switch kind {
        case "input":
            handleInput(message)
        case "terminal.action":
            handleTerminalAction(message)
        default:
            break
        }
    }

    private func handleInput(_ message: [String: Any]) {
        guard let event = message.object("event"),
              let container = event.string("containerName") else { return }
        let selected = event.string("selectedName")
        let requestId = event.string("requestId")

        switch container {
        case "capdata":
            guard let selected, let index = event.int("selectedIndex") else { return }
            let upper = selected.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            guard upper == "AGENT TERMINAL" || upper.hasPrefix("AGENT TERMINAL · ") else { return }

            switch screen {
            case .agentPicker(let curReq):
                guard requestId == curReq else { return }
                guard agentRowKeys.indices.contains(index) else {
                    Log.add("glasses terminal: agent row \(index) out of bounds (\(agentRowKeys.count)); refreshing")
                    openPicker(requestId: curReq)
                    return
                }
                let pickedLane = agentRowKeys[index]
                Task { @MainActor [weak self] in
                    self?.openSessionPickerFromAgentTap(laneKey: pickedLane, requestId: curReq)
                }

            case .sessionPicker(let laneKey, let curReq):
                guard requestId == curReq else { return }
                if upper.hasPrefix("AGENT TERMINAL · "),
                   upper != "AGENT TERMINAL · \(laneKey.uppercased())" {
                    return
                }
                guard sessionRows.indices.contains(index) else {
                    Log.add("glasses terminal: session row \(index) out of bounds (\(sessionRows.count))")
                    return
                }
                let action = sessionRows[index]
                let resolvedReq = curReq ?? requestId ?? ("voice-" + ConfirmGate.hex(6))
                let traceId = event.string("traceId") ?? resolvedReq
                Task { @MainActor [weak self] in
                    self?.openLiveSessionFromPicker(
                        laneKey: laneKey,
                        action: action,
                        requestId: resolvedReq,
                        selectionId: traceId
                    )
                }

            default:
                break
            }

        case "capability-lifecycle":
            guard let selected else { return }
            switch selected {
            case "terminal-home":
                guard isMatchingRequest(requestId) else { return }
                if let trace = event.string("traceId"),
                   case .liveSession(_, _, let selId) = screen,
                   trace != selId {
                    return
                }
                retireInternal(notifyMenu: true)

            case "terminal-agents":
                guard screen != .closed, isMatchingRequest(requestId) else { return }
                let resolvedReq = requestId ?? currentRequestId
                stopPolling()
                if ownsActivePrompt {
                    ownsActivePrompt = false
                    prompts.cancel("returned to terminal agents")
                }
                visitRevision += 1
                screen = .agentPicker(requestId: resolvedReq)
                sessionRows = []
                lastEmittedPayloadJSON = nil
                optimisticTurn = nil
                lastInlineError = nil
                menu?.syncTerminalVisit(requestId: resolvedReq)
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let built = Self.buildAgentPickerModel(from: self.activeTerminal.state.agents)
                    self.agentRowKeys = built.keys
                }

            case "terminal-sessions":
                guard let laneKey = currentLaneKey, isMatchingRequest(requestId) else { return }
                let resolvedReq = requestId ?? currentRequestId
                stopPolling()
                if ownsActivePrompt {
                    ownsActivePrompt = false
                    prompts.cancel("returned to terminal sessions")
                }
                visitRevision += 1
                let rev = visitRevision
                screen = .sessionPicker(laneKey: laneKey, requestId: resolvedReq)
                lastEmittedPayloadJSON = nil
                optimisticTurn = nil
                lastInlineError = nil
                menu?.syncTerminalVisit(requestId: resolvedReq)
                Task { @MainActor [weak self] in
                    guard let self, self.visitRevision == rev else { return }
                    let terminal = self.activeTerminal
                    let cached = Self.buildSessionPickerModel(laneKey: laneKey, agents: terminal.state.agents)
                    self.sessionRows = cached.actions
                    self.lastRenderedPickerItems = cached.rows
                    self.lastRenderedPickerSelectable = true
                    await terminal.fetchState(quiet: true)
                    guard self.visitRevision == rev,
                          case .sessionPicker(let curLane, let curReq) = self.screen,
                          curLane == laneKey, curReq == resolvedReq else { return }
                    self.paintSessionPicker(laneKey: laneKey, agents: terminal.state.agents, requestId: resolvedReq)
                }

            default:
                if selected.hasPrefix("surface-"), !selected.hasPrefix("surface-context-menu") {
                    retireInternal(notifyMenu: false)
                }
            }

        case "menu":
            if selected == "closed" {
                retireInternal(notifyMenu: false)
            }

        default:
            break
        }
    }

    private func handleTerminalAction(_ message: [String: Any]) {
        guard case .liveSession(let laneKey, let reqId, let selId) = screen,
              let action = message.string("action") else { return }

        Task { @MainActor [weak self] in
            guard let self,
                  case .liveSession(let curLane, let curReq, let curSel) = self.screen,
                  curLane == laneKey, curReq == reqId, curSel == selId else { return }

            let terminal = self.activeTerminal
            let agent = Self.findAgent(matching: laneKey, in: terminal.state.agents)
            let providerName = agent.map { $0.name.isEmpty ? $0.provider : $0.name } ?? laneKey
            let displayTitle = agent?.displayTitle ?? Self.defaultDisplayTitle(for: laneKey)

            switch action {
            case "begin_terminal_input":
                self.ownsActivePrompt = true
                self.prompts.ask(
                    "What should \(displayTitle) do?",
                    ownerKind: "terminal",
                    ownerId: "term-" + ConfirmGate.hex(8),
                    field: "instruction"
                ) { [weak self] said in
                    guard let self else { return }
                    self.ownsActivePrompt = false
                    let clean = said?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    guard !clean.isEmpty else { return }
                    Task { @MainActor [weak self] in
                        guard let self,
                              case .liveSession(let activeLane, let activeReq, let activeSel) = self.screen,
                              activeLane == laneKey, activeReq == reqId, activeSel == selId else { return }
                        await self.submitSpokenInstruction(
                            clean,
                            laneKey: laneKey,
                            providerName: providerName,
                            requestId: reqId,
                            selectionId: selId
                        )
                    }
                }

            case "approve_terminal":
                self.lastInlineError = nil
                let ok = await terminal.decideApproval(decision: "approve", provider: providerName)
                if !ok { self.lastInlineError = terminal.actionError }
                self.emitCurrentTerminalViewIfOpen(force: false)
                self.startPolling()

            case "deny_terminal":
                self.lastInlineError = nil
                let ok = await terminal.decideApproval(decision: "deny", provider: providerName)
                if !ok { self.lastInlineError = terminal.actionError }
                self.emitCurrentTerminalViewIfOpen(force: false)
                self.startPolling()

            case "cancel_terminal":
                self.optimisticTurn = nil
                self.lastInlineError = nil
                let ok = await terminal.cancelRun(provider: providerName)
                if !ok { self.lastInlineError = terminal.actionError }
                self.emitCurrentTerminalViewIfOpen(force: false)
                self.startPolling()

            case "retry_terminal":
                self.lastInlineError = nil
                await terminal.refresh()
                self.emitCurrentTerminalViewIfOpen(force: true)
                self.startPolling()

            default:
                break
            }
        }
    }

    @MainActor
    private func submitSpokenInstruction(
        _ instruction: String,
        laneKey: String,
        providerName: String,
        requestId: String,
        selectionId: String
    ) async {
        lastInlineError = nil
        optimisticTurn = OptimisticTurn(laneKey: laneKey, userText: instruction, running: true)
        emitTerminalView(laneKey: laneKey, requestId: requestId, selectionId: selectionId, force: false)
        startPolling()

        let terminal = activeTerminal
        let ok = await terminal.sendInstruction(provider: providerName, instruction: instruction)
        guard case .liveSession(let curLane, let curReq, let curSel) = screen,
              curLane == laneKey, curReq == requestId, curSel == selectionId else { return }

        if !ok {
            optimisticTurn = nil
            lastInlineError = terminal.actionError ?? "Could not send instruction to your Mac."
            emitTerminalView(laneKey: laneKey, requestId: requestId, selectionId: selectionId, force: false)
            return
        }

        // Keep the user text visible if `/v1/config` returned before appending to `recentMessages`,
        // while letting `agent.busy` from the fresh snapshot drive the running state.
        if var opt = optimisticTurn, opt.laneKey == laneKey {
            opt.running = false
            optimisticTurn = opt
        }
        emitTerminalView(laneKey: laneKey, requestId: requestId, selectionId: selectionId, force: false)
        startPolling()
    }

    // MARK: - Helpers

    private var currentRequestId: String? {
        switch screen {
        case .closed: return nil
        case .agentPicker(let r), .sessionPicker(_, let r): return r
        case .liveSession(_, let r, _): return r
        }
    }

    private var currentLaneKey: String? {
        switch screen {
        case .closed, .agentPicker: return nil
        case .sessionPicker(let lane, _), .liveSession(let lane, _, _): return lane
        }
    }

    private func isMatchingRequest(_ incoming: String?) -> Bool {
        guard let incoming else { return true }
        guard let current = currentRequestId else { return true }
        return incoming == current
    }

    private func sendHudList(_ list: [String: Any], requestId: String?) {
        if let menu {
            menu.sendList(list, requestId: requestId)
            return
        }
        var wire = list
        wire["kind"] = "hud.list"
        let items = (list["items"] as? [String] ?? []).prefix(20).map { $0.utf16Prefix(64) }
        wire["items"] = Array(items)
        if let t = list["title"] as? String { wire["title"] = t.utf16Prefix(46) }
        if let h = list["hint"] as? String { wire["hint"] = h.utf16Prefix(96) }
        if wire["selectable"] == nil { wire["selectable"] = false }
        if let requestId { wire["requestId"] = String(requestId.prefix(128)) }
        link.send(wire)
    }

    static func findAgent(matching key: String, in agents: [MacTerminal.AgentItem]) -> MacTerminal.AgentItem? {
        let lower = key.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !lower.isEmpty else { return nil }
        return agents.first {
            $0.name.lowercased() == lower
                || $0.provider.lowercased() == lower
                || $0.displayTitle.lowercased() == lower
        }
    }

    static func defaultDisplayTitle(for key: String) -> String {
        switch key.lowercased() {
        case "claude", "claude code": return "Claude Code"
        case "codex": return "Codex"
        case "antigravity": return "Antigravity"
        default:
            return key.isEmpty ? "Agent" : key.prefix(1).uppercased() + key.dropFirst()
        }
    }

    private static func activeSummary(for agent: MacTerminal.AgentItem) -> String? {
        if let title = agent.activeSessionTitle?.trimmingCharacters(in: .whitespacesAndNewlines),
           !title.isEmpty {
            return compactText(title, maxChars: 34)
        }
        let detail = agent.statusDetail.trimmingCharacters(in: .whitespacesAndNewlines)
        if !detail.isEmpty, detail.lowercased() != "ready", detail.lowercased() != "idle" {
            return compactText(detail, maxChars: 34)
        }
        if let lastUser = agent.recentMessages.last(where: { $0.role.lowercased() == "user" }) {
            let text = compactText(lastUser.text, maxChars: 34)
            if !text.isEmpty { return text }
        }
        if let lastAny = agent.recentMessages.last {
            let text = compactText(lastAny.text, maxChars: 34)
            if !text.isEmpty { return text }
        }
        return nil
    }

    private static func compactText(_ raw: String, maxChars: Int) -> String {
        let oneLine = raw.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard oneLine.count > maxChars else { return oneLine }
        return String(oneLine.prefix(max(1, maxChars - 1))).trimmingCharacters(in: .whitespaces) + "…"
    }
}
