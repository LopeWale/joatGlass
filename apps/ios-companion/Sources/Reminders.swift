import Foundation
import EventKit

/// Reminders on the phone: create, complete, the due card, and the rail's
/// Planner tab. Ports, by file:
///   services/productivity_actions.py  confirm → verifying → native write →
///                                     read-back → saved/completed (wording verbatim)
///   services/task_monitor.py          the due card (reminder_due_hud_view)
///   services/planner_panel.py         the Planner tab rows
///   core/protocol.py                  productivity.view, reminder_due.* wire
final class Reminders: BrainFeature, VoiceCommand, PanelProvider {
    let name = "reminders"
    let uplinkKinds: Set<String> = ["reminder_due.ack", "reminder_due.complete"]
    let tab = "planner"
    let statusRows = 0

    let store = RemindersStore()
    private let link: PluginLink
    private let hud: Hud
    private let confirm: ConfirmGate
    /// Speak through the phone's audio route (AirPods).
    var say: ((String) -> Void)?
    /// Push the Planner tab.
    var publishPanel: (() -> Void)?
    /// Ask a question; the next sentence (no wake word needed) is the answer.
    var askFollowUp: ((_ question: String, _ answer: @escaping (String) -> Void) -> Void)?

    init(link: PluginLink, hud: Hud, confirm: ConfirmGate) {
        self.link = link
        self.hud = hud
        self.confirm = confirm
        dues = Self.loadDues()
    }

    func start() {
        Task { @MainActor in
            await store.requestAccess()
            await reloadPanel()
            armDues()
            // planner_panel.py _POLL_S: read again every minute, push on change.
            let t = DispatchSource.makeTimerSource(queue: .main)
            t.schedule(deadline: .now() + 60, repeating: 60)
            t.setEventHandler { [weak self] in Task { @MainActor in await self?.reloadPanel() } }
            t.resume()
            pollTimer = t
        }
    }
    private var pollTimer: DispatchSourceTimer?

    // MARK: - Planner tab (planner_panel.py)

    static let panelRows = 8
    static let lookbackDays = 7
    static let aheadDays = 6
    private var rows: [String] = []
    private var empty: String? = "Reading your reminders…"
    private var shownRows: [String]? = nil
    private var shownEmpty: String?? = nil

    func snapshot() -> PanelSnapshot {
        shownRows = rows
        shownEmpty = .some(empty)
        return PanelSnapshot(rows: rows, empty: empty)
    }
    func refresh(completion: @escaping () -> Void) {
        Task { @MainActor in await reloadPanel(); completion() }
    }
    /// The tab has no rows of its own to open (the plugin opens Planning).
    func open(index: Int, completion: @escaping (Bool) -> Void) { completion(false) }

    @MainActor
    private func reloadPanel() async {
        guard store.authorized else {
            rows = []
            empty = "Allow Reminders for JOAT Brain in Settings."
            pushIfChanged()
            return
        }
        let cal = Calendar.current
        let today = cal.startOfDay(for: Date())
        let from = cal.date(byAdding: .day, value: -Self.lookbackDays, to: today)!
        let until = cal.date(byAdding: .day, value: Self.aheadDays + 1, to: today)!
        let open = await store.incomplete().filter {
            guard let d = $0.dueDate else { return false }
            return d >= from && d < until
        }
        let shown = open.count <= Self.panelRows ? open : Array(open.prefix(Self.panelRows - 1))
        rows = shown.map { Self.row($0, today: today) }
        if open.count > shown.count { rows.append("+\(open.count - shown.count) more") }
        empty = rows.isEmpty ? "Nothing due this week." : nil
        pushIfChanged()
    }

    private func pushIfChanged() {
        if shownRows == rows, shownEmpty == .some(empty) { return }
        publishPanel?()
    }

    /// planner_panel._label_and_value: "[Late] …", "[09:00] …", "[Tmrw] 09:00 …".
    /// The Reminders page reads the clock as its card does ("3:00 PM").
    static func row(_ r: EKReminder, today: Date, twelveHour: Bool = false) -> String {
        let cal = Calendar.current
        let title = r.title ?? "Reminder"
        guard let comps = r.dueDateComponents, let due = r.dueDate else { return "[Today] \(title)" }
        let timed = comps.hour != nil
        let hhmm: String = {
            guard twelveHour else { return Self.hhmm(due) }
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "h:mm a"
            return f.string(from: due)
        }()
        let delta = cal.dateComponents([.day], from: today, to: cal.startOfDay(for: due)).day ?? 0
        if delta < 0 { return "[Late] \(title)" }
        if delta == 0 { return timed ? "[\(hhmm)] \(title)" : "[Today] \(title)" }
        let label: String
        if delta == 1 { label = "Tmrw" } else {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "EEE"
            label = f.string(from: due)
        }
        return "[\(label)] " + (timed ? "\(hhmm) \(title)" : title)
    }

    // MARK: - Voice

    static let create = Grammar.regex(
        #"^(?:please )?(?:remind me|set (?:me )?a reminder|add a reminder|create a reminder|make a reminder),?(?: (?<rest>.+))?$"#)
    static let complete = Grammar.regex(
        #"^(?:complete|finish|check off|tick off) (?:the )?reminder (?<title>.+)$|^mark (?:the )?reminder (?<titletwo>.+?) (?:as )?(?:done|complete|completed)$"#)
    static let list = Grammar.regex(
        #"^(?:what are|what's|what is|read|tell me|show|list)(?: me)? (?:my |the )?(?:open )?reminders(?: today| this week)?$"#)

    func handle(_ request: String) -> CommandReply? {
        let t = Grammar.normalize(request)
        if let g = Grammar.match(Self.create, t, groups: ["rest"]) {
            // Keep the wearer's own casing for the title: take the same span
            // from the original words.
            let original = request.trimmingCharacters(in: .whitespacesAndNewlines)
            let rest = g["rest"].map { restOf(original, lowerRest: $0) } ?? ""
            guard !rest.isEmpty else {
                askFollowUp?("What should I remind you about, and when?") { [weak self] answer in
                    self?.beginCreate(answer)
                }
                return CommandReply(text: "", activated: false)
            }
            beginCreate(rest)
            return CommandReply(text: "", activated: false)
        }
        if let g = Grammar.match(Self.complete, t, groups: ["title", "titletwo"]), let title = g["title"] ?? g["titletwo"] {
            beginComplete(spokenTitle: title)
            return CommandReply(text: "", activated: false)
        }
        if Grammar.match(Self.list, t) != nil {
            Task { @MainActor in
                let open = await store.incomplete()
                let text: String
                if !store.authorized { text = "Allow Reminders for JOAT Brain in Settings first." }
                else if open.isEmpty { text = "You have no open reminders." }
                else {
                    let names = open.prefix(3).map { $0.title ?? "Reminder" }.joined(separator: "; ")
                    text = "You have \(open.count) open reminder\(open.count == 1 ? "" : "s"): \(names)\(open.count > 3 ? "; and more." : ".")"
                }
                self.hud.receipt(text)
                self.say?(text)
            }
            return CommandReply(text: "", activated: false)
        }
        return nil
    }

    /// The span of the original text that `lowerRest` (lowercased) came from.
    private func restOf(_ original: String, lowerRest: String) -> String {
        var o = original
        while let last = o.last, ".,!?".contains(last) { o.removeLast() }
        if let r = o.range(of: lowerRest, options: [.caseInsensitive, .backwards]) { return String(o[r]) }
        return lowerRest
    }

    // MARK: - Create (productivity_actions.create_reminder)

    private func beginCreate(_ rest: String) {
        if let parsed = ReminderTime.parse(rest), !parsed.title.isEmpty {
            confirmCreate(title: parsed.title, due: parsed.due)
            return
        }
        let title = ReminderTime.cleanTitle(rest)
        guard !title.isEmpty else { return }
        askFollowUp?("When should I remind you to \(title.prefix(1).lowercased() + title.dropFirst())?") { [weak self] answer in
            guard let self else { return }
            if let parsed = ReminderTime.parse(answer) {
                self.confirmCreate(title: title, due: parsed.due)
            } else {
                let text = "I didn't catch a time. Say: remind me to \(title.lowercased()) tomorrow at 9."
                self.hud.receipt(text)
                self.say?(text)
            }
        }
    }

    private func confirmCreate(title rawTitle: String, due rawDue: Date) {
        // Reminders keeps minutes, not seconds: "in 20 minutes" lands on the
        // whole minute, so the read-back and the due monitor compare equal.
        let due = Date(timeIntervalSince1970: (rawDue.timeIntervalSince1970 / 60).rounded(.down) * 60)
        if Bundle.main.bundleIdentifier != nil,
           [.denied, .restricted].contains(RemindersStore.status) {
            tell("Allow Reminders for JOAT Brain in Settings, then ask again.")
            return
        }
        let title = String(rawTitle.prefix(200))
        let list = store.authorized ? (store.store.defaultCalendarForNewReminders()?.title ?? "Reminders") : "Reminders"
        let zone = TimeZone.current.identifier
        var prompt = "Create reminder + JOAT alert “\(title)”? \(list) · \(Self.display(due)) · \(zone)"
        if prompt.utf16.count > ConfirmGate.promptMaxUnits {
            let tail = "”? \(Self.displayShort(due))"
            let head = "Remind + alert “"
            let room = ConfirmGate.promptMaxUnits - (head + tail).utf16.count
            prompt = head + Self.fit(title, units: room) + tail
        }
        let dueMs = Int(due.timeIntervalSince1970 * 1000)
        confirm.ask(prompt: prompt, payload: "reminders.create|\(list)|\(title)|\(dueMs)") { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                self.view("cancelled", title: title, kind: "create", anchor: dueMs)
                self.finish("Reminder cancelled.")
            case .unverified:
                self.view("not_saved", title: title, kind: "create", anchor: dueMs)
                self.finish("Reminder was not saved because confirmation could not be verified.")
            case .accepted:
                if self.store.authorized {
                    self.write(title: title, due: due, dueMs: dueMs)
                } else {
                    Task { @MainActor in
                        _ = await self.store.requestAccess()
                        self.write(title: title, due: due, dueMs: dueMs)
                    }
                }
            }
        }
    }

    @MainActor
    func prepareCreateFromTool(title rawTitle: String, due: Date?) -> PhoneToolOutcome {
        let cleaned = ReminderTime.cleanTitle(rawTitle)
        let title = cleaned.isEmpty ? rawTitle.trimmingCharacters(in: .whitespacesAndNewlines) : cleaned
        guard !title.isEmpty else {
            return PhoneToolOutcome("error: title is required to create a reminder.", claimsLens: false)
        }
        if let due {
            confirmCreate(title: title, due: due)
            if confirm.hasPending {
                return PhoneToolOutcome(
                    "Reminder “\(title)” for \(Self.display(due)) is awaiting touchpad confirmation on the glasses.",
                    claimsLens: true
                )
            }
            return PhoneToolOutcome(
                "Allow Reminders for JOAT Brain in Settings, then ask again.",
                claimsLens: true
            )
        }
        let loweredLead = title.prefix(1).lowercased() + title.dropFirst()
        let question = "When should I remind you to \(loweredLead)?"
        askFollowUp?(question) { [weak self] answer in
            guard let self else { return }
            if let parsed = ReminderTime.parse(answer) {
                self.confirmCreate(title: title, due: parsed.due)
            } else {
                let text = "I didn't catch a time. Say: remind me to \(title.lowercased()) tomorrow at 9."
                self.hud.receipt(text)
                self.say?(text)
            }
        }
        return PhoneToolOutcome("Asked the wearer on the glasses: \(question)", claimsLens: true)
    }

    private func write(title: String, due: Date, dueMs: Int) {
        view("verifying", title: title, kind: "create")
        let comps = Calendar.current.dateComponents([.calendar, .timeZone, .year, .month, .day, .hour, .minute], from: due)
        do {
            let saved = try store.create(title: title, due: comps)
            // Read back what EventKit now holds: same title, same due minute.
            let back = store.reminder(withIdentifier: saved.calendarItemIdentifier)
            let backDue = back?.dueDate.map { Int($0.timeIntervalSince1970 / 60) }
            guard let back, back.title == title, backDue == Int(due.timeIntervalSince1970 / 60) else {
                view("check", title: title, kind: "create", anchor: dueMs)
                finish("The reminder may have been created. Check Reminders before retrying.")
                return
            }
            arm(DueItem(id: UUID().uuidString.lowercased(), reminderId: back.calendarItemIdentifier,
                        title: title, dueMs: dueMs, alerted: false))
            view("saved", title: title, kind: "create", anchor: dueMs, monitoringArmed: true)
            finish("Created Reminder: \(title).")
            Task { @MainActor in await self.reloadPanel() }
        } catch {
            Log.add("reminder create failed: \(error)")
            view("failed", title: title, kind: "create", anchor: dueMs)
            finish("Reminders did not create the reminder.")
        }
    }

    // MARK: - Complete (productivity_actions._complete_reminder)

    @MainActor
    func completeFromTool(spokenTitle: String) async -> PhoneToolOutcome {
        let trimmed = spokenTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return PhoneToolOutcome("error: title is required to complete a reminder.", claimsLens: false)
        }
        if RemindersStore.status == .notDetermined, Bundle.main.bundleIdentifier != nil {
            _ = await store.requestAccess()
        }
        guard store.authorized else {
            let msg = "Allow Reminders for JOAT Brain in Settings, then ask again."
            tell(msg)
            return PhoneToolOutcome(msg, claimsLens: true)
        }
        let open = await store.incomplete()
        let wanted = trimmed.trimmingCharacters(in: CharacterSet(charactersIn: " .,!?")).lowercased()
        var matches = open.filter { ($0.title ?? "").lowercased() == wanted }
        if matches.isEmpty {
            matches = open.filter {
                let t = ($0.title ?? "").lowercased()
                return !t.isEmpty && (t.contains(wanted) || wanted.contains(t))
            }
        }
        guard matches.count == 1, let target = matches.first else {
            let msg = matches.isEmpty
                ? "No open reminder is named \(trimmed)."
                : "More than one open reminder matches \(trimmed). Say its full name."
            tell(msg)
            return PhoneToolOutcome(msg, claimsLens: true)
        }
        confirmComplete(target)
        let targetTitle = target.title ?? trimmed
        return PhoneToolOutcome(
            confirm.hasPending
                ? "Completion of reminder “\(targetTitle)” is awaiting touchpad confirmation on the glasses."
                : "Reminder completion could not be started.",
            claimsLens: true
        )
    }

    private func beginComplete(spokenTitle: String) {
        Task { @MainActor in
            let open = await store.incomplete()
            let wanted = spokenTitle.trimmingCharacters(in: CharacterSet(charactersIn: " .,!?")).lowercased()
            var matches = open.filter { ($0.title ?? "").lowercased() == wanted }
            if matches.isEmpty {
                // Spoken titles rarely match exactly; accept one unambiguous containment.
                matches = open.filter {
                    let t = ($0.title ?? "").lowercased()
                    return t.contains(wanted) || wanted.contains(t)
                }
            }
            guard matches.count == 1, let target = matches.first else {
                tell(matches.isEmpty ? "No open reminder is named \(spokenTitle)."
                       : "More than one open reminder matches \(spokenTitle). Say its full name.")
                return
            }
            confirmComplete(target)
        }
    }

    /// A Planning row or the due page: complete this reminder after the
    /// glasses confirm (productivity_actions.complete_reminder).
    func complete(identifier: String) {
        guard let r = store.reminder(withIdentifier: identifier), !r.isCompleted else {
            tell("That reminder is already done or no longer exists.")
            return
        }
        confirmComplete(r)
    }

    private func confirmComplete(_ target: EKReminder) {
        let title = target.title ?? "Reminder"
        if target.hasRecurrenceRules {
            tell("Recurring reminder completion is not enabled until occurrence behavior is device-verified.")
            return
        }
        let list = target.calendar?.title ?? "Reminders"
        let anchor = target.dueDate.map { Int($0.timeIntervalSince1970 * 1000) }
        confirm.ask(prompt: "Complete reminder “\(Self.fit(title, units: 90))”? \(list)",
                    payload: "reminders.complete|\(target.calendarItemIdentifier)") { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                self.view("cancelled", title: title, kind: "complete", anchor: anchor)
                self.finish("Reminder completion cancelled.")
            case .unverified:
                self.view("not_saved", title: title, kind: "complete", anchor: anchor)
                self.finish("Reminder completion was not saved because confirmation could not be verified.")
            case .accepted:
                self.view("verifying", title: title, kind: "complete")
                do {
                    try self.store.complete(target)
                    let back = self.store.reminder(withIdentifier: target.calendarItemIdentifier)
                    guard back?.isCompleted == true else {
                        self.view("check", title: title, kind: "complete", anchor: anchor)
                        self.finish("The reminder may have been completed. Check Reminders before retrying.")
                        return
                    }
                    self.dues.removeAll { $0.reminderId == target.calendarItemIdentifier }
                    self.saveDues()
                    self.view("completed", title: title, kind: "complete", anchor: anchor)
                    self.finish("Completed Reminder: \(title).")
                    Task { @MainActor in await self.reloadPanel() }
                } catch {
                    Log.add("reminder complete failed: \(error)")
                    self.view("failed", title: title, kind: "complete", anchor: anchor)
                    self.finish("Reminders did not complete the reminder.")
                }
            }
        }
    }

    // MARK: - Due monitor (task_monitor.py, reminder_due.* wire)

    struct DueItem: Codable {
        let id: String
        let reminderId: String
        let title: String
        let dueMs: Int
        var alerted: Bool
        var attemptId: String? = nil
        var generation: Int? = nil
    }
    private static let duesKey = "joat.reminders.dues.v1"
    private var dues: [DueItem]
    private var dueTimers: [String: DispatchSourceTimer] = [:]
    private var generation = 0

    private static func loadDues() -> [DueItem] {
        guard let d = UserDefaults.standard.data(forKey: duesKey),
              let items = try? JSONDecoder().decode([DueItem].self, from: d) else { return [] }
        return items
    }
    private func saveDues() {
        if let d = try? JSONEncoder().encode(dues) { UserDefaults.standard.set(d, forKey: Self.duesKey) }
    }

    private func arm(_ item: DueItem) {
        dues.removeAll { $0.reminderId == item.reminderId }
        dues.append(item)
        saveDues()
        schedule(item)
    }

    private func armDues() { dues.filter { !$0.alerted }.forEach(schedule) }

    private func schedule(_ item: DueItem) {
        dueTimers[item.id]?.cancel()
        let delay = max(0, Double(item.dueMs) / 1000 - Date().timeIntervalSince1970)
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + delay, repeating: 30)   // hub_gateway reprobe: 30 s
        t.setEventHandler { [weak self] in self?.fireDue(item.id) }
        t.resume()
        dueTimers[item.id] = t
    }

    private func fireDue(_ id: String) {
        guard let i = dues.firstIndex(where: { $0.id == id }), !dues[i].alerted else {
            dueTimers.removeValue(forKey: id)?.cancel(); return
        }
        // evaluate_reminder_occurrence: show it only while still incomplete
        // at the same due time.
        let item = dues[i]
        guard let r = store.reminder(withIdentifier: item.reminderId), !r.isCompleted,
              r.dueDate.map({ Int($0.timeIntervalSince1970 * 1000) }) == item.dueMs else {
            dues.remove(at: i); saveDues(); dueTimers.removeValue(forKey: id)?.cancel(); return
        }
        // The plugin acknowledges only a generation > 0: wait for a plugin.
        guard generation > 0 else { return }
        let attempt = UUID().uuidString.lowercased()
        dues[i].attemptId = attempt
        dues[i].generation = generation
        saveDues()
        link.send(["kind": "productivity.view", "productivity": [
            "domain": "reminders", "phase": "due",
            "title": String(item.title.prefix(72)),
            "primary": "Due now", "secondary": "This reminder is still incomplete.",
            "hint": "Say complete reminder · Back: left x2",
            "planningAnchorUnixMs": item.dueMs,
            "reminderDueId": item.id, "reminderDueAttemptId": attempt, "reminderDueGeneration": generation,
        ] as [String: Any]])
        Log.add("tx reminder due \(item.title)")
    }

    // MARK: - BrainFeature

    func pluginConnected() {
        generation += 1
        // A due card nobody accepted is shown again on the new connection.
        for item in dues where !item.alerted && item.dueMs <= Int(Date().timeIntervalSince1970 * 1000) {
            fireDue(item.id)
        }
        Task { @MainActor in await reloadPanel() }
    }

    func handle(kind: String, message: [String: Any]) {
        guard let id = message.string("reminderDueId"),
              let i = dues.firstIndex(where: { $0.id == id }),
              message.string("reminderDueAttemptId") == dues[i].attemptId,
              message.int("generation") == dues[i].generation else { return }
        switch kind {
        case "reminder_due.ack":
            if message.string("outcome") == "accepted" {
                dues[i].alerted = true
                saveDues()
                dueTimers.removeValue(forKey: id)?.cancel()
                Log.add("reminder due accepted on the glasses: \(dues[i].title)")
            }
        case "reminder_due.complete":
            let item = dues[i]
            if let r = store.reminder(withIdentifier: item.reminderId) { confirmComplete(r) }
        default:
            break
        }
    }

    // MARK: - Lens and voice

    /// productivity.view with the Mac's _workflow_copy wording.
    private func view(_ phase: String, title: String, kind: String, anchor: Int? = nil, monitoringArmed: Bool = false) {
        let back = "Back to Reminders: left x2 · auto in 5s"
        let copy: (String, String, String)
        switch phase {
        case "verifying":
            copy = kind == "complete"
                ? ("Completing reminder", "Checking native completion read-back", "Please wait")
                : ("Saving reminder", "Checking native read-back", "Please wait")
        case "saved":
            copy = monitoringArmed
                ? ("Saved and verified", "Native alarm set · JOAT monitoring armed.", "Back to Reminders: left x2 · monitoring next")
                : ("Saved and verified", "Reminders read-back matched", back)
        case "completed": copy = ("Completed and verified", "Reminders read-back matched", back)
        case "check": copy = ("Check Reminders", "It may have changed. Do not retry.", back)
        case "cancelled": copy = ("Cancelled", "No reminders item was changed.", back)
        case "not_saved": copy = ("Not saved", "No reminders item was changed.", back)
        default:
            copy = kind == "complete"
                ? ("Completion failed", "Reminder was not completed.", back)
                : ("Save failed", "No reminders item was changed.", back)
        }
        var p: [String: Any] = [
            "domain": "reminders", "phase": phase == "failed" ? "failed" : phase,
            "title": String(title.prefix(72)),
            "primary": copy.0, "secondary": copy.1, "hint": copy.2,
        ]
        if let anchor { p["planningAnchorUnixMs"] = anchor }
        if monitoringArmed { p["monitoringArmed"] = true }
        link.send(["kind": "productivity.view", "productivity": p])
        Log.add("tx productivity.view \(phase) \(title)")
    }

    /// After an outcome page: the page shows it, the voice says it.
    private func finish(_ text: String) {
        Log.add("reminders: \(text)")
        say?(text)
    }

    /// No outcome page to show it on: a receipt on the lens, and the voice.
    private func tell(_ text: String) {
        Log.add("reminders: \(text)")
        hud.receipt(text)
        say?(text)
    }

    // productivity_actions _Civil.display / display_short
    static func display(_ d: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMM d, yyyy 'at' HH:mm"
        return f.string(from: d)
    }
    static func displayShort(_ d: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMM d 'at' HH:mm"
        return f.string(from: d)
    }
    static func hhmm(_ d: Date) -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "HH:mm"
        return f.string(from: d)
    }
    /// productivity_actions._fit_to_units: cut to `units` UTF-16 units with "…".
    static func fit(_ text: String, units: Int) -> String {
        if text.utf16.count <= units { return text }
        return text.utf16Prefix(max(1, units - 1)) + "…"
    }
}

// MARK: - A batch from a note (productivity_actions.create_reminder_batch)

extension Reminders {
    struct BatchItem {
        var title: String
        var due: Date
        /// Where it came from ("voice_note.commitment:<note>:<sentence key>"),
        /// so saying it again, or tapping twice, finds the one already made.
        let origin: String
        /// A day with no time said: an all-day reminder on that day.
        var allDay: Bool = false
    }

    struct BatchResult {
        var created = 0
        var already = 0
        var problems: [String] = []
        var declined = false
        var refused: String?
    }

    private static let originsKey = "joat.reminders.origins.v1"

    private var origins: [String: String] {
        get { UserDefaults.standard.dictionary(forKey: Self.originsKey) as? [String: String] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: Self.originsKey) }
    }

    /// A reminder from this origin was created and still exists.
    func isCreated(origin: String) -> Bool {
        guard let id = origins[origin] else { return false }
        return store.reminder(withIdentifier: id) != nil
    }

    /// Ask once on the glasses, then write each reminder whole, with an
    /// alarm at its due minute, and read each one back. `confirmed`: the
    /// wearer already accepted this list (tapped it on the glasses, or Add in
    /// JOAT Brain), so it is not asked a second time.
    func createBatch(_ items: [BatchItem], label: String, confirmed: Bool = false,
                     completion: @escaping (BatchResult) -> Void) {
        guard store.authorized else {
            completion(BatchResult(refused: "Allow Reminders for JOAT Brain in Settings, then ask again."))
            return
        }
        var result = BatchResult()
        let members = items.filter { item in
            if isCreated(origin: item.origin) { result.already += 1; return false }
            return true
        }
        guard !members.isEmpty else { completion(result); return }
        let count = members.count
        let head = "Add \(count) reminder\(count == 1 ? "" : "s") from “"
        let source = label.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        let prompt = head + Self.fit(source.isEmpty ? "Untitled" : source,
                                     units: ConfirmGate.promptMaxUnits - (head + "”?").utf16.count) + "”?"
        let children = members.map { m in
            ConfirmGate.sha256("\(m.title)|\(Int(m.due.timeIntervalSince1970 / 60))|\(m.allDay)|\(m.origin)")
        }
        let payload = "reminders.create_batch|" + children.joined(separator: ",") + "|" + ConfirmGate.hex(16)
        let decide: (@escaping (ConfirmGate.Outcome) -> Void) -> Void = { [confirm] answer in
            if confirmed { answer(.accepted) } else { confirm.ask(prompt: prompt, payload: payload, completion: answer) }
        }
        decide { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .declined:
                result.declined = true
                completion(result)
            case .unverified:
                result.declined = true
                result.refused = "Reminders were not added because confirmation could not be verified."
                completion(result)
            case .accepted:
                var map = self.origins
                for m in members {
                    let due = Date(timeIntervalSince1970: (m.due.timeIntervalSince1970 / 60).rounded(.down) * 60)
                    // All day: a date with no time and no time zone (EventKit's floating day).
                    let comps = m.allDay
                        ? Calendar.current.dateComponents([.calendar, .year, .month, .day], from: due)
                        : Calendar.current.dateComponents([.calendar, .timeZone, .year, .month, .day, .hour, .minute], from: due)
                    do {
                        let saved = try self.store.create(title: m.title, due: comps)
                        let back = self.store.reminder(withIdentifier: saved.calendarItemIdentifier)
                        let landed = m.allDay
                            ? back?.dueDateComponents.map { $0.year == comps.year && $0.month == comps.month && $0.day == comps.day && $0.hour == nil } ?? false
                            : back?.dueDate.map({ Int($0.timeIntervalSince1970 / 60) }) == Int(due.timeIntervalSince1970 / 60)
                        guard let back, back.title == m.title, landed else {
                            result.problems.append("“\(m.title)” may have been created. Check Reminders before retrying.")
                            continue
                        }
                        map[m.origin] = back.calendarItemIdentifier
                        result.created += 1
                    } catch {
                        Log.add("batch reminder failed: \(error)")
                        result.problems.append("Reminders did not create “\(m.title)”.")
                    }
                }
                self.origins = map
                Task { @MainActor in await self.reloadPanel() }
                completion(result)
            }
        }
    }
}

extension Reminders {
    /// The menu's Reminders page (the wearer's pick, 2026-10-24): the plugin
    /// draws the next reminder as an image card on the left and these rows as
    /// a native list on the right. Rows use the overview tab's labels
    /// ("[Late] …", "[09:00] …", "[Tmrw] …"), overdue first, then the
    /// soonest, undated last; a tap asks to mark one done.
    @MainActor
    func listPage() async -> (list: [String: Any], ids: [String?]) {
        guard store.authorized else {
            return (["title": "REMINDERS · SETUP", "items": ["Allow Reminders for JOAT Brain in Settings."],
                     "hint": CapabilityMenu.backHint], [])
        }
        let open = await store.incomplete()
        guard !open.isEmpty else {
            return (["title": "REMINDERS · EMPTY", "items": ["No open reminders.", "Say: remind me to … at …"],
                     "hint": CapabilityMenu.backHint], [])
        }
        let now = Date()
        let cal = Calendar.current
        let today = cal.startOfDay(for: now)
        let tomorrow = cal.date(byAdding: .day, value: 1, to: today)!
        let dayAfter = cal.date(byAdding: .day, value: 2, to: today)!
        let dated = open.filter { $0.dueDate != nil }.sorted { $0.dueDate! < $1.dueDate! }
        let undated = open.filter { $0.dueDate == nil }
        func late(_ r: EKReminder) -> Bool {
            guard let due = r.dueDate else { return false }
            return r.dueDateComponents?.hour != nil ? due < now : due < today
        }
        var rows: [String] = []
        var ids: [String?] = []
        let ordered = dated + undated
        let shown = ordered.count <= 20 ? ordered : Array(ordered.prefix(19))
        for r in shown {
            rows.append((r.dueDate == nil ? "[No date] \(r.title ?? "Reminder")" : Self.row(r, today: today, twelveHour: true)).utf16Prefix(64))
            ids.append(r.calendarItemIdentifier)
        }
        if ordered.count > shown.count {
            rows.append("+\(ordered.count - shown.count) more")
            ids.append(nil)
        }
        // The card: the next reminder not yet late, and the counts.
        var card: [String: Any] = [
            "late": dated.filter(late).count,
            "today": dated.filter { !late($0) && $0.dueDate! < tomorrow }.count,
            "tomorrow": dated.filter { $0.dueDate! >= tomorrow && $0.dueDate! < dayAfter }.count,
            "later": dated.filter { $0.dueDate! >= dayAfter }.count,
            "undated": undated.count,
        ]
        if let next = dated.first(where: { !late($0) }), let due = next.dueDate {
            card["title"] = next.title ?? "Reminder"
            card["dueUnixMs"] = Int(due.timeIntervalSince1970 * 1000)
            card["allDay"] = next.dueDateComponents?.hour == nil
        }
        return (["title": "REMINDERS · \(open.count) OPEN", "items": rows, "hint": "tap: done · left x2: back",
                 "selectable": true, "reminderCard": card], ids)
    }
}
