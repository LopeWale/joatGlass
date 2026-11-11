import Foundation

/// Prep notes (Even's Conversate Prep Notes; the wearer's ask, 2026-10-29):
/// material added before a conversation (a shared or picked file, a link,
/// typed or pasted text). Claude turns it into a few talking points for the
/// glasses, and the AI cues answer from it while a QuickNote records. One
/// prep is in use at a time: the newest, unless the wearer picks another in
/// JOAT Brain. Kept in Documents/prep.json.
final class Prep: ObservableObject {
    struct Doc: Codable, Identifiable, Equatable {
        let id: String
        var title: String
        /// Where it came from: "PDF · budget.pdf", "Link · example.com", "Typed".
        var source: String
        var text: String
        var points: [String]
        var added: Date
    }

    private struct Stored: Codable {
        var docs: [Doc] = []
        var activeId: String?
    }

    @Published private(set) var docs: [Doc] = []
    @Published private(set) var activeId: String?
    /// What JOAT Brain is doing with a new prep ("Reading budget.pdf…").
    @Published private(set) var status = ""

    var active: Doc? { docs.first { $0.id == activeId } ?? docs.first }

    private let file: URL
    private let claude: ClaudeLink

    init(claude: ClaudeLink, directory: URL? = nil) {
        self.claude = claude
        file = (directory ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0])
            .appendingPathComponent("prep.json")
        if let data = try? Data(contentsOf: file) {
            let d = JSONDecoder()
            d.dateDecodingStrategy = .iso8601
            do {
                let s = try d.decode(Stored.self, from: data)
                docs = s.docs
                activeId = s.activeId
            } catch { SafeStore.quarantine(file, error) }
        }
    }

    private func persist() {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        do {
            try e.encode(Stored(docs: docs, activeId: activeId))
                .write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch { Log.add("prep save failed: \(error)") }
    }

    // MARK: - Adding

    /// A file shared to JOAT Brain (Share › JOAT Brain) or picked in it.
    func addFile(_ url: URL) {
        let name = url.lastPathComponent
        status = "Reading \(name)…"
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let text = try PrepExtract.text(fromFile: url)
            let kind = url.pathExtension.isEmpty ? "File" : url.pathExtension.uppercased()
            ingest(title: url.deletingPathExtension().lastPathComponent, source: "\(kind) · \(name)", text: text)
        } catch {
            status = "\(name): \(error)"
            Log.add("prep: \(name) unreadable: \(error)")
        }
        // A shared file is a copy in Documents/Inbox: not kept twice.
        if url.path.contains("/Documents/Inbox/") { try? FileManager.default.removeItem(at: url) }
    }

    /// A link: the page is fetched now and its text kept.
    func addLink(_ url: URL) {
        status = "Reading \(url.host ?? "the link")…"
        Task { @MainActor in
            do {
                let page = try await PrepExtract.text(fromLink: url)
                self.ingest(title: page.title, source: "Link · \(url.host ?? url.absoluteString)", text: page.text)
            } catch {
                self.status = "\(url.host ?? "Link"): \(error)"
                Log.add("prep: link unreadable: \(error)")
            }
        }
    }

    /// Typed or pasted notes, or a notebook page (title and source given).
    func addText(_ text: String, title: String = "Notes", source: String = "Typed") {
        do { ingest(title: title, source: source, text: try PrepExtract.tidy(text)) }
        catch { status = "\(error)" }
    }

    /// Whatever is on the clipboard: a link, or text.
    func paste(_ string: String?, url: URL?) {
        if let url, url.scheme?.hasPrefix("http") == true { addLink(url); return }
        let s = (string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if let u = URL(string: s), u.scheme?.hasPrefix("http") == true, !s.contains(" ") { addLink(u); return }
        guard !s.isEmpty else { status = "The clipboard is empty."; return }
        addText(s)
    }

    func select(_ id: String) {
        activeId = id
        persist()
    }

    func remove(_ id: String) {
        docs.removeAll { $0.id == id }
        if activeId == id { activeId = docs.first?.id }
        persist()
    }

    private func ingest(title: String, source: String, text: String) {
        let doc = Doc(id: UUID().uuidString.lowercased(), title: String(title.prefix(60)), source: source,
                      text: text, points: [], added: Date())
        docs.insert(doc, at: 0)
        activeId = doc.id
        persist()
        Log.add("prep added: \(source), \(text.count) chars")
        guard claude.configured else { status = "Added. Talking points need Claude on this phone."; return }
        status = "Making talking points…"
        claude.complete(instructions: Self.pointsInstructions, text: String(text.prefix(PrepExtract.maxChars))) { [weak self] result in
            guard let self, let i = self.docs.firstIndex(where: { $0.id == doc.id }) else { return }
            switch result {
            case .success(let reply):
                let parsed = Self.parsePoints(reply)
                if let t = parsed.title { self.docs[i].title = t }
                self.docs[i].points = parsed.points
                self.status = "Ready: \(self.docs[i].title), \(parsed.points.count) talking points."
                Log.add("prep points: \(parsed.points.count) for “\(self.docs[i].title)”")
            case .failure(let error):
                self.status = "Added without talking points: \(error)"
            }
            self.persist()
        }
    }

    // MARK: - Talking points

    static let pointsInstructions = """
    The wearer will have a conversation and has given you their prep material. \
    Write the briefing they will read on smart glasses during it.
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
