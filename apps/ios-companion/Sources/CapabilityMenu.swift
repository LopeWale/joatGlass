import Foundation

/// The glasses' capability menu, answered on the phone: a port of
/// services/capabilities.py CapabilityRouter.
///
/// Every capability — including `Messages`, `Agent Terminal`, and `Activity` —
/// stays 100% on `JOAT Brain` (`ws://127.0.0.1:8787/glasses`), relaying Mac
/// Messages (`/messages`) and Mac Agent Terminal (`/terminal`) over Tailscale
/// in the background so speech recognition and JOAT Assistant never leave the
/// iPhone.
final class CapabilityMenu: BrainFeature, VoiceCommand {
    let name = "capabilities"
    let uplinkKinds: Set<String> = ["input", "hello"]
    /// Zero capabilities hand off the WebSocket away from the iPhone brain.
    static let macPages: Set<String> = []
    private var pluginHandsOff = false

    /// capabilities.py BACK_HINT
    static let backHint = "left x2: back"
    static let planningNames: Set<String> = ["PLANNING", "CALENDAR", "TASKS"]

    private let link: PluginLink
    private let panels: PanelPublisher
    let reminders: Reminders
    /// The Reminders page's rows, bound to reminder ids.
    private var reminderIds: [String?] = []
    private var cachedOpenRemindersCount: Int?
    private let cards: Cards
    let quickNotes: QuickNotes
    /// The Navigation page's question and saved places (Brain sets it).
    var navigation: Navigation?
    /// The Messages page's conversations (Brain sets it).
    var messages: Messages?
    /// The Messages page's rows, bound to senders; empty on a thread.
    private var messageSenders: [String] = []
    private var messagesShowingList = false
    /// The Mail page's unified Gmail threads (Brain sets it).
    var mail: Mail?
    /// The Mail page's rows, bound to thread ids (`accountId|threadKey`).
    private var mailThreadIds: [String] = []
    private var mailShowingList = false
    /// Mac Agent Terminal state & glasses 3-level terminal controller (Brain sets them).
    var macTerminal: MacTerminal?
    var glassesTerminal: GlassesTerminal?
    /// Pomodoro / Timer state for `ACTIVITY` summary (Brain sets it).
    var pomodoro: Pomodoro?
    /// The Notifications page's rows, bound to card ids (a new arrival
    /// cannot shift what a tap opens).
    private var notificationRows: [Cards.Card] = []
    private(set) var activeCapability: String?
    private(set) var activeRequestId: String?
    var isMessagesConversationOnGlass: Bool { activeCapability == "MESSAGES" && !messagesShowingList }
    var isMailConversationOnGlass: Bool { activeCapability == "MAIL" && !mailShowingList }

    init(link: PluginLink, panels: PanelPublisher, reminders: Reminders, cards: Cards, quickNotes: QuickNotes) {
        self.link = link
        self.panels = panels
        self.reminders = reminders
        self.cards = cards
        self.quickNotes = quickNotes
    }

    /// A new plugin connection starts with no page open (on_disconnect).
    func pluginConnected() {
        pluginHandsOff = false
        retire()
    }

    private func retire() {
        if activeCapability == "MESSAGES" { messages?.leaveUI() }
        if activeCapability == "MAIL" { mail?.leaveUI() }
        if activeCapability == "AGENT TERMINAL" { glassesTerminal?.retireFromMenu() }
        activeCapability = nil
        activeRequestId = nil
        reminderIds = []
        messagesShowingList = false
        mailShowingList = false
        quickNotes.departed()
    }

    private var planningActive: Bool { activeCapability.map(Self.planningNames.contains) ?? false }

    /// capabilities._canonical_capability
    static func canonical(_ name: String) -> String {
        let n = name.trimmingCharacters(in: .whitespaces).uppercased()
        if n == "REMINDERS" { return "TASKS" }
        return n
    }

    func handle(kind: String, message: [String: Any]) {
        if kind == "hello" {
            if let explicit = message["handoff"] as? Bool {
                pluginHandsOff = explicit
            } else if let bridged = message.bool("handoff") {
                pluginHandsOff = bridged
            } else {
                pluginHandsOff = false
            }
            Log.add("plugin hello: phone-local capabilities active")
            // A new plugin page starts on Home: the phone locked and Even
            // reloaded the plugin in the background, or the link dropped and
            // came back. A note still open goes back on the glasses.
            if quickNotes.active, activeCapability != "QUICKNOTES" {
                Log.add("quicknotes: a new plugin page while a note is open; showing the note again")
                openByVoice("QuickNotes")
            }
            return
        }
        guard kind == "input", let event = message.object("event"),
              let container = event.string("containerName") else { return }
        let selected = event.string("selectedName")
        let requestId = event.string("requestId")
        switch container {
        case "capmenu":
            guard let selected, !selected.isEmpty else { return }
            open(selected, requestId: requestId, anchorMs: event.int("planningAnchorUnixMs"))
        case "capdata":
            guard let selected, let index = event.int("selectedIndex") else { return }
            // Rail tabs (NEWS, POMODORO, SEARCH) are the panel publisher's.
            if PanelPublisher.tab(forTitle: selected) != nil { return }
            // Agent Terminal Level 1 & Level 2 taps are handled by `GlassesTerminal`.
            let canonicalSelected = Self.canonical(selected)
            if canonicalSelected == "AGENT TERMINAL" || canonicalSelected.hasPrefix("AGENT TERMINAL · ") {
                return
            }
            // Planning, Calendar and Tasks are one page (Reminders): opened by
            // voice as "planning", its taps come back as REMINDERS (TASKS).
            let family = Self.planningNames.contains(canonicalSelected) && planningActive
            guard requestId == activeRequestId, canonicalSelected == activeCapability || family else {
                Log.add("dropping stale capability tap \(selected) request=\(requestId ?? "nil")")
                return
            }
            if planningActive {
                if reminderIds.indices.contains(index), let id = reminderIds[index] { reminders.complete(identifier: id) }
            }
            else if activeCapability == "NOTIFICATIONS" { selectNotification(index, requestId: requestId) }
            else if activeCapability == "QUICKNOTES" {
                quickNotes.select(index, traceId: event.string("traceId"), pageIndex: event.int("quickNotesPageIndex"))
            }
            else if activeCapability == "NAVIGATION" { navigation?.selectPageRow(index) }
            else if activeCapability == "MESSAGES", let messages {
                // Row 0 is New message; the senders follow (capabilities.py).
                if index == 0 {
                    messages.beginNew(requestId: requestId, traceId: event.string("traceId"))
                } else if messageSenders.indices.contains(index - 1) {
                    let sender = messageSenders[index - 1]
                    messagesShowingList = false
                    sendList(messages.thread(sender, requestId: requestId, traceId: event.string("traceId")),
                             requestId: requestId)
                } else {
                    Log.add("messages: row \(index) is not on the list this side sent (\(messageSenders.count) senders); sending the list again")
                    showMessages(requestId: requestId)
                }
            }
            else if activeCapability == "MAIL", let mail {
                // Row 0 is "+ New email"; unified inbox threads follow.
                if index == 0 {
                    mail.beginNew(requestId: requestId, traceId: event.string("traceId"))
                } else if mailThreadIds.indices.contains(index - 1) {
                    let threadId = mailThreadIds[index - 1]
                    mailShowingList = false
                    sendList(mail.thread(threadId, requestId: requestId, traceId: event.string("traceId")),
                             requestId: requestId)
                } else {
                    Log.add("mail: row \(index) is not on the list this side sent (\(mailThreadIds.count) threads); sending the list again")
                    showMail(requestId: requestId, triggerSync: true)
                }
            }
        case "capmessage":
            // A tap on the open conversation replies to it.
            guard requestId == activeRequestId else {
                Log.add("dropping stale comms reply request=\(requestId ?? "nil")")
                return
            }
            if activeCapability == "MESSAGES" {
                messages?.beginReply()
            } else if activeCapability == "MAIL" {
                mail?.beginReply()
            } else {
                Log.add("dropping stale comms reply capability=\(activeCapability ?? "nil") request=\(requestId ?? "nil")")
            }
        case "message-cancel":
            if let selected {
                messages?.cancelUI(actionId: selected)
                mail?.cancelUI(actionId: selected)
            }
        case "message-retry":
            if let selected {
                messages?.retryUI(actionId: selected)
                mail?.retryUI(actionId: selected)
            }
        case "cappage":
            if selected == "QUICKNOTES · LIST", activeCapability == "QUICKNOTES", requestId == activeRequestId {
                quickNotes.pageList(eventType: event.int("eventType"), selectedIndex: event.int("selectedIndex"))
            }
        case "capability-lifecycle":
            if selected == "messages-list", activeCapability == "MESSAGES", requestId == activeRequestId {
                messagesShowingList = true
                messages?.returnedToList()
                return
            }
            if selected == "messages-conversation", activeCapability == "MESSAGES", requestId == activeRequestId {
                messagesShowingList = false
                return
            }
            if selected == "messages-home", activeCapability == "MESSAGES",
               requestId == nil || requestId == activeRequestId {
                retire()
                return
            }
            if selected == "mail-list", activeCapability == "MAIL", requestId == activeRequestId {
                mailShowingList = true
                mail?.returnedToList()
                return
            }
            if selected == "mail-conversation", activeCapability == "MAIL", requestId == activeRequestId {
                mailShowingList = false
                return
            }
            if selected == "mail-home", activeCapability == "MAIL",
               requestId == nil || requestId == activeRequestId {
                retire()
                return
            }
            if let selected, selected.hasPrefix("terminal-") {
                // `GlassesTerminal` handles `terminal-home`, `terminal-agents`, and `terminal-sessions`.
                return
            }
            if let selected, selected.hasPrefix("quicknotes-") {
                // A release always ends a hold, wherever the wearer now is.
                if selected == "quicknotes-hold-end"
                    || (activeCapability == "QUICKNOTES" && requestId == activeRequestId) {
                    quickNotes.gesture(selected)
                }
            } else if let selected, selected.hasPrefix("surface-"), !selected.hasPrefix("surface-context-menu") {
                // Home, menu, settings, a rail tab: the Hub moved on locally.
                retire()
            }
        case "menu":
            if selected == "closed" { retire() }
        case "notification-dismiss":
            // A tap on an AI cue's card opens the cue (Cards ignores ids it never raised).
            if activeCapability == "QUICKNOTES", let id = event.string("traceId") { quickNotes.cueTapped(id) }
        default:
            break
        }
    }

    /// Return the glasses to the default resting overview ("go home", "open home").
    func openHome() {
        retire()
        link.send(["kind": "hud.rebuild", "layout": "default"])
        Log.add("tx hud.rebuild default (home)")
    }

    /// capabilities.open: one list generation per request.
    func open(_ name: String, requestId: String?, anchorMs: Int?) {
        if name == "Home" {
            openHome()
            return
        }
        if activeCapability == "MESSAGES" { messages?.leaveUI() }
        if activeCapability == "MAIL" { mail?.leaveUI() }
        if activeCapability == "AGENT TERMINAL", Self.canonical(name) != "AGENT TERMINAL" {
            glassesTerminal?.retireFromMenu()
        }
        reminderIds = []
        activeCapability = Self.canonical(name)
        activeRequestId = requestId
        Log.add("capability open \(name) request=\(requestId ?? "nil")")
        switch name {
        case "Planning", "Calendar", "Tasks", "Reminders":
            Task { @MainActor in
                let page = await self.reminders.listPage()
                guard self.activeRequestId == requestId else { return }
                self.reminderIds = page.ids
                self.sendList(page.list, requestId: requestId)
            }
        // Rail tabs are pushed, not drawn on request (capabilities.open).
        case "News", "Pomodoro", "Search":
            panels.activate(name.lowercased())
        case "Notifications":
            showNotifications(requestId: requestId)
        case "QuickNotes":
            quickNotes.show()
        case "Navigation" where navigation != nil:
            sendList(["title": "NAVIGATION", "items": navigation?.pageRows() ?? [],
                      "hint": "tap: choose · left x2: back", "selectable": true], requestId: requestId)
        case "Messages" where messages != nil:
            showMessages(requestId: requestId)
        case "Mail" where mail != nil:
            showMail(requestId: requestId, triggerSync: true)
        case "Agent Terminal":
            if let glassesTerminal {
                glassesTerminal.openPicker(requestId: requestId)
            } else {
                notOnPhone(name, requestId: requestId)
            }
        case "Activity":
            showActivity(requestId: requestId)
        default:
            notOnPhone(name, requestId: requestId)
        }
    }

    // MARK: - Agent Terminal Coordination

    /// Keeps `CapabilityMenu` synchronized when `GlassesTerminal` opens a
    /// voice generation or returns from Level 3 back to Level 2 / Level 1.
    func syncTerminalVisit(requestId: String?) {
        if activeCapability == "MESSAGES" { messages?.leaveUI() }
        if activeCapability == "MAIL" { mail?.leaveUI() }
        reminderIds = []
        messagesShowingList = false
        mailShowingList = false
        quickNotes.departed()
        activeCapability = "AGENT TERMINAL"
        activeRequestId = requestId
    }

    func leaveTerminalIfOpen() {
        guard activeCapability == "AGENT TERMINAL" else { return }
        activeCapability = nil
        activeRequestId = nil
    }

    // MARK: - Activity (`ACTIVITY` phone-local summary)

    private func showActivity(requestId: String?, triggerSync: Bool = true) {
        let rows = buildActivityRows()
        sendList(
            [
                "title": "ACTIVITY",
                "items": rows,
                "hint": Self.backHint,
                "selectable": false,
            ],
            requestId: requestId
        )

        guard triggerSync else { return }
        Task { @MainActor [weak self] in
            guard let self,
                  self.activeCapability == "ACTIVITY",
                  self.activeRequestId == requestId else { return }
            let terminal = self.glassesTerminal?.activeTerminal ?? self.macTerminal
            if let terminal {
                terminal.reloadConfig()
                if terminal.configured, terminal.state.agents.isEmpty {
                    await terminal.fetchState(quiet: true)
                }
            }
            if self.reminders.store.authorized {
                let openItems = await self.reminders.store.incomplete()
                self.cachedOpenRemindersCount = openItems.count
            }
            guard self.activeCapability == "ACTIVITY",
                  self.activeRequestId == requestId else { return }
            let updatedRows = self.buildActivityRows()
            if updatedRows != rows {
                self.sendList(
                    [
                        "title": "ACTIVITY",
                        "items": updatedRows,
                        "hint": Self.backHint,
                        "selectable": false,
                    ],
                    requestId: requestId
                )
            }
        }
    }

    private func buildActivityRows() -> [String] {
        var rows: [String] = []

        // 1. Live Mac Agent Terminal lanes
        if Thread.isMainThread {
            MainActor.assumeIsolated {
                let terminal = glassesTerminal?.activeTerminal ?? macTerminal
                if let terminal, !terminal.state.agents.isEmpty {
                    for agent in terminal.state.agents.prefix(6) {
                        rows.append(GlassesTerminal.formatAgentPickerRow(agent))
                    }
                } else if terminal?.configured == true {
                    rows.append("Agent Terminal · syncing with Mac…")
                } else {
                    rows.append("Agent Terminal · Mac not linked")
                }
            }
        } else {
            rows.append("Agent Terminal · syncing with Mac…")
        }

        // 2. Pomodoro / Timer status
        if let pomodoro {
            let ws = pomodoro.wireState()
            let isRunning = (ws["running"] as? Bool) ?? false
            let phase = pomodoro.phase
            if phase == "idle" && !isRunning {
                rows.append("Pomodoro · idle")
            } else {
                let mins = max(1, (pomodoro.remainingMs() + 59_999) / 60_000)
                let phaseLabel: String
                switch phase {
                case "focus": phaseLabel = "Focus"
                case "break": phaseLabel = "Break"
                case "long_break": phaseLabel = "Long break"
                case "timer": phaseLabel = "Timer"
                default: phaseLabel = phase.capitalized
                }
                rows.append("Pomodoro · \(phaseLabel) \(isRunning ? "running" : "paused") (\(mins)m left)")
            }
        }

        // 3. Open Reminders count
        if reminders.store.authorized {
            if let count = cachedOpenRemindersCount {
                rows.append("Reminders · \(count) open")
            } else {
                rows.append("Reminders · checking…")
            }
        } else {
            rows.append("Reminders · allow access in Settings")
        }

        // 4. Recent Messages & Mail counts
        if let messages {
            rows.append(messages.activitySummaryRow)
        }
        if let mail {
            rows.append(mail.activitySummaryRow)
        }

        return rows
    }

    // MARK: - Messages

    private func showMessages(requestId: String?) {
        guard let messages else { return }
        // Opening the page asks the Mac for its conversations; the list
        // repaints when they arrive (refreshMessages).
        if requestId != activeRequestId || !messagesShowingList { messages.refreshFromMac() }
        let page = messages.pageList()
        messageSenders = page.senders
        messagesShowingList = true
        sendList(page.list, requestId: requestId)
    }

    /// A reply went out from the open conversation: show it as its newest row.
    func refreshThread() {
        guard activeCapability == "MESSAGES", !messagesShowingList, let messages,
              let sender = messages.threadOnGlass else { return }
        sendList(messages.thread(sender, requestId: nil, traceId: nil), requestId: activeRequestId)
    }

    /// A text arrived or went out: repaint the conversation list if it is
    /// on the lens (an open thread stays as it is).
    func refreshMessages() {
        guard activeCapability == "MESSAGES", messagesShowingList else { return }
        showMessages(requestId: activeRequestId)
    }

    // MARK: - Mail (standalone Gmail on this iPhone)

    private func showMail(requestId: String?, triggerSync: Bool = false) {
        guard let mail else { return }
        if triggerSync {
            Task { @MainActor in await mail.syncNow() }
        }
        let page = mail.pageList()
        mailThreadIds = page.threadIds
        mailShowingList = true
        sendList(page.list, requestId: requestId)
    }

    /// A reply went out or new email arrived in the open thread: repaint it.
    func refreshMailThread() {
        guard activeCapability == "MAIL", !mailShowingList, let mail,
              let threadId = mail.threadOnGlass else { return }
        sendList(mail.thread(threadId, requestId: nil, traceId: nil), requestId: activeRequestId)
    }

    /// An email arrived or accounts synced: repaint the Mail list if open.
    func refreshMail() {
        guard activeCapability == "MAIL", mailShowingList else { return }
        showMail(requestId: activeRequestId)
    }

    // MARK: - Navigation

    /// Saved places or active route state changed while the Navigation list
    /// is open on the glasses: repaint it in place.
    func refreshNavigation() {
        guard activeCapability == "NAVIGATION", let navigation else { return }
        sendList(["title": "NAVIGATION", "items": navigation.pageRows(),
                  "hint": "tap: choose · left x2: back", "selectable": true], requestId: activeRequestId)
    }

    /// Navigation took over the lens with `navigation.view` or `navigation.resume`:
    /// retire the menu's list state so place/state updates do not send stale lists.
    func leaveNavigationIfOpen() {
        guard activeCapability == "NAVIGATION" else { return }
        retire()
    }

    // MARK: - Notifications (capabilities._show_notifications)

    private func showNotifications(requestId: String?) {
        notificationRows = cards.active
        guard !notificationRows.isEmpty else {
            sendList(["title": "NOTIFICATIONS · EMPTY", "items": ["No unread JOAT notifications."],
                      "hint": Self.backHint], requestId: requestId)
            return
        }
        sendList([
            "title": "NOTIFICATIONS",
            "items": notificationRows.map { "[\(Cards.age($0.at))] \($0.render.components(separatedBy: "\n")[0])" }
                + ["Clear JOAT notifications"],
            "controlStartIndex": notificationRows.count,
            "hint": "tap: read · left x2: back",
            "selectable": true,
        ], requestId: requestId)
    }

    private func selectNotification(_ index: Int, requestId: String?) {
        if !notificationRows.isEmpty, index == notificationRows.count {
            cards.dismiss(notificationRows.map(\.id))
            showNotifications(requestId: requestId)
            return
        }
        guard notificationRows.indices.contains(index) else { return }
        let card = notificationRows[index]
        cards.dismiss([card.id])
        let header = card.render.components(separatedBy: "\n")[0]
        let rows = [header] + ReaderText.rows(card.body.isEmpty ? card.summary : card.body)
        sendList(["title": "NOTIFICATIONS · DETAIL", "items": Array(rows.prefix(19)), "hint": Self.backHint],
                 requestId: requestId)
    }

    /// A page not ported yet. Said plainly, on the page the wearer opened,
    /// instead of a 12-second wait and a Mac timeout.
    private func notOnPhone(_ name: String, requestId: String?) {
        sendList([
            "title": "\(name.uppercased()) · NOT YET",
            "items": ["\(name) is not on the phone yet.", "It is being moved from the Mac."],
            "hint": Self.backHint,
            "selectable": false,
        ], requestId: requestId)
    }

    /// A page the brain opens itself (a voice command, a recording's own
    /// repaint): a fresh "voice-" generation the plugin adopts on arrival
    /// (capabilities.voice_request_id).
    func openByVoice(_ name: String) {
        open(name, requestId: "voice-" + ConfirmGate.hex(6), anchorMs: nil)
    }

    /// Take the surface for a page the brain shows without an open (a
    /// review the wearer asked for by voice): a fresh "voice-" generation.
    func claim(_ capability: String) {
        if activeCapability == "MESSAGES", capability != "MESSAGES" { messages?.leaveUI() }
        if activeCapability == "MAIL", capability != "MAIL" { mail?.leaveUI() }
        if activeCapability == "AGENT TERMINAL", capability != "AGENT TERMINAL" {
            glassesTerminal?.retireFromMenu()
        }
        messagesShowingList = false
        mailShowingList = false
        activeCapability = capability
        activeRequestId = "voice-" + ConfirmGate.hex(6)
    }

    /// Send under whatever request is open now.
    func sendCurrent(_ list: [String: Any]) { sendList(list, requestId: activeRequestId) }

    // MARK: - Voice: "open my notes", "show planning", "open mail", "open claude", "go home"

    static let homeVoice = try! NSRegularExpression(
        pattern: #"^(?:go(?:\s+back)?\s+home|(?:open|show|bring\s+up)(?:\s+(?:my|the))?\s+(?:home(?:\s+screen|\s+page)?|overview|dashboard))$"#,
        options: [.caseInsensitive])
    static let openCapability = try! NSRegularExpression(
        pattern: #"^(?:open|show|read|bring\s+up)(?:\s+(?:my|the))?\s+(?<name>messages|texts|conversations|mail|email|emails|inbox|gmail|quick\s?notes|notes|planning|calendar|tasks|reminders|notifications|activity|agent\s?terminal|terminal|claude(?:\s+code)?|codex|antigravity|navigation|directions|map|news|pomodoro|timer|search)$|^check(?:\s+(?:my|the))?\s+(?<name2>mail|email|emails|inbox|gmail|messages|texts|conversations|agent\s?terminal|terminal|claude(?:\s+code)?|codex|antigravity)$"#,
        options: [.caseInsensitive])
    static let spoken: [String: String] = [
        "messages": "Messages", "texts": "Messages", "conversations": "Messages",
        "mail": "Mail", "email": "Mail", "emails": "Mail", "inbox": "Mail", "gmail": "Mail",
        "quicknotes": "QuickNotes", "quick notes": "QuickNotes", "notes": "QuickNotes",
        "planning": "Planning", "calendar": "Calendar", "tasks": "Tasks", "reminders": "Reminders",
        "notifications": "Notifications", "activity": "Activity",
        "agent terminal": "Agent Terminal", "agentterminal": "Agent Terminal", "terminal": "Agent Terminal",
        "claude": "Agent Terminal", "claude code": "Agent Terminal", "codex": "Agent Terminal", "antigravity": "Agent Terminal",
        "navigation": "Navigation", "directions": "Navigation", "map": "Navigation",
        "news": "News", "pomodoro": "Pomodoro", "timer": "Pomodoro", "search": "Search",
    ]

    func handle(_ request: String) -> CommandReply? {
        let normalized = request.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?")).trimmingCharacters(in: .whitespaces)
        if Grammar.match(Self.homeVoice, normalized) != nil {
            openHome()
            return CommandReply(text: "Opening Home.", activated: true)
        }
        guard let g = Grammar.match(Self.openCapability, normalized, groups: ["name", "name2"]),
              let said = g["name"] ?? g["name2"] else { return nil }
        let key = said.lowercased().split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard let name = Self.spoken[key] else { return nil }

        if name == "Agent Terminal", let glassesTerminal {
            glassesTerminal.openByVoice(agentHint: key)
            let agentShown: String
            switch key {
            case "claude", "claude code": agentShown = "Claude Code"
            case "codex": agentShown = "Codex"
            case "antigravity": agentShown = "Antigravity"
            default: agentShown = "Agent Terminal"
            }
            return CommandReply(text: "Opening \(agentShown).", activated: true, suppressesHudReceipt: true)
        }

        openByVoice(name)
        let shown = ["Planning", "Calendar", "Tasks"].contains(name) ? "Reminders" : name
        let suppressOverlay = ["Messages", "Agent Terminal", "Activity"].contains(name)
        return CommandReply(text: "Opening \(shown).", activated: true, suppressesHudReceipt: suppressOverlay)
    }

    /// protocol.py HudList.to_wire, under the request it answers. A reply
    /// for a request that is no longer the open one is dropped (_send_list).
    func sendList(_ list: [String: Any], requestId: String?) {
        guard requestId == activeRequestId else { return }
        var wire = list
        wire["kind"] = "hud.list"
        let items = (list["items"] as? [String] ?? []).prefix(20).map { $0.utf16Prefix(64) }
        wire["items"] = Array(items)
        if let t = list["title"] as? String { wire["title"] = t.utf16Prefix(46) }
        if let h = list["hint"] as? String { wire["hint"] = h.utf16Prefix(96) }
        if let c = list["contextLabel"] as? String { wire["contextLabel"] = c.utf16Prefix(96) }
        if let i = list["controlStartIndex"] as? Int { wire["controlStartIndex"] = max(0, min(i, items.count)) }
        if wire["selectable"] == nil { wire["selectable"] = false }
        if let requestId { wire["requestId"] = String(requestId.prefix(128)) }
        link.send(wire)
        Log.add("tx hud.list \(wire["title"] as? String ?? "") rows=\(items.count)")
    }
}
