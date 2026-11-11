import Foundation

/// Prep notes, as Even's Conversate has them (the wearer, 2026-11-11): a prep
/// note is a folder. It holds typed notes, files (shared to JOAT Brain or
/// picked in it), links and notebook pages, as many as the conversation
/// needs. Claude turns all of it into talking points for the glasses, and the
/// AI cues answer from all of it while a QuickNote records.
///
/// Even: create a Prep Note by typing preparation details, uploading
/// supporting files, or both ("Add notes or upload documents to automatically
/// generate Prep Notes"); then select the Prep Note to start Conversate with
/// it, or start without one. One prep note is in use at a time: picked in
/// JOAT Brain's Prep tab, or on the glasses when a QuickNote starts. Kept in
/// Documents/prep.json.
final class Prep: ObservableObject {
    /// One thing in a prep note: a file, a link, typed notes, a notebook page.
    struct Item: Codable, Identifiable, Equatable {
        let id: String
        var title: String
        /// Where it came from: "PDF · budget.pdf", "Link · example.com", "Typed".
        var source: String
        var text: String
        var added: Date
    }

    struct Note: Codable, Identifiable, Equatable {
        let id: String
        var title: String
        /// The wearer named it: Claude's title doesn't replace it.
        var named: Bool
        var items: [Item]
        /// From everything in it, made again when an item comes or goes.
        var points: [String]
        var added: Date
        var updated: Date

        /// Everything in it for Claude, each item under its own heading. Each
        /// gets an even share of the cap; a short one leaves its share to the rest.
        var material: String {
            var budget = PrepExtract.maxChars
            var share: [String: Int] = [:]
            var left = items.sorted { $0.text.count < $1.text.count }
            while !left.isEmpty {
                let each = budget / left.count
                let item = left.removeFirst()
                let take = min(item.text.count, each)
                share[item.id] = take
                budget -= take
            }
            return items.map { "## \($0.title) (\($0.source))\n\(String($0.text.prefix(share[$0.id] ?? 0)))" }
                .joined(separator: "\n\n")
        }

        /// "3 items · 6 talking points"
        var summary: String {
            let things = items.count == 1 ? "1 item" : "\(items.count) items"
            return points.isEmpty ? "\(things) · no talking points yet" : "\(things) · \(points.count) talking points"
        }
    }

    /// The prep notes from before they were folders: one file, link or
    /// typed note each.
    private struct OldDoc: Codable {
        let id: String
        var title: String
        var source: String
        var text: String
        var points: [String]
        var added: Date
    }

    private struct Stored: Codable {
        var notes: [Note]?
        var docs: [OldDoc]?
        var activeId: String?
    }

    @Published private(set) var notes: [Note] = []
    @Published private(set) var activeId: String?
    /// What JOAT Brain is doing ("Reading budget.pdf…", "Making talking points…").
    @Published private(set) var status = ""

    /// The prep note in use: the one picked, else the newest.
    var active: Note? { notes.first { $0.id == activeId } ?? notes.first }

    private let file: URL
    private let claude: ClaudeLink
    /// Talking points being made, by prep note (made again if it changed meanwhile).
    private var making: [String: Int] = [:]
    private var changes: [String: Int] = [:]

    init(claude: ClaudeLink, directory: URL? = nil) {
        self.claude = claude
        file = (directory ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0])
            .appendingPathComponent("prep.json")
        guard let data = try? Data(contentsOf: file) else { return }
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        do {
            let s = try d.decode(Stored.self, from: data)
            activeId = s.activeId
            if let saved = s.notes {
                notes = saved
            } else if let docs = s.docs {
                // Each earlier prep becomes a prep note holding it (the
                // wearer's choice), with the same id, so the one in use stays.
                notes = docs.map { doc in
                    Note(id: doc.id, title: doc.title, named: false,
                         items: [Item(id: UUID().uuidString.lowercased(), title: doc.title, source: doc.source,
                                      text: doc.text, added: doc.added)],
                         points: doc.points, added: doc.added, updated: doc.added)
                }
                persist()
                Log.add("prep: \(docs.count) earlier prep\(docs.count == 1 ? "" : "s") became prep notes")
            }
        } catch { SafeStore.quarantine(file, error) }
    }

    private func persist() {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        do {
            try e.encode(Stored(notes: notes, docs: nil, activeId: activeId))
                .write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch { Log.add("prep save failed: \(error)") }
    }

    // MARK: - Prep notes

    /// A new, empty prep note, now in use. Returns its id.
    @discardableResult
    func create(title: String? = nil) -> String {
        let name = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let now = Date()
        let note = Note(id: UUID().uuidString.lowercased(), title: name.isEmpty ? Self.untitled(now) : String(name.prefix(60)),
                        named: !name.isEmpty, items: [], points: [], added: now, updated: now)
        notes.insert(note, at: 0)
        activeId = note.id
        persist()
        Log.add("prep: new prep note “\(note.title)”")
        return note.id
    }

    static func untitled(_ date: Date) -> String {
        "Prep " + date.formatted(date: .abbreviated, time: .shortened)
    }

    func rename(_ id: String, to title: String) {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, let i = notes.firstIndex(where: { $0.id == id }) else { return }
        notes[i].title = String(name.prefix(60))
        notes[i].named = true
        persist()
    }

    func select(_ id: String) {
        activeId = id
        persist()
    }

    func remove(_ id: String) {
        notes.removeAll { $0.id == id }
        if activeId == id { activeId = notes.first?.id }
        persist()
    }

    func removeItem(_ itemId: String, from noteId: String) {
        guard let i = notes.firstIndex(where: { $0.id == noteId }) else { return }
        notes[i].items.removeAll { $0.id == itemId }
        notes[i].updated = Date()
        persist()
        makePoints(noteId)
    }

    // MARK: - Adding (nil: a new prep note for it)

    /// A file shared to JOAT Brain (Share › JOAT Brain) or picked in it.
    func addFile(_ url: URL, to noteId: String?) {
        let name = url.lastPathComponent
        status = "Reading \(name)…"
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let text = try PrepExtract.text(fromFile: url)
            let kind = url.pathExtension.isEmpty ? "File" : url.pathExtension.uppercased()
            add(Item(id: Self.newId(), title: String(url.deletingPathExtension().lastPathComponent.prefix(60)),
                     source: "\(kind) · \(name)", text: text, added: Date()), to: noteId)
        } catch {
            status = "\(name): \(error)"
            Log.add("prep: \(name) unreadable: \(error)")
        }
        Self.dropInboxCopy(url)
    }

    /// A shared file's copy in Documents/Inbox isn't kept twice (or at all,
    /// when sharing it was cancelled).
    static func dropInboxCopy(_ url: URL) {
        if url.path.contains("/Documents/Inbox/") { try? FileManager.default.removeItem(at: url) }
    }

    /// A link: the page is fetched now and its text kept.
    func addLink(_ url: URL, to noteId: String?) {
        status = "Reading \(url.host ?? "the link")…"
        Task { @MainActor in
            do {
                let page = try await PrepExtract.text(fromLink: url)
                self.add(Item(id: Self.newId(), title: String(page.title.prefix(60)), source: "Link · \(url.host ?? url.absoluteString)",
                              text: page.text, added: Date()), to: noteId)
            } catch {
                self.status = "\(url.host ?? "Link"): \(error)"
                Log.add("prep: link unreadable: \(error)")
            }
        }
    }

    /// Typed or pasted notes, or a notebook page (title and source given).
    func addText(_ text: String, title: String = "Notes", source: String = "Typed", to noteId: String?) {
        do {
            add(Item(id: Self.newId(), title: String(title.prefix(60)), source: source, text: try PrepExtract.tidy(text),
                     added: Date()), to: noteId)
        } catch { status = "\(error)" }
    }

    /// Whatever is on the clipboard: a link, or text.
    func paste(_ string: String?, url: URL?, to noteId: String?) {
        if let url, url.scheme?.hasPrefix("http") == true { addLink(url, to: noteId); return }
        let s = (string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if let u = URL(string: s), u.scheme?.hasPrefix("http") == true, !s.contains(" ") { addLink(u, to: noteId); return }
        guard !s.isEmpty else { status = "The clipboard is empty."; return }
        addText(s, title: "Pasted notes", source: "Pasted", to: noteId)
    }

    private static func newId() -> String { UUID().uuidString.lowercased() }

    private func add(_ item: Item, to noteId: String?) {
        let id: String
        if let noteId, notes.contains(where: { $0.id == noteId }) {
            id = noteId
        } else {
            // A new prep note, named after its first item until Claude names it.
            id = create()
            if let i = notes.firstIndex(where: { $0.id == id }) { notes[i].title = item.title }
        }
        guard let i = notes.firstIndex(where: { $0.id == id }) else { return }
        notes[i].items.append(item)
        notes[i].updated = Date()
        persist()
        Log.add("prep: added \(item.source) (\(item.text.count) chars) to “\(notes[i].title)”")
        makePoints(id)
    }

    // MARK: - Talking points

    /// From everything in the prep note. If it changes while they're being
    /// made, they're made again.
    private func makePoints(_ id: String) {
        changes[id, default: 0] += 1
        guard let note = notes.first(where: { $0.id == id }) else { return }
        guard !note.items.isEmpty else {
            if let i = notes.firstIndex(where: { $0.id == id }) { notes[i].points = [] }
            persist()
            return
        }
        guard claude.configured else { status = "Added. Talking points need Claude on this phone."; return }
        guard making[id] == nil else { return }
        let asked = changes[id] ?? 0
        making[id] = asked
        status = "Making talking points…"
        claude.complete(instructions: Self.pointsInstructions, text: note.material) { [weak self] result in
            guard let self else { return }
            self.making[id] = nil
            guard let i = self.notes.firstIndex(where: { $0.id == id }) else { return }
            switch result {
            case .success(let reply):
                let parsed = Self.parsePoints(reply)
                if let t = parsed.title, !self.notes[i].named { self.notes[i].title = t }
                self.notes[i].points = parsed.points
                self.status = "Ready: \(self.notes[i].title), \(parsed.points.count) talking points."
                Log.add("prep points: \(parsed.points.count) for “\(self.notes[i].title)” from \(self.notes[i].items.count) items")
            case .failure(let error):
                self.status = "Added without talking points: \(error)"
            }
            self.persist()
            if (self.changes[id] ?? 0) != asked { self.makePoints(id) }
        }
    }

    static let pointsInstructions = """
    The wearer will have a conversation and has given you their prep material: one or more items, \
    each under its own heading. Write the briefing they will read on smart glasses during it, \
    drawing on all of the items.
    First line: TITLE: a name for this material, at most 30 characters.
    Then up to 8 lines, each starting with "- ": one talking point each, at most 80 characters: \
    the facts, numbers, names, goals and questions that matter most, most important first.
    Use only the material. Plain text: no markdown besides the dashes, no other lines.
    """

    static func parsePoints(_ reply: String) -> (title: String?, points: [String]) {
        var title: String?
        var points: [String] = []
        for raw in reply.split(whereSeparator: { $0.isNewline }) {
            let line = raw.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "**", with: "")
            if line.uppercased().hasPrefix("TITLE:") {
                let t = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
                if !t.isEmpty { title = String(t.prefix(40)) }
            } else if line.hasPrefix("- ") || line.hasPrefix("• ") || line.hasPrefix("* ") {
                let p = line.dropFirst(2).trimmingCharacters(in: .whitespaces)
                if !p.isEmpty, points.count < 8 { points.append(Hud.shorten(p, limit: 90)) }
            }
        }
        return (title, points)
    }
}
