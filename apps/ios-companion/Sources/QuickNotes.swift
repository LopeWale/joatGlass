import Foundation

/// QuickNotes on the phone: voice notes (talk while holding the touchpad, or
/// record continuously) and exact text notes, one list. Ports, by file:
///   services/voice_note_capture.py  the open note, its lane, undo, receipts
///   services/capabilities.py        list / live / FINISH / DISCARD? / readers
///   services/quicknotes_panel.py    the rail tab (four newest)
///   services/note_commands.py       "take a note …", "find notes …", undo/restore
///   persistence/voice_notes.py, notes.py  what is kept
///
/// The phone hears through the streaming dictation engine instead of the
/// Mac's chunked Whisper: while the note's lane is open, finished sentences
/// go into the note and nothing reaches the wake gate (the Mac's exclusive
/// capture lane). No raw audio is kept, so a saved note is its transcript.
/// Summaries, "ask the note" and reminders from a note need a language model:
/// they come with the Mac link (the wearer's choice, 2026-10-22).
final class QuickNotes: VoiceCommand, PanelProvider {
    // MARK: - Storage

    struct VoiceNote: Codable {
        var id: String
        var title: String
        /// "open" while being recorded, then "saved".
        var state: String
        var created: Date
        var updated: Date
        var paragraphs: [String]
        var summary: String?
        var keyPoints: [String] = []
        var todos: [String] = []
        /// The AI cues shown while it recorded ("Concept: OPC UA — …").
        var cues: [String]?
        /// A notebook page written in JOAT Brain (Notebook.swift), not a
        /// recording: its typed words, and its handwriting as Claude read it
        /// (nil until read). The ink is in Documents/pages/<id>.drawing.
        var page: Bool? = nil
        var typed: String? = nil
        var handwriting: String? = nil
        var paperTemplate: String? = nil
        /// The page changed after its summary was made: the summary stays on
        /// show (marked as such) until the new one arrives.
        var summaryStale: Bool? = nil
    }

    struct TextNote: Codable {
        var id: String
        var text: String
        var created: Date
        var updated: Date
        var deleted: Bool = false
    }

    private struct Stored: Codable {
        var voice: [VoiceNote] = []
        var text: [TextNote] = []

        init() {}
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            voice = try c.decodeIfPresent([VoiceNote].self, forKey: .voice) ?? []
            text = try c.decodeIfPresent([TextNote].self, forKey: .text) ?? []
        }
    }

    private var stored = Stored()
    private let file: URL

    private func load() {
        guard let data = try? Data(contentsOf: file) else { return }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        do { stored = try decoder.decode(Stored.self, from: data) }
        catch { SafeStore.quarantine(file, error) }
    }

    private func persist() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        do { try encoder.encode(stored).write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]) }
        catch { Log.add("quicknotes save failed: \(error)") }
        onStoreChange?()
    }

    /// JOAT Brain's Notes tab: saved voice notes newest first, text notes.
    var savedNotes: [VoiceNote] { retained }
    var textNotes: [TextNote] { activeText }
    /// Something saved changed (the Notes tab repaints).
    var onStoreChange: (() -> Void)?

    /// voice_notes.retained(): saved notes, most recently updated first.
    private var retained: [VoiceNote] {
        stored.voice.filter { $0.state == "saved" }.sorted { ($0.updated, $0.id) > ($1.updated, $1.id) }
    }
    /// notes.active(): updated desc, then id asc.
    private var activeText: [TextNote] {
        stored.text.filter { !$0.deleted }.sorted { $0.updated != $1.updated ? $0.updated > $1.updated : $0.id < $1.id }
    }

    // MARK: - Wiring

    weak var menu: CapabilityMenu?
    private let link: PluginLink
    private let hud: Hud
    /// Push the rail tab.
    var publishPanel: (() -> Void)?
    /// Summaries, "ask the note" and the commitments pass run on the Mac's
    /// assistant (MacLink); reminders are written here through Reminders.
    var macLink: MacLink?
    /// Notes whose summary came back unusable: not retried on the timer
    /// ("summarize the last note" still asks again).
    var insightsGaveUp = Set<String>()
    weak var reminders: Reminders?
    /// AI cues while a note records, and the prep they answer from.
    var cues: Cues? {
        didSet { cues?.onCue = { [weak self] cue in self?.cueArrived(cue) } }
    }
    weak var prep: Prep?
    private var insightsAvailable: Bool { macLink?.configured == true }
    /// A model (Gemini, Claude or the Mac) can summarize notes on this phone.
    var insightsConfigured: Bool { insightsAvailable }
    /// A note's summary started or finished (the page card and list repaint).
    static let insightsChangedNotification = Notification.Name("joat.quicknotes.insightsChanged")
    func isSummarizing(_ id: String) -> Bool { summaryRuns.isRunning(id) }
    private func insightsChanged(_ id: String) {
        DispatchQueue.main.async { NotificationCenter.default.post(name: Self.insightsChangedNotification, object: id) }
    }

    init(link: PluginLink, hud: Hud, directory: URL? = nil) {
        self.link = link
        self.hud = hud
        file = (directory ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0])
            .appendingPathComponent("quicknotes.json")
        load()
        recoverInterrupted()
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 60, repeating: 60)
        t.setEventHandler { [weak self] in self?.pushPanelIfChanged() }
        t.resume()
        panelTimer = t
    }
    private var panelTimer: DispatchSourceTimer?

    /// A note still open when JOAT Brain stopped is saved as Save would
    /// have saved it (voice_note_capture recover at launch).
    private func recoverInterrupted() {
        var changed = false
        for i in stored.voice.indices.reversed() where stored.voice[i].state == "open" {
            stored.voice[i].paragraphs = stored.voice[i].paragraphs.filter { !$0.isEmpty }
            if stored.voice[i].paragraphs.isEmpty { stored.voice.remove(at: i) }
            else { stored.voice[i].state = "saved" }
            changed = true
        }
        if changed { persist() }
    }

    // MARK: - The open note (voice_note_capture.py)

    private enum Rec { case paused, recording }
    private struct Input { let paragraph: Int; let offset: Int }

    private var openIndex: Int?
    private var rec = Rec.paused
    private var holding = false
    private var holdInput: Input?
    /// The last release of the lane (pause, hold end, stop): words whose
    /// audio ended before it still belong to the note for a moment.
    private var laneClosedAt: Date?
    private var inputs: [Input] = []
    /// The paragraph a continuous stretch is growing.
    private var growing: Int?
    private var partial = ""
    private var discardPending = false
    private var resumeAfterKeep = false
    private var finalizing = false
    private var laneOpenedAt: Date?
    private var recordedBefore: TimeInterval = 0
    private var lastAudioAt: Date?
    private var lastFinalEnd: Date?
    private var speechStartedAt: Date?
    private var micReopened = false

    var active: Bool { openIndex != nil }
    var hasOpenNote: Bool { openIndex != nil }
    var openVoiceNoteSnapshot: VoiceNote? { note }
    private var note: VoiceNote? { openIndex.map { stored.voice[$0] } }

    /// Silence this long inside a continuous recording starts a new paragraph
    /// (the Mac: a wholly silent chunk).
    static let paragraphSilence: TimeInterval = 3
    /// Words still arriving after a release or a stop belong to it this long
    /// (dictation finalises a sentence a moment after its audio).
    static let grace: TimeInterval = 3
    /// A sentence still running this long past the release is the note's.
    static let releaseTolerance: TimeInterval = 1

    /// Is the lane open for a sentence that ended at `audioEnd`? While it is,
    /// the sentence belongs to the note and the wake gate never sees it.
    func wantsSpeech(audioEnd: Date) -> Bool {
        guard active else { return false }
        if rec == .recording { return true }
        if let end = laneClosedAt, Date().timeIntervalSince(end) < Self.grace,
           audioEnd <= end.addingTimeInterval(Self.releaseTolerance) { return true }
        return false
    }

    var laneOpen: Bool { active && (rec == .recording || finalizing) }

    /// The glasses' audio while a note records: its level in the log, and
    /// the audio itself when Diagnostics › Save note audio is on.
    let audioCheck = AudioCheck()

    func audioFrame(_ samples: [Int16]) {
        lastAudioAt = Date()
        audioCheck.frame(samples, note: rec == .recording ? note?.id : nil)
        guard rec == .recording else { return }
        if micReopened { micReopened = false; changed() }
    }

    func hearing(_ text: String) {
        guard laneOpen else { return }
        if partial.isEmpty, speechStartedAt == nil { speechStartedAt = Date() }
        partial = text
        changed()
    }

    func heard(_ text: String, audioEnd: Date) {
        guard let i = openIndex else { return }
        partial = ""
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        defer { lastFinalEnd = audioEnd; speechStartedAt = nil }
        guard !words.isEmpty else { changed(); return }
        let target: Int
        if let hold = holdInput {
            target = hold.paragraph
        } else {
            let silence = (speechStartedAt ?? audioEnd).timeIntervalSince(lastFinalEnd ?? .distantPast)
            if growing == nil || silence > Self.paragraphSilence {
                stored.voice[i].paragraphs.append("")
                growing = stored.voice[i].paragraphs.count - 1
                inputs.append(Input(paragraph: growing!, offset: 0))
            }
            target = growing!
        }
        let p = stored.voice[i].paragraphs[target]
        stored.voice[i].paragraphs[target] = p.isEmpty ? words : p + " " + words
        stored.voice[i].updated = Date()
        persist()
        changed()
        cues?.heard(words)
    }

    private func openLane() {
        rec = .recording
        laneOpenedAt = Date()
        lastAudioAt = nil
        micReopened = false
        growing = nil
    }

    private func closeLane() {
        if let at = laneOpenedAt { recordedBefore += Date().timeIntervalSince(at) }
        laneOpenedAt = nil
        rec = .paused
        laneClosedAt = Date()
        partial = ""
        audioCheck.laneClosed()
    }

    private var recordedSeconds: Int {
        Int(recordedBefore + (laneOpenedAt.map { Date().timeIntervalSince($0) } ?? 0))
    }

    private var silentFor: TimeInterval {
        guard rec == .recording, let began = laneOpenedAt else { return 0 }
        return Date().timeIntervalSince(lastAudioAt ?? began)
    }

    private func start(title: String?, hold: Bool) -> String {
        if let n = note { return "Voice Note is already \(rec == .recording ? "recording" : "paused"): \(n.title)." }
        guard let raw = title?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return "Say a title after “start a voice note”."
        }
        guard raw.count <= 200 else { return "Voice Note title must be 1 to 200 visible characters." }
        let now = Date()
        stored.voice.append(VoiceNote(id: UUID().uuidString.lowercased(), title: raw, state: "open",
                                      created: now, updated: now, paragraphs: []))
        openIndex = stored.voice.count - 1
        inputs = []; recordedBefore = 0; discardPending = false; resumeAfterKeep = false
        holding = false; holdInput = nil; laneClosedAt = nil; finalizing = false
        closeCueViews()
        cues?.begin(usePrep: notePrepChoice)
        notePrepChoice = true
        persist()
        #if DEBUG
        DebugSeed.noteOpened(self, cues: cues)
        #endif
        if hold {
            rec = .paused
            changed()
            return "Opened QuickNote: \(raw). Tap to record, or hold the touchpad to talk."
        }
        openLane()
        changed()
        return "Recording Voice Note: \(raw). Use the Voice Note screen to pause, save, or discard."
    }

    private func pause() -> String {
        guard let n = note else { return "No Voice Note is recording." }
        if rec == .paused { return "Voice Note is already paused: \(n.title)." }
        holding = false
        closeLane()
        changed()
        return "Paused Voice Note: \(n.title)."
    }

    private func resume() -> String {
        guard let n = note else { return "No Voice Note is paused." }
        if rec == .recording { return "Voice Note is already recording: \(n.title)." }
        if discardPending { return "Say confirm discard voice note or keep voice note first." }
        settleHold()
        openLane()
        changed()
        return "Resumed Voice Note: \(n.title)."
    }

    /// Touchpad held on the live page: listen until it is released.
    func holdStart() -> Bool {
        guard let i = openIndex, !holding, rec == .paused, !discardPending else { return false }
        settleHold()
        stored.voice[i].paragraphs.append("")
        holdInput = Input(paragraph: stored.voice[i].paragraphs.count - 1, offset: 0)
        holding = true
        openLane()
        changed()
        return true
    }

    /// The last tap on the open note: when, and whether it was recording.
    private var lastTap: (at: Date, wasRecording: Bool)?

    /// The glasses' menu opens with a tap, then a long press, and that tap
    /// reached the note first: put the recording back as it was.
    private func undoMenuTap() -> Bool {
        guard let tap = lastTap, Date().timeIntervalSince(tap.at) < 3, active, !discardPending else { return false }
        lastTap = nil
        if tap.wasRecording, rec == .paused { _ = resume() }
        else if !tap.wasRecording, rec == .recording { _ = pause() }
        else { return false }
        Log.add("quicknotes: that tap opened the glasses' menu; recording \(tap.wasRecording ? "carries on" : "stays paused")")
        return true
    }

    func holdEnd() -> Bool {
        guard active, holding else { return false }
        holding = false
        closeLane()
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.grace) { [weak self] in self?.settleHold() }
        changed()
        return true
    }

    /// The press opened the glasses' menu instead: keep none of it.
    func holdCancel() -> Bool {
        guard let i = openIndex, holding, let hold = holdInput else { return false }
        holding = false
        closeLane()
        if stored.voice[i].paragraphs.indices.contains(hold.paragraph) {
            stored.voice[i].paragraphs[hold.paragraph] = ""
        }
        holdInput = nil
        dropEmptyParagraphs()
        persist()
        changed()
        return true
    }

    /// A released hold becomes one input if it caught words.
    private func settleHold() {
        guard let i = openIndex, let hold = holdInput, !holding else { return }
        holdInput = nil
        if stored.voice[i].paragraphs.indices.contains(hold.paragraph), !stored.voice[i].paragraphs[hold.paragraph].isEmpty {
            inputs.append(hold)
        }
        dropEmptyParagraphs()
        persist()
        changed()
    }

    private func dropEmptyParagraphs() {
        guard let i = openIndex else { return }
        var remap: [Int: Int] = [:]
        var kept: [String] = []
        for (k, p) in stored.voice[i].paragraphs.enumerated() {
            let keep = !p.isEmpty || k == holdInput?.paragraph || k == growing
            if keep { remap[k] = kept.count; kept.append(p) }
        }
        stored.voice[i].paragraphs = kept
        inputs = inputs.compactMap { remap[$0.paragraph].map { Input(paragraph: $0, offset: 0) } }
        holdInput = holdInput.flatMap { h in remap[h.paragraph].map { Input(paragraph: $0, offset: h.offset) } }
        growing = growing.flatMap { remap[$0] }
    }

    /// "Undo last input": the newest paragraph leaves the note.
    func undo() -> String {
        guard let i = openIndex else { return "No Voice Note is open." }
        if holding { return "Let go of the touchpad first." }
        settleHold()
        guard let last = inputs.popLast(), stored.voice[i].paragraphs.indices.contains(last.paragraph) else {
            return "Nothing to undo."
        }
        stored.voice[i].paragraphs[last.paragraph] = ""
        if growing == last.paragraph { growing = nil }
        dropEmptyParagraphs()
        stored.voice[i].updated = Date()
        persist()
        changed()
        return "Removed the last paragraph."
    }

    private func requestDiscard() -> String {
        guard let n = note else { return "No Voice Note is active." }
        if !discardPending {
            resumeAfterKeep = rec == .recording && !holding
            if rec == .recording { holding = false; closeLane() }
            discardPending = true
            changed()
        }
        return "Discard Voice Note \(n.title)? Say confirm discard voice note or keep voice note."
    }

    private func confirmDiscard() -> String {
        guard active, discardPending else { return "No Voice Note discard is awaiting confirmation." }
        purgeOpen()
        return "Discarded Voice Note and purged its local audio and transcript."
    }

    private func keep() -> String {
        guard let n = note, discardPending else { return "No Voice Note discard is awaiting confirmation." }
        discardPending = false
        if resumeAfterKeep {
            resumeAfterKeep = false
            openLane()
            changed()
            return "Kept and resumed Voice Note: \(n.title)."
        }
        changed()
        return "Kept Voice Note: \(n.title)."
    }

    /// The exit prompt's Discard: the prompt was the question.
    private func discardNow() {
        guard active else { return }
        purgeOpen()
    }

    private func purgeOpen() {
        guard let i = openIndex else { return }
        stored.voice.remove(at: i)
        clearOpen()
        persist()
        changed()
    }

    private func clearOpen() {
        audioCheck.noteEnded()
        openIndex = nil
        rec = .paused; holding = false; holdInput = nil; laneClosedAt = nil
        inputs = []; growing = nil; partial = ""; discardPending = false; resumeAfterKeep = false
        finalizing = false; laneOpenedAt = nil; recordedBefore = 0
        closeCueViews()
        cues?.end()
    }

    /// Save: let the last words land, then keep the transcript.
    private func stop(completion: @escaping (String) -> Void) {
        guard note != nil else { completion("No Voice Note is active."); return }
        guard !finalizing else { completion("Saving quicknote…"); return }
        let wasListening = rec == .recording || holding || laneClosedAt.map { Date().timeIntervalSince($0) < Self.grace } == true
        holding = false
        if rec == .recording { closeLane() }
        finalizing = true
        changed()
        // Wait out the grace so the last sentence lands; words dictation has
        // not finalised by then are kept as heard, not dropped.
        DispatchQueue.main.asyncAfter(deadline: .now() + (wasListening ? Self.grace + 0.3 : 0)) { [weak self] in
            guard let self, let i = self.openIndex else { return }
            let late = self.partial.trimmingCharacters(in: .whitespacesAndNewlines)
            if !late.isEmpty {
                Log.add("quicknotes: kept \(late.count) chars not finalised at save")
                self.heard(late, audioEnd: Date())
            }
            self.settleHold()
            let title = self.stored.voice[i].title
            let sections = self.stored.voice[i].paragraphs.filter { !$0.isEmpty }
            if sections.isEmpty {
                self.stored.voice.remove(at: i)
                self.clearOpen()
                self.persist()
                self.changed()
                completion("Nothing was recorded, so the note was discarded.")
                return
            }
            self.stored.voice[i].paragraphs = sections
            if let given = self.cues?.given, !given.isEmpty {
                self.stored.voice[i].cues = given.map { "\($0.label): \($0.title) — \($0.body)" }
            }
            self.stored.voice[i].state = "saved"
            self.stored.voice[i].updated = Date()
            self.clearOpen()
            self.persist()
            self.changed()
            self.publishPanel?()
            let savedId = self.stored.voice[i].id
            completion("Saved Voice Note: \(title). \(sections.count) transcript section\(sections.count == 1 ? "" : "s").")
            // note_insights.schedule_summarize: quietly, after every save.
            if self.insightsAvailable {
                self.summarize(savedId) { receipt in Log.add("auto-summary: \(receipt)") }
                // The reminders said in the note, offered without asking
                // (the wearer's choice, 2026-10-30): one tap adds them.
                self.offerReminders(savedId)
            }
        }
    }

    // MARK: - Pages (capabilities.py, QuickNotes half)

    /// cues: the AI cues & prep list; reader: one cue, or the prep notes;
    /// prepPick: the prep note for a new note, or Skip & start.
    private enum Page { case list, live, finish, discard, cues, reader, prepPick }
    private enum Row { case control(String, String), voice(VoiceNote), text(TextNote), cue(Int), prep }

    static let pageSize = 15
    /// The newest rows of the open note sent to the glasses, which page it
    /// (a swipe reads back through it).
    static let liveRows = 400
    static let liveTitle = "QUICKNOTES · NEW NOTE"
    static let finishControls = [("Save", "stop"), ("Discard", "discard_now"), ("Cancel", "back_to_live")]
    static let discardControls = [("Discard", "confirm_discard"), ("Cancel", "keep")]

    private var page: Page?
    /// The next note starts with the prep note in use (false: Skip & start).
    private var notePrepChoice = true
    private var finishShown = false
    private var rows: [Row] = []
    private var listRows: [Row] = []
    private var listIndex = 0
    private var readerOpen = false
    /// What a tap does while a reader is open (note_reader.on_tap).
    private var readerTap: (() -> Void)?
    /// Notes being summarized right now; a request meanwhile reruns once after.
    private var summaryRuns = NotesInsights.RunQueue()
    /// Why the last summary of a note did not arrive (shown on the page).
    private(set) var insightProblems: [String: String] = [:]
    /// The reminders review page's answer, and its 90 s clock.
    private var reviewDecide: ((Bool?) -> Void)?
    /// Reminders found after a save while QuickNotes was not on the glasses.
    private var pendingOffer: ReminderOffer?
    private var reviewTimer: DispatchWorkItem?
    private var livePainted: [String]?
    private var liveKey = ""
    private var liveClock = -1
    private var liveTicker: DispatchSourceTimer?
    private var repaintPending = false

    private var current: Bool { menu?.activeCapability == "QUICKNOTES" }

    /// Leaving the surface (Home, another page): the recording keeps going.
    func departed() {
        leaveReview()
        page = nil
        finishShown = false
        readerOpen = false
        rows = []
        stopTicker()
    }

    #if DEBUG
    /// DebugSeed: words as if they had been heard.
    func debugAppend(_ paragraphs: [String]) {
        guard let i = openIndex else { return }
        stored.voice[i].paragraphs += paragraphs
        changed()
    }
    #endif

    /// capabilities._show_notes: the open note's page, else the list.
    func show() {
        readerOpen = false
        if active { showLive(); return }
        if let offer = pendingOffer {
            pendingOffer = nil
            presentOffer(offer) { r in Log.add("reminders offer: \(r)") }
            return
        }
        finishShown = false
        var all: [(Date, Int, String, Row)] = []
        for n in retained { all.append((n.updated, 1, n.id, .voice(n))) }
        for t in activeText { all.append((t.updated, 0, t.id, .text(t))) }
        all.sort { a, b in a.0 != b.0 ? a.0 > b.0 : (a.1 != b.1 ? a.1 < b.1 : a.2 < b.2) }
        listRows = all.map(\.3)
        listIndex = 0
        showListPage()
    }

    private func label(_ row: Row) -> String {
        switch row {
        case .control(let l, _): return l
        case .voice(let n): return "[\(Cards.age(n.updated))] ● \(n.title.isEmpty ? "Untitled" : n.title)"
        case .text(let t): return "[\(Cards.age(t.updated))] \(t.text)"
        case .cue(let i): return cues.flatMap { $0.given.indices.contains(i) ? $0.given[i] : nil }.map { "\($0.label) · \($0.title)" } ?? ""
        case .prep: return "Prep notes · \(notePrep?.title ?? "")"
        }
    }

    private func showListPage() {
        let count = max(1, (listRows.count + Self.pageSize - 1) / Self.pageSize)
        listIndex = min(listIndex, count - 1)
        let start = listIndex * Self.pageSize
        let notes = Array(listRows[min(start, listRows.count)..<min(start + Self.pageSize, listRows.count)])
        rows = [.control("+ Create new note", "start_hold")] + notes
        page = .list
        stopTicker()
        var hints = ["tap: open · " + CapabilityMenu.backHint]
        if listIndex > 0 { hints.insert("up newer", at: 0) }
        if listIndex + 1 < count { hints.insert("down older", at: 0) }
        var labels = rows.map(label)
        var controls: [String: Int] = [:]
        if listIndex > 0 { controls["newerIndex"] = labels.count; labels.append("▲ Newer notes") }
        if listIndex + 1 < count { controls["olderIndex"] = labels.count; labels.append("▼ Older notes") }
        menu?.sendCurrent([
            "title": notes.isEmpty ? "QUICKNOTES · EMPTY" : "QUICKNOTES",
            "items": labels,
            "hint": hints.joined(separator: " · "),
            "selectable": true,
            "quickNotesPage": ["index": listIndex, "count": count],
            "pageControls": controls,
        ])
    }

    /// cappage on "QUICKNOTES · LIST": a swipe past the edge or a paging row.
    func pageList(eventType: Int?, selectedIndex: Int?) {
        guard page == .list, !readerOpen, selectedIndex == listIndex else { return }
        let count = max(1, (listRows.count + Self.pageSize - 1) / Self.pageSize)
        let delta = eventType == 1 ? -1 : (eventType == 2 ? 1 : 0)
        let target = listIndex + delta
        guard delta != 0, (0..<count).contains(target) else { return }
        listIndex = target
        showListPage()
    }

    private func showLive() {
        if discardPending { showControls("DISCARD?", Self.discardControls, page: .discard); return }
        if finalizing { hud.receipt("Saving quicknote…"); rows = []; page = nil; return }
        if finishShown { showControls("FINISH", Self.finishControls, page: .finish); return }
        paintLive(force: true)
        ensureTicker()
    }

    private func showControls(_ name: String, _ controls: [(String, String)], page p: Page) {
        rows = controls.map { .control($0.0, $0.1) }
        page = p
        livePainted = nil
        stopTicker()
        let title = note?.title ?? "Untitled"
        menu?.sendCurrent([
            "title": "QUICKNOTES · \(name)",
            "items": controls.map(\.0),
            "hint": "\(String(title.prefix(24))) · tap: choose · left x2: cancel",
            "selectable": true,
        ])
    }

    // MARK: - AI cues and prep (Even's Conversate)

    /// A new cue pops up over the page as a notification card that closes
    /// by itself after 5 s (the plugin's NOTIFICATION_CARD_MS; Even: cues
    /// "appear automatically when relevant, then fade away"). A tap on the
    /// card opens that cue. Card ids are 32 hex, the plugin's card rule.
    private var cueCards: [String: Int] = [:]
    /// Home keeps a card's text as its own notification until it is retired:
    /// a cue is retired just after its card closes.
    static let cueCardSeconds: TimeInterval = 6
    /// Where 2x tap on a reader goes: the list it was opened from, or the note.
    private var readerFrom: Page = .live

    private func cueArrived(_ cue: Cues.Cue) {
        guard let index = cues?.given.lastIndex(of: cue) else { return }
        guard active, current, page == .live || page == .cues || page == .reader else { return }
        let id = ConfirmGate.hex(16)
        cueCards[id] = index
        let title = cue.title.replacingOccurrences(of: " · ", with: " - ")
        // cardFromRenderedText: "<sender> · <app>" then the message.
        link.send(["kind": "hud.text", "containerId": 2, "text": "\(title) · AI cue: \(cue.label)\n\(cue.body)",
                   "textColor": 3, "contentKind": "notification", "traceId": id])
        Log.add("quicknotes: cue card \(cue.kind) “\(cue.title)”")
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.cueCardSeconds) { [weak self] in
            self?.link.send(["kind": "hud.text", "containerId": 2, "text": "", "contentKind": "notification", "traceId": id])
        }
    }

    /// A tap on a cue's card (notification-dismiss): read that cue.
    func cueTapped(_ id: String) {
        guard let index = cueCards[id], active, current, page == .live || page == .cues || page == .reader else { return }
        openCue(index, from: page == .cues ? .cues : .live)
    }

    private func closeCueViews() {
        cueCards = [:]
        readerFrom = .live
    }

    /// "AI cues" in the open note's menu: the prep notes, then
    /// the cues, newest first. Tap one to read it; 2x tap goes back to the note.
    private func showCues() {
        guard active else { return }
        var list: [Row] = []
        if notePrep != nil { list.append(.prep) }
        let given = cues?.given ?? []
        list += given.indices.reversed().map { .cue($0) }
        var labels = list.map(label)
        if given.isEmpty {
            if let cues, !cues.available { labels.append("AI cues need Gemini or Claude on this phone.") }
            else if let cues, !cues.enabled { labels.append("AI cues are off. Turn them on in JOAT Brain.") }
            else { labels.append("No AI cues yet. They pop up as you talk.") }
            list.append(.control(labels.last!, "cues"))
        }
        rows = list
        page = .cues
        livePainted = nil
        stopTicker()
        menu?.sendCurrent(["title": "QUICKNOTES · AI CUES", "items": labels,
                           "hint": "tap: open · " + CapabilityMenu.backHint, "selectable": true])
    }

    private func openCue(_ index: Int, from: Page) {
        guard let given = cues?.given, given.indices.contains(index) else { showCues(); return }
        let cue = given[index]
        showReader("QUICKNOTES · CUE", "\(cue.label.uppercased()) · \(Cards.age(cue.at))\n\(cue.title)\n\n\(cue.body)", from: from)
    }

    /// The prep note this note uses: none when it started with Skip & start.
    private var notePrep: Prep.Note? { cues.map { $0.prepNote } ?? prep?.active }

    /// The prep note's talking points to read during the note (Even's
    /// expanded Prep Note: numbered points, scrolled by swipe), and what's in it.
    private func openPrep(from: Page) {
        var lines: [String]
        if let note = notePrep {
            lines = ["PREP NOTES", note.title, ""]
            if note.points.isEmpty {
                lines.append("Talking points aren't ready yet.")
                if let first = note.items.first { lines += ["", String(first.text.prefix(3000))] }
            } else {
                lines += note.points.enumerated().map { "\($0.offset + 1). \($0.element)" }
            }
            if note.items.count > 1 {
                lines += ["", "From: " + note.items.map(\.title).joined(separator: " · ")]
            }
        } else if cues?.usesPrep == false {
            lines = ["PREP NOTES", "This note started without a prep note (Skip & start)."]
        } else {
            lines = ["PREP NOTES", "No prep notes yet.", "",
                     "On your phone, share a file to JOAT Brain, or paste a link or notes in its Prep tab."]
        }
        showReader("QUICKNOTES · PREP", lines.joined(separator: "\n"), from: from)
    }

    private func showReader(_ title: String, _ body: String, from: Page) {
        rows = []
        page = .reader
        readerFrom = from
        livePainted = nil
        stopTicker()
        menu?.sendCurrent(["title": title, "items": [String](), "body": body,
                           "hint": CapabilityMenu.backHint, "selectable": false])
    }

    /// capabilities._live_view + _paint_live: the whole note, newest words
    /// last; the glasses open it on its last page and a swipe reads back.
    private func paintLive(force: Bool) {
        guard let n = note, current, !finishShown, !discardPending, !finalizing else { return }
        var lines = n.paragraphs.filter { !$0.isEmpty }
        let capturing = rec == .recording
        var notice: String?
        if capturing, silentFor >= 3 { notice = "No audio from the mic — check the glasses." }
        else if !partial.isEmpty { lines.append(partial) }
        if lines.isEmpty, notice == nil { notice = capturing ? "Listening…" : "Tap to record, or hold to talk." }
        let body = (lines + (notice.map { [$0] } ?? [])).joined(separator: "\n")
        let rowsOut = Array(ReaderText.rows(body).suffix(Self.liveRows))
        let s = recordedSeconds
        let clock = String(format: "%02d:%02d", s / 60, s % 60)
        let hint = holding ? "● Listening \(clock) · let go: done"
            : capturing ? "● REC \(clock) · tap: pause · left x2: finish"
            : "\(clock) · tap: record · hold: talk"
        let key = "\(rowsOut)|\(capturing)|\(holding)"
        if !force, key == liveKey, page == .live { return }
        rows = [.control("live", "toggle")]
        page = .live
        liveKey = key
        liveClock = s
        menu?.sendCurrent(["title": Self.liveTitle, "items": rowsOut, "hint": hint, "selectable": true])
    }

    private func ensureTicker() {
        guard liveTicker == nil else { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        liveTicker = t
    }

    private func stopTicker() {
        liveTicker?.cancel()
        liveTicker = nil
    }

    /// capabilities._live_check on the 1 s beat.
    private func tick() {
        guard active, page == .live, current else { stopTicker(); return }
        if rec == .recording, silentFor >= 1.5 {
            if !micReopened {
                // A muted mic makes a silent recording: ask the plugin to open it.
                micReopened = true
                link.send(["kind": "audio.control", "open": true])
                Log.add("quicknotes: no audio for 1.5 s, asked the plugin to reopen the mic")
            }
        }
        paintLive(force: recordedSeconds != liveClock)
    }

    /// The recorder changed: repaint within 0.25 s, coalesced.
    private func changed() {
        guard !repaintPending else { return }
        repaintPending = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self else { return }
            self.repaintPending = false
            guard self.current else { return }
            if !self.active, let shown = self.page, [Page.live, .finish, .discard, .cues, .reader].contains(shown) {
                self.show()   // saved or discarded by voice: back to the list
            } else if self.page == .live {
                self.paintLive(force: false)
            }
        }
    }

    // MARK: - Taps and gestures

    /// capdata on QUICKNOTES.
    func select(_ index: Int, traceId: String?, pageIndex: Int?) {
        // A tap inside an open reader is the reader's own action (add the
        // note's reminders, go on to the confirmation), never a list row.
        if readerOpen {
            readerTap?()
            return
        }
        // 2x tap on the prep note page: the plugin went back to the list by
        // itself, so this tap (it carries the list's page) is on the list.
        if page == .prepPick, pageIndex != nil { show() }
        if page == .list, (pageIndex ?? 0) != listIndex { return }
        guard rows.indices.contains(index) else { return }
        switch rows[index] {
        case .control(_, let action): apply(action)
        case .cue(let i): openCue(i, from: .cues)
        case .prep: openPrep(from: .cues)
        case .voice(let n): openVoiceReader(n, traceId: traceId ?? "hud")
        case .text(let t):
            readerOpen = true
            readerTap = nil
            menu?.sendCurrent(["title": "QUICKNOTES · DETAIL", "items": [String](), "body": "Exact text\n\n\(t.text)",
                               "hint": CapabilityMenu.backHint, "selectable": false,
                               "readerPage": ["id": traceId ?? "hud", "index": 0, "count": 1]])
        }
    }

    /// capabilities._apply_recording_control
    private func apply(_ action: String) {
        var receipt: String?
        switch action {
        case "start_hold":
            // Even's Conversate: choose a Prep Note, or Skip & Start.
            if let prep, !prep.notes.isEmpty {
                showPrepPicker()
                return
            }
            receipt = startHeld(usePrep: true)
        case "start_skip":
            receipt = startHeld(usePrep: false)
        case let pick where pick.hasPrefix("start_prep:"):
            prep?.select(String(pick.dropFirst("start_prep:".count)))
            receipt = startHeld(usePrep: true)
        case "toggle":
            // A tap on the open note records or pauses; a long press talks one
            // paragraph; AI cues and Prep notes are in the note's menu (the
            // wearer, 2026-10-29 and again 2026-11-12: tap had been changed
            // to open the AI cues, which left hold-to-talk the only way to
            // record from the glasses).
            if active, !holding, !discardPending {
                lastTap = (Date(), rec == .recording)
                let receipt = rec == .recording ? pause() : resume()
                Log.add("quicknotes: tap: \(receipt)")
            }
            showLive()
            return
        case "cues":
            // The cues page's status row.
            showCues()
            return
        case "back_to_live":
            finishShown = false
        case "stop":
            finishShown = false
            hud.receipt("Saving quicknote…")
            stop { [weak self] result in
                guard let self else { return }
                if result.hasPrefix("Saved Voice Note") {
                    // A save shows itself: the list with the note on top.
                    self.link.send(["kind": "assistant.overlay", "operation": "settle", "dismissAfterMs": 6_000])
                } else {
                    self.hud.receipt(result)
                }
                if self.current { self.show() }
            }
            return
        case "discard_now":
            finishShown = false
            discardNow()
        case "confirm_discard":
            receipt = confirmDiscard()
        case "keep":
            receipt = keep()
        default:
            break
        }
        if let receipt { hud.receipt(receipt) }
        show()
    }

    /// + Create new note, with the prep note picked (or none).
    private func startHeld(usePrep: Bool) -> String? {
        finishShown = false
        micReopened = false
        notePrepChoice = usePrep
        let started = start(title: Self.clockTitle(), hold: true)
        return active ? nil : started
    }

    /// The prep note for the new note: the one in use first, then the
    /// others, newest first, then Skip & start. 2x tap: back to the list.
    private func showPrepPicker() {
        guard let prep else { return }
        let inUse = prep.active?.id
        let ordered = prep.notes.filter { $0.id == inUse } + prep.notes.filter { $0.id != inUse }
        rows = ordered.map { .control($0.id == inUse ? "● \($0.title)" : $0.title, "start_prep:\($0.id)") }
            + [.control("Skip & start", "start_skip")]
        page = .prepPick
        stopTicker()
        // Not "QUICKNOTES · PREP": the plugin draws that title as a text page.
        menu?.sendCurrent(["title": "QUICKNOTES · PREP NOTE", "items": rows.map(label),
                           "hint": "tap: start with it · " + CapabilityMenu.backHint, "selectable": true])
    }

    static func clockTitle() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "'Note' HH:mm"
        return f.string(from: Date())
    }

    /// capability-lifecycle quicknotes-* from the live / FINISH / DISCARD? pages.
    func gesture(_ name: String) {
        switch name {
        case "quicknotes-finish":
            if active, page == .live { finishShown = true; showLive() }
        case "quicknotes-back":
            if page == .prepPick, !active { show(); return }
            guard active else { return }
            if discardPending { apply("keep") } else if finishShown { apply("back_to_live") }
            else if page == .reader, readerFrom == .cues { showCues() }
            else if page == .reader || page == .cues { showLive() }
        case "quicknotes-cues":
            // "AI cues" in the open note's menu.
            if active, page == .live { showCues() }
        case "quicknotes-prep":
            // "Prep notes" in the open note's menu.
            if active, page == .live { openPrep(from: .live) }
        case "quicknotes-hold-start":
            if page == .live, holdStart() { paintLive(force: true) }
        case "quicknotes-hold-end":
            // A release always ends a hold, even if the page moved on.
            if holdEnd(), page == .live { paintLive(force: true) }
        case "quicknotes-hold-cancel":
            // The glasses' menu opened (tap, then long press): nothing of the
            // press is kept, and the tap before it changes nothing either.
            let dropped = holdCancel()
            let undone = undoMenuTap()
            if dropped || undone, page == .live { paintLive(force: true) }
        case "quicknotes-undo":
            guard page == .live else { return }
            let r = undo()
            if r != "Removed the last paragraph." { hud.receipt(r) }
            paintLive(force: true)
        case "quicknotes-list":
            leaveReview()
            readerOpen = false
            readerTap = nil
        case "quicknotes-home":
            leaveReview()
            readerOpen = false
            readerTap = nil
            rows = []
            page = nil
        default:
            break
        }
    }

    /// capabilities._show_voice_note_detail: the reader template, sent whole.
    private func openVoiceReader(_ n: VoiceNote, traceId: String) {
        let sections = n.paragraphs.filter { !$0.isEmpty }
        var lines = [n.title.isEmpty ? "Untitled" : n.title,
                     "Recording · \(sections.count) transcript section\(sections.count == 1 ? "" : "s")", ""]
        let latest = retained.first?.id == n.id
        if let summary = n.summary {
            lines += ["OVERVIEW", summary]
            if !n.keyPoints.isEmpty { lines += ["", "KEY POINTS"] + n.keyPoints.map { "– \($0)" } }
            if !n.todos.isEmpty { lines += ["", "NEXT STEPS"] + n.todos.map { "□ \($0)" } }
        } else {
            lines.append("Summary is not ready yet.")
            if latest, insightsAvailable { lines.append("Say: summarize the last note.") }
        }
        if latest, insightsAvailable { lines += ["", "Say: ask the last note …"] }
        if let c = n.cues, !c.isEmpty { lines += ["", "AI CUES"] + c }
        lines += ["", "TRANSCRIPT"] + sections
        if sections.isEmpty { lines.append("No transcript is available; retained audio may need recovery.") }
        readerOpen = true
        let canAdd = insightsAvailable && reminders != nil && !sections.isEmpty
        let id = n.id
        readerTap = canAdd ? { [weak self] in
            guard let self else { return }
            self.hud.receipt("Finding your reminders…")
            self.createReminders(id) { receipt in self.hud.receipt(receipt); self.say?(receipt) }
        } : nil
        menu?.sendCurrent(["title": "QUICKNOTES · NOTE", "items": [String](), "body": lines.joined(separator: "\n"),
                           "hint": (canAdd ? "tap: reminders · " : "") + CapabilityMenu.backHint, "selectable": canAdd,
                           "readerPage": ["id": traceId, "index": 0, "count": 1]])
    }

    // MARK: - Rail tab (quicknotes_panel.py)

    let tab = "quicknotes"
    let statusRows = 0
    private var shownPanel: [String]?

    private var panelRows: [String] {
        var rows: [(Date, String)] = retained.map { ($0.updated, "[\(Cards.age($0.updated))] \($0.title.isEmpty ? "Untitled" : $0.title)") }
        rows += activeText.map { ($0.updated, "[\(Cards.age($0.updated))] \($0.text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " "))") }
        return rows.sorted { $0.0 > $1.0 }.prefix(4).map { String($0.1.prefix(120)) }
    }

    func snapshot() -> PanelSnapshot {
        shownPanel = panelRows
        return PanelSnapshot(rows: shownPanel ?? [], empty: "No notes yet. Tap to create one.")
    }
    func refresh(completion: @escaping () -> Void) { completion() }
    func open(index: Int, completion: @escaping (Bool) -> Void) { completion(false) }

    private func pushPanelIfChanged() {
        if panelRows != shownPanel { publishPanel?() }
    }

    // MARK: - Voice (voice_note_capture.py, note_commands.py)

    static let nouns = ["voice note", "quick note", "quicknote", "recording", "note"]
    static func phrases(_ templates: [String]) -> Set<String> {
        var out = Set<String>()
        for t in templates { for n in nouns { for a in ["", "the ", "this "] { out.insert(t.replacingOccurrences(of: "{noun}", with: a + n)) } } }
        return out
    }
    static let startRe = try! NSRegularExpression(
        pattern: #"^(?:start|begin)(?: recording)? (?:a )?(?:voice note|quick ?note)(?: (?:called|titled|about))?(?: (?<title>.+))?$"#,
        options: [.caseInsensitive])
    static let stopSet = phrases(["stop {noun}", "save {noun}", "finish {noun}"]).union(["save and finish"])
    static let pauseSet = phrases(["pause {noun}"])
    static let resumeSet = phrases(["resume {noun}", "continue {noun}"])
    static let discardSet = phrases(["discard {noun}", "cancel {noun}"])
    static let confirmSet = phrases(["confirm discard {noun}"]).union(["confirm discard"])
    static let keepSet = phrases(["keep {noun}", "cancel discard {noun}"])

    private var lastDeletedTextId: String?

    func handle(_ request: String) -> CommandReply? {
        let normalized = request.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: ".,!?")).trimmingCharacters(in: .whitespaces)
        let lowered = normalized.lowercased()
        if let g = Grammar.match(Self.startRe, normalized, groups: ["title"]) {
            return reflect(start(title: g["title"], hold: false))
        }
        if Self.pauseSet.contains(lowered) || (active && lowered == "pause") { return reflect(pause()) }
        if Self.resumeSet.contains(lowered) || (active && (lowered == "resume" || lowered == "continue")) { return reflect(resume()) }
        if Self.discardSet.contains(lowered) { return reflect(requestDiscard()) }
        if Self.confirmSet.contains(lowered) { return reflect(confirmDiscard()) }
        if Self.keepSet.contains(lowered) { return reflect(keep()) }
        if Self.stopSet.contains(lowered) {
            stop { [weak self] result in
                guard let self else { return }
                self.menu?.openByVoice("QuickNotes")
                self.hud.receipt(result)
                self.say?(result)
            }
            return CommandReply(text: "", activated: true)
        }
        // Insight commands (note_insights.py), ahead of the "note …" prefix.
        if Grammar.match(Self.summarizeRe, normalized) != nil {
            summarizeLast { [weak self] r in self?.hud.receipt(r); self?.say?(r) }
            return CommandReply(text: "", activated: false)
        }
        if let g = Grammar.match(Self.askRe, normalized, groups: ["question"]), let q = g["question"] {
            askLast(q) { [weak self] r in self?.hud.receipt(r); self?.say?(r) }
            return CommandReply(text: "", activated: false)
        }
        if Grammar.match(Self.remindersRe, normalized) != nil {
            guard let latest = retained.first else {
                return CommandReply(text: "There is no retained note to create reminders from.", activated: false)
            }
            hud.receipt("Finding your reminders…")
            createReminders(latest.id) { [weak self] r in self?.hud.receipt(r); self?.say?(r) }
            return CommandReply(text: "", activated: false)
        }
        // Text notes (note_commands.py): "remember for me that …", "take a note …", "note …", "find notes …".
        let stripped = request.trimmingCharacters(in: .whitespacesAndNewlines)
        let isRelationshipFact = stripped.range(
            of: #"\bis\s+my\s+(?:wife|husband|mom|mother|dad|father|brother|sister|boss|partner|boyfriend|girlfriend|friend)\b"#,
            options: [.regularExpression, .caseInsensitive]
        ) != nil
        let notePrefixes = [
            "remember for me that",
            "remember for me",
            "remember that",
            "take a note that",
            "take a note",
            "note to self that",
            "note to self",
            "note that",
            "jot down that",
            "jot down",
            "note",
            "find notes",
        ]
        for prefix in notePrefixes {
            if prefix.hasPrefix("remember") && isRelationshipFact { continue }
            guard let body = Self.after(prefix, in: stripped) else { continue }
            if body.isEmpty { break }
            return reflect(prefix == "find notes" ? search(body) : saveText(body))
        }
        let command = lowered
        if command == "take a note" || command == "note" { return reflect("Say the text after “take a note”.") }
        if command == "find notes" { return reflect("Say what to search for after “find notes”.") }
        if command == "undo last note" { return reflect(deleteLatestText()) }
        if command == "restore last note" { return reflect(restoreText()) }
        return nil
    }

    /// Speak through the phone (set by the Brain).
    var say: ((String) -> Void)?

    /// commands.py: every note command repaints QuickNotes, then the receipt.
    private func reflect(_ text: String) -> CommandReply {
        menu?.openByVoice("QuickNotes")
        return CommandReply(text: text, activated: true)
    }

    /// note_commands._after_prefix: the prefix, then a space or a mark.
    static func after(_ prefix: String, in text: String) -> String? {
        guard text.lowercased().hasPrefix(prefix) else { return nil }
        let rest = text.dropFirst(prefix.count)
        guard let first = rest.first else { return "" }
        guard first == " " || ".,:;!?-—".contains(first) else { return nil }
        return rest.drop(while: { $0 == " " || ".,:;!?-—".contains($0) }).trimmingCharacters(in: .whitespaces)
    }

    private func saveText(_ text: String) -> String {
        let now = Date()
        stored.text.append(TextNote(id: UUID().uuidString.lowercased(), text: text, created: now, updated: now))
        persist()
        publishPanel?()
        let preview = text.count <= 72 ? text : String(text.prefix(69)) + "…"
        return "Saved QuickNote: \(preview)"
    }

    // MARK: - Assistant Tool Belt Hooks

    @MainActor
    func saveTextFromTool(_ rawText: String) -> PhoneToolOutcome {
        let trimmed = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !rawText.contains("\u{0}") else {
            return PhoneToolOutcome("error: text must be non-empty UTF-8 text without null characters.", claimsLens: false)
        }
        let receipt = saveText(trimmed)
        menu?.openByVoice("QuickNotes")
        hud.receipt(receipt)
        return PhoneToolOutcome(receipt, claimsLens: true, spokenReceipt: receipt)
    }

    @MainActor
    func controlVoiceNoteFromTool(
        action rawAction: String,
        title rawTitle: String?,
        dismissConversation: @MainActor () -> Void
    ) async -> PhoneToolOutcome {
        let action = rawAction.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch action {
        case "start":
            dismissConversation()
            let cleanTitle = rawTitle?.trimmingCharacters(in: .whitespacesAndNewlines)
            let resolvedTitle = (cleanTitle?.isEmpty == false) ? cleanTitle! : Self.clockTitle()
            let receipt = start(title: resolvedTitle, hold: false)
            menu?.openByVoice("QuickNotes")
            hud.receipt(receipt)
            return PhoneToolOutcome(receipt, claimsLens: true, spokenReceipt: receipt)

        case "pause":
            let receipt = pause()
            return PhoneToolOutcome(receipt, claimsLens: false)

        case "resume":
            let receipt = resume()
            return PhoneToolOutcome(receipt, claimsLens: false)

        case "stop":
            dismissConversation()
            let receipt: String = await withCheckedContinuation { cont in
                stop { result in
                    cont.resume(returning: result)
                }
            }
            menu?.openByVoice("QuickNotes")
            hud.receipt(receipt)
            return PhoneToolOutcome(receipt, claimsLens: true, spokenReceipt: receipt)

        case "summarize_last":
            let receipt: String = await withCheckedContinuation { cont in
                summarizeLast { result in
                    cont.resume(returning: result)
                }
            }
            return PhoneToolOutcome(receipt, claimsLens: false)

        default:
            return PhoneToolOutcome(
                "error: action must be one of start, pause, resume, stop, or summarize_last.",
                claimsLens: false
            )
        }
    }

    private func search(_ query: String) -> String {
        let q = query.lowercased()
        let matches = activeText.filter { $0.text.lowercased().contains(q) }.prefix(5)
        guard !matches.isEmpty else { return "No matching QuickNotes." }
        let snippets = matches.map { String($0.text.replacingOccurrences(of: "\n", with: " ").prefix(72)) }
        return "\(snippets.count) \(snippets.count == 1 ? "note" : "notes"): " + snippets.joined(separator: " · ")
    }

    private func deleteLatestText() -> String {
        guard let latest = activeText.first, let i = stored.text.firstIndex(where: { $0.id == latest.id }) else {
            return "There is no active QuickNote to remove."
        }
        stored.text[i].deleted = true
        stored.text[i].updated = Date()
        lastDeletedTextId = latest.id
        persist()
        publishPanel?()
        return "Removed the last QuickNote. Say restore last note to undo."
    }

    private func restoreText() -> String {
        guard let id = lastDeletedTextId else { return "There is no QuickNote deletion to restore in this session." }
        guard let i = stored.text.firstIndex(where: { $0.id == id }) else {
            return "The deleted QuickNote is no longer available to restore."
        }
        stored.text[i].deleted = false
        stored.text[i].updated = Date()
        lastDeletedTextId = nil
        persist()
        publishPanel?()
        return "Restored the last QuickNote."
    }
}

// MARK: - Reading older saved notes

// Synthesized Decodable ignores default values: a note saved before
// keyPoints, todos or deleted existed would fail the whole file. Fields that
// came later are read as optional here.
extension QuickNotes.VoiceNote {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "saved"
        created = try c.decode(Date.self, forKey: .created)
        updated = try c.decodeIfPresent(Date.self, forKey: .updated) ?? created
        paragraphs = try c.decodeIfPresent([String].self, forKey: .paragraphs) ?? []
        summary = try c.decodeIfPresent(String.self, forKey: .summary)
        keyPoints = try c.decodeIfPresent([String].self, forKey: .keyPoints) ?? []
        todos = try c.decodeIfPresent([String].self, forKey: .todos) ?? []
        cues = try c.decodeIfPresent([String].self, forKey: .cues)
        page = try c.decodeIfPresent(Bool.self, forKey: .page)
        typed = try c.decodeIfPresent(String.self, forKey: .typed)
        handwriting = try c.decodeIfPresent(String.self, forKey: .handwriting)
        paperTemplate = try c.decodeIfPresent(String.self, forKey: .paperTemplate)
        summaryStale = try c.decodeIfPresent(Bool.self, forKey: .summaryStale)
    }
}

extension QuickNotes.TextNote {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        created = try c.decode(Date.self, forKey: .created)
        updated = try c.decodeIfPresent(Date.self, forKey: .updated) ?? created
        deleted = try c.decodeIfPresent(Bool.self, forKey: .deleted) ?? false
    }
}

// MARK: - Insights on the Mac (note_insights.py, commitments.py)

extension QuickNotes {
    static let noteNoun = #"(?:voice note|quick ?note|note)"#
    static let summarizeRe = try! NSRegularExpression(pattern: #"^(?:re)?summari[sz]e the last "# + noteNoun + #"(?:,? with claude)?$|^resummari[sz]e with claude$"#, options: [.caseInsensitive])
    static let askRe = try! NSRegularExpression(pattern: #"^ask the (?:last )?"# + noteNoun + #"[,:]? (?<question>.+)$"#, options: [.caseInsensitive])
    static let remindersRe = try! NSRegularExpression(pattern: #"^create reminders from the last "# + noteNoun + #"$"#, options: [.caseInsensitive])
    static let clockTitleRe = try! NSRegularExpression(pattern: #"^(?:Note|Page) \d{2}:\d{2}$"#)

    static let insightsSystem = """
        You distill the transcript of a recorded conversation, meeting, speech or dictated note, or a page from the person's own notebook. Reply in EXACTLY this plain-text shape, with these four headers:
        TITLE:
        2 to 6 words naming what it is about
        OVERVIEW:
        2 to 5 sentences capturing what was said, discussed and decided.
        KEY POINTS:
        - one point per line, 1 to 8 lines
        NEXT STEPS:
        - one actionable task per line, at most 8 lines; write '- none' if the transcript contains no tasks
        No markdown emphasis, no extra sections, no commentary, no preamble.
        """
    static let askSystem = "You answer one question about a recorded note. Use ONLY the note content provided. If the answer is not in it, say the note does not cover that. Answer in under 60 words of plain text."
    static let commitmentSystem = """
        You read the transcript of a recorded note and list the tasks the speaker said THEY will do, need to do, must do, or asked to be reminded of.
        Reply with one block per task, in exactly this shape and nothing else:
        TASK: the task as a short instruction of 3 to 12 words
        QUOTE: the speaker's own words, copied exactly from the transcript
        WHEN: the day and time the speaker said for this task, as YYYY-MM-DD HH:MM (24-hour clock); YYYY-MM-DD when they said a day but no time; NONE when they said neither
        Never invent a task. Do not list opinions, facts, ideas, or things other people will do. One task for each sentence. If the speaker named no task of their own, reply with the single word NONE.
        Work WHEN out from the speaker's own words only, counting from the moment the note was recorded (given below): "Wednesday next week at 5 o'clock" said on a Saturday is the Wednesday four days later at 17:00; "tomorrow" is the day after the recording; a bare hour from 1 to 6 is in the afternoon.
        """

    /// A notebook page is the person's own writing: its to-dos are theirs
    /// (2026-11-10: "Call Mike about the tag map on Friday at 3 pm" on a page
    /// was not offered, as it says no "I will").
    static let pageCommitmentNote = """

        This is a page the person wrote in their own notebook, not a recording. A task written as a to-do or as an \
        instruction to themselves ("Call Mike about the tag map", "Order two spare relays") is a task they will do. \
        QUOTE is copied exactly from the page.
        """

    /// The commitment instructions with the note's recording moment, so a
    /// day said in the note counts from then, not from today.
    static func commitmentInstructions(recorded: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "EEEE, yyyy-MM-dd HH:mm"
        return commitmentSystem + "\nThe note was recorded on \(f.string(from: recorded)), local time."
    }

    private func transcript(_ n: VoiceNote) -> String { n.paragraphs.filter { !$0.isEmpty }.joined(separator: "\n") }

    /// A note saved while no model answered (no internet, Mac asleep) gets
    /// its summary and title as soon as one does. Brain runs this every ten
    /// minutes and soon after launch.
    func retryPendingInsights() {
        guard insightsAvailable else { return }
        let pending = retained.filter {
            ($0.summary == nil || ($0.page == true && $0.summaryStale == true)) && !transcript($0).isEmpty
                && !summaryRuns.isRunning($0.id) && !insightsGaveUp.contains($0.id)
        }.prefix(3)
        for n in pending {
            summarize(n.id) { receipt in Log.add("pending summary: \(receipt)") }
        }
    }

    func summarizeLast(_ completion: @escaping (String) -> Void) {
        guard let latest = retained.first else { completion("There is no retained note to summarize."); return }
        summarize(latest.id, completion)
    }

    /// note_insights.summarize, via OnPhoneAI (Gemini / Claude) or the Mac.
    func summarize(_ id: String, _ completion: @escaping (String) -> Void) {
        guard let link = macLink, link.configured else { completion("Summaries need Gemini, Claude, or the Mac, and none is set up on this phone."); return }
        guard let n = stored.voice.first(where: { $0.id == id }) else { completion("That note no longer exists."); return }
        let title = n.title.isEmpty ? "Untitled" : n.title
        let text = transcript(n)
        guard !text.isEmpty else { completion("“\(title)” has no transcript to summarize."); return }
        // One run per note; a request meanwhile (newer words) reruns once after it.
        guard summaryRuns.begin(id) else { completion("“\(title)” is already being summarized. One moment."); return }
        insightProblems[id] = nil
        insightsChanged(id)
        let material = n.page == true ? "NOTEBOOK PAGE (written by hand and typed, not a recording):\n\(text)" : "TRANSCRIPT:\n\(text)"
        link.complete(instructions: Self.insightsSystem, text: material) { [weak self] result in
            guard let self else { return }
            let rerun = self.summaryRuns.finish(id)
            defer {
                self.insightsChanged(id)
                if rerun { self.summarize(id) { receipt in Log.add("summary rerun: \(receipt)") } }
            }
            let raw: String
            switch result {
            case .success(let r): raw = r
            case .failure(let f):
                self.insightProblems[id] = MacLink.explain(f)
                completion(MacLink.explain(f))
                return
            }
            guard let parsed = Self.parseInsights(raw), let i = self.stored.voice.firstIndex(where: { $0.id == id }) else {
                self.insightsGaveUp.insert(id)
                self.insightProblems[id] = "The summarizer returned an unusable answer. Tap Re-summarize to retry."
                completion("The summarizer returned an unusable answer. Say: summarize the last note, to retry.")
                return
            }
            self.stored.voice[i].summary = parsed.summary
            self.stored.voice[i].keyPoints = parsed.keyPoints
            self.stored.voice[i].todos = parsed.todos
            // Newer words are on their way: still stale until that run lands.
            self.stored.voice[i].summaryStale = rerun ? true : nil
            self.stored.voice[i].updated = Date()
            var shown = self.stored.voice[i].title
            if !parsed.title.isEmpty, Grammar.match(Self.clockTitleRe, shown) != nil {
                // A tapped start is titled by the clock until there is something better.
                self.stored.voice[i].title = parsed.title
                shown = parsed.title
            }
            self.persist()
            self.publishPanel?()
            if self.current, self.page == .list, !self.readerOpen { self.show() }
            let p = parsed.keyPoints.count, t = parsed.todos.count
            completion("Summarized “\(shown)”: \(p) key point\(p == 1 ? "" : "s"), \(t) todo\(t == 1 ? "" : "s"). Open QuickNotes to read it.")
        }
    }

    /// note_insights.ask_last
    func askLast(_ question: String, _ completion: @escaping (String) -> Void) {
        guard let n = retained.first else { completion("There is no retained note to ask about."); return }
        guard let link = macLink, link.configured else { completion("Asking a note needs Gemini, Claude, or the Mac, and none is set up on this phone."); return }
        let cleaned = question.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard !cleaned.isEmpty else { completion("Say the question after: ask the last note."); return }
        let title = n.title.isEmpty ? "Untitled" : n.title
        let text = transcript(n)
        guard !text.isEmpty else { completion("“\(title)” has no transcript to ask about."); return }
        var parts = ["NOTE TITLE: \(title)"]
        if let s = n.summary { parts.append("SUMMARY: \(s)") }
        if !n.keyPoints.isEmpty { parts.append("KEY POINTS: " + n.keyPoints.joined(separator: " · ")) }
        parts.append(text.count > 18_000 ? "TRANSCRIPT (earlier content omitted):\n" + String(text.suffix(18_000)) : "TRANSCRIPT:\n\(text)")
        link.complete(instructions: Self.askSystem, text: parts.joined(separator: "\n\n") + "\n\nQUESTION: \(cleaned)") { result in
            switch result {
            case .success(let a): completion(a.split(whereSeparator: { $0.isWhitespace }).joined(separator: " "))
            case .failure(let f): completion(MacLink.explain(f))
            }
        }
    }

    struct Insights { let summary: String; let keyPoints: [String]; let todos: [String]; let title: String }

    /// note_insights.parse_insights: tolerant of decorated headers.
    static func parseInsights(_ raw: String) -> Insights? {
        let headers: [(String, String)] = [("title", "title"), ("summary", "summary"), ("overview", "summary"),
                                           ("key points", "key points"), ("todos", "todos"), ("next steps", "todos")]
        var sections: [String: [String]] = ["title": [], "summary": [], "key points": [], "todos": []]
        var current: String?
        for line in raw.components(separatedBy: .newlines) {
            let text = line.trimmingCharacters(in: .whitespaces)
            let undecorated = text.replacingOccurrences(of: #"^[*_#\s]+"#, with: "", options: .regularExpression)
            let lowered = undecorated.lowercased()
            var section: String?
            var remainder = ""
            for (candidate, target) in headers where lowered.hasPrefix(candidate) {
                let tail = String(undecorated.dropFirst(candidate.count))
                if tail.range(of: #"^[*_#:\s]*$"#, options: .regularExpression) != nil { section = target; break }
                if let r = tail.range(of: #"^[*_\s]*:[*_\s]*"#, options: .regularExpression) {
                    section = target
                    remainder = String(tail[r.upperBound...]).trimmingCharacters(in: .whitespaces)
                    break
                }
            }
            if let section {
                current = section
                if !remainder.isEmpty { sections[section, default: []].append(remainder) }
                continue
            }
            if let current, !text.isEmpty { sections[current, default: []].append(text) }
        }
        let summary = (sections["summary"] ?? []).joined(separator: " ").split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard !summary.isEmpty else { return nil }
        func bullets(_ lines: [String]) -> [String] {
            Array(lines.map { $0.replacingOccurrences(of: #"^[-•*\d.)\s]+"#, with: "", options: .regularExpression).trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty && !["none", "none."].contains($0.lowercased()) }
                .map { String($0.prefix(500)) }
                .prefix(12))
        }
        var title = (sections["title"] ?? []).joined(separator: " ").split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        title = title.trimmingCharacters(in: CharacterSet(charactersIn: "\"'“”‘’*_#")).trimmingCharacters(in: .whitespaces)
        while let l = title.last, ".:;,!".contains(l) { title.removeLast() }
        if title.count > 80 {
            let head = String(title.prefix(80))
            title = head.contains(" ") ? String(head[..<head.lastIndex(of: " ")!]) : head
        }
        return Insights(summary: String(summary.prefix(4000)), keyPoints: bullets(sections["key points"] ?? []),
                        todos: bullets(sections["todos"] ?? []), title: title)
    }

    // MARK: Commitments → reminders (commitments.py, create_reminders_for)

    struct Commitment {
        let task: String
        let quote: String
        let day: String?
        var when: String? = nil
        /// The whole transcript sentence the quote sits in: Claude may quote
        /// only "I need to drop the forms" from "…on Wednesday next week at
        /// 5 o'clock, I need to drop the forms".
        var context: String = ""
    }

    /// The transcript sentence holding these words, else the words.
    static func sentence(containing quoteWords: [String], in transcript: String) -> String {
        let needle = " " + quoteWords.joined(separator: " ") + " "
        let parts = transcript.replacingOccurrences(of: #"(?<=[.!?])\s+|\n+"#, with: "\u{1}", options: .regularExpression)
            .components(separatedBy: "\u{1}")
        return parts.first { (" " + words($0).joined(separator: " ") + " ").contains(needle) }?
            .trimmingCharacters(in: .whitespaces) ?? quoteWords.joined(separator: " ")
    }

    static let cue = try! NSRegularExpression(pattern: #"\b(?:i need to|i will|i'll|i have to|i must|i'm going to|i am going to|i'm gonna|i've got to|i have got to|i gotta|i've gotta|remind me|don't forget|do not forget|we need to)\b"#)
    static let dayRe = try! NSRegularExpression(pattern: #"\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"#)
    static let wordRe = try! NSRegularExpression(pattern: #"[a-z0-9]+(?:'[a-z]+)?"#)
    static let filler: Set<String> = ["a", "an", "the", "to", "of", "and", "for", "my", "on", "in", "at", "with",
                                       "up", "it", "i", "we", "me", "our", "that", "this", "so", "then", "also"]

    /// commitments._words: lowercase words, apostrophes kept.
    static func words(_ text: String) -> [String] {
        let t = text.lowercased().replacingOccurrences(of: "’", with: "'").replacingOccurrences(of: "‘", with: "'")
        return wordRe.matches(in: t, range: NSRange(t.startIndex..., in: t)).compactMap { Range($0.range, in: t).map { String(t[$0]) } }
    }

    static func content(_ text: String) -> Set<String> {
        Set(words(text).filter { !filler.contains($0) }.map { $0.count > 5 ? String($0.prefix(5)) : $0 })
    }

    /// commitments.parse_blocks: TASK/QUOTE pairs, tolerant of decoration.
    static func parseBlocks(_ raw: String) -> [(String, String, String?)] {
        let header = try! NSRegularExpression(pattern: #"^[\s>*_#\-•\d.)]*(task|quote|when)\s*[*_]*\s*:\s*[*_]*\s*(.*)$"#, options: [.caseInsensitive])
        var pairs: [(String, String, String?)] = []
        var task: String?
        var quote: String?
        var when: String?
        var current: String?
        func flush() {
            if let t = task, let q = quote, !t.isEmpty, !q.isEmpty { pairs.append((t, q, when)) }
            task = nil; quote = nil; when = nil; current = nil
        }
        for line in raw.components(separatedBy: .newlines) {
            if let m = header.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
               let f = Range(m.range(at: 1), in: line), let v = Range(m.range(at: 2), in: line) {
                let value = String(line[v]).trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\"“”")).trimmingCharacters(in: .whitespaces)
                switch line[f].lowercased() {
                case "task": flush(); task = value; current = "task"
                case "quote": quote = value; current = "quote"
                default: when = value.trimmingCharacters(in: CharacterSet(charactersIn: "*_ ")); current = "when"
                }
                continue
            }
            let text = line.trimmingCharacters(in: .whitespaces)
            if text.isEmpty { continue }
            if current == "quote", let q = quote { quote = q + " " + text } else if current == "task", let t = task { task = t + " " + text }
        }
        flush()
        return pairs
    }

    /// commitments.keep_backed_commitments: only what the transcript says.
    /// `page`: a notebook page, where a written to-do ("Order two spare
    /// relays") is the writer's own task with no "I will" in it.
    static func keepBacked(_ proposed: [(String, String, String?)], transcript: String, page: Bool = false) -> [Commitment] {
        let haystack = " " + words(transcript).joined(separator: " ") + " "
        var kept: [Commitment] = []
        var seenQuotes = Set<String>()
        for (taskText, quoteText, whenText) in proposed {
            let collapsed = quoteText.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            let sentences = collapsed.replacingOccurrences(of: #"(?<=[.!?])\s+"#, with: "\u{1}", options: .regularExpression)
                .components(separatedBy: "\u{1}")
            guard let sentence = (page ? sentences.first : sentences.first(where: { s in
                let w = words(s).joined(separator: " ")
                return cue.firstMatch(in: w, range: NSRange(w.startIndex..., in: w)) != nil
            }))?.trimmingCharacters(in: .whitespaces) else { continue }
            let quoteWords = words(sentence)
            guard quoteWords.count >= 3, haystack.contains(" " + quoteWords.joined(separator: " ") + " ") else { continue }
            var task = taskText.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
                .trimmingCharacters(in: CharacterSet(charactersIn: " \"“”'.,;:!-–—"))
            task = task.replacingOccurrences(of: #"^(?:to|please)\s+"#, with: "", options: [.regularExpression, .caseInsensitive])
            if task.count > 160 {
                let head = String(task.prefix(160))
                task = head.contains(" ") ? String(head[..<head.lastIndex(of: " ")!]) : head
            }
            task = task.prefix(1).uppercased() + task.dropFirst()
            let taskStems = content(task)
            guard !taskStems.isEmpty else { continue }
            guard Double(taskStems.intersection(content(sentence)).count) / Double(taskStems.count) >= 0.6 else { continue }
            let normalized = quoteWords.joined(separator: " ")
            guard !seenQuotes.contains(normalized) else { continue }
            if kept.contains(where: { other in
                let o = content(other.task)
                return Double(taskStems.intersection(o).count) / Double(max(1, taskStems.union(o).count)) >= 0.6
            }) { continue }
            seenQuotes.insert(normalized)
            let context = Self.sentence(containing: quoteWords, in: transcript)
            let lowered = (sentence + " " + context).lowercased()
            let day = dayRe.firstMatch(in: lowered, range: NSRange(lowered.startIndex..., in: lowered))
                .flatMap { Range($0.range(at: 1), in: lowered) }
                .map { lowered[$0] == "tonight" ? "today" : String(lowered[$0]) }
            kept.append(Commitment(task: task, quote: sentence, day: day, when: whenText, context: context))
            if kept.count == 8 { break }
        }
        return kept
    }

    /// note_insights._due_date: 09:00 on the day said, else the note's day;
    /// a 09:00 already gone moves to the next 09:00 ahead.
    static func dueDate(_ day: String?, noteDay: Date, now: Date) -> Date {
        let cal = Calendar.current
        var base = cal.startOfDay(for: noteDay)
        let weekdays = ["monday": 2, "tuesday": 3, "wednesday": 4, "thursday": 5, "friday": 6, "saturday": 7, "sunday": 1]
        if day == "tomorrow" {
            base = cal.date(byAdding: .day, value: 1, to: base)!
        } else if let d = day, let target = weekdays[d] {
            let current = cal.component(.weekday, from: base)
            base = cal.date(byAdding: .day, value: (target - current + 6) % 7 + 1, to: base)!
        }
        let nine = cal.date(bySettingHour: 9, minute: 0, second: 0, of: base)!
        if nine > now { return nine }
        var ahead = cal.date(bySettingHour: 9, minute: 0, second: 0, of: now)!
        if ahead <= now { ahead = cal.date(byAdding: .day, value: 1, to: ahead)! }
        return ahead
    }

    /// When a note's task is due (the wearer, 2026-10-24: "Wednesday next
    /// week at 5 o'clock" must become that Wednesday at 5 PM). Claude's WHEN,
    /// counted from the recording, is taken only when the sentence itself
    /// names a day or a time; a day with no time is an all-day reminder.
    /// Nothing said: the old rule, 09:00 on the note's day or the next 09:00.
    static func due(for c: Commitment, recorded: Date, now: Date) -> (date: Date, allDay: Bool) {
        let lower = (c.quote + " " + c.context).lowercased()
        let range = NSRange(lower.startIndex..., in: lower)
        let saidTime = ReminderTime.timeWords.firstMatch(in: lower, range: range) != nil
        let saidDay = ReminderTime.dateWords.firstMatch(in: lower, range: range) != nil
            || lower.contains("week") || lower.contains("month")
        let cal = Calendar.current
        let earliest = cal.startOfDay(for: recorded)
        if let when = c.when?.trimmingCharacters(in: .whitespaces), saidTime || saidDay {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "yyyy-MM-dd HH:mm"
            if saidTime, let d = f.date(from: when), d >= earliest { return (d, false) }
            f.dateFormat = "yyyy-MM-dd"
            if let d = f.date(from: String(when.prefix(10))), d >= earliest {
                return saidTime ? (d, true) : (cal.startOfDay(for: d), true)
            }
        }
        if c.day != nil {
            return (cal.startOfDay(for: dueDate(c.day, noteDay: recorded, now: now)), true)
        }
        return (dueDate(nil, noteDay: recorded, now: now), false)
    }

    static func dueLabel(_ due: (date: Date, allDay: Bool)) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = due.allDay ? "EEE MMM d" : "EEE MMM d, h:mm a"
        return f.string(from: due.date) + (due.allDay ? " (all day)" : "")
    }

    /// Reminders found in a note, not added yet.
    struct ReminderOffer {
        let noteId: String
        let title: String
        var items: [Reminders.BatchItem]
        /// Found again but already in Reminders.
        let already: Int
    }

    /// Claude reads the note for what the speaker said they will do and
    /// when (commitments.py); what is already in Reminders is left out.
    /// `nil` with the reason when there is nothing to offer.
    func proposeReminders(_ id: String, _ completion: @escaping (ReminderOffer?, String) -> Void) {
        guard let reminders else { completion(nil, "Reminders are unavailable on this phone."); return }
        guard let link = macLink, link.configured else {
            completion(nil, "Finding reminders in a note needs Gemini, Claude, or the Mac, and none is set up on this phone.")
            return
        }
        guard let n = stored.voice.first(where: { $0.id == id }) else { completion(nil, "That note no longer exists."); return }
        let title = n.title.isEmpty ? "Untitled" : n.title
        let text = transcript(n)
        guard !text.isEmpty else { completion(nil, "“\(title)” has no transcript."); return }
        let instructions = Self.commitmentInstructions(recorded: n.created) + (n.page == true ? Self.pageCommitmentNote : "")
        link.complete(instructions: instructions, text: text) { result in
            let raw: String
            switch result {
            case .success(let r): raw = r
            case .failure(let f): completion(nil, MacLink.explain(f)); return
            }
            let none = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) == "none"
            let proposed = none ? [] : Self.parseBlocks(raw)
            let found = Self.keepBacked(proposed, transcript: text, page: n.page == true)
            Log.add("commitments: \(proposed.count) proposed, \(found.count) kept")
            guard !found.isEmpty else { completion(nil, "“\(title)” has no reminders: I add only what you said you will do."); return }
            var already = 0
            var items: [Reminders.BatchItem] = []
            for c in found {
                let key = String(ConfirmGate.sha256(Self.words(c.quote).joined(separator: " ")).prefix(20))
                let origin = "voice_note.commitment:\(id):\(key)"
                if reminders.isCreated(origin: origin) { already += 1; continue }
                let due = Self.due(for: c, recorded: n.created, now: Date())
                Log.add("commitment due: when=\(c.when ?? "-") → \(Self.dueLabel(due))")
                items.append(Reminders.BatchItem(title: c.task, due: due.date, origin: origin, allDay: due.allDay))
            }
            guard !items.isEmpty else {
                completion(nil, "The reminder\(already == 1 ? "" : "s") from “\(title)” \(already == 1 ? "is" : "are") already in Reminders.")
                return
            }
            completion(ReminderOffer(noteId: id, title: title, items: items, already: already), "")
        }
    }

    /// Add what the wearer accepted (a tap on the glasses' list, or Add in
    /// JOAT Brain's Notes tab): that act is the confirmation, so nothing asks
    /// a second time.
    func addReminders(_ offer: ReminderOffer, _ completion: @escaping (String) -> Void) {
        guard let reminders else { completion("Reminders are unavailable on this phone."); return }
        let title = offer.title
        reminders.createBatch(offer.items, label: title, confirmed: true) { r in
            if let refused = r.refused { completion(refused); return }
            if r.declined { completion("Reminders cancelled."); return }
            let total = offer.items.count
            var summary = r.created == total
                ? "Added \(r.created) reminder\(total == 1 ? "" : "s") from “\(title)”."
                : "Added \(r.created) of \(total) reminder\(total == 1 ? "" : "s") from “\(title)”."
            if offer.already + r.already > 0 { summary += " \(offer.already + r.already) already in Reminders." }
            completion(r.problems.first.map { "\(summary) \($0)" } ?? summary)
        }
    }

    /// The list on the glasses: a tap adds them all, 2x tap leaves.
    private func presentOffer(_ offer: ReminderOffer, _ completion: @escaping (String) -> Void) {
        let lines = offer.items.map { "□ \($0.title) — \(Self.dueLabel(($0.due, $0.allDay)))" }
        reviewReminders(title: offer.title, lines: lines) { [weak self] decision in
            guard let self else { return }
            guard let decision else { completion("Reminders were not added: the list was not answered in time."); return }
            guard decision else { completion("Reminders not added."); return }
            self.addReminders(offer, completion)
        }
    }

    /// Found after a save: shown now if QuickNotes' list is on the glasses,
    /// else the next time QuickNotes opens.
    private func offerReminders(_ id: String) {
        proposeReminders(id) { [weak self] offer, why in
            guard let self else { return }
            guard let offer else { Log.add("reminders offer: \(why)"); return }
            if self.current, self.page == .list, !self.readerOpen, !self.active {
                self.presentOffer(offer) { [weak self] r in self?.hud.receipt(r); self?.say?(r) }
            } else {
                self.pendingOffer = offer
                Log.add("reminders offer kept for QuickNotes: \(offer.items.count)")
            }
        }
    }

    /// "Create reminders from the last note", or the saved note's tap.
    func createReminders(_ id: String, _ completion: @escaping (String) -> Void) {
        proposeReminders(id) { [weak self] offer, why in
            guard let self else { return }
            guard let offer else { completion(why); return }
            self.presentOffer(offer, completion)
        }
    }

    // MARK: The review page (capabilities.review_note_reminders)

    private func reviewReminders(title: String, lines: [String], completion: @escaping (Bool?) -> Void) {
        leaveReview()
        let count = lines.count
        // From the note's own page the list answers that tap in the same
        // visit; said from anywhere else it is a request of its own.
        if !(readerOpen && current) { menu?.claim("QUICKNOTES") }
        reviewDecide = completion
        readerOpen = true
        readerTap = { [weak self] in self?.finishReview(true) }
        let timer = DispatchWorkItem { [weak self] in self?.finishReview(nil) }
        reviewTimer = timer
        DispatchQueue.main.asyncAfter(deadline: .now() + 90, execute: timer)
        menu?.sendCurrent([
            "title": "QUICKNOTES · DETAIL", "items": [String](),
            "body": "Add \(count) reminder\(count == 1 ? "" : "s") from “\(title)”\n\n" + lines.joined(separator: "\n"),
            "hint": "tap: add \(count) · " + CapabilityMenu.backHint, "selectable": true,
            "readerPage": ["id": "review-" + ConfirmGate.hex(16), "index": 0, "count": 1],
        ])
    }

    private func finishReview(_ decision: Bool?) {
        guard let decide = reviewDecide else { return }
        reviewDecide = nil
        reviewTimer?.cancel()
        reviewTimer = nil
        readerTap = nil
        decide(decision)
    }

    /// The wearer left the review page: that is a no.
    func leaveReview() {
        if reviewDecide != nil { finishReview(false) }
    }
}

// MARK: - Notebook pages (Notebook.swift; the wearer, 2026-11-09)

extension QuickNotes {
    static func pageClockTitle(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "'Page' HH:mm"
        return f.string(from: date)
    }

    func renameNote(_ id: String, to title: String) {
        let cleaned = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty, let i = stored.voice.firstIndex(where: { $0.id == id }) else { return }
        let capped = String(cleaned.prefix(120))
        guard stored.voice[i].title != capped else { return }
        stored.voice[i].title = capped
        stored.voice[i].updated = Date()
        persist()
        publishPanel?()
    }

    func renameNote(_ id: String, title: String) {
        renameNote(id, to: title)
    }

    func setPaperTemplate(_ id: String, template: String) {
        guard let i = stored.voice.firstIndex(where: { $0.id == id }) else { return }
        guard stored.voice[i].paperTemplate != template else { return }
        stored.voice[i].paperTemplate = template
        stored.voice[i].updated = Date()
        persist()
    }

    @discardableResult
    func createBlankPage(paperTemplate: String = "ruled") -> String {
        let now = Date()
        let id = UUID().uuidString.lowercased()
        stored.voice.append(VoiceNote(
            id: id,
            title: Self.pageClockTitle(now),
            state: "saved",
            created: now,
            updated: now,
            paragraphs: [],
            page: true,
            typed: "",
            handwriting: nil,
            paperTemplate: paperTemplate
        ))
        persist()
        publishPanel?()
        Log.add("notebook: created blank page \(id.prefix(6)) (\(paperTemplate))")
        return id
    }

    @discardableResult
    func ensureBlankPage(id: String, paperTemplate: String = "ruled") -> String {
        if stored.voice.contains(where: { $0.id == id }) { return id }
        let now = Date()
        stored.voice.append(VoiceNote(
            id: id,
            title: Self.pageClockTitle(now),
            state: "saved",
            created: now,
            updated: now,
            paragraphs: [],
            page: true,
            typed: "",
            handwriting: nil,
            paperTemplate: paperTemplate
        ))
        persist()
        publishPanel?()
        Log.add("notebook: ensured blank page \(id.prefix(6)) (\(paperTemplate))")
        return id
    }

    func deleteNote(_ id: String) {
        var changed = false
        if let i = stored.voice.firstIndex(where: { $0.id == id && $0.state != "open" }) {
            let wasPage = stored.voice[i].page == true
            stored.voice.remove(at: i)
            if wasPage { PageStore.deleteFiles(id) }
            changed = true
        } else if let t = stored.text.firstIndex(where: { $0.id == id && !$0.deleted }) {
            stored.text[t].deleted = true
            stored.text[t].updated = Date()
            lastDeletedTextId = id
            changed = true
        }
        if changed {
            persist()
            publishPanel?()
            Log.add("quicknotes: deleted note \(id.prefix(6))")
        }
    }

    func deleteEmptyPageIfUntouched(_ id: String) {
        guard let n = stored.voice.first(where: { $0.id == id }),
              n.page == true,
              n.paragraphs.isEmpty,
              (n.typed ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !PageStore.hasInk(id),
              PageStore.imageNames(id).isEmpty else { return }
        deleteNote(id)
    }

    /// A page from JOAT Brain's notebook joins the one list of notes (the
    /// wearer's choice): the glasses list and read it, OnPhoneAI summarizes it,
    /// and its next steps are offered as reminders, as after a voice note.
    /// Its text is the typed words, then the handwriting as read by vision OCR.
    /// `inkChanged`: the handwriting is read again (Notebook.read).
    @discardableResult
    func savePage(id: String?, typed: String, inkChanged: Bool, paperTemplate: String? = nil) -> String {
        let now = Date()
        if let id, let i = stored.voice.firstIndex(where: { $0.id == id }) {
            let previousTyped = stored.voice[i].typed ?? ""
            let typedChanged = previousTyped != typed
            stored.voice[i].page = true
            stored.voice[i].typed = typed
            if let paperTemplate {
                stored.voice[i].paperTemplate = paperTemplate
            }
            // Changed ink keeps the last reading (and the summary stays on
            // show, marked stale) until the page is read again a few seconds
            // after writing stops: insights no longer vanish while writing.
            stored.voice[i].paragraphs = Self.pageParagraphs(typed: typed, handwriting: stored.voice[i].handwriting)
            if typedChanged || inkChanged {
                insightsGaveUp.remove(id)
                if stored.voice[i].summary != nil { stored.voice[i].summaryStale = true }
            }
            stored.voice[i].updated = now
            persist()
            publishPanel?()
            Log.add("notebook: page \(id.prefix(6)) auto-saved (\(typed.count) typed chars)")
            return id
        }
        let newId = id ?? UUID().uuidString.lowercased()
        stored.voice.append(VoiceNote(id: newId, title: Self.pageClockTitle(now), state: "saved",
                                      created: now, updated: now,
                                      paragraphs: Self.pageParagraphs(typed: typed, handwriting: nil),
                                      page: true, typed: typed,
                                      paperTemplate: paperTemplate ?? "ruled"))
        persist()
        publishPanel?()
        Log.add("notebook: page \(newId.prefix(6)) saved (\(typed.count) typed chars)")
        return newId
    }

    /// The handwriting as read by vision OCR ("NONE": no words on the page).
    func pageRead(_ id: String, handwriting raw: String, summarize: Bool = true, offerReminders: Bool = true) {
        guard let i = stored.voice.firstIndex(where: { $0.id == id }) else { return }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        stored.voice[i].handwriting = text.uppercased() == "NONE" ? "" : text
        stored.voice[i].paragraphs = Self.pageParagraphs(typed: stored.voice[i].typed ?? "",
                                                         handwriting: stored.voice[i].handwriting)
        insightsGaveUp.remove(id)
        stored.voice[i].updated = Date()
        persist()
        publishPanel?()
        Log.add("notebook: page \(id.prefix(6)) read, \(text.count) chars")
        if summarize { pageReady(id, offerReminders: offerReminders) }
    }

    /// The summary, a title and the reminders offer, as after a voice note.
    /// `offerReminders` false: a writing pause, not leaving the page (the
    /// reminders pass runs once, on leave).
    func pageReady(_ id: String, offerReminders offer: Bool = true) {
        guard insightsAvailable, let n = stored.voice.first(where: { $0.id == id }) else { return }
        guard !transcript(n).isEmpty else {
            // Everything erased: no summary of words that are gone.
            if n.summary != nil, let i = stored.voice.firstIndex(where: { $0.id == id }) {
                stored.voice[i].summary = nil
                stored.voice[i].keyPoints = []
                stored.voice[i].todos = []
                stored.voice[i].summaryStale = nil
                persist()
                insightsChanged(id)
            }
            return
        }
        if n.summary == nil || n.summaryStale == true || summaryRuns.isRunning(id) {
            summarize(id) { receipt in Log.add("page summary: \(receipt)") }
        }
        if offer { offerReminders(id) }
    }

    #if DEBUG
    /// Simulator screenshots: a page's insights as a model would give them.
    func debugSetInsights(_ id: String, summary: String, keyPoints: [String], todos: [String]) {
        guard let i = stored.voice.firstIndex(where: { $0.id == id }) else { return }
        stored.voice[i].summary = summary
        stored.voice[i].keyPoints = keyPoints
        stored.voice[i].todos = todos
        stored.voice[i].summaryStale = nil
        persist()
        insightsChanged(id)
    }
    #endif

    /// One paragraph per line, typed first, then handwritten.
    static func pageParagraphs(typed: String, handwriting: String?) -> [String] {
        (typed + "\n" + (handwriting ?? "")).components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}
