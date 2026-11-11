import SwiftUI
import Speech
import EventKit
import Contacts
import UserNotifications
import UniformTypeIdentifiers
import PencilKit

// JOAT Brain's own screen as an app, not a lab console (the wearer,
// 2026-10-30: "very bad"; they picked tabs): Home says whether everything
// works and what to do when it doesn't, Notes holds the saved notes and
// makes reminders from them, Prep and Messages have their own tabs, and the
// lab readouts live under Diagnostics.

extension QuickNotes.VoiceNote: Identifiable {}
extension QuickNotes.TextNote: Identifiable {}

/// The saved notes, for the Notes tab; QuickNotes tells it when they change.
@MainActor
final class NotesModel: ObservableObject {
    @Published private(set) var voice: [QuickNotes.VoiceNote] = []
    @Published private(set) var text: [QuickNotes.TextNote] = []

    func reload() {
        guard let q = JOATRuntime.shared.brain?.quickNotes else { return }
        voice = q.savedNotes
        text = q.textNotes
    }
}

/// What JOAT Brain needs from iOS, and whether it has it.
@MainActor
final class Permissions: ObservableObject {
    enum State { case allowed, notAsked, denied }
    struct Item: Identifiable {
        let id: String
        let name: String
        let why: String
        let state: State
    }
    @Published private(set) var items: [Item] = []

    func refresh() async {
        let speech: State = switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let reminders: State = switch EKEventStore.authorizationStatus(for: .reminder) {
        case .fullAccess: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let contacts: State = switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized, .limited: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        let notifications: State = switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral: .allowed
        case .notDetermined: .notAsked
        default: .denied
        }
        items = [
            Item(id: "speech", name: "Speech recognition", why: "Hears what you say to the glasses.", state: speech),
            Item(id: "reminders", name: "Reminders", why: "Adds, lists and completes your reminders.", state: reminders),
            Item(id: "contacts", name: "Contacts", why: "Finds the number of the person you text.", state: contacts),
            Item(id: "notifications", name: "Notifications", why: "Hands texts you confirm to Shortcuts.", state: notifications),
        ]
    }

    /// Ask iOS; a permission already refused can only change in Settings.
    func fix(_ item: Item) async {
        if item.state == .denied {
            if let url = URL(string: UIApplication.openSettingsURLString) { await UIApplication.shared.open(url) }
            return
        }
        switch item.id {
        case "speech":
            _ = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) } }
            JOATRuntime.shared.stt.start()
        case "reminders":
            await JOATRuntime.shared.brain?.reminders.store.requestAccess()
        case "contacts":
            _ = try? await CNContactStore().requestAccess(for: .contacts)
        case "notifications":
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        default:
            break
        }
        await refresh()
    }
}

// MARK: - Home

struct HomeView: View {
    @ObservedObject private var server = JOATRuntime.shared.server
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var stt = JOATRuntime.shared.stt
    @ObservedObject private var voice = JOATRuntime.shared.voice
    @ObservedObject var permissions: Permissions
    @AppStorage(Brain.followUpKey) private var followUpSeconds: Double = 8
    @AppStorage(Cues.enabledKey) private var cuesOn = true

    private var glassesOK: Bool { server.authenticated && server.clients > 0 }
    private var listening: Bool { stt.engine.hasSuffix("listening") }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    StatusRow(ok: glassesOK,
                              title: glassesOK ? "Glasses connected" : "Glasses not connected",
                              detail: glassesOK ? "JOAT on the glasses is talking to this iPhone."
                                  : "Waiting for JOAT on the glasses. Steps below.")
                    StatusRow(ok: keepAlive.active,
                              title: keepAlive.active ? "Keeps running in the background" : "Not kept running",
                              detail: keepAlive.active ? "Works behind the Even app and with the screen locked."
                                  : "Without it, iOS pauses JOAT Brain when you leave it.",
                              actionTitle: keepAlive.active ? nil : "Restart") { keepAlive.start() }
                    StatusRow(ok: listening,
                              title: listening ? "Listening for “Hey JOAT”" : "Speech recognition not ready",
                              detail: listening ? "Speech is turned into text on this iPhone." : stt.engine)
                    StatusRow(ok: JOATRuntime.shared.claudeConfigured,
                              title: JOATRuntime.shared.claudeConfigured ? "Claude connected" : "No Claude key in this build",
                              detail: JOATRuntime.shared.claudeConfigured
                                  ? "Answers, note summaries, reminders from notes and AI cues."
                                  : "Answers and note summaries need a build with a Claude key.")
                }
                if !glassesOK {
                    Section("Connect the glasses") {
                        Label("Open the Even Realities app.", systemImage: "1.circle")
                        Label("Start JOAT: from the glasses' menu, or Even Hub › JOAT in the app.", systemImage: "2.circle")
                        Label("If JOAT asks which brain to use, pick “This iPhone · JOAT Brain” and Connect.", systemImage: "3.circle")
                        Label("It connects by itself within about 15 seconds.", systemImage: "4.circle")
                    }
                }
                Section {
                    DisclosureGroup("Start JOAT Brain with the Even app (1 min)") {
                        Step(n: 1, "Open Shortcuts › Automation › + (New Automation) › App.")
                        Step(n: 2, "Choose Even Realities, tick Is Opened, choose Run Immediately, tap Next.")
                        Step(n: 3, "New Blank Automation › Add Action › “Start JOAT Brain”. Tap Done.")
                    }
                } footer: {
                    Text("Optional. If iOS ever closes JOAT Brain (a restart, a long call), opening the Even app brings it back without you opening this app.")
                }
                let missing = permissions.items.filter { $0.state != .allowed }
                if !missing.isEmpty {
                    Section("Needs your OK") {
                        ForEach(missing) { item in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.name)
                                    Text(item.why).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button(item.state == .denied ? "Settings" : "Allow") {
                                    Task { await permissions.fix(item) }
                                }
                                .buttonStyle(.borderedProminent)
                            }
                        }
                    }
                }
                Section("Settings") {
                    Toggle("Speak replies in headphones", isOn: $voice.spokenReplies)
                    Toggle("AI cues during QuickNotes", isOn: $cuesOn)
                    Stepper(value: $followUpSeconds, in: 0...30, step: 1) {
                        LabeledContent("Follow-up without “Hey JOAT”",
                                       value: followUpSeconds == 0 ? "Off" : "\(Int(followUpSeconds)) s")
                    }
                }
            }
            .navigationTitle("JOAT Brain")
            .task { await permissions.refresh() }
            .refreshable { await permissions.refresh() }
        }
    }
}

struct StatusRow: View {
    let ok: Bool
    let title: String
    let detail: String
    var actionTitle: String? = nil
    var perform: () -> Void = {}

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ok ? .green : .orange)
                .font(.title3)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let actionTitle {
                Button(actionTitle, action: perform).buttonStyle(.bordered)
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Notes

struct NotesView: View {
    @ObservedObject var model: NotesModel
    @State private var newPage = false

    var body: some View {
        NavigationStack {
            List {
                if model.voice.isEmpty && model.text.isEmpty {
                    ContentUnavailableView("No notes yet", systemImage: "note.text",
                                           description: Text("Tap ✎ to write a page, or on the glasses: QuickNotes › + Create new note."))
                }
                if !model.voice.isEmpty {
                    Section("QuickNotes") {
                        ForEach(model.voice) { n in
                            NavigationLink(value: n.id) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Label {
                                        Text(n.title.isEmpty ? "Untitled" : n.title).font(.body.weight(.semibold))
                                    } icon: {
                                        Image(systemName: n.page == true ? "pencil.and.scribble" : "waveform")
                                    }
                                    Text(n.summary ?? n.paragraphs.first
                                         ?? (Notebook.reading.contains(n.id) ? "Reading your handwriting…" : ""))
                                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                                    Text(n.updated.formatted(date: .abbreviated, time: .shortened))
                                        .font(.caption2).foregroundStyle(.tertiary)
                                }
                            }
                        }
                    }
                }
                if !model.text.isEmpty {
                    Section("Text notes") {
                        ForEach(model.text) { t in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(t.text)
                                Text(t.updated.formatted(date: .abbreviated, time: .shortened))
                                    .font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Notes")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { newPage = true } label: { Label("New page", systemImage: "square.and.pencil") }
                }
            }
            .fullScreenCover(isPresented: $newPage) {
                PageEditorView(noteId: nil, typed: "", drawing: PKDrawing()) { _ in model.reload() }
            }
            #if DEBUG
            .onReceive(NotificationCenter.default.publisher(for: Notebook.debugOpen)) { _ in newPage = true }
            #endif
            .navigationDestination(for: String.self) { id in NoteDetailView(noteId: id, model: model) }
            .onAppear { model.reload() }
            .refreshable { model.reload() }
        }
    }
}

struct OfferSheet: Identifiable {
    let id = UUID()
    let offer: QuickNotes.ReminderOffer
}

struct NoteDetailView: View {
    let noteId: String
    @ObservedObject var model: NotesModel
    @State private var sheet: OfferSheet?
    @State private var finding = false
    @State private var summarizing = false
    @State private var message = ""
    @State private var editing = false
    @Environment(\.colorScheme) private var colorScheme

    private var note: QuickNotes.VoiceNote? { model.voice.first { $0.id == noteId } }
    private var quickNotes: QuickNotes? { JOATRuntime.shared.brain?.quickNotes }

    var body: some View {
        if let n = note {
            List {
                if n.page == true { pageSection(n) }
                Section {
                    Text(n.created.formatted(date: .complete, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                    Button {
                        findReminders()
                    } label: {
                        Label(finding ? "Finding reminders…" : "Find reminders in this note", systemImage: "checklist")
                    }
                    .disabled(finding)
                    if !message.isEmpty {
                        Text(message).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let summary = n.summary {
                    Section("Overview") { Text(summary) }
                } else {
                    Section {
                        Button(summarizing ? "Summarizing…" : "Summarize this note") {
                            summarizing = true
                            quickNotes?.summarize(n.id) { r in
                                summarizing = false
                                message = r
                                model.reload()
                            }
                        }
                        .disabled(summarizing)
                    }
                }
                if !n.keyPoints.isEmpty {
                    Section("Key points") { ForEach(n.keyPoints, id: \.self) { Text("• \($0)") } }
                }
                if !n.todos.isEmpty {
                    Section("Next steps") { ForEach(n.todos, id: \.self) { Text("□ \($0)") } }
                }
                if let cues = n.cues, !cues.isEmpty {
                    Section("AI cues") { ForEach(cues, id: \.self) { Text($0).font(.callout) } }
                }
                Section(n.page == true ? "Text" : "Transcript") {
                    ForEach(Array(n.paragraphs.enumerated()), id: \.offset) { Text($0.element) }
                }
            }
            .navigationTitle(n.title.isEmpty ? "Untitled" : n.title)
            .navigationBarTitleDisplayMode(.inline)
            .sheet(item: $sheet) { s in
                ReminderReviewSheet(offer: s.offer) { result in message = result }
            }
            .fullScreenCover(isPresented: $editing) {
                PageEditorView(noteId: n.id, typed: n.typed ?? "", drawing: PageStore.load(n.id)) { _ in model.reload() }
            }
        } else {
            ContentUnavailableView("This note is gone", systemImage: "note.text")
        }
    }

    /// A notebook page: its ink, Edit, reading the handwriting again, and
    /// using it as the prep notes the AI cues answer from.
    @ViewBuilder
    private func pageSection(_ n: QuickNotes.VoiceNote) -> some View {
        Section {
            if let image = PageStore.preview(PageStore.load(n.id), dark: colorScheme == .dark) {
                Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 420)
            }
            Button { editing = true } label: { Label("Edit page", systemImage: "square.and.pencil") }
            if PageStore.hasInk(n.id), n.handwriting == nil {
                if Notebook.reading.contains(n.id) {
                    Label("Reading your handwriting…", systemImage: "hourglass")
                } else if let quickNotes {
                    Button {
                        Notebook.read(n.id, PageStore.load(n.id), quickNotes: quickNotes)
                        model.reload()
                    } label: { Label("Read my handwriting", systemImage: "text.viewfinder") }
                }
            }
            Button {
                let text = n.paragraphs.joined(separator: "\n")
                guard !text.isEmpty else { message = "This page has no text to use yet."; return }
                JOATRuntime.shared.prep.addText(text, title: n.title.isEmpty ? "Page" : n.title, source: "Notebook page")
                message = "Added to Prep notes: the AI cues use it now."
            } label: { Label("Use as prep notes", systemImage: "doc.text.magnifyingglass") }
        }
    }

    private func findReminders() {
        guard let quickNotes else { return }
        finding = true
        message = ""
        quickNotes.proposeReminders(noteId) { offer, why in
            finding = false
            if let offer { sheet = OfferSheet(offer: offer) } else { message = why }
        }
    }
}

/// The reminders Claude found in a note: untick any, change the words or
/// the date, then Add. Add is the confirmation; the glasses don't ask again.
struct ReminderReviewSheet: View {
    @State private var offer: QuickNotes.ReminderOffer
    @State private var include: [Bool]
    @State private var adding = false
    @Environment(\.dismiss) private var dismiss
    let done: (String) -> Void

    init(offer: QuickNotes.ReminderOffer, done: @escaping (String) -> Void) {
        _offer = State(initialValue: offer)
        _include = State(initialValue: Array(repeating: true, count: offer.items.count))
        self.done = done
    }

    private var chosen: Int { include.filter { $0 }.count }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("From “\(offer.title)”. Untick any you don't want; change the words or the time.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(offer.items.indices, id: \.self) { i in
                    Section {
                        Toggle(isOn: $include[i]) {
                            TextField("Reminder", text: $offer.items[i].title)
                        }
                        if include[i] {
                            Toggle("All day", isOn: $offer.items[i].allDay)
                            DatePicker("When", selection: $offer.items[i].due,
                                       displayedComponents: offer.items[i].allDay ? [.date] : [.date, .hourAndMinute])
                        }
                    }
                }
            }
            .navigationTitle("Reminders")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(adding ? "Adding…" : "Add \(chosen)") { add() }
                        .disabled(chosen == 0 || adding)
                }
            }
        }
    }

    private func add() {
        guard let quickNotes = JOATRuntime.shared.brain?.quickNotes else { return }
        adding = true
        var picked = offer
        picked.items = offer.items.indices.filter { include[$0] }.map { offer.items[$0] }
        quickNotes.addReminders(picked) { result in
            adding = false
            done(result)
            dismiss()
        }
    }
}

// MARK: - Prep

struct PrepView: View {
    @ObservedObject private var prep = JOATRuntime.shared.prep
    @AppStorage(Cues.enabledKey) private var cuesOn = true
    @State private var importing = false
    @State private var typing = false
    @State private var typed = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button { importing = true } label: { Label("Add a file…", systemImage: "doc.badge.plus") }
                    PasteButton(payloadType: String.self) { strings in
                        DispatchQueue.main.async { prep.paste(strings.first, url: nil) }
                    }
                    Button { typing = true } label: { Label("Type notes…", systemImage: "square.and.pencil") }
                    if !prep.status.isEmpty {
                        Text(prep.status).font(.caption).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("Or share a PDF or text file to JOAT Brain from any app. Paste takes a link (the page is read) or text.")
                }
                if !prep.docs.isEmpty {
                    Section {
                        ForEach(prep.docs) { doc in
                            Button { prep.select(doc.id) } label: {
                                HStack(alignment: .top) {
                                    Image(systemName: doc.id == prep.active?.id ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(doc.id == prep.active?.id ? .green : .secondary)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(doc.title).foregroundStyle(.primary)
                                        Text("\(doc.source) · \(doc.points.isEmpty ? "no talking points yet" : "\(doc.points.count) talking points")")
                                            .font(.caption).foregroundStyle(.secondary)
                                        if doc.id == prep.active?.id {
                                            ForEach(doc.points, id: \.self) { Text("– \($0)").font(.caption) }
                                        }
                                    }
                                }
                            }
                        }
                        .onDelete { offsets in offsets.map { prep.docs[$0].id }.forEach { prep.remove($0) } }
                    } header: {
                        Text("In use: the ticked one")
                    } footer: {
                        Text("On an open QuickNote, new AI cues pop up for a few seconds. Tap the note (or use its menu) to see the prep and every cue.")
                    }
                }
                Section { Toggle("AI cues during QuickNotes", isOn: $cuesOn) }
            }
            .navigationTitle("Prep")
            .fileImporter(isPresented: $importing, allowedContentTypes: [.pdf, .text, .html, .rtf]) { result in
                if case .success(let url) = result { prep.addFile(url) }
            }
            .sheet(isPresented: $typing) {
                NavigationStack {
                    TextEditor(text: $typed)
                        .padding()
                        .navigationTitle("Prep notes")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { typing = false } }
                            ToolbarItem(placement: .confirmationAction) {
                                Button("Add") { prep.addText(typed); typed = ""; typing = false }
                                    .disabled(typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }
                }
            }
        }
    }
}

// MARK: - Diagnostics

struct DiagnosticsView: View {
    @ObservedObject private var log = Log.shared
    @ObservedObject private var server = JOATRuntime.shared.server
    @ObservedObject private var keepAlive = JOATRuntime.shared.keepAlive
    @ObservedObject private var stt = JOATRuntime.shared.stt
    @ObservedObject private var endurance = JOATRuntime.shared.endurance
    @ObservedObject private var voice = JOATRuntime.shared.voice
    @StateObject private var selfTest = SelfTest()

    var body: some View {
        NavigationStack {
            List {
                Section("Glasses link") {
                    LabeledContent("Listener", value: server.state)
                    LabeledContent("Plugin origin", value: server.lastOrigin)
                    LabeledContent("Authenticated", value: server.authenticated ? "yes" : "no")
                    LabeledContent("Heartbeat", value: "\(server.pongs) pongs, last \(server.lastPongMs) ms")
                    LabeledContent("Glasses audio", value: "\(server.audioFrames) frames")
                    HStack {
                        Button("Self-test") { selfTest.run(port: server.port) }.buttonStyle(.borderless)
                        Spacer()
                        Text(selfTest.result).foregroundStyle(.secondary)
                    }
                }
                Section("Speech") {
                    LabeledContent("Engine", value: stt.engine)
                    LabeledContent("Hearing", value: stt.lastVolatile.isEmpty ? "—" : stt.lastVolatile)
                    LabeledContent("Last sentence", value: stt.lastFinal.isEmpty ? "—" : stt.lastFinal)
                    LabeledContent("Finalised after", value: stt.lastLatencyMs.map { "\($0) ms" } ?? "—")
                    Button("Speak a test line") { voice.say("JOAT Brain, speaking from the phone.") }
                }
                Section("Keep-alive") {
                    LabeledContent("Active", value: keepAlive.active ? "yes (\(keepAlive.mode))" : "no (\(keepAlive.mode))")
                    LabeledContent("Route", value: keepAlive.route)
                    // Borderless: in a List row two plain buttons BOTH fire on a tap.
                    HStack {
                        Button("Start") { keepAlive.start() }.buttonStyle(.borderless)
                        Spacer()
                        Button("Stop", role: .destructive) { keepAlive.stop() }.buttonStyle(.borderless)
                    }
                }
                Section("Uptime") {
                    Text(endurance.summary).font(.system(.caption, design: .monospaced))
                }
                Section {
                    ForEach(Array(log.lines.enumerated().reversed()), id: \.offset) { _, line in
                        Text(line).font(.system(.caption, design: .monospaced))
                    }
                } header: {
                    HStack { Text("Log"); Spacer(); Button("Clear") { log.clear() }.font(.caption) }
                }
            }
            .navigationTitle("Diagnostics")
        }
    }
}

// MARK: - Messages

/// Texts on the glasses with the iPhone alone. iOS lets no app read or send
/// Messages (the Mac does it by reading Messages' own database, which iOS
/// never allows), so Apple's Shortcuts carries them: one automation hands
/// each text to JOAT Brain, another sends the reply you confirm on the
/// glasses from your own number. Researched 2026-10-30: nothing else works on
/// a stock iPhone (Even's plugin SDK has no notification access, Bluetooth
/// message access needs a second device, AccessoryNotifications is EU-only).
struct MessagesView: View {
    @State private var texts: [Messages.Text] = []
    @State private var showIn = false
    @State private var showOut = false

    private var received: Messages.Text? { texts.last { !$0.outgoing } }
    private var sent: Messages.Text? { texts.last { $0.outgoing } }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    StatusRow(ok: received != nil,
                              title: received != nil ? "Texts reach the glasses" : "Texts: set up step 1",
                              detail: received.map { "Last from \($0.from), \($0.at.formatted(.relative(presentation: .named)))." }
                                  ?? "One automation hands each text to JOAT Brain.")
                    StatusRow(ok: sent != nil,
                              title: sent != nil ? "Replies send from your number" : "Replies: set up step 2",
                              detail: sent.map { "Last to \($0.from), \($0.at.formatted(.relative(presentation: .named)))." }
                                  ?? "A second automation sends what you confirm on the glasses.")
                } footer: {
                    Text("Once set up: a text shows on the glasses. On the glasses, Messages › tap a conversation, tap to reply, say it, then tap to confirm. Or say “reply …” or “text Mom saying …”. Nothing is sent without your tap.")
                }
                Section("Step 1 · Show texts on the glasses") {
                    DisclosureGroup("How to set it up (2 min)", isExpanded: $showIn) {
                        Step(n: 1, "Open Shortcuts › Automation › + (New Automation).")
                        Step(n: 2, "Choose Message. Message Contains: type one space. Sender: anyone.")
                        Step(n: 3, "Choose Run Immediately, turn Notify When Run off, tap Next.")
                        Step(n: 4, "Tap New Blank Automation › Add Action, search “Show Text on Glasses” (JOAT Brain).")
                        Step(n: 5, "Tap From › Shortcut Input, then tap it again and pick Sender.")
                        Step(n: 6, "Tap Message › Shortcut Input, then pick Content. Tap Done.")
                        Step(n: 7, "Ask someone to text you: it shows on the glasses and here.")
                    }
                    Button { open("shortcuts://") } label: { Label("Open Shortcuts", systemImage: "arrow.up.forward.app") }
                }
                Section("Step 2 · Send replies") {
                    DisclosureGroup("How to set it up (3 min)", isExpanded: $showOut) {
                        Step(n: 1, "Open Shortcuts › Automation › + (New Automation).")
                        Step(n: 2, "Choose Notification. App: JOAT Brain. Title contains: JOAT • text to send.")
                        Step(n: 3, "Choose Run Immediately, turn Notify When Run off, tap Next › New Blank Automation.")
                        Step(n: 4, "Add Action “Get Text to Send” (JOAT Brain).")
                        Step(n: 5, "Add Action “If”. Input: Text to Send › Group. Condition: has any value.")
                        Step(n: 6, "Inside the If: add “Find Conversations”, filter Group Name is Text to Send › Group, Limit 1. Then add “Send Message”. Message: Text to Send › Message. Recipients: Conversations.")
                        Step(n: 7, "Under Otherwise: add “Send Message”. Message: Text to Send › Message. Recipients: Text to Send › Recipient.")
                        Step(n: 8, "On each Send Message, tap the arrow and turn Show When Run off.")
                        Step(n: 9, "After End If: add “Text Sent on Glasses” (JOAT Brain). Tap Done.")
                        Step(n: 10, "Test: when a text arrives, say “reply on my way” and tap yes on the glasses.")
                    }
                    Button { open("shortcuts://") } label: { Label("Open Shortcuts", systemImage: "arrow.up.forward.app") }
                }
                Section {
                    Text("Even's app can also show every text on the glasses by itself: iPhone Settings › Notifications › Messages › Show Previews › Always. With AirPods, Siri can read texts and send replies hands-free: Settings › Notifications › Announce Notifications, and Siri › Reply Without Confirmation.")
                        .font(.footnote).foregroundStyle(.secondary)
                } header: {
                    Text("Without setup")
                }
                if !texts.isEmpty {
                    Section("Recent") {
                        ForEach(texts.reversed().prefix(30), id: \.id) { t in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(t.outgoing ? "You → \(t.from)" : t.from).font(.caption.weight(.semibold))
                                Text(t.body)
                                Text(t.at.formatted(date: .omitted, time: .shortened))
                                    .font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Messages")
            .onAppear { reload() }
            .refreshable { reload() }
        }
    }

    private func reload() { texts = JOATRuntime.shared.brain?.messages.texts ?? [] }
    private func open(_ link: String) { if let url = URL(string: link) { UIApplication.shared.open(url) } }
}

struct Step: View {
    let n: Int
    let text: String
    init(n: Int, _ text: String) { self.n = n; self.text = text }
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text("\(n).").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
            Text(text).font(.callout)
        }
    }
}
