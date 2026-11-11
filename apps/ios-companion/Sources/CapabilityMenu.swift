import Foundation

/// The glasses' capability menu, answered on the phone: a port of
/// services/capabilities.py CapabilityRouter.
///
/// The plugin's menu is navigation only. A row pick sends `input` with
/// containerName "capmenu" and waits 12 s for a `hud.list` carrying the
/// same requestId; with no answer it says "No response from the Mac yet."
/// (CapabilityMenu.ts showResponseTimeout). So every row gets an answer
/// here: a ported page, a rail tab, or an honest "not on the phone yet".
final class CapabilityMenu: BrainFeature, VoiceCommand {
    let name = "capabilities"
    let uplinkKinds: Set<String> = ["input"]

    /// capabilities.py BACK_HINT
    static let backHint = "left x2: back"
    static let planningNames: Set<String> = ["PLANNING", "CALENDAR", "TASKS"]

    private let link: PluginLink
    private let panels: PanelPublisher
    let reminders: Reminders
    /// The Reminders page's rows, bound to reminder ids.
    private var reminderIds: [String?] = []
    private let cards: Cards
    let quickNotes: QuickNotes
    /// The Navigation page's question and saved places (Brain sets it).
    var navigation: Navigation?
    /// The Messages page's conversations (Brain sets it).
    var messages: Messages?
    /// The Messages page's rows, bound to senders; empty on a thread.
    private var messageSenders: [String] = []
    private var messagesShowingList = false
    /// The Notifications page's rows, bound to card ids (a new arrival
    /// cannot shift what a tap opens).
    private var notificationRows: [Cards.Card] = []
    private(set) var activeCapability: String?
    private(set) var activeRequestId: String?

    init(link: PluginLink, panels: PanelPublisher, reminders: Reminders, cards: Cards, quickNotes: QuickNotes) {
        self.link = link
        self.panels = panels
        self.reminders = reminders
        self.cards = cards
        self.quickNotes = quickNotes
    }

    /// A new plugin connection starts with no page open (on_disconnect).
    func pluginConnected() { retire() }

    private func retire() {
        if activeCapability == "MESSAGES" { messages?.leaveUI() }
        activeCapability = nil
        activeRequestId = nil
        reminderIds = []
        messagesShowingList = false
        quickNotes.departed()
    }

    private var planningActive: Bool { activeCapability.map(Self.planningNames.contains) ?? false }

    /// capabilities._canonical_capability
    static func canonical(_ name: String) -> String {
        let n = name.trimmingCharacters(in: .whitespaces).uppercased()
        if n == "REMINDERS" { return "TASKS" }
        if n == "DEBATE" { return "BRAINSTORM" }
        return n
    }

    func handle(kind: String, message: [String: Any]) {
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
            // Planning, Calendar and Tasks are one page (Reminders): opened by
            // voice as "planning", its taps come back as REMINDERS (TASKS).
            let family = Self.planningNames.contains(Self.canonical(selected)) && planningActive
            guard requestId == activeRequestId, Self.canonical(selected) == activeCapability || family else {
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
                    messageSenders = []
                    messagesShowingList = false
                    sendList(messages.thread(sender, requestId: requestId, traceId: event.string("traceId")),
                             requestId: requestId)
                }
            }
        case "capmessage":
            // A tap on the open conversation replies to it.
            guard activeCapability == "MESSAGES", requestId == activeRequestId else {
                Log.add("dropping stale Messages reply request=\(requestId ?? "nil")")
                return
            }
            messages?.beginReply()
        case "message-cancel":
            if let selected { messages?.cancelUI(actionId: selected) }
        case "message-retry":
            if let selected { messages?.retryUI(actionId: selected) }
        case "cappage":
            if selected == "QUICKNOTES · LIST", activeCapability == "QUICKNOTES", requestId == activeRequestId {
                quickNotes.pageList(eventType: event.int("eventType"), selectedIndex: event.int("selectedIndex"))
            }
        case "capability-lifecycle":
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

    /// capabilities.open: one list generation per request.
    func open(_ name: String, requestId: String?, anchorMs: Int?) {
        if activeCapability == "MESSAGES" { messages?.leaveUI() }
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
        default:
            notOnPhone(name, requestId: requestId)
        }
    }

    // MARK: - Messages (texts from the Shortcuts automation)

    private func showMessages(requestId: String?) {
        guard let messages else { return }
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
        if cards.acknowledge(card.id) {
            cards.retire(card.id)
            cards.publish()
        }
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
        activeCapability = capability
        activeRequestId = "voice-" + ConfirmGate.hex(6)
    }

    /// Send under whatever request is open now.
    func sendCurrent(_ list: [String: Any]) { sendList(list, requestId: activeRequestId) }

    // MARK: - Voice: "open my notes", "show planning" (CapabilityOpenController)

    static let openCapability = try! NSRegularExpression(
        pattern: #"^(?:open|show|read)(?:\s+(?:my|the))?\s+(?<name>messages|texts|conversations|quick\s?notes|notes|planning|calendar|tasks|reminders|notifications|activity|brainstorm|debate|agent\s?terminal|terminal|navigation|directions|map)$"#,
        options: [.caseInsensitive])
    static let spoken: [String: String] = [
        "messages": "Messages", "texts": "Messages", "conversations": "Messages",
        "quicknotes": "QuickNotes", "quick notes": "QuickNotes", "notes": "QuickNotes",
        "planning": "Planning", "calendar": "Calendar", "tasks": "Tasks", "reminders": "Reminders",
        "notifications": "Notifications", "activity": "Activity",
        "brainstorm": "Brainstorm", "debate": "Brainstorm",
        "agent terminal": "Agent Terminal", "agentterminal": "Agent Terminal", "terminal": "Agent Terminal",
        "navigation": "Navigation", "directions": "Navigation", "map": "Navigation",
    ]

    func handle(_ request: String) -> CommandReply? {
        let normalized = request.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?")).trimmingCharacters(in: .whitespaces)
        guard let g = Grammar.match(Self.openCapability, normalized, groups: ["name"]), let said = g["name"] else { return nil }
        let key = said.lowercased().split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard let name = Self.spoken[key] else { return nil }
        openByVoice(name)
        let shown = ["Planning", "Calendar", "Tasks"].contains(name) ? "Reminders" : name
        return CommandReply(text: "Opening \(shown).", activated: true)
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
