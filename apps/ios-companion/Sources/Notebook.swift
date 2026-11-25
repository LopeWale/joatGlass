import SwiftUI
import PencilKit
import PhotosUI
import CryptoKit
import UniformTypeIdentifiers

/// JOAT Brain's Apple Pencil & keyboard notebook: fixed `612 × 792 pt` US Letter
/// stationery sheets (`#FAF8F4`) with per-page vector templates (`Ruled`,
/// `Dot Grid`, `Square Grid`, `Blank`), continuous per-`noteId` auto-save,
/// quota-safe vision OCR through `OnPhoneAI` (Gemini 3.5 Flash Lite / Claude),
/// and adaptive support for both compact outer screens and unfolded iPhone Duo
/// `NavigationSplitView` detail panes.

// MARK: - Paper Templates

enum PaperTemplate: String, CaseIterable, Identifiable, Codable {
    case ruled
    case dotGrid
    case squareGrid
    case blank

    var id: String { rawValue }

    var title: String {
        switch self {
        case .ruled: return "Ruled"
        case .dotGrid: return "Dot Grid"
        case .squareGrid: return "Square Grid"
        case .blank: return "Blank"
        }
    }

    var shortTitle: String {
        switch self {
        case .ruled: return "Ruled"
        case .dotGrid: return "Dots"
        case .squareGrid: return "Grid"
        case .blank: return "Blank"
        }
    }

    var icon: String {
        switch self {
        case .ruled: return "line.3.horizontal"
        case .dotGrid: return "circle.grid.3x3"
        case .squareGrid: return "squareshape.split.3x3"
        case .blank: return "doc"
        }
    }

    static func resolve(_ raw: String?, isLegacySaved: Bool = false) -> PaperTemplate {
        if let raw, let found = PaperTemplate(rawValue: raw) { return found }
        return isLegacySaved ? .blank : .ruled
    }
}

// MARK: - The ink & images on disk

enum PageStore {
    /// Warm ivory stationery (`#FAF8F4`).
    static let stationeryColor = UIColor(red: 0.980, green: 0.973, blue: 0.957, alpha: 1.0)
    private static let ioQueue = DispatchQueue(label: "com.joat.notebook.io", qos: .utility)

    static var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("pages")
    }

    static func url(_ id: String) -> URL { directory.appendingPathComponent("\(id).drawing") }

    static func load(_ id: String) -> PKDrawing {
        guard let data = try? Data(contentsOf: url(id)) else { return PKDrawing() }
        do { return try PKDrawing(data: data) } catch {
            Log.add("notebook: page \(id.prefix(6)) ink unreadable: \(error)")
            return PKDrawing()
        }
    }

    static func save(_ drawing: PKDrawing, _ id: String) {
        if drawing.strokes.isEmpty {
            try? FileManager.default.removeItem(at: url(id))
            return
        }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try drawing.dataRepresentation().write(to: url(id), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch { Log.add("notebook: ink not saved: \(error)") }
    }

    /// Non-blocking mid-edit ink persistence so Apple Pencil strokes never stall the main thread.
    static func saveAsync(_ drawing: PKDrawing, _ id: String) {
        ioQueue.async {
            save(drawing, id)
        }
    }

    static func hasInk(_ id: String) -> Bool { FileManager.default.fileExists(atPath: url(id).path) }

    static func deleteFiles(_ id: String) {
        let fm = FileManager.default
        try? fm.removeItem(at: url(id))
        try? fm.removeItem(at: richURL(id))
        for file in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? []
        where file.hasPrefix("\(id)-") && file.hasSuffix(".jpg") {
            try? fm.removeItem(at: directory.appendingPathComponent(file))
        }
    }

    // MARK: Pasted images (`<id>.page.json` + `<id>-<hash>.jpg`)

    private struct RichPage: Codable {
        struct Segment: Codable {
            var text: String?
            var image: String?
        }
        var segments: [Segment]
    }

    static func richURL(_ id: String) -> URL { directory.appendingPathComponent("\(id).page.json") }

    static var textAttributes: [NSAttributedString.Key: Any] {
        [
            .font: UIFont.preferredFont(forTextStyle: .body),
            .foregroundColor: UIColor(red: 0.11, green: 0.12, blue: 0.14, alpha: 1.0),
        ]
    }

    static func imageNames(_ id: String) -> [String] {
        guard let data = try? Data(contentsOf: richURL(id)),
              let page = try? JSONDecoder().decode(RichPage.self, from: data) else { return [] }
        return page.segments.compactMap(\.image)
    }

    static func images(_ id: String) -> [UIImage] {
        imageNames(id).compactMap { UIImage(contentsOfFile: directory.appendingPathComponent($0).path) }
    }

    static func attachments(_ text: NSAttributedString) -> [NSTextAttachment] {
        var out: [NSTextAttachment] = []
        text.enumerateAttribute(.attachment, in: NSRange(location: 0, length: text.length)) { value, _, _ in
            if let a = value as? NSTextAttachment, a.image != nil { out.append(a) }
        }
        return out
    }

    static func loadRich(_ id: String) -> NSAttributedString? {
        guard let data = try? Data(contentsOf: richURL(id)),
              let page = try? JSONDecoder().decode(RichPage.self, from: data) else { return nil }
        let out = NSMutableAttributedString()
        for segment in page.segments {
            if let text = segment.text {
                out.append(NSAttributedString(string: text, attributes: textAttributes))
            } else if let name = segment.image,
                      let image = UIImage(contentsOfFile: directory.appendingPathComponent(name).path) {
                let a = PageImage(image: image)
                if name.hasPrefix("\(draftId)-") { a.draftName = name } else { a.fileName = name }
                out.append(NSAttributedString(attachment: a))
            }
        }
        return out
    }

    static func saveRich(_ text: NSAttributedString, _ id: String, draft: Bool = false) {
        let fm = FileManager.default
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var segments: [RichPage.Segment] = []
        var names: [String] = []
        text.enumerateAttribute(.attachment, in: NSRange(location: 0, length: text.length)) { value, range, _ in
            if let a = value as? NSTextAttachment {
                guard let name = store(a, id, draft: draft) else { return }
                names.append(name)
                segments.append(.init(image: name))
            } else {
                let part = (text.string as NSString).substring(with: range)
                if !part.isEmpty { segments.append(.init(text: part)) }
            }
        }
        if names.isEmpty {
            try? fm.removeItem(at: richURL(id))
        } else if let data = try? JSONEncoder().encode(RichPage(segments: segments)) {
            try? data.write(to: richURL(id), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
        let keep = Set(names)
        for file in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? []
        where file.hasPrefix("\(id)-") && file.hasSuffix(".jpg") && !keep.contains(file) {
            try? fm.removeItem(at: directory.appendingPathComponent(file))
        }
        if !draft, !names.isEmpty {
            Log.add("notebook: page \(id.prefix(6)) has \(names.count) pasted image\(names.count == 1 ? "" : "s")")
        }
    }

    private static func store(_ a: NSTextAttachment, _ id: String, draft: Bool) -> String? {
        let page = a as? PageImage
        func existing(_ name: String?) -> String? {
            name.flatMap { FileManager.default.fileExists(atPath: directory.appendingPathComponent($0).path) ? $0 : nil }
        }
        if draft, let name = existing(page?.fileName) ?? existing(page?.draftName) { return name }
        if !draft, let name = existing(page?.fileName), name.hasPrefix("\(id)-") { return name }
        guard let jpeg = a.image?.jpegData(compressionQuality: 0.85) else { return nil }
        let hash = SHA256.hash(data: jpeg).prefix(6).map { String(format: "%02x", $0) }.joined()
        let name = "\(id)-\(hash).jpg"
        do {
            try jpeg.write(to: directory.appendingPathComponent(name), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            Log.add("notebook: image not saved: \(error)")
            return nil
        }
        if draft { page?.draftName = name } else { page?.fileName = name }
        return name
    }

    // MARK: Legacy single-draft migration support

    struct Draft: Codable {
        var noteId: String?
        var typed: String
        var inkChanged: Bool
        var updated: Date
    }

    static let draftId = "draft"
    private static var draftURL: URL { directory.appendingPathComponent("\(draftId).json") }

    @discardableResult
    static func writeDraft(noteId: String?, typed: String, rich: NSAttributedString, drawing: PKDrawing, inkChanged: Bool) -> Bool {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        save(drawing, draftId)
        saveRich(rich, draftId, draft: true)
        let draft = Draft(noteId: noteId, typed: typed, inkChanged: inkChanged, updated: Date())
        do {
            try JSONEncoder().encode(draft).write(to: draftURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            return true
        } catch {
            Log.add("notebook: draft not kept: \(error)")
            return false
        }
    }

    static func readDraft() -> (draft: Draft, rich: NSAttributedString?, drawing: PKDrawing)? {
        guard let data = try? Data(contentsOf: draftURL), let draft = try? JSONDecoder().decode(Draft.self, from: data) else { return nil }
        return (draft, loadRich(draftId), load(draftId))
    }

    static func clearDraft() {
        let fm = FileManager.default
        for url in [draftURL, url(draftId), richURL(draftId)] { try? fm.removeItem(at: url) }
        for file in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? [] where file.hasPrefix("\(draftId)-") {
            try? fm.removeItem(at: directory.appendingPathComponent(file))
        }
    }

    static func hasImage(_ provider: NSItemProvider) -> Bool {
        provider.canLoadObject(ofClass: UIImage.self)
            || provider.registeredTypeIdentifiers.contains { UTType($0)?.conforms(to: .image) == true }
    }

    enum Pasted {
        case image(UIImage)
        case text(String)
    }

    static func load(_ provider: NSItemProvider, completion: @escaping (Pasted?) -> Void) {
        let imageTypes = provider.registeredTypeIdentifiers.filter { UTType($0)?.conforms(to: .image) == true }
        func fromData(_ types: ArraySlice<String>) {
            guard let type = types.first else { completion(nil); return }
            _ = provider.loadDataRepresentation(forTypeIdentifier: type) { data, error in
                if let image = data.flatMap(UIImage.init(data:)) { completion(.image(image)); return }
                Log.add("notebook: \(type) not pasted: \(error?.localizedDescription ?? "no image in it")")
                fromData(types.dropFirst())
            }
        }
        if provider.canLoadObject(ofClass: UIImage.self) {
            _ = provider.loadObject(ofClass: UIImage.self) { object, error in
                if let image = object as? UIImage { completion(.image(image)); return }
                Log.add("notebook: image not pasted: \(error?.localizedDescription ?? "none")")
                fromData(imageTypes[...])
            }
        } else if !imageTypes.isEmpty {
            fromData(imageTypes[...])
        } else if provider.canLoadObject(ofClass: String.self) {
            _ = provider.loadObject(ofClass: String.self) { text, _ in completion(text.map(Pasted.text)) }
        } else {
            completion(nil)
        }
    }

    /// Vision OCR input JPEG: long edge capped at 1568 px.
    static func jpegForClaude(_ image: UIImage) -> Data? {
        scaled(image, longEdge: 1568).jpegData(compressionQuality: 0.85)
    }

    static func scaled(_ image: UIImage, longEdge: CGFloat) -> UIImage {
        let pixels = CGSize(width: image.size.width * image.scale, height: image.size.height * image.scale)
        let factor = min(1, longEdge / max(pixels.width, pixels.height, 1))
        if factor == 1, image.scale == 1 { return image }
        let size = CGSize(width: (pixels.width * factor).rounded(), height: (pixels.height * factor).rounded())
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: size, format: format).image { context in
            UIColor.white.setFill()
            context.fill(CGRect(origin: .zero, size: size))
            image.draw(in: CGRect(origin: .zero, size: size))
        }
    }

    /// Crisp ink thumbnail on warm `#FAF8F4` stationery for Notes cards and detail previews.
    static func preview(_ drawing: PKDrawing) -> UIImage? {
        guard !drawing.strokes.isEmpty else { return nil }
        let rect = drawing.bounds.insetBy(dx: -12, dy: -12)
        var ink: UIImage?
        UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
            ink = drawing.image(from: rect, scale: 2)
        }
        guard let ink else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        format.opaque = true
        return UIGraphicsImageRenderer(size: rect.size, format: format).image { context in
            stationeryColor.setFill()
            context.fill(CGRect(origin: .zero, size: rect.size))
            ink.draw(at: .zero)
        }
    }

    /// High-contrast dark ink on pure white PNG tiles for vision OCR (`OnPhoneAI.readHandwriting`).
    static func pngTiles(_ drawing: PKDrawing) -> [Data] {
        let strokes = drawing.strokes.map(\.renderBounds).filter { !$0.isNull && !$0.isEmpty }
        guard !strokes.isEmpty else { return [] }
        let width = max(drawing.bounds.maxX + 16, 320)
        let tileHeight = width * 1.3
        var bands: [(top: CGFloat, bottom: CGFloat)] = []
        for r in strokes.sorted(by: { $0.minY < $1.minY }) {
            if let last = bands.last, r.minY <= last.bottom + 4 {
                bands[bands.count - 1].bottom = max(last.bottom, r.maxY)
            } else {
                bands.append((r.minY, r.maxY))
            }
        }
        var tiles: [(top: CGFloat, bottom: CGFloat)] = []
        for band in bands {
            if let last = tiles.last, band.bottom - last.top <= tileHeight {
                tiles[tiles.count - 1].bottom = band.bottom
            } else {
                tiles.append(band)
            }
        }
        let scale = min(2, 1000 / width)
        return tiles.prefix(20).compactMap { tile in
            let rect = CGRect(x: 0, y: max(0, tile.top - 12), width: width, height: tile.bottom - tile.top + 24)
            var ink: UIImage?
            UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
                ink = drawing.image(from: rect, scale: scale)
            }
            guard let ink else { return nil }
            let format = UIGraphicsImageRendererFormat()
            format.scale = scale
            format.opaque = true
            return UIGraphicsImageRenderer(size: rect.size, format: format).pngData { context in
                UIColor.white.setFill()
                context.fill(CGRect(origin: .zero, size: rect.size))
                ink.draw(at: .zero)
            }
        }
    }
}

final class PageImage: NSTextAttachment {
    var fileName: String?
    var draftName: String?
}

final class PastingTextView: UITextView, UITextPasteDelegate {
    var onPasteImages: (([UIImage]) -> Void)?

    func acceptImages() {
        let accepted = UIPasteConfiguration(forAccepting: UIImage.self)
        accepted.addTypeIdentifiers(forAccepting: NSString.self)
        pasteConfiguration = accepted
        pasteDelegate = self
    }

    func textPasteConfigurationSupporting(_ textPasteConfigurationSupporting: any UITextPasteConfigurationSupporting,
                                          transform item: any UITextPasteItem) {
        let provider = item.itemProvider
        guard PageStore.hasImage(provider) else { item.setDefaultResult(); return }
        PageStore.load(provider) { pasted in
            if case .image(let image) = pasted {
                item.setResult(attachment: PageImage(image: PageStore.scaled(image, longEdge: 2048)))
                Log.add("notebook: pasted an image (keyboard or drop)")
            } else {
                item.setNoResult()
            }
        }
    }

    override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
        if action == #selector(paste(_:)), UIPasteboard.general.hasImages { return true }
        return super.canPerformAction(action, withSender: sender)
    }

    override func paste(_ sender: Any?) {
        let board = UIPasteboard.general
        if board.hasImages, let images = board.images, !images.isEmpty {
            onPasteImages?(images)
        } else if board.hasStrings, let words = board.string, let range = selectedTextRange {
            replace(range, withText: words)
        } else {
            super.paste(sender)
        }
    }
}

// MARK: - Saving & Quota-Safe Vision OCR

@MainActor
enum Notebook {
    static let readingChangedNotification = Notification.Name("joat.notebook.readingChanged")
    static let autoSaveDebounceSeconds: TimeInterval = 2.0

    /// Pages whose handwriting/images are currently being read by `OnPhoneAI`.
    static private(set) var reading: Set<String> = []

    @discardableResult
    static func save(
        noteId: String?,
        typed rawTyped: String,
        rich: NSAttributedString?,
        drawing: PKDrawing,
        inkChanged: Bool,
        paperTemplate: String? = nil,
        triggerOCR: Bool = true,
        quickNotes: QuickNotes
    ) -> String? {
        let typed = rawTyped.trimmingCharacters(in: .whitespacesAndNewlines)
        let ink = !drawing.strokes.isEmpty
        let attachments = rich.map(PageStore.attachments) ?? []
        let pasted = !attachments.isEmpty
        if noteId == nil, typed.isEmpty, !ink, !pasted { return nil }
        let before = noteId.map(PageStore.imageNames) ?? []
        let imagesChanged = attachments.map { ($0 as? PageImage)?.fileName } != before.map { Optional($0) }
        let id = quickNotes.savePage(
            id: noteId,
            typed: typed,
            inkChanged: inkChanged || imagesChanged,
            paperTemplate: paperTemplate
        )
        if inkChanged || noteId == nil || !ink { PageStore.save(drawing, id) }
        if let rich, pasted || !before.isEmpty { PageStore.saveRich(rich, id) }
        if triggerOCR {
            if (inkChanged || imagesChanged) && (ink || pasted) {
                read(id, drawing, quickNotes: quickNotes)
            } else if inkChanged || imagesChanged {
                quickNotes.pageRead(id, handwriting: "NONE")
            } else {
                quickNotes.pageReady(id)
            }
        }
        return id
    }

    /// Migrates any legacy `draft.json` once into a concrete per-`noteId` page, then clears it.
    @discardableResult
    static func saveDraft(quickNotes: QuickNotes) -> String? {
        guard let d = PageStore.readDraft() else { return nil }
        let rich = d.rich ?? NSAttributedString(string: d.draft.typed, attributes: PageStore.textAttributes)
        let id = save(noteId: d.draft.noteId, typed: d.draft.typed, rich: rich, drawing: d.drawing,
                      inkChanged: d.draft.inkChanged, quickNotes: quickNotes)
        PageStore.clearDraft()
        return id
    }

    /// Safe compatible entry point: migrates any legacy `draft.json` once if present, else no-op.
    static func recoverDraft(quickNotes: QuickNotes, except: String?? = .none) {
        guard let d = PageStore.readDraft() else { return }
        if let except, d.draft.noteId == except { return }
        Log.add("notebook: migrated legacy page draft (\(d.draft.noteId.map { "page " + $0.prefix(6) } ?? "new page"))")
        saveDraft(quickNotes: quickNotes)
    }

    static func recoverIfClosed(quickNotes: QuickNotes) {
        if PageView.open == nil { recoverDraft(quickNotes: quickNotes) }
    }

    /// Newer ink that arrived while the page was being read: read once more after.
    static private var rereadPending: [String: (drawing: PKDrawing, offerReminders: Bool)] = [:]
    /// Why the last reading of a page failed (shown on the page's insight card).
    static private(set) var readProblems: [String: String] = [:]

    /// Routes handwriting + pasted-image OCR through `OnPhoneAI.shared.readHandwriting`
    /// (Claude first when its key is on the phone, Gemini as backup), then the summary.
    /// One reading per page at a time; newer ink meanwhile is read right after.
    /// Runs under a background task, so leaving the app mid-read still lands it.
    static func read(
        _ id: String,
        _ drawing: PKDrawing,
        quickNotes: QuickNotes,
        offerReminders: Bool = true,
        completion: ((Result<String, MacLink.Failure>) -> Void)? = nil
    ) {
        let hasStrokes = !drawing.strokes.isEmpty
        let imageNames = PageStore.imageNames(id)
        guard hasStrokes || !imageNames.isEmpty else {
            quickNotes.pageRead(id, handwriting: "NONE", offerReminders: offerReminders)
            completion?(.failure(.empty))
            return
        }
        guard !reading.contains(id) else {
            let earlier = rereadPending[id]?.offerReminders ?? false
            rereadPending[id] = (drawing, offerReminders || earlier)
            Log.add("notebook: page \(id.prefix(6)) changed while being read; reading again after")
            completion?(.failure(.empty))
            return
        }
        guard OnPhoneAI.shared.visionConfigured else {
            Log.add("notebook: handwriting not read: no Gemini or Claude key on this phone")
            readProblems[id] = "Handwriting is not read: no Claude or Gemini key on this phone."
            quickNotes.pageReady(id, offerReminders: offerReminders)
            NotificationCenter.default.post(name: readingChangedNotification, object: id)
            completion?(.failure(.notConfigured))
            return
        }
        reading.insert(id)
        readProblems[id] = nil
        NotificationCenter.default.post(name: readingChangedNotification, object: id)
        let hold = BackgroundHold("JOAT read page")
        let pagesDir = PageStore.directory

        // Rasterize ink PNG tiles and scale pasted JPEGs off the main actor so
        // closing or navigating away from a page never stalls UI animations.
        Task.detached(priority: .userInitiated) {
            let tiles = PageStore.pngTiles(drawing)
            let pasted = imageNames.prefix(10)
                .compactMap { UIImage(contentsOfFile: pagesDir.appendingPathComponent($0).path) }
                .compactMap(PageStore.jpegForClaude)
            await MainActor.run {
                guard !(tiles.isEmpty && pasted.isEmpty) else {
                    reading.remove(id)
                    quickNotes.pageRead(id, handwriting: "NONE", offerReminders: offerReminders)
                    NotificationCenter.default.post(name: readingChangedNotification, object: id)
                    completion?(.failure(.empty))
                    hold.end(after: quickNotes.isSummarizing(id) ? 20 : 0)
                    return
                }
                Log.add("notebook: reading page \(id.prefix(6)), \(tiles.count) ink image\(tiles.count == 1 ? "" : "s"), "
                    + "\(pasted.count) pasted")
                OnPhoneAI.shared.readHandwriting(tiles, pasted: Array(pasted)) { result in
                    Task { @MainActor in
                        reading.remove(id)
                        let again = rereadPending.removeValue(forKey: id)
                        switch result {
                        case .success(let text):
                            // Newer ink is read next: summarize once, after that.
                            quickNotes.pageRead(id, handwriting: text, summarize: again == nil, offerReminders: offerReminders)
                        case .failure(let f):
                            Log.add("notebook: handwriting not read: \(MacLink.explain(f))")
                            readProblems[id] = "Handwriting not read: \(MacLink.explain(f))"
                            if again == nil { quickNotes.pageReady(id, offerReminders: offerReminders) }
                        }
                        NotificationCenter.default.post(name: readingChangedNotification, object: id)
                        completion?(result)
                        if let again {
                            read(id, again.drawing, quickNotes: quickNotes, offerReminders: again.offerReminders || offerReminders)
                        }
                        // Let the summary that follows the reading land too.
                        hold.end(after: quickNotes.isSummarizing(id) ? 20 : 0)
                    }
                }
            }
        }
    }
}

/// Keeps JOAT Brain running briefly in the background so a page read (and its
/// summary) started as the wearer leaves the app is not dropped.
@MainActor
final class BackgroundHold {
    private var task: UIBackgroundTaskIdentifier = .invalid

    init(_ name: String) {
        task = UIApplication.shared.beginBackgroundTask(withName: name) { [weak self] in
            MainActor.assumeIsolated { self?.end(after: 0) }
        }
    }

    func end(after seconds: TimeInterval) {
        guard task != .invalid else { return }
        if seconds > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [self] in end(after: 0) }
            return
        }
        UIApplication.shared.endBackgroundTask(task)
        task = .invalid
    }
}

// MARK: - The Page Canvas (`UIView`)

final class PageView: UIView, UITextViewDelegate, PKCanvasViewDelegate, PKToolPickerObserver {
    /// Fixed US Letter sheet: 8.5 × 11 in at 72 pt/in (`612 × 792 pt`).
    static let sheet = CGSize(width: 612, height: 792)
    static weak var open: PageView?
    private static let pages = NSHashTable<PageView>.weakObjects()
    private static let pickers = NSHashTable<PKToolPicker>.weakObjects()
    static let launched: Date = {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0 else { return Date() }
        let t = info.kp_proc.p_starttime
        return Date(timeIntervalSince1970: Double(t.tv_sec) + Double(t.tv_usec) / 1_000_000)
    }()

    private let textScroll = UIScrollView()
    let textView = PastingTextView()
    let canvas = PKCanvasView()
    private let sheetLines = SheetLines()
    private let toolPicker = PKToolPicker()
    private lazy var textScrollDelegate = TextScrollDelegate(page: self)

    /// Concrete per-page identifier used for immediate continuous persistence.
    let pageId: String
    /// Legacy optional accessor (`pageId` is always non-empty).
    var noteId: String? { pageId }
    let pageWidth: CGFloat

    private var fitScale: CGFloat = 0
    private var saveDirty = false
    private var ocrDirty = false
    private var typedSummaryDirty = false
    private var edited = false
    private var settledClose = false
    private var saveTimer: Timer?
    private weak var quickNotesOverride: QuickNotes?
    private var undoObservers: [NSObjectProtocol] = []

    var shouldCleanupEmptyOnClose = false
    var userRenamedTitle = false
    var onUndoStateChange: ((_ canUndo: Bool, _ canRedo: Bool) -> Void)?

    var template: PaperTemplate {
        get { sheetLines.template }
        set {
            guard sheetLines.template != newValue else { return }
            sheetLines.template = newValue
        }
    }

    var rich: NSAttributedString { textView.attributedText ?? NSAttributedString() }
    var writing = true {
        didSet {
            if writing != oldValue {
                applyMode()
                notifyUndoState()
            }
        }
    }

    private var pendingDrawing: PKDrawing?
    private var settingDrawing = false
    private var pageSize = CGSize.zero
    private var following = false
    private var lastInk: PKToolPickerItem?
    private var lastAttachments: [ObjectIdentifier]?

    init(
        noteId: String?,
        typed: String,
        rich: NSAttributedString?,
        drawing: PKDrawing,
        template: PaperTemplate = .ruled,
        inkChanged: Bool = false,
        unsaved: Bool = false,
        quickNotes: QuickNotes? = nil
    ) {
        let resolvedId = noteId ?? UUID().uuidString.lowercased()
        self.pageId = resolvedId
        self.quickNotesOverride = quickNotes
        let inkEdge = drawing.strokes.isEmpty ? 0 : drawing.bounds.maxX
        pageWidth = max(Self.sheet.width, ceil(inkEdge + 24))
        self.ocrDirty = inkChanged
        super.init(frame: .zero)
        Self.open = self
        (quickNotes ?? JOATRuntime.shared.brain?.quickNotes)?.beginEditingPage(resolvedId)

        // Stationery always renders in light appearance so ink colors and swatch
        // colors stay faithful even when iOS is in Dark Mode.
        overrideUserInterfaceStyle = .light
        toolPicker.colorUserInterfaceStyle = .light
        backgroundColor = UIColor(red: 0.92, green: 0.91, blue: 0.89, alpha: 1.0)

        for scroller in [textScroll, canvas] as [UIScrollView] {
            scroller.alwaysBounceVertical = true
            scroller.isDirectionalLockEnabled = true
        }
        textScroll.keyboardDismissMode = .interactive
        textScroll.delegate = textScrollDelegate

        textView.font = .preferredFont(forTextStyle: .body)
        textView.adjustsFontForContentSizeCategory = true
        if let rich { textView.attributedText = rich } else { textView.text = typed }
        textView.typingAttributes = PageStore.textAttributes
        textView.onPasteImages = { [weak self] in self?.insert(images: $0) }
        textView.acceptImages()
        lastAttachments = PageStore.attachments(textView.attributedText ?? NSAttributedString()).map(ObjectIdentifier.init)
        textView.isScrollEnabled = false
        textView.backgroundColor = .clear
        textView.textContainerInset = UIEdgeInsets(top: 20, left: 16, bottom: 20, right: 16)
        textView.delegate = self

        sheetLines.template = template
        textView.insertSubview(sheetLines, at: 0)

        pendingDrawing = drawing.strokes.isEmpty ? nil : drawing
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = .default
        canvas.delegate = self

        addSubview(textScroll)
        textScroll.addSubview(textView)
        addSubview(canvas)

        toolPicker.addObserver(canvas)
        toolPicker.addObserver(self)
        toolPicker.setVisible(true, forFirstResponder: canvas)

        if !(toolPicker.selectedToolItem is PKToolPickerInkingItem),
           let pen = toolPicker.toolItems.first(where: { $0 is PKToolPickerInkingItem }) {
            toolPicker.selectedToolItem = pen
        }
        if toolPicker.selectedToolItem is PKToolPickerInkingItem { lastInk = toolPicker.selectedToolItem }

        Self.pages.add(self)
        Self.pickers.add(toolPicker)
        let up = Int(Date().timeIntervalSince(Self.launched))
        Log.add("notebook: page \(resolvedId.prefix(6)) opened (\(template.rawValue)); \(Self.pages.allObjects.count) page views alive; "
            + "up \(up / 3600) h \(up / 60 % 60) min")

        for name in [
            UIApplication.willResignActiveNotification,
            UIApplication.didEnterBackgroundNotification,
            UIScene.didDisconnectNotification,
        ] {
            NotificationCenter.default.addObserver(self, selector: #selector(handleAppBackgrounding), name: name, object: nil)
        }

        for undoName in [
            Notification.Name.NSUndoManagerDidCloseUndoGroup,
            Notification.Name.NSUndoManagerDidUndoChange,
            Notification.Name.NSUndoManagerDidRedoChange,
        ] {
            let token = NotificationCenter.default.addObserver(forName: undoName, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.notifyUndoState() }
            }
            undoObservers.append(token)
        }

        if unsaved {
            markChanged(ink: !drawing.strokes.isEmpty)
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        if window != nil {
            settledClose = false
            (quickNotesOverride ?? JOATRuntime.shared.brain?.quickNotes)?.beginEditingPage(pageId)
            DispatchQueue.main.async { [weak self] in
                self?.applyMode()
                self?.notifyUndoState()
            }
        } else {
            flushAutoSave(triggerOCR: false, quiet: true)
        }
    }

    // MARK: - Undo / Redo

    var activeUndoManager: UndoManager? {
        writing ? canvas.undoManager : textView.undoManager
    }

    var canUndo: Bool { activeUndoManager?.canUndo ?? false }
    var canRedo: Bool { activeUndoManager?.canRedo ?? false }

    func performUndo() {
        guard let um = activeUndoManager, um.canUndo else { return }
        um.undo()
        markChanged(ink: writing)
        setNeedsLayout()
        notifyUndoState()
    }

    func performRedo() {
        guard let um = activeUndoManager, um.canRedo else { return }
        um.redo()
        markChanged(ink: writing)
        setNeedsLayout()
        notifyUndoState()
    }

    func notifyUndoState() {
        let u = canUndo
        let r = canRedo
        onUndoStateChange?(u, r)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.onUndoStateChange?(self.canUndo, self.canRedo)
        }
    }

    // MARK: - Continuous Per-Page Auto-Save & Post-Close OCR/Summary

    var typedText: String { textView.text.replacingOccurrences(of: "\u{FFFC}", with: "") }

    var currentDrawing: PKDrawing { pendingDrawing ?? canvas.drawing }

    var hasAnyContent: Bool {
        !typedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !currentDrawing.strokes.isEmpty
            || !PageStore.attachments(rich).isEmpty
    }

    private func markChanged(ink: Bool = false, imageChanged: Bool = false) {
        settledClose = false
        saveDirty = true
        edited = true
        if ink || imageChanged {
            ocrDirty = true
        } else {
            typedSummaryDirty = true
        }

        // Only persist locally while the page is open — OCR and AI summary run
        // once the wearer closes or leaves the modified page.
        saveTimer?.invalidate()
        saveTimer = Timer.scheduledTimer(withTimeInterval: Notebook.autoSaveDebounceSeconds, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated {
                _ = self?.flushAutoSave(triggerOCR: false, quiet: true)
            }
        }
    }

    /// Leaving the app mid-page: save, and read/summarize what changed now
    /// (under a background task) instead of dropping it.
    @objc private func handleAppBackgrounding() {
        flushAutoSave(triggerOCR: ocrDirty || typedSummaryDirty, offerReminders: false, quiet: false)
    }

    @discardableResult
    func flushAutoSave(
        quickNotes explicitQuickNotes: QuickNotes? = nil,
        triggerOCR: Bool = false,
        offerReminders: Bool = true,
        quiet: Bool = false
    ) -> String? {
        saveTimer?.invalidate()
        saveTimer = nil

        let drawingNow = currentDrawing
        let richNow = rich
        let cleanTyped = typedText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachments = PageStore.attachments(richNow)
        let qn = explicitQuickNotes ?? quickNotesOverride ?? JOATRuntime.shared.brain?.quickNotes

        if saveDirty {
            if quiet {
                PageStore.saveAsync(drawingNow, pageId)
            } else {
                PageStore.save(drawingNow, pageId)
            }
            PageStore.saveRich(richNow, pageId, draft: false)
            if let qn {
                qn.savePage(
                    id: pageId,
                    typed: cleanTyped,
                    inkChanged: ocrDirty,
                    paperTemplate: template.rawValue,
                    quiet: quiet
                )
            } else {
                PageStore.writeDraft(
                    noteId: pageId,
                    typed: cleanTyped,
                    rich: richNow,
                    drawing: drawingNow,
                    inkChanged: ocrDirty
                )
            }
            saveDirty = false
        }

        if triggerOCR, let qn {
            if ocrDirty {
                ocrDirty = false
                typedSummaryDirty = false
                if !drawingNow.strokes.isEmpty || !attachments.isEmpty {
                    Notebook.read(pageId, drawingNow, quickNotes: qn, offerReminders: offerReminders)
                } else {
                    // The ink was erased: drop its old reading, summarize what is left.
                    qn.pageRead(pageId, handwriting: "NONE", offerReminders: offerReminders)
                }
            } else if typedSummaryDirty {
                typedSummaryDirty = false
                qn.pageReady(pageId, offerReminders: offerReminders)
            }
        }

        return pageId
    }

    /// Settles the page on exit (`Done`, `onDisappear`, or switching pages in `NavigationSplitView`):
    /// flushes pending edits, triggers post-close OCR + AI summary if modified, or removes an
    /// untouched brand-new blank page.
    @discardableResult
    func closeAndSettle(quickNotes explicitQuickNotes: QuickNotes? = nil) -> String? {
        let qn = explicitQuickNotes ?? quickNotesOverride ?? JOATRuntime.shared.brain?.quickNotes
        qn?.endEditingPage(pageId)
        guard !settledClose else { return hasAnyContent ? pageId : nil }
        settledClose = true
        saveTimer?.invalidate()
        saveTimer = nil

        if !hasAnyContent && shouldCleanupEmptyOnClose && !userRenamedTitle {
            saveDirty = false
            ocrDirty = false
            typedSummaryDirty = false
            PageStore.clearDraft()
            qn?.deleteEmptyPageIfUntouched(pageId)
            return nil
        }

        PageStore.clearDraft()
        let result = flushAutoSave(quickNotes: qn, triggerOCR: true, quiet: false)
        qn?.onStoreChange?()
        qn?.publishPanel?()
        return result
    }

    /// Backwards-compatible explicit save entry point.
    func save(quickNotes: QuickNotes) -> String? {
        closeAndSettle(quickNotes: quickNotes)
    }

    func writeDraft() {
        flushAutoSave(triggerOCR: false, quiet: false)
    }

    func discardDraft() {
        saveTimer?.invalidate()
        saveTimer = nil
        saveDirty = false
        ocrDirty = false
        typedSummaryDirty = false
        (quickNotesOverride ?? JOATRuntime.shared.brain?.quickNotes)?.endEditingPage(pageId)
        PageStore.clearDraft()
    }

    private func applyMode() {
        guard window != nil else { return }
        canvas.isUserInteractionEnabled = writing
        let fingerDraws = !UIPencilInteraction.prefersPencilOnlyDrawing
        if writing {
            textView.resignFirstResponder()
            canvas.becomeFirstResponder()
        } else {
            canvas.resignFirstResponder()
            textView.becomeFirstResponder()
        }
        Log.add("notebook: \(writing ? "write" : "type") mode (canvas first: \(canvas.isFirstResponder), "
            + "Pencil only: \(!fingerDraws), tools shown: \(toolPicker.isVisible))")
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        textScroll.frame = bounds
        canvas.frame = bounds
        guard bounds.width > 0 else { return }
        fitImages()
        let textHeight = textView.sizeThatFits(CGSize(width: pageWidth, height: .greatestFiniteMagnitude)).height
        let drawing = currentDrawing
        let ink = drawing.strokes.isEmpty ? 0 : drawing.bounds.maxY
        let sheets = max(1, Int(ceil((max(textHeight, ink) + 160) / Self.sheet.height)))
        let size = CGSize(width: pageWidth, height: CGFloat(sheets) * Self.sheet.height)
        let fit = bounds.width / pageWidth
        let refit = abs(fit - fitScale) > 0.0001
        guard size != pageSize || refit else { centrePage(); return }
        pageSize = size
        following = true
        let offset = refit ? zoomToFit(fit) : nil
        let zoom = canvas.zoomScale
        let zoomed = CGSize(width: size.width * zoom, height: size.height * zoom)
        textView.bounds = CGRect(origin: .zero, size: size)
        textView.center = CGPoint(x: zoomed.width / 2, y: zoomed.height / 2)
        sheetLines.frame = CGRect(origin: .zero, size: size)
        sheetLines.sheets = sheets
        textScroll.contentSize = zoomed
        canvas.contentSize = zoomed
        centrePage()
        if let offset {
            let inset = canvas.adjustedContentInset
            let most = CGPoint(x: max(-inset.left, zoomed.width - bounds.width + inset.right),
                               y: max(-inset.top, zoomed.height - bounds.height + inset.bottom))
            let shown = CGPoint(x: min(max(offset.x, -inset.left), most.x), y: min(max(offset.y, -inset.top), most.y))
            canvas.contentOffset = shown
            textScroll.contentOffset = shown
        }
        following = false
        if let pending = pendingDrawing {
            pendingDrawing = nil
            settingDrawing = true
            canvas.drawing = pending
            settingDrawing = false
            notifyUndoState()
        }
        if refit { sharpenText() }
    }

    /// Width-fit on first show; pinch goes out to half of that (the page
    /// centred) and in to at least 4× (NotesInsights.zoomLimits).
    private func zoomToFit(_ fit: CGFloat) -> CGPoint {
        let relative = fitScale > 0 ? canvas.zoomScale / fitScale : 1
        let before = canvas.zoomScale
        let offset = canvas.contentOffset
        fitScale = fit
        let limits = NotesInsights.zoomLimits(fit: Double(fit))
        let zoom = CGFloat(NotesInsights.clampZoom(Double(fit * relative), fit: Double(fit)))
        for scroller in [textScroll, canvas] as [UIScrollView] {
            scroller.minimumZoomScale = CGFloat(limits.min)
            scroller.maximumZoomScale = CGFloat(limits.max)
            scroller.bouncesZoom = true
            scroller.zoomScale = zoom
        }
        let ratio = before > 0 ? zoom / before : 1
        Log.add("notebook: page \(Int(pageWidth)) pt wide on a \(Int(bounds.width)) pt screen, zoom \(String(format: "%.2f", zoom))")
        return CGPoint(x: offset.x * ratio, y: offset.y * ratio)
    }

    private func fitImages() {
        let inset = textView.textContainerInset
        let room = max(40, pageWidth - inset.left - inset.right - 2 * textView.textContainer.lineFragmentPadding)
        let storage = textView.textStorage
        var resized: [(NSTextAttachment, NSRange)] = []
        storage.enumerateAttribute(.attachment, in: NSRange(location: 0, length: storage.length)) { value, range, _ in
            guard let a = value as? NSTextAttachment, let image = a.image, image.size.width > 0 else { return }
            let w = min(image.size.width, room).rounded(.down)
            let size = CGSize(width: w, height: (image.size.height * w / image.size.width).rounded(.down))
            if a.bounds.size != size {
                a.bounds = CGRect(origin: .zero, size: size)
                resized.append((a, range))
            }
        }
        guard !resized.isEmpty else { return }
        storage.beginEditing()
        for (a, range) in resized { storage.addAttribute(.attachment, value: a, range: range) }
        storage.endEditing()
    }

    fileprivate func follow(_ moved: UIScrollView) {
        guard !following else { return }
        following = true
        let other: UIScrollView = moved === canvas ? textScroll : canvas
        if other.zoomScale != moved.zoomScale {
            other.zoomScale = moved.zoomScale
            if other === canvas {
                canvas.contentSize = CGSize(width: pageSize.width * moved.zoomScale, height: pageSize.height * moved.zoomScale)
            }
            centrePage()
        }
        other.contentOffset = moved.contentOffset
        following = false
    }

    /// Room under the tool picker, kept apart from the centring insets.
    private var toolInsetBottom: CGFloat = 0

    /// A page zoomed out smaller than the screen sits in its middle; both
    /// scroll views get the same insets so ink and text stay aligned.
    private func centrePage() {
        guard pageSize.width > 0, bounds.width > 0 else { return }
        let zoom = canvas.zoomScale
        let c = NotesInsights.centringInset(content: (Double(pageSize.width * zoom), Double(pageSize.height * zoom)),
                                            bounds: (Double(bounds.width), Double(bounds.height - toolInsetBottom)))
        let inset = UIEdgeInsets(top: CGFloat(c.top), left: CGFloat(c.left),
                                 bottom: CGFloat(c.top) + toolInsetBottom, right: CGFloat(c.left))
        for scroller in [textScroll, canvas] as [UIScrollView] where scroller.contentInset != inset {
            scroller.contentInset = inset
            scroller.verticalScrollIndicatorInsets.bottom = toolInsetBottom
        }
    }

    #if DEBUG
    /// Simulator screenshots (no pinch): zoom to `factor` × width-fit.
    func debugZoom(_ factor: CGFloat) {
        guard fitScale > 0 else { return }
        let zoom = CGFloat(NotesInsights.clampZoom(Double(fitScale * factor), fit: Double(fitScale)))
        following = true
        canvas.zoomScale = zoom
        textScroll.zoomScale = zoom
        canvas.contentSize = CGSize(width: pageSize.width * zoom, height: pageSize.height * zoom)
        following = false
        centrePage()
        let start = CGPoint(x: -canvas.contentInset.left, y: -canvas.contentInset.top)
        canvas.contentOffset = start
        textScroll.contentOffset = start
        sharpenText()
        Log.add("debug: page zoom \(String(format: "%.2f", zoom)) (fit \(String(format: "%.2f", fitScale)))")
    }
    #endif

    fileprivate func sharpenText() {
        let scale = traitCollection.displayScale * min(max(textScroll.zoomScale, 0.5), 2.5)
        func apply(_ view: UIView) {
            if view is SheetLines { return }
            view.contentScaleFactor = scale
            view.setNeedsDisplay()
            view.subviews.forEach(apply)
        }
        apply(textView)
    }

    deinit {
        for token in undoObservers {
            NotificationCenter.default.removeObserver(token)
        }
        saveTimer?.invalidate()
        let pid = pageId
        let qnRef = quickNotesOverride
        let wasSaveDirty = saveDirty
        let wasOcrDirty = ocrDirty
        let wasTypedDirty = typedSummaryDirty
        let wasSettled = settledClose
        let cleanupEmpty = shouldCleanupEmptyOnClose && !userRenamedTitle
        var capturedTyped = ""
        var capturedDrawing: PKDrawing?
        var capturedRich: NSAttributedString?
        var capturedTemplate = PaperTemplate.ruled.rawValue
        var capturedHasContent = true
        if Thread.isMainThread, !wasSettled {
            capturedTyped = typedText.trimmingCharacters(in: .whitespacesAndNewlines)
            let d = pendingDrawing ?? canvas.drawing
            let r = textView.attributedText ?? NSAttributedString()
            capturedDrawing = d
            capturedRich = r
            capturedTemplate = sheetLines.template.rawValue
            capturedHasContent = !capturedTyped.isEmpty || !d.strokes.isEmpty || !PageStore.attachments(r).isEmpty
        }
        Log.add("notebook: page view \(pid.prefix(6)) closed")
        guard !wasSettled else {
            DispatchQueue.main.async {
                (qnRef ?? JOATRuntime.shared.brain?.quickNotes)?.endEditingPage(pid)
            }
            return
        }
        DispatchQueue.main.async {
            guard let qn = qnRef ?? JOATRuntime.shared.brain?.quickNotes else { return }
            qn.endEditingPage(pid)
            if !capturedHasContent && cleanupEmpty {
                qn.deleteEmptyPageIfUntouched(pid)
                return
            }
            if wasSaveDirty, let d = capturedDrawing, let r = capturedRich {
                PageStore.save(d, pid)
                PageStore.saveRich(r, pid, draft: false)
                qn.savePage(id: pid, typed: capturedTyped, inkChanged: wasOcrDirty, paperTemplate: capturedTemplate)
            }
            if wasOcrDirty, let d = capturedDrawing {
                if !d.strokes.isEmpty || !PageStore.imageNames(pid).isEmpty {
                    Notebook.read(pid, d, quickNotes: qn)
                } else {
                    qn.pageRead(pid, handwriting: "NONE")
                }
            } else if wasTypedDirty {
                qn.pageReady(pid)
            }
        }
    }

    #if DEBUG
    func debugSelectEraser() {
        guard let eraser = toolPicker.toolItems.first(where: { $0 is PKToolPickerEraserItem }) else { return }
        toolPicker.selectedToolItem = eraser
        Log.add("debug: eraser picked")
    }
    #endif

    func textViewDidChange(_ textView: UITextView) {
        // Only a pasted/removed image re-reads the page; typing next to one just re-summarizes.
        let shown = textView.attributedText.map { PageStore.attachments($0).map(ObjectIdentifier.init) } ?? []
        let hasAttachmentChange = lastAttachments.map { $0 != shown } ?? !shown.isEmpty
        lastAttachments = shown
        markChanged(ink: false, imageChanged: hasAttachmentChange)
        setNeedsLayout()
        notifyUndoState()
    }

    func insert(images: [UIImage], text: String = "") {
        guard !images.isEmpty || !text.isEmpty else { return }
        let page = NSMutableAttributedString(attributedString: rich)
        var range = writing ? NSRange(location: page.length, length: 0) : textView.selectedRange
        if range.location == NSNotFound || NSMaxRange(range) > page.length { range = NSRange(location: page.length, length: 0) }
        let attributes = PageStore.textAttributes
        let piece = NSMutableAttributedString()
        func newline() { piece.append(NSAttributedString(string: "\n", attributes: attributes)) }
        if !images.isEmpty, range.location > 0, (page.string as NSString).character(at: range.location - 1) != 10 { newline() }
        for image in images {
            piece.append(NSAttributedString(attachment: PageImage(image: PageStore.scaled(image, longEdge: 2048))))
            newline()
        }
        if !text.isEmpty { piece.append(NSAttributedString(string: text, attributes: attributes)) }
        page.replaceCharacters(in: range, with: piece)
        textView.attributedText = page
        fitImages()
        lastAttachments = PageStore.attachments(textView.attributedText ?? page).map(ObjectIdentifier.init)
        textView.typingAttributes = attributes
        textView.selectedRange = NSRange(location: range.location + piece.length, length: 0)
        markChanged(ink: false, imageChanged: !images.isEmpty)
        setNeedsLayout()
        notifyUndoState()
        Log.add("notebook: inserted \(images.count) image\(images.count == 1 ? "" : "s")"
            + (text.isEmpty ? "" : " and \(text.count) characters"))
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.layoutIfNeeded()
            self.textView.layoutIfNeeded()
            let end = self.textView.caretRect(for: self.textView.endOfDocument)
            let shown = self.textView.convert(end, to: self.textScroll)
            self.textScroll.scrollRectToVisible(shown.insetBy(dx: 0, dy: -24), animated: true)
        }
    }

    func pasteItems(_ providers: [NSItemProvider]) {
        final class Found: @unchecked Sendable {
            let lock = NSLock()
            var items: [PageStore.Pasted?]
            init(_ count: Int) { items = Array(repeating: nil, count: count) }
        }
        let found = Found(providers.count)
        let group = DispatchGroup()
        for (i, provider) in providers.enumerated() {
            group.enter()
            PageStore.load(provider) { item in
                found.lock.lock()
                found.items[i] = item
                found.lock.unlock()
                group.leave()
            }
        }
        group.notify(queue: .main) { [weak self] in
            MainActor.assumeIsolated {
                var images: [UIImage] = []
                var words: [String] = []
                for item in found.items {
                    switch item {
                    case .image(let image): images.append(image)
                    case .text(let text): words.append(text)
                    case nil: break
                    }
                }
                if images.isEmpty, words.isEmpty {
                    let board = UIPasteboard.general
                    if board.hasImages { images = board.images ?? [] } else if board.hasStrings, let text = board.string { words = [text] }
                }
                self?.insert(images: images, text: words.joined(separator: "\n"))
            }
        }
    }

    func textViewDidChangeSelection(_ textView: UITextView) {
        guard textView.isFirstResponder, let range = textView.selectedTextRange else { return }
        let caret = textView.convert(textView.caretRect(for: range.end), to: textScroll)
        textScroll.scrollRectToVisible(caret.insetBy(dx: 0, dy: -24), animated: true)
    }

    func scrollViewDidScroll(_ scrollView: UIScrollView) { follow(scrollView) }
    func scrollViewDidZoom(_ scrollView: UIScrollView) { follow(scrollView) }
    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        sharpenText()
    }

    func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
        guard !settingDrawing else { return }
        markChanged(ink: true)
        setNeedsLayout()
        notifyUndoState()
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        notifyUndoState()
        guard toolPicker.selectedToolItem is PKToolPickerEraserItem, let ink = lastInk else { return }
        toolPicker.selectedToolItem = ink
        Log.add("notebook: eraser lifted, back to \(ink.identifier)")
    }

    func toolPickerSelectedToolItemDidChange(_ toolPicker: PKToolPicker) {
        if toolPicker.selectedToolItem is PKToolPickerInkingItem { lastInk = toolPicker.selectedToolItem }
    }

    func toolPickerFramesObscuredDidChange(_ toolPicker: PKToolPicker) {
        let obscured = toolPicker.frameObscured(in: self)
        toolInsetBottom = obscured.isNull ? 0 : max(0, bounds.maxY - obscured.minY)
        centrePage()
    }
}

private final class TextScrollDelegate: NSObject, UIScrollViewDelegate {
    weak var page: PageView?
    init(page: PageView) { self.page = page }
    func viewForZooming(in scrollView: UIScrollView) -> UIView? { page?.textView }
    func scrollViewDidScroll(_ scrollView: UIScrollView) { page?.follow(scrollView) }
    func scrollViewDidZoom(_ scrollView: UIScrollView) { page?.follow(scrollView) }
    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        page?.sharpenText()
    }
}

// MARK: - Warm Stationery & Crisp Vector Paper Templates (`SheetLines`)

final class SheetLines: UIView {
    var sheets = 1 {
        didSet { if sheets != oldValue { setNeedsLayout() } }
    }
    var template: PaperTemplate = .ruled {
        didSet { if template != oldValue { setNeedsLayout() } }
    }

    private let patternLayer = CAShapeLayer()
    private let marginLayer = CAShapeLayer()
    private let breaks = CAShapeLayer()
    private var numbers: [UILabel] = []

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = PageStore.stationeryColor
        isUserInteractionEnabled = false
        isAccessibilityElement = false

        patternLayer.fillColor = nil
        marginLayer.fillColor = nil
        breaks.fillColor = nil
        breaks.lineWidth = 1
        breaks.lineDashPattern = [6, 4]

        layer.addSublayer(patternLayer)
        layer.addSublayer(marginLayer)
        layer.addSublayer(breaks)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        let width = bounds.width
        let sheetHeight = PageView.sheet.height
        guard width > 0, sheets >= 1 else { return }

        patternLayer.frame = bounds
        marginLayer.frame = bounds
        breaks.frame = bounds

        // 1. Vector stationery pattern (`CAShapeLayer` only — zero bitmap backing store)
        switch template {
        case .ruled:
            let rulesPath = UIBezierPath()
            let ruleSpacing: CGFloat = 28
            let topMargin: CGFloat = 56
            let bottomMargin: CGFloat = 36
            for s in 0..<sheets {
                let baseY = CGFloat(s) * sheetHeight
                var y = topMargin
                while y <= sheetHeight - bottomMargin {
                    rulesPath.move(to: CGPoint(x: 0, y: baseY + y))
                    rulesPath.addLine(to: CGPoint(x: width, y: baseY + y))
                    y += ruleSpacing
                }
            }
            patternLayer.path = rulesPath.cgPath
            patternLayer.fillColor = nil
            patternLayer.strokeColor = UIColor(red: 0.66, green: 0.73, blue: 0.82, alpha: 0.52).cgColor
            patternLayer.lineWidth = 0.75

            let marginPath = UIBezierPath()
            marginPath.move(to: CGPoint(x: 52, y: 0))
            marginPath.addLine(to: CGPoint(x: 52, y: bounds.height))
            marginLayer.path = marginPath.cgPath
            marginLayer.strokeColor = UIColor(red: 0.84, green: 0.58, blue: 0.58, alpha: 0.38).cgColor
            marginLayer.lineWidth = 0.85

        case .dotGrid:
            let dotsPath = CGMutablePath()
            let step: CGFloat = 24
            let dotSize: CGFloat = 2.0
            let half = dotSize / 2
            for s in 0..<sheets {
                let baseY = CGFloat(s) * sheetHeight
                var y: CGFloat = step
                while y <= sheetHeight - 16 {
                    var x: CGFloat = step
                    while x <= width - 16 {
                        dotsPath.addEllipse(in: CGRect(x: x - half, y: baseY + y - half, width: dotSize, height: dotSize))
                        x += step
                    }
                    y += step
                }
            }
            patternLayer.path = dotsPath
            patternLayer.strokeColor = nil
            patternLayer.fillColor = UIColor(red: 0.56, green: 0.61, blue: 0.68, alpha: 0.52).cgColor
            marginLayer.path = nil

        case .squareGrid:
            let gridPath = UIBezierPath()
            let step: CGFloat = 24
            var x: CGFloat = step
            while x < width {
                gridPath.move(to: CGPoint(x: x, y: 0))
                gridPath.addLine(to: CGPoint(x: x, y: bounds.height))
                x += step
            }
            for s in 0..<sheets {
                let baseY = CGFloat(s) * sheetHeight
                var y: CGFloat = step
                while y < sheetHeight {
                    gridPath.move(to: CGPoint(x: 0, y: baseY + y))
                    gridPath.addLine(to: CGPoint(x: width, y: baseY + y))
                    y += step
                }
            }
            patternLayer.path = gridPath.cgPath
            patternLayer.fillColor = nil
            patternLayer.strokeColor = UIColor(red: 0.68, green: 0.74, blue: 0.80, alpha: 0.42).cgColor
            patternLayer.lineWidth = 0.6
            marginLayer.path = nil

        case .blank:
            patternLayer.path = nil
            marginLayer.path = nil
        }

        // 2. Dashed US Letter page-break dividers & page numbers
        let breakPath = UIBezierPath()
        for k in stride(from: 1, to: sheets, by: 1) {
            breakPath.move(to: CGPoint(x: 0, y: CGFloat(k) * sheetHeight))
            breakPath.addLine(to: CGPoint(x: width, y: CGFloat(k) * sheetHeight))
        }
        breaks.path = breakPath.cgPath
        breaks.strokeColor = UIColor(red: 0.60, green: 0.58, blue: 0.54, alpha: 0.45).cgColor

        while numbers.count < sheets {
            let label = UILabel()
            label.font = .preferredFont(forTextStyle: .caption2)
            label.textColor = UIColor(red: 0.48, green: 0.46, blue: 0.42, alpha: 0.85)
            addSubview(label)
            numbers.append(label)
        }
        for (i, label) in numbers.enumerated() {
            label.isHidden = sheets < 2 || i >= sheets
            label.text = "\(i + 1)"
            label.sizeToFit()
            label.frame.origin = CGPoint(
                x: width - label.frame.width - 16,
                y: CGFloat(i + 1) * sheetHeight - label.frame.height - 8
            )
        }
    }
}

// MARK: - SwiftUI Canvas Bridge (`PageCanvas`)

final class PageHandle {
    weak var view: PageView?
}

struct PageCanvas: UIViewRepresentable {
    let pageId: String
    let typed: String
    let drawing: PKDrawing
    let template: PaperTemplate
    let unsaved: Bool
    let shouldCleanupEmptyOnClose: Bool
    let userRenamedTitle: Bool
    let handle: PageHandle
    var writing: Bool
    let onUndoChange: (Bool, Bool) -> Void

    func makeUIView(context: Context) -> PageView {
        if let quickNotes = JOATRuntime.shared.brain?.quickNotes {
            Notebook.recoverDraft(quickNotes: quickNotes, except: .some(pageId))
        }
        let view: PageView
        if let d = PageStore.readDraft(), d.draft.noteId == pageId {
            view = PageView(
                noteId: pageId,
                typed: d.draft.typed,
                rich: d.rich,
                drawing: d.drawing,
                template: template,
                inkChanged: d.draft.inkChanged,
                unsaved: true
            )
            PageStore.clearDraft()
        } else {
            view = PageView(
                noteId: pageId,
                typed: typed,
                rich: PageStore.loadRich(pageId),
                drawing: drawing,
                template: template,
                unsaved: unsaved
            )
        }
        view.shouldCleanupEmptyOnClose = shouldCleanupEmptyOnClose
        view.userRenamedTitle = userRenamedTitle
        view.onUndoStateChange = onUndoChange
        handle.view = view
        view.writing = writing
        return view
    }

    func updateUIView(_ view: PageView, context: Context) {
        view.writing = writing
        view.template = template
        view.shouldCleanupEmptyOnClose = shouldCleanupEmptyOnClose
        view.userRenamedTitle = userRenamedTitle
        view.onUndoStateChange = onUndoChange
    }

    static func dismantleUIView(_ uiView: PageView, coordinator: ()) {
        MainActor.assumeIsolated {
            _ = uiView.closeAndSettle()
        }
    }
}

// MARK: - Apple Pencil & iPhone Duo Page Editor (`PageEditorView`)

struct PageEditorView: View {
    private let initialNoteId: String?
    private let embedded: Bool
    private let onDone: (String?) -> Void
    private let initialTyped: String
    private let initialDrawing: PKDrawing
    private let initialUnsaved: Bool
    private let wasInitiallyEmpty: Bool

    @State private var activeNoteId: String
    @State private var pageTitle: String
    @State private var titleDraft: String
    @State private var userRenamedTitle = false
    @State private var editingTitlePopover = false
    @State private var writing = true
    @State private var template: PaperTemplate
    @State private var canUndo = false
    @State private var canRedo = false
    @State private var handle = PageHandle()
    @State private var pickedPhotos: [PhotosPickerItem] = []
    @State private var showingInspector = false
    @State private var isReadingHandwriting = false
    @State private var debugWidth: CGFloat?
    @Environment(\.dismiss) private var dismiss

    init(
        noteId: String?,
        typed: String = "",
        drawing: PKDrawing = PKDrawing(),
        embedded: Bool = false,
        onDone: @escaping (String?) -> Void = { _ in }
    ) {
        self.initialNoteId = noteId
        self.embedded = embedded
        self.onDone = onDone

        let existingNote = noteId.flatMap { id in
            JOATRuntime.shared.brain?.quickNotes.savedNotes.first(where: { $0.id == id })
        }

        let resolvedTyped: String
        let resolvedDrawing: PKDrawing
        let resolvedUnsaved: Bool

        #if DEBUG
        let start = noteId == nil ? Notebook.debugStart : nil
        Notebook.debugStart = nil
        if let start {
            resolvedTyped = start.typed
            resolvedDrawing = start.drawing
            resolvedUnsaved = true
        } else {
            resolvedTyped = !typed.isEmpty ? typed : (existingNote?.typed ?? "")
            resolvedDrawing = !drawing.strokes.isEmpty ? drawing : (noteId.map(PageStore.load) ?? PKDrawing())
            resolvedUnsaved = false
        }
        #else
        resolvedTyped = !typed.isEmpty ? typed : (existingNote?.typed ?? "")
        resolvedDrawing = !drawing.strokes.isEmpty ? drawing : (noteId.map(PageStore.load) ?? PKDrawing())
        resolvedUnsaved = false
        #endif

        self.initialTyped = resolvedTyped
        self.initialDrawing = resolvedDrawing
        self.initialUnsaved = resolvedUnsaved

        let mintedId = noteId ?? UUID().uuidString.lowercased()
        _activeNoteId = State(initialValue: mintedId)

        let defaultTitle = existingNote.flatMap { $0.title.isEmpty ? nil : $0.title }
            ?? QuickNotes.pageClockTitle()
        _pageTitle = State(initialValue: defaultTitle)
        _titleDraft = State(initialValue: defaultTitle)

        let resolvedTemplate = PaperTemplate.resolve(
            existingNote?.paperTemplate,
            isLegacySaved: existingNote != nil && existingNote?.paperTemplate == nil
        )
        _template = State(initialValue: resolvedTemplate)

        let hasExistingImages = noteId.map { !PageStore.imageNames($0).isEmpty } ?? false
        let hasExistingParagraphs = !(existingNote?.paragraphs ?? []).isEmpty
        self.wasInitiallyEmpty = resolvedTyped.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && resolvedDrawing.strokes.isEmpty
            && !hasExistingImages
            && !hasExistingParagraphs
    }

    var body: some View {
        Group {
            if embedded {
                editorSurface
            } else {
                NavigationStack {
                    editorSurface
                }
            }
        }
        .sheet(isPresented: $showingInspector) {
            PageInspectorSheet(noteId: activeNoteId, handle: handle)
        }
        .onChange(of: pickedPhotos) { _, newItems in
            guard !newItems.isEmpty else { return }
            loadPickedPhotos(newItems)
        }
        .onAppear {
            isReadingHandwriting = Notebook.reading.contains(activeNoteId)
            if initialNoteId == nil, let qn = JOATRuntime.shared.brain?.quickNotes {
                qn.ensureBlankPage(id: activeNoteId, paperTemplate: template.rawValue)
            }
            refreshTitleFromStore()
        }
        .onDisappear {
            _ = handle.view?.closeAndSettle()
        }
        .onReceive(NotificationCenter.default.publisher(for: Notebook.readingChangedNotification)) { _ in
            isReadingHandwriting = Notebook.reading.contains(activeNoteId)
            refreshTitleFromStore()
        }
        #if DEBUG
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugDone)) { _ in closeModal() }
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugWidth)) { note in
            debugWidth = note.object as? CGFloat
        }
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugDrop)) { _ in
            if !embedded { dismiss() }
        }
        #endif
    }

    private var editorSurface: some View {
        VStack(spacing: 0) {
            stationeryControlStrip
            Divider()
            PageInsightCard(noteId: activeNoteId, handle: handle) {
                _ = handle.view?.flushAutoSave(triggerOCR: false)
                showingInspector = true
            }
            PageCanvas(
                pageId: activeNoteId,
                typed: initialTyped,
                drawing: initialDrawing,
                template: template,
                unsaved: initialUnsaved,
                shouldCleanupEmptyOnClose: wasInitiallyEmpty,
                userRenamedTitle: userRenamedTitle,
                handle: handle,
                writing: writing,
                onUndoChange: { u, r in
                    if canUndo != u { canUndo = u }
                    if canRedo != r { canRedo = r }
                }
            )
            .frame(maxWidth: debugWidth ?? .infinity, maxHeight: .infinity)
            .ignoresSafeArea(.container, edges: .bottom)
        }
        .navigationTitle(pageTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if !embedded {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { closeModal() }
                        .fontWeight(.semibold)
                }
            }
            ToolbarItem(placement: .principal) {
                Button {
                    titleDraft = pageTitle
                    editingTitlePopover = true
                } label: {
                    HStack(spacing: 5) {
                        Text(pageTitle)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Image(systemName: "pencil")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(.quaternary.opacity(0.5), in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Rename page \(pageTitle)")
                .popover(isPresented: $editingTitlePopover) {
                    renamePopoverContent
                        .presentationCompactAdaptation(.popover)
                }
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button {
                    handle.view?.performUndo()
                } label: {
                    Label("Undo", systemImage: "arrow.uturn.backward")
                }
                .disabled(!canUndo)

                Button {
                    handle.view?.performRedo()
                } label: {
                    Label("Redo", systemImage: "arrow.uturn.forward")
                }
                .disabled(!canRedo)

                Button {
                    _ = handle.view?.flushAutoSave(triggerOCR: false)
                    showingInspector = true
                } label: {
                    Label("AI Insights & Prep", systemImage: isReadingHandwriting ? "sparkles.rectangle.stack" : "sparkles")
                        .symbolEffect(.pulse, isActive: isReadingHandwriting)
                }
            }
        }
    }

    private var stationeryControlStrip: some View {
        HStack(spacing: 10) {
            Picker("Mode", selection: $writing) {
                Text("Write").tag(true)
                Text("Type").tag(false)
            }
            .pickerStyle(.segmented)
            .frame(width: 138)

            Spacer(minLength: 4)

            Menu {
                Section("Paper Template") {
                    ForEach(PaperTemplate.allCases) { option in
                        Button {
                            applyTemplate(option)
                        } label: {
                            Label(
                                option.title,
                                systemImage: template == option ? "checkmark" : option.icon
                            )
                        }
                    }
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: template.icon)
                    Text(template.shortTitle)
                        .font(.subheadline.weight(.medium))
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(.quaternary.opacity(0.6), in: Capsule())
            }
            .accessibilityLabel("Paper template: \(template.title)")

            PhotosPicker(
                selection: $pickedPhotos,
                maxSelectionCount: 6,
                matching: .images
            ) {
                Image(systemName: "photo.badge.plus")
                    .font(.body.weight(.medium))
                    .padding(7)
                    .background(.quaternary.opacity(0.6), in: Circle())
            }
            .accessibilityLabel("Insert photo")

            PasteButton(supportedContentTypes: [.image, .plainText]) { providers in
                handle.view?.pasteItems(providers)
            }
            .labelStyle(.iconOnly)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(.ultraThinMaterial)
    }

    private var renamePopoverContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Page Title")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            TextField("Page title", text: $titleDraft)
                .textFieldStyle(.roundedBorder)
                .frame(minWidth: 230)
                .submitLabel(.done)
                .onSubmit { commitRename() }
            HStack {
                Button("Cancel") {
                    editingTitlePopover = false
                }
                .buttonStyle(.bordered)
                Spacer()
                Button("Save") {
                    commitRename()
                }
                .buttonStyle(.borderedProminent)
                .disabled(titleDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(14)
    }

    private func commitRename() {
        let cleaned = titleDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            editingTitlePopover = false
            return
        }
        let capped = String(cleaned.prefix(120))
        pageTitle = capped
        userRenamedTitle = true
        if let qn = JOATRuntime.shared.brain?.quickNotes {
            qn.ensureBlankPage(id: activeNoteId, paperTemplate: template.rawValue)
            qn.renameNote(activeNoteId, to: capped)
        }
        editingTitlePopover = false
    }

    private func applyTemplate(_ newTemplate: PaperTemplate) {
        template = newTemplate
        handle.view?.template = newTemplate
        if let qn = JOATRuntime.shared.brain?.quickNotes {
            qn.ensureBlankPage(id: activeNoteId, paperTemplate: newTemplate.rawValue)
            qn.setPaperTemplate(activeNoteId, template: newTemplate.rawValue)
        }
    }

    private func refreshTitleFromStore() {
        guard !userRenamedTitle,
              let stored = JOATRuntime.shared.brain?.quickNotes.savedNotes.first(where: { $0.id == activeNoteId }),
              !stored.title.isEmpty,
              stored.title != pageTitle else { return }
        pageTitle = stored.title
    }

    private func loadPickedPhotos(_ items: [PhotosPickerItem]) {
        Task { @MainActor in
            var loaded: [UIImage] = []
            for item in items {
                if let data = try? await item.loadTransferable(type: Data.self),
                   let image = UIImage(data: data) {
                    loaded.append(image)
                }
            }
            pickedPhotos.removeAll()
            if !loaded.isEmpty {
                handle.view?.insert(images: loaded)
            }
        }
    }

    private func closeModal() {
        let settledId: String?
        if let qn = JOATRuntime.shared.brain?.quickNotes {
            settledId = handle.view?.closeAndSettle(quickNotes: qn)
        } else {
            _ = handle.view?.flushAutoSave(triggerOCR: false)
            settledId = activeNoteId
        }
        onDone(settledId)
        dismiss()
    }
}

// MARK: - Inline Page Insights Card (summary, key points, todos)

/// The page's AI summary, key points and next steps, right above the paper
/// (collapsible), with what is happening while handwriting is read or the
/// page summarized, and a clear line when no AI key is set up.
struct PageInsightCard: View {
    let noteId: String
    let handle: PageHandle
    let openInspector: () -> Void

    @ObservedObject private var notesModel = JOATRuntime.shared.notes
    @ObservedObject private var ai = OnPhoneAI.shared
    @AppStorage("joat.notes.insightCardExpanded") private var expanded = true
    @State private var reading = false
    @State private var summarizing = false
    @State private var problem: String?

    private var quickNotes: QuickNotes? { JOATRuntime.shared.brain?.quickNotes }
    private var note: QuickNotes.VoiceNote? { notesModel.voice.first(where: { $0.id == noteId }) }

    private var status: NotesInsights.Status {
        let n = note
        let summary = (n?.summary ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let hasContent = !(n?.paragraphs ?? []).isEmpty || (handle.view?.hasAnyContent ?? false)
        return NotesInsights.status(reading: reading, summarizing: summarizing,
                                    configured: quickNotes?.insightsConfigured ?? ai.anyConfigured,
                                    hasSummary: !summary.isEmpty, stale: n?.summaryStale == true,
                                    hasContent: hasContent)
    }

    var body: some View {
        let n = note
        let summary = (n?.summary ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let points = n?.keyPoints ?? []
        let todos = n?.todos ?? []
        let hasInsights = !summary.isEmpty || !points.isEmpty || !todos.isEmpty
        let shouldShow = NotesInsights.shouldShowInlineEditorCard(
            hasSummary: !summary.isEmpty,
            hasKeyPoints: !points.isEmpty,
            hasTodos: !todos.isEmpty,
            reading: reading,
            summarizing: summarizing
        )

        Group {
            if shouldShow {
                let s = status
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        if s.inFlight {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: s == .noKey ? "key.slash" : "sparkles")
                                .font(.footnote.weight(.semibold))
                                .foregroundStyle(s == .noKey ? Color.orange : JoatTheme.accent)
                        }
                        Text(headerText(s, hasInsights: hasInsights))
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(s == .noKey ? Color.orange : .primary)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        Button(action: openInspector) {
                            Image(systemName: "ellipsis.circle")
                                .font(.body)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("AI Insights & Prep")
                        if hasInsights || s == .noKey {
                            Button {
                                withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
                            } label: {
                                Image(systemName: expanded ? "chevron.up" : "chevron.down")
                                    .font(.footnote.weight(.semibold))
                                    .padding(4)
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(.secondary)
                            .accessibilityLabel(expanded ? "Collapse summary" : "Expand summary")
                        }
                    }

                    if expanded, hasInsights {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 8) {
                                if !summary.isEmpty {
                                    Text(summary)
                                        .font(.callout)
                                        .foregroundStyle(n?.summaryStale == true ? .secondary : .primary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                if !points.isEmpty {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text("KEY POINTS").font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                                        ForEach(points, id: \.self) { p in
                                            Text("•  \(p)").font(.footnote).fixedSize(horizontal: false, vertical: true)
                                        }
                                    }
                                }
                                if !todos.isEmpty {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text("NEXT STEPS").font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                                        ForEach(todos, id: \.self) { t in
                                            Text("☐  \(t)").font(.footnote).fixedSize(horizontal: false, vertical: true)
                                        }
                                    }
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 190)
                        .fixedSize(horizontal: false, vertical: true)
                    } else if !expanded, !summary.isEmpty {
                        Text(summary)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }

                    if expanded, s == .noKey {
                        Text(s.label)
                            .font(.caption)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let problem, !s.inFlight, expanded {
                        Text(problem)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(JoatTheme.card)
                .overlay(alignment: .bottom) { Divider() }
            }
        }
        .onAppear(perform: refresh)
        .onReceive(NotificationCenter.default.publisher(for: Notebook.readingChangedNotification)) { _ in refresh() }
        .onReceive(NotificationCenter.default.publisher(for: QuickNotes.insightsChangedNotification)) { _ in refresh() }
    }

    private func headerText(_ s: NotesInsights.Status, hasInsights: Bool) -> String {
        switch s {
        case .readingHandwriting, .summarizing: return s.label
        case .noKey: return "No AI key — summaries are off"
        case .ready: return "AI summary"
        case .stale: return "AI summary · updates on close"
        case .pending, .empty: return hasInsights ? "AI summary" : s.label
        }
    }

    private func refresh() {
        reading = Notebook.reading.contains(noteId)
        summarizing = quickNotes?.isSummarizing(noteId) ?? false
        problem = Notebook.readProblems[noteId] ?? quickNotes?.insightProblems[noteId]
        notesModel.reload()
    }
}

// MARK: - Sparkles AI Insights & Prep Inspector (`PageInspectorSheet`)

private struct InspectorOfferItem: Identifiable {
    let id = UUID()
    let offer: QuickNotes.ReminderOffer
}

struct PageInspectorSheet: View {
    let noteId: String
    let handle: PageHandle

    @ObservedObject private var notesModel = JOATRuntime.shared.notes
    @ObservedObject private var ai = OnPhoneAI.shared
    @State private var isReading = false
    @State private var isSummarizing = false
    @State private var isFindingReminders = false
    @State private var statusMessage = ""
    @State private var reminderOfferItem: InspectorOfferItem?
    @State private var choosingPrep = false
    @Environment(\.dismiss) private var dismiss

    private var quickNotes: QuickNotes? { JOATRuntime.shared.brain?.quickNotes }
    private var note: QuickNotes.VoiceNote? {
        notesModel.voice.first(where: { $0.id == noteId })
            ?? quickNotes?.savedNotes.first(where: { $0.id == noteId })
    }
    private var currentDrawing: PKDrawing {
        handle.view?.currentDrawing ?? PageStore.load(noteId)
    }
    private var hasInk: Bool { !currentDrawing.strokes.isEmpty || PageStore.hasInk(noteId) }
    private var hasImages: Bool {
        if let view = handle.view {
            return !PageStore.attachments(view.rich).isEmpty
        }
        return !PageStore.imageNames(noteId).isEmpty
    }

    var body: some View {
        NavigationStack {
            List {
                handwritingSection
                overviewSection
                if let pts = note?.keyPoints, !pts.isEmpty {
                    Section("Key Points") {
                        ForEach(pts, id: \.self) { point in
                            Label(point, systemImage: "circle.fill")
                                .font(.callout)
                                .labelStyle(.titleOnly)
                        }
                    }
                }
                remindersSection
                prepSection
                if !statusMessage.isEmpty {
                    Section {
                        Text(statusMessage)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Page Insights & Prep")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear {
                isReading = Notebook.reading.contains(noteId)
                notesModel.reload()
            }
            .onReceive(NotificationCenter.default.publisher(for: Notebook.readingChangedNotification)) { _ in
                isReading = Notebook.reading.contains(noteId)
                notesModel.reload()
            }
            .sheet(item: $reminderOfferItem) { item in
                ReminderReviewSheet(offer: item.offer) { receipt in
                    statusMessage = receipt
                }
            }
            .sheet(isPresented: $choosingPrep) {
                let label = (note?.title.isEmpty == false) ? "“\(note!.title)”" : "this page"
                PrepTargetPicker(what: label) { target in
                    addPageToPrep(target: target)
                }
            }
        }
    }

    @ViewBuilder
    private var handwritingSection: some View {
        Section("Handwriting & Slide OCR") {
            if isReading {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("Reading handwriting & images…")
                        .foregroundStyle(.secondary)
                }
            } else if let hw = note?.handwriting, !hw.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Extracted text (\(hw.count) chars)", systemImage: "checkmark.circle.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.green)
                    Text(hw)
                        .font(.callout)
                        .foregroundStyle(.primary)
                        .lineLimit(8)
                }
            } else if note?.handwriting == "" {
                Text("Last scan found no handwritten or slide words.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else if hasInk || hasImages {
                Text("Handwriting and pasted images are read and summarized automatically once you close this page, or tap below to read now.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else {
                Text("Write with Apple Pencil or insert a slide photo to extract searchable text.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            if !ai.visionConfigured {
                Text("Add a Gemini or Claude API key in Home to enable vision OCR.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            Button {
                triggerManualOCR()
            } label: {
                Label(
                    (note?.handwriting != nil) ? "Re-read handwriting now" : "Read handwriting now",
                    systemImage: "text.viewfinder"
                )
            }
            .disabled(isReading || (!hasInk && !hasImages) || !ai.visionConfigured)
        }
    }

    @ViewBuilder
    private var overviewSection: some View {
        Section("AI Overview") {
            if let summary = note?.summary, !summary.isEmpty {
                Text(summary)
                    .font(.callout)
            }
            Button {
                triggerSummary()
            } label: {
                if isSummarizing {
                    HStack(spacing: 8) {
                        ProgressView()
                        Text("Summarizing page…")
                    }
                } else {
                    Label(
                        (note?.summary?.isEmpty == false) ? "Re-summarize page" : "Summarize page",
                        systemImage: "sparkles"
                    )
                }
            }
            .disabled(isSummarizing)
        }
    }

    @ViewBuilder
    private var remindersSection: some View {
        Section("Next Steps & Reminders") {
            if let todos = note?.todos, !todos.isEmpty {
                ForEach(todos, id: \.self) { todo in
                    Text("□ \(todo)")
                        .font(.callout)
                }
            }
            Button {
                findReminders()
            } label: {
                if isFindingReminders {
                    HStack(spacing: 8) {
                        ProgressView()
                        Text("Finding reminders…")
                    }
                } else {
                    Label("Find reminders in this page", systemImage: "checklist")
                }
            }
            .disabled(isFindingReminders)
        }
    }

    @ViewBuilder
    private var prepSection: some View {
        Section("Conversation Prep") {
            Button {
                _ = handle.view?.flushAutoSave(triggerOCR: false)
                notesModel.reload()
                let text = (note?.paragraphs ?? []).joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else {
                    statusMessage = hasInk || hasImages
                        ? "Tap “Read handwriting now” first so your handwritten notes can be added to Prep."
                        : "Type or write something on this page before adding it to Prep."
                    return
                }
                choosingPrep = true
            } label: {
                Label("Add to Prep…", systemImage: "doc.text.magnifyingglass")
            }
        }
    }

    private func triggerManualOCR() {
        guard let qn = quickNotes else { return }
        _ = handle.view?.flushAutoSave(quickNotes: qn, triggerOCR: false)
        let drawing = currentDrawing
        statusMessage = ""
        isReading = true
        Notebook.read(noteId, drawing, quickNotes: qn) { result in
            isReading = false
            notesModel.reload()
            switch result {
            case .success(let text):
                let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
                statusMessage = clean.uppercased() == "NONE" || clean.isEmpty
                    ? "No handwritten words were found on this page."
                    : "Handwriting read (\(clean.count) chars) and added to page transcript."
            case .failure(let failure):
                statusMessage = MacLink.explain(failure)
            }
        }
    }

    private func triggerSummary() {
        guard let qn = quickNotes else { return }
        _ = handle.view?.flushAutoSave(quickNotes: qn, triggerOCR: false)
        isSummarizing = true
        statusMessage = ""
        qn.summarize(noteId) { receipt in
            isSummarizing = false
            statusMessage = receipt
            notesModel.reload()
        }
    }

    private func findReminders() {
        guard let qn = quickNotes else { return }
        _ = handle.view?.flushAutoSave(quickNotes: qn, triggerOCR: false)
        isFindingReminders = true
        statusMessage = ""
        qn.proposeReminders(noteId) { offer, why in
            isFindingReminders = false
            if let offer {
                reminderOfferItem = InspectorOfferItem(offer: offer)
            } else {
                statusMessage = why
            }
        }
    }

    private func addPageToPrep(target: String?) {
        guard let n = note else { return }
        let text = n.paragraphs.joined(separator: "\n")
        guard !text.isEmpty else { return }
        let prep = JOATRuntime.shared.prep
        prep.addText(
            text,
            title: n.title.isEmpty ? "Page" : n.title,
            source: "Notebook page",
            to: target
        )
        let targetName = target.flatMap { id in prep.notes.first(where: { $0.id == id })?.title } ?? "a new prep note"
        statusMessage = "Added to \(targetName)."
    }
}

#if DEBUG
// MARK: - Simulator Debug Hooks (`JOAT_DEBUG_PAGE=1`, `JOAT_UITEST=1`)

extension Notebook {
    static let debugOpen = Notification.Name("joat.debug.page.open")
    static let debugDone = Notification.Name("joat.debug.page.done")
    static let debugWidth = Notification.Name("joat.debug.page.width")
    static let debugDrop = Notification.Name("joat.debug.page.drop")
    static var debugStart: (typed: String, drawing: PKDrawing)?

    static func debugDrawing(_ lines: [String], top: CGFloat) -> PKDrawing {
        let font = CTFontCreateWithName("SnellRoundhand" as CFString, 30, nil)
        let ink = PKInk(.pen, color: .black)
        var strokes: [PKStroke] = []
        for (row, line) in lines.enumerated() {
            let chars = Array(line.utf16)
            var glyphs = [CGGlyph](repeating: 0, count: chars.count)
            CTFontGetGlyphsForCharacters(font, chars, &glyphs, chars.count)
            var advances = [CGSize](repeating: .zero, count: glyphs.count)
            CTFontGetAdvancesForGlyphs(font, .horizontal, glyphs, &advances, glyphs.count)
            var x: CGFloat = 20
            let baseline = top + CGFloat(row) * 48
            for (glyph, advance) in zip(glyphs, advances) {
                var flip = CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: x, ty: baseline)
                if let path = CTFontCreatePathForGlyph(font, glyph, &flip) {
                    var points: [CGPoint] = []
                    func flush() {
                        if points.count > 1 {
                            let ctrl = points.enumerated().map { i, p in
                                PKStrokePoint(location: p, timeOffset: TimeInterval(i) * 0.01, size: CGSize(width: 2.5, height: 2.5),
                                              opacity: 1, force: 1, azimuth: 0, altitude: .pi / 2)
                            }
                            strokes.append(PKStroke(ink: ink, path: PKStrokePath(controlPoints: ctrl, creationDate: Date())))
                        }
                        points = []
                    }
                    path.applyWithBlock { element in
                        let e = element.pointee
                        switch e.type {
                        case .moveToPoint: flush(); points.append(e.points[0])
                        case .addLineToPoint: points.append(e.points[0])
                        case .addQuadCurveToPoint: points.append(e.points[0]); points.append(e.points[1])
                        case .addCurveToPoint: points.append(e.points[0]); points.append(e.points[1]); points.append(e.points[2])
                        case .closeSubpath: if let first = points.first { points.append(first) }
                        @unknown default: break
                        }
                    }
                    flush()
                }
                x += advance.width
            }
        }
        return PKDrawing(strokes: strokes)
    }

    static func debugSnapshot(_ name: String) {
        guard let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first,
              let window = scene.keyWindow else { Log.add("debug: no window"); return }
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { context in
            if !window.drawHierarchy(in: window.bounds, afterScreenUpdates: true) { window.layer.render(in: context.cgContext) }
            for other in scene.windows where other !== window && !other.isHidden && other.windowLevel >= window.windowLevel {
                other.drawHierarchy(in: other.frame, afterScreenUpdates: false)
            }
        }
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("debug-\(name).png")
        try? image.pngData()?.write(to: url)
        Log.add("debug: snapshot \(name)")
    }

    static func findCanvas(in view: UIView) -> PKCanvasView? {
        if let canvas = view as? PKCanvasView { return canvas }
        for sub in view.subviews { if let canvas = findCanvas(in: sub) { return canvas } }
        return nil
    }

    static func debugTiles(_ drawing: PKDrawing) {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        for (i, png) in PageStore.pngTiles(drawing).enumerated() {
            try? png.write(to: dir.appendingPathComponent("debug-tile-\(i).png"))
        }
    }

    static func listenForSnaps() {
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in
                Notebook.snapCount += 1
                Notebook.debugSnapshot("ui-\(Notebook.snapCount)")
            }
        }, "com.joat.debug.snap" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in
                let windows = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }
                (windows.compactMap { Notebook.findCanvas(in: $0) }.first?.superview as? PageView)?.debugSelectEraser()
            }
        }, "com.joat.debug.eraser" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in Notebook.debugCopySlide() }
        }, "com.joat.debug.copyslide" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in
                UIPasteboard.general.string = "Learning rate: start at 0.01"
                Log.add("debug: copied text")
            }
        }, "com.joat.debug.copytext" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugWidth, object: CGFloat(300)) }
        }, "com.joat.debug.narrow" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugWidth, object: nil) }
        }, "com.joat.debug.wide" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugDrop, object: nil) }
        }, "com.joat.debug.drop" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in PageView.open?.debugZoom(0.5) }
        }, "com.joat.debug.zoomout" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in PageView.open?.debugZoom(1) }
        }, "com.joat.debug.zoomfit" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in PageView.open?.debugZoom(8) }
        }, "com.joat.debug.zoomin" as CFString, nil, .deliverImmediately)
        Log.add("debug: UI test pictures on")
    }
    private static var snapCount = 0

    static func debugCopySlide() {
        let slides = [("Week 3: Gradient Descent", ["Pick a learning rate", "Step against the gradient", "Stop when the loss flattens"]),
                      ("Choosing the learning rate", ["Too small: slow", "Too big: it never settles"]),
                      ("When the learning rate is too high", ["The loss bounces instead of falling", "Halve it and try again"])]
        let (title, points) = slides[slideCount % slides.count]
        slideCount += 1
        let size = CGSize(width: 1280, height: 720)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let png = UIGraphicsImageRenderer(size: size, format: format).pngData { context in
            UIColor(red: 0.08, green: 0.15, blue: 0.3, alpha: 1).setFill()
            context.fill(CGRect(origin: .zero, size: size))
            (title as NSString).draw(at: CGPoint(x: 80, y: 80),
                                     withAttributes: [.font: UIFont.boldSystemFont(ofSize: 64), .foregroundColor: UIColor.white])
            for (i, point) in points.enumerated() {
                ("•  " + point as NSString).draw(at: CGPoint(x: 100, y: 240 + CGFloat(i) * 90),
                                                  withAttributes: [.font: UIFont.systemFont(ofSize: 44), .foregroundColor: UIColor.white])
            }
        }
        UIPasteboard.general.setItems([[UTType.png.identifier: png]])
        Log.add("debug: copied slide \(slideCount): \(title)")
    }
    private static var slideCount = 0

    static func debugRun() {
        let drawing = debugDrawing(["Send Liam the slide deck", "on Friday at 3 pm", "Book design review room"], top: 140)
        debugStart = ("Design review, Tuesday", drawing)
        debugTiles(drawing)
        NotificationCenter.default.post(name: debugOpen, object: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { debugSnapshot("1-editor") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { NotificationCenter.default.post(name: debugDone, object: nil) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 9) { debugSnapshot("2-list") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 45) { debugSnapshot("3-list-later") }
    }
}
#endif
