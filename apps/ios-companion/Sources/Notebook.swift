import SwiftUI
import PencilKit
import CryptoKit
import UniformTypeIdentifiers

/// JOAT Brain's notebook (the wearer, 2026-11-09, after TypeDraw): a page you
/// type on and write or draw on with Apple Pencil or a finger, in one view.
/// A saved page is a QuickNote (QuickNotes.savePage): the glasses list and
/// read it, Claude summarizes it and offers its next steps as reminders, and
/// it can be the prep notes the AI cues answer from. Claude reads the
/// handwriting into text (ClaudeLink.readHandwriting); the ink itself is kept
/// in Documents/pages/<id>.drawing. Text and images paste into the typing
/// (a screenshot of a course slide, say: the wearer, 2026-11-11); Claude
/// reads the images' words into the page's text too.
///
/// A page is US Letter paper (612 × 792 points a sheet), the same on every
/// screen, zoomed to fill the screen's width; it never reflows (2026-11-11:
/// a page begun on the Duo's outer screen showed a big gap on the inner one).
/// The page being written is kept as a draft on disk as it changes, and a
/// draft left behind is saved as Done would have (2026-11-11: the wearer's
/// pages from the day before were gone).
///
/// The iPhone Duo takes the USB-C Apple Pencil from iOS 27.2: tilt, no
/// pressure (MacRumors, 2026-09-09). PencilKit draws with either.

// MARK: - The ink on disk

enum PageStore {
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

    static func hasInk(_ id: String) -> Bool { FileManager.default.fileExists(atPath: url(id).path) }

    // MARK: Pasted images (2026-11-11, the wearer: "I cannot paste anything
    // on the writing and typing sections, even an image")

    /// The typed text with its pasted images, in order, in `<id>.page.json`
    /// beside the ink; each image is `<id>-<hash>.jpg`. Absent when the page
    /// has no images: its typed text is then the note's `typed`.
    private struct RichPage: Codable {
        struct Segment: Codable {
            var text: String?
            var image: String?
        }
        var segments: [Segment]
    }

    static func richURL(_ id: String) -> URL { directory.appendingPathComponent("\(id).page.json") }

    /// The page's typing as the text view shows it.
    static var textAttributes: [NSAttributedString.Key: Any] {
        [.font: UIFont.preferredFont(forTextStyle: .body), .foregroundColor: UIColor.label]
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

    /// The typing with its images, for the editor; nil when it has none.
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

    /// Keeps the images and where they sit in the typing. With no images
    /// left, only the typed text is kept. Images no longer on the page go.
    /// `draft`: the page being written (`draftId`); it may point at the
    /// note's own image files, which stay until Done.
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
        if !draft { Log.add("notebook: page \(id.prefix(6)) has \(names.count) pasted image\(names.count == 1 ? "" : "s")") }
    }

    /// An image's file: the page's own, else a new JPEG. A draft keeps
    /// pointing at the note's file, or writes `draft-<hash>.jpg`.
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

    // MARK: The page being written

    /// What the editor shows, on disk as it changes: `draft.json`, with its
    /// ink (`draft.drawing`) and images (`draft.page.json`, `draft-*.jpg`).
    struct Draft: Codable {
        /// The note it changes; nil: a new page.
        var noteId: String?
        var typed: String
        var inkChanged: Bool
        var updated: Date
    }

    static let draftId = "draft"
    private static var draftURL: URL { directory.appendingPathComponent("\(draftId).json") }

    /// False: it wasn't written (a full disk, say).
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

    /// What a pasted item holds.
    enum Pasted {
        case image(UIImage)
        case text(String)
    }

    /// A pasted item, loading at once: an image (as an image, else from its
    /// image file types in turn), else its words. Nil: nothing usable.
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

    /// A pasted image for Claude: JPEG, its long edge at most 1568 px,
    /// past which Claude downscales it (vision docs).
    static func jpegForClaude(_ image: UIImage) -> Data? {
        scaled(image, longEdge: 1568).jpegData(compressionQuality: 0.85)
    }

    /// The image with its long edge at most `longEdge` pixels, at scale 1
    /// (a pasted screenshot is kept at 2048: sharp enough to read, small
    /// enough to keep many).
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

    /// The ink as the page shows it, for the note's screen.
    static func preview(_ drawing: PKDrawing, dark: Bool) -> UIImage? {
        guard !drawing.strokes.isEmpty else { return nil }
        let rect = drawing.bounds.insetBy(dx: -12, dy: -12)
        var image: UIImage?
        UITraitCollection(userInterfaceStyle: dark ? .dark : .light).performAsCurrent {
            image = drawing.image(from: rect, scale: 2)
        }
        return image
    }

    /// The page for Claude to read, as PNGs from top to bottom: dark ink on
    /// white, cut between lines of writing so no line is split, each about a
    /// screen tall. Claude downscales an image past its long-edge limit
    /// (vision docs), so one tall image would make the writing too small.
    static func pngTiles(_ drawing: PKDrawing) -> [Data] {
        let strokes = drawing.strokes.map(\.renderBounds).filter { !$0.isNull && !$0.isEmpty }
        guard !strokes.isEmpty else { return [] }
        let width = max(drawing.bounds.maxX + 16, 320)
        let tileHeight = width * 1.3
        // Lines of writing: strokes whose heights overlap.
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

/// A pasted image in the typing (PageView sizes it to the page). It
/// remembers its file once saved, so a page saved again keeps it rather
/// than writing it anew.
final class PageImage: NSTextAttachment {
    var fileName: String?
    /// Its file in the draft of the page being written.
    var draftName: String?
}

/// The page's typing. It also takes pasted images: a text view on its own
/// pastes only the words. The edit menu's Paste (and ⌘V) comes to paste(_:);
/// the keyboard's "Photo, Paste from …" suggestion (iOS 27) and drops come
/// as item providers, through the paste delegate (UITextPasteDelegate.h).
final class PastingTextView: UITextView, UITextPasteDelegate {
    var onPasteImages: (([UIImage]) -> Void)?

    /// Takes images as well as words (UIKit's own: words only, while
    /// allowsEditingTextAttributes is off).
    func acceptImages() {
        let accepted = UIPasteConfiguration(forAccepting: UIImage.self)
        accepted.addTypeIdentifiers(forAccepting: NSString.self)
        pasteConfiguration = accepted
        pasteDelegate = self
    }

    func textPasteConfigurationSupporting(_ textPasteConfigurationSupporting: any UITextPasteConfigurationSupporting,
                                          transform item: any UITextPasteItem) {
        let provider = item.itemProvider
        #if DEBUG
        Log.add("debug: paste item \(provider.registeredTypeIdentifiers)")
        #endif
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
        // hasImages doesn't read the pasteboard, so no "Allow Paste" alert.
        if action == #selector(paste(_:)), UIPasteboard.general.hasImages { return true }
        return super.canPerformAction(action, withSender: sender)
    }

    #if DEBUG
    override func target(forAction action: Selector, withSender sender: Any?) -> Any? {
        let name = NSStringFromSelector(action)
        if name.localizedCaseInsensitiveContains("paste") { Log.add("debug: text view action \(name)") }
        return super.target(forAction: action, withSender: sender)
    }
    #endif

    override func paste(_ sender: Any?) {
        // Paste chosen in the edit menu (or ⌘V): reading it asks nothing.
        let board = UIPasteboard.general
        if board.hasImages, let images = board.images, !images.isEmpty {
            onPasteImages?(images)
        } else if board.hasStrings, let words = board.string, let range = selectedTextRange {
            // Words as typed: UIKit's own paste, through the item providers
            // this view now takes, put a blank line before them (Simulator).
            replace(range, withText: words)
        } else {
            super.paste(sender)
        }
    }
}

// MARK: - Saving a page

@MainActor
enum Notebook {
    private static let claude = ClaudeLink()
    /// Pages Claude is reading now (the note's screen says so).
    static private(set) var reading: Set<String> = []

    /// Done on the page: the words, the pasted images and the ink are kept
    /// at once; the page is read again when the ink or the images changed.
    /// Nil when nothing was saved. `rich`: the typing with its images.
    @discardableResult
    static func save(noteId: String?, typed rawTyped: String, rich: NSAttributedString?, drawing: PKDrawing,
                     inkChanged: Bool, quickNotes: QuickNotes) -> String? {
        let typed = rawTyped.trimmingCharacters(in: .whitespacesAndNewlines)
        let ink = !drawing.strokes.isEmpty
        let attachments = rich.map(PageStore.attachments) ?? []
        let pasted = !attachments.isEmpty
        if noteId == nil, typed.isEmpty, !ink, !pasted { return nil }
        let before = noteId.map(PageStore.imageNames) ?? []
        let imagesChanged = attachments.map { ($0 as? PageImage)?.fileName } != before.map { Optional($0) }
        let id = quickNotes.savePage(id: noteId, typed: typed, inkChanged: inkChanged || imagesChanged)
        if inkChanged || noteId == nil { PageStore.save(drawing, id) }
        if let rich, pasted || !before.isEmpty { PageStore.saveRich(rich, id) }
        if (inkChanged || imagesChanged) && (ink || pasted) {
            read(id, drawing, quickNotes: quickNotes)
        } else {
            quickNotes.pageReady(id)
        }
        return id
    }

    /// The page being written, from its draft, saved as a note.
    @discardableResult
    static func saveDraft(quickNotes: QuickNotes) -> String? {
        guard let d = PageStore.readDraft() else { return nil }
        let rich = d.rich ?? NSAttributedString(string: d.draft.typed, attributes: PageStore.textAttributes)
        let id = save(noteId: d.draft.noteId, typed: d.draft.typed, rich: rich, drawing: d.drawing,
                      inkChanged: d.draft.inkChanged, quickNotes: quickNotes)
        PageStore.clearDraft()
        return id
    }

    /// A draft left behind (JOAT Brain stopped with the page open, or the
    /// editor closed without Done or Cancel) is saved as Done would have.
    /// `except`: the page about to be opened, which carries on from its own.
    static func recoverDraft(quickNotes: QuickNotes, except: String?? = .none) {
        guard let d = PageStore.readDraft() else { return }
        if let except, d.draft.noteId == except { return }
        Log.add("notebook: saved a page left open (\(d.draft.noteId.map { "page " + $0.prefix(6) } ?? "a new page"), "
            + "last changed \(d.draft.updated.formatted(date: .abbreviated, time: .shortened)))")
        saveDraft(quickNotes: quickNotes)
    }

    /// The Notes tab: a draft whose page is no longer open (its editor went
    /// away without Done or Cancel) is saved now.
    static func recoverIfClosed(quickNotes: QuickNotes) {
        if PageView.open == nil { recoverDraft(quickNotes: quickNotes) }
    }

    /// Claude reads the handwriting, and the pasted images, into the
    /// page's text.
    static func read(_ id: String, _ drawing: PKDrawing, quickNotes: QuickNotes) {
        let tiles = PageStore.pngTiles(drawing)
        let pasted = PageStore.images(id).prefix(10).compactMap(PageStore.jpegForClaude)
        guard !(tiles.isEmpty && pasted.isEmpty), !reading.contains(id) else { return }
        guard claude.configured else {
            Log.add("notebook: handwriting not read: no Claude key on this phone")
            quickNotes.pageReady(id)
            return
        }
        reading.insert(id)
        Log.add("notebook: reading page \(id.prefix(6)), \(tiles.count) ink image\(tiles.count == 1 ? "" : "s"), "
            + "\(pasted.count) pasted")
        claude.readHandwriting(tiles, pasted: Array(pasted)) { result in
            reading.remove(id)
            switch result {
            case .success(let text): quickNotes.pageRead(id, handwriting: text)
            case .failure(let f):
                Log.add("notebook: handwriting not read: \(MacLink.explain(f))")
                quickNotes.pageReady(id)
            }
        }
    }
}

// MARK: - The page

/// Typing and ink on one page. Write: the Pencil (or a finger, unless iOS is
/// set to draw with Apple Pencil only) draws, and two fingers scroll and
/// pinch to zoom. Type: the keyboard (or Scribble with the Pencil) types on
/// the same page, under the ink; one finger scrolls, two fingers zoom.
///
/// The canvas zooms itself, so the ink stays sharp (a parent scroll view's
/// zoom blurs PencilKit ink: Apple forums 698317). The typed text has its own
/// scroll view under the canvas; the two follow each other's scroll and zoom.
/// Write moves the canvas, Type moves the text.
///
/// The page is US Letter wide, as in GoodNotes and Notability (Letter is
/// GoodNotes' default size in the US), and as many sheets tall as the
/// writing needs. It opens zoomed to fill the screen's width, as Apple's
/// PencilKit sample fits its fixed-width canvas (zoom = view width ÷ canvas
/// width); pinch in, and it scrolls both ways. Folding, unfolding or turning
/// the phone zooms the same page; nothing moves on it.
final class PageView: UIView, UITextViewDelegate, PKCanvasViewDelegate, PKToolPickerObserver {
    /// One sheet: US Letter, 8.5 × 11 in at 72 points an inch.
    static let sheet = CGSize(width: 612, height: 792)
    /// The page open now: a draft with no page open was left behind.
    static weak var open: PageView?
    private let textScroll = UIScrollView()
    let textView = PastingTextView()
    let canvas = PKCanvasView()
    private let sheetLines = SheetLines()
    private let toolPicker = PKToolPicker()
    private lazy var textScrollDelegate = TextScrollDelegate(page: self)
    /// The note this page changes; nil: a new page.
    let noteId: String?
    /// Letter, or wider for a page written before pages had a width, whose
    /// ink reaches further (it was as wide as the screen it was written on).
    let pageWidth: CGFloat
    /// The zoom that fills the screen's width now.
    private var fitScale: CGFloat = 0
    /// Changed since the draft was last written.
    private var dirty = false
    /// Changed since it opened (or carries on from a draft).
    private var edited = false
    private var inkChanged = false
    private var draftTimer: Timer?
    /// The typing with its pasted images.
    var rich: NSAttributedString { textView.attributedText ?? NSAttributedString() }
    var writing = true {
        didSet { if writing != oldValue { applyMode() } }
    }
    /// The saved ink, given to the canvas once it has its size: Edit page
    /// showed a blank page (and new strokes didn't show) when the canvas got
    /// its drawing while still zero-sized (Simulator UI test, 2026-11-10).
    private var pendingDrawing: PKDrawing?
    private var settingDrawing = false
    /// The page at zoom 1, in points.
    private var pageSize = CGSize.zero
    /// One scroll view is following the other.
    private var following = false
    /// The pen, pencil or marker in use before the eraser: an eraser stroke
    /// goes back to it when the Pencil lifts (the wearer's ask, 2026-11-10).
    private var lastInk: PKToolPickerItem?

    /// `rich`: the typing with its pasted images, if it has any. `inkChanged`
    /// and `unsaved`: it carries on from a draft, or starts with content
    /// that isn't saved yet.
    init(noteId: String?, typed: String, rich: NSAttributedString?, drawing: PKDrawing,
         inkChanged: Bool = false, unsaved: Bool = false) {
        self.noteId = noteId
        let inkEdge = drawing.strokes.isEmpty ? 0 : drawing.bounds.maxX
        pageWidth = max(Self.sheet.width, ceil(inkEdge + 24))
        self.inkChanged = inkChanged
        super.init(frame: .zero)
        Self.open = self
        // Around the paper (seen only past its end).
        backgroundColor = .secondarySystemBackground
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
        textView.isScrollEnabled = false
        textView.backgroundColor = .clear
        textView.textContainerInset = UIEdgeInsets(top: 20, left: 16, bottom: 20, right: 16)
        textView.delegate = self
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
        // A page starts with a pen, pencil or marker: the tool picker keeps
        // its last tool, and one left on the eraser had no pen to go back to.
        if !(toolPicker.selectedToolItem is PKToolPickerInkingItem),
           let pen = toolPicker.toolItems.first(where: { $0 is PKToolPickerInkingItem }) {
            Log.add("notebook: started with \(toolPicker.selectedToolItem.identifier); \(pen.identifier) instead")
            toolPicker.selectedToolItem = pen
        }
        if toolPicker.selectedToolItem is PKToolPickerInkingItem { lastInk = toolPicker.selectedToolItem }
        // The draft is written at once when the app leaves the screen.
        for name in [UIApplication.willResignActiveNotification, UIApplication.didEnterBackgroundNotification,
                     UIScene.didDisconnectNotification] {
            NotificationCenter.default.addObserver(self, selector: #selector(writeDraftNow), name: name, object: nil)
        }
        if unsaved { markChanged() }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        // First responders need a window: the tool picker follows the canvas.
        if window != nil { DispatchQueue.main.async { [weak self] in self?.applyMode() } } else { writeDraft() }
    }

    // MARK: The draft

    /// The words alone (an image is one U+FFFC character in the text).
    var typedText: String { textView.text.replacingOccurrences(of: "\u{FFFC}", with: "") }

    /// A change: the draft is written a moment later.
    private func markChanged(ink: Bool = false) {
        if ink { inkChanged = true }
        dirty = true
        edited = true
        draftTimer?.invalidate()
        draftTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.writeDraft() }
        }
    }

    @objc private func writeDraftNow() { writeDraft() }

    /// The page as it is now, on disk (Done saves it from there).
    func writeDraft() {
        draftTimer?.invalidate()
        draftTimer = nil
        guard dirty else { return }
        // Not written (a full disk): tried again at the next change.
        dirty = !PageStore.writeDraft(noteId: noteId, typed: typedText, rich: rich, drawing: pendingDrawing ?? canvas.drawing,
                                      inkChanged: inkChanged)
    }

    /// Done: the page as it is now, saved as a note (from the page itself,
    /// so a draft that couldn't be written doesn't matter); the draft goes.
    func save(quickNotes: QuickNotes) -> String? {
        draftTimer?.invalidate()
        draftTimer = nil
        defer {
            dirty = false
            PageStore.clearDraft()
        }
        guard edited else { return noteId }
        return Notebook.save(noteId: noteId, typed: typedText, rich: rich, drawing: pendingDrawing ?? canvas.drawing,
                             inkChanged: inkChanged, quickNotes: quickNotes)
    }

    /// Cancel: the page stays as it was saved.
    func discardDraft() {
        draftTimer?.invalidate()
        draftTimer = nil
        dirty = false
        PageStore.clearDraft()
    }

    private func applyMode() {
        guard window != nil else { return }
        // Type: the canvas only shows the ink; touches reach the text.
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
            + "Pencil only: \(!fingerDraws), tools shown: \(toolPicker.isVisible), tool \(toolPicker.selectedToolItem.identifier))")
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        textScroll.frame = bounds
        canvas.frame = bounds
        guard bounds.width > 0 else { return }
        fitImages()
        let textHeight = textView.sizeThatFits(CGSize(width: pageWidth, height: .greatestFiniteMagnitude)).height
        let drawing = pendingDrawing ?? canvas.drawing
        let ink = drawing.strokes.isEmpty ? 0 : drawing.bounds.maxY
        // Another sheet once the writing nears the end of the last one.
        let sheets = max(1, Int(ceil((max(textHeight, ink) + 160) / Self.sheet.height)))
        let size = CGSize(width: pageWidth, height: CGFloat(sheets) * Self.sheet.height)
        let fit = bounds.width / pageWidth
        let refit = abs(fit - fitScale) > 0.0001
        guard size != pageSize || refit else { return }
        pageSize = size
        following = true
        let offset = refit ? zoomToFit(fit) : nil
        // Both in zoomed points, as Apple's PencilKit sample sets the canvas.
        let zoom = canvas.zoomScale
        let zoomed = CGSize(width: size.width * zoom, height: size.height * zoom)
        textView.bounds = CGRect(origin: .zero, size: size)
        textView.center = CGPoint(x: zoomed.width / 2, y: zoomed.height / 2)
        sheetLines.frame = CGRect(origin: .zero, size: size)
        sheetLines.sheets = sheets
        textScroll.contentSize = zoomed
        canvas.contentSize = zoomed
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
        }
        if refit { sharpenText() }
        #if DEBUG
        Log.add("debug: page layout: view \(Int(bounds.width)), sheets \(sheets), text frame \(textView.frame.integral), "
            + "zoom text \(String(format: "%.3f", textScroll.zoomScale)) ink \(String(format: "%.3f", canvas.zoomScale))")
        #endif
    }

    /// The zoom for this screen's width: as far in as before, relative to
    /// filling the width (first: filling it). Returns where to scroll so the
    /// same part of the page stays in view.
    private func zoomToFit(_ fit: CGFloat) -> CGPoint {
        let relative = fitScale > 0 ? canvas.zoomScale / fitScale : 1
        let before = canvas.zoomScale
        let offset = canvas.contentOffset
        fitScale = fit
        let most = max(4, fit * 4)
        let zoom = min(max(fit * relative, fit), most)
        for scroller in [textScroll, canvas] as [UIScrollView] {
            scroller.minimumZoomScale = fit
            scroller.maximumZoomScale = most
            scroller.zoomScale = zoom
        }
        let ratio = before > 0 ? zoom / before : 1
        Log.add("notebook: page \(Int(pageWidth)) pt wide on a \(Int(bounds.width)) pt screen, zoom \(String(format: "%.2f", zoom))")
        return CGPoint(x: offset.x * ratio, y: offset.y * ratio)
    }

    /// Each pasted image as wide as the page, or its own width if narrower
    /// (the image's own size showed a 1280-point slide cut off, Simulator,
    /// 2026-11-11).
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
        // Set again, so the text lays out with the new sizes.
        storage.beginEditing()
        for (a, range) in resized { storage.addAttribute(.attachment, value: a, range: range) }
        storage.endEditing()
    }

    /// The other scroll view takes the one that moved: zoom, then position.
    fileprivate func follow(_ moved: UIScrollView) {
        guard !following else { return }
        following = true
        let other: UIScrollView = moved === canvas ? textScroll : canvas
        if other.zoomScale != moved.zoomScale {
            other.zoomScale = moved.zoomScale
            if other === canvas {
                canvas.contentSize = CGSize(width: pageSize.width * moved.zoomScale, height: pageSize.height * moved.zoomScale)
            }
        }
        other.contentOffset = moved.contentOffset
        following = false
    }

    /// Text drawn again at the zoom it's shown at, so it isn't blurred.
    fileprivate func sharpenText() {
        // At most 2.5× sharper: each line (and pasted image) is drawn at this
        // scale, and a bigger one costs memory the phone may not have.
        let scale = traitCollection.displayScale * min(textScroll.zoomScale, 2.5)
        func apply(_ view: UIView) {
            // Not the sheets: a backing store as big as the zoomed page.
            if view is SheetLines { return }
            view.contentScaleFactor = scale
            view.setNeedsDisplay()
            view.subviews.forEach(apply)
        }
        apply(textView)
        Log.add("notebook: zoom \(String(format: "%.1f", canvas.zoomScale))x")
    }

    deinit {
        #if DEBUG
        Log.add("notebook: page view closed")
        #endif
        // Closed without Done or Cancel (both leave no draft): saved now.
        DispatchQueue.main.async {
            if let quickNotes = JOATRuntime.shared.brain?.quickNotes { Notebook.recoverIfClosed(quickNotes: quickNotes) }
        }
    }

    #if DEBUG
    /// The UI test picks the eraser (JOAT_UITEST=1, Darwin "com.joat.debug.eraser").
    func debugSelectEraser() {
        guard let eraser = toolPicker.toolItems.first(where: { $0 is PKToolPickerEraserItem }) else { return }
        toolPicker.selectedToolItem = eraser
        Log.add("debug: eraser picked")
    }
    #endif

    func textViewDidChange(_ textView: UITextView) {
        changed()
    }

    #if DEBUG
    func textView(_ textView: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
        if text.count > 1 || text.contains("\u{FFFC}") { Log.add("debug: text change at \(range.location), \(text.count) chars") }
        return true
    }
    #endif

    private func changed() {
        markChanged()
        setNeedsLayout()
    }

    /// Pasted images go into the typing, each on its own line: at the cursor
    /// in Type, after the last line in Write.
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
        textView.typingAttributes = attributes
        textView.selectedRange = NSRange(location: range.location + piece.length, length: 0)
        changed()
        Log.add("notebook: pasted \(images.count) image\(images.count == 1 ? "" : "s")"
            + (text.isEmpty ? "" : " and \(text.count) characters") + (writing ? " (end of page)" : ""))
        // Show what was pasted once the page has made room for it.
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.layoutIfNeeded()
            self.textView.layoutIfNeeded()
            let end = self.textView.caretRect(for: self.textView.endOfDocument)
            let shown = self.textView.convert(end, to: self.textScroll)
            self.textScroll.scrollRectToVisible(shown.insetBy(dx: 0, dy: -24), animated: true)
        }
    }

    /// The toolbar's Paste button (Write or Type): images and words, in
    /// order. Each item starts loading at once, inside the button's callback.
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
                    // The button's items wouldn't load (the Simulator, under a UI
                    // test: "Cannot load representation of type public.png"):
                    // the pasteboard itself, which may ask "Allow Paste?".
                    let board = UIPasteboard.general
                    if board.hasImages { images = board.images ?? [] } else if board.hasStrings, let text = board.string { words = [text] }
                    Log.add("notebook: Paste button's items didn't load (\(providers.map(\.registeredTypeIdentifiers))); "
                        + "read the pasteboard: \(images.count) image\(images.count == 1 ? "" : "s"), \(words.count) text")
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

    // The canvas (Write) moves the text.
    func scrollViewDidScroll(_ scrollView: UIScrollView) { follow(scrollView) }
    func scrollViewDidZoom(_ scrollView: UIScrollView) { follow(scrollView) }
    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        sharpenText()
    }

    func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
        guard !settingDrawing else { return }
        markChanged(ink: true)
        setNeedsLayout()
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        // The Pencil (or finger) lifted after an eraser stroke: back to the
        // pen, pencil or marker used before the eraser.
        guard toolPicker.selectedToolItem is PKToolPickerEraserItem, let ink = lastInk else { return }
        toolPicker.selectedToolItem = ink
        Log.add("notebook: eraser lifted, back to \(ink.identifier)")
    }

    func toolPickerSelectedToolItemDidChange(_ toolPicker: PKToolPicker) {
        if toolPicker.selectedToolItem is PKToolPickerInkingItem { lastInk = toolPicker.selectedToolItem }
    }

    func toolPickerFramesObscuredDidChange(_ toolPicker: PKToolPicker) {
        let obscured = toolPicker.frameObscured(in: self)
        let bottom = obscured.isNull ? 0 : max(0, bounds.maxY - obscured.minY)
        for scroller in [textScroll, canvas] as [UIScrollView] {
            scroller.contentInset.bottom = bottom
            scroller.verticalScrollIndicatorInsets.bottom = bottom
        }
    }
}

/// The text scroll view's delegate (Type moves the canvas). Separate from
/// the canvas's, so the canvas keeps zooming its own content.
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

/// The paper under the typing: white sheets, a dashed line where one ends
/// and the next begins, and their numbers. Layers and labels only: a view
/// that draws itself would need a backing store as big as the page.
final class SheetLines: UIView {
    var sheets = 1 {
        didSet { if sheets != oldValue { setNeedsLayout() } }
    }
    private let breaks = CAShapeLayer()
    private var numbers: [UILabel] = []

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .systemBackground
        isUserInteractionEnabled = false
        isAccessibilityElement = false
        breaks.fillColor = nil
        breaks.lineWidth = 1
        breaks.lineDashPattern = [6, 4]
        layer.addSublayer(breaks)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        let height = PageView.sheet.height
        let path = UIBezierPath()
        for k in stride(from: 1, to: sheets, by: 1) {
            path.move(to: CGPoint(x: 0, y: CGFloat(k) * height))
            path.addLine(to: CGPoint(x: bounds.width, y: CGFloat(k) * height))
        }
        breaks.frame = bounds
        breaks.path = path.cgPath
        breaks.strokeColor = UIColor.separator.resolvedColor(with: traitCollection).cgColor
        while numbers.count < sheets {
            let label = UILabel()
            label.font = .preferredFont(forTextStyle: .caption2)
            label.textColor = .tertiaryLabel
            addSubview(label)
            numbers.append(label)
        }
        for (i, label) in numbers.enumerated() {
            label.isHidden = sheets < 2 || i >= sheets
            label.text = "\(i + 1)"
            label.sizeToFit()
            label.frame.origin = CGPoint(x: bounds.width - label.frame.width - 16, y: CGFloat(i + 1) * height - label.frame.height - 8)
        }
    }

    override func traitCollectionDidChange(_ previous: UITraitCollection?) {
        super.traitCollectionDidChange(previous)
        setNeedsLayout()
    }
}

/// The editor's way to the page it shows (Paste, the draft at Done).
final class PageHandle {
    weak var view: PageView?
}

struct PageCanvas: UIViewRepresentable {
    let noteId: String?
    let typed: String
    let drawing: PKDrawing
    /// The page starts with content that isn't saved (the simulator check).
    let unsaved: Bool
    let handle: PageHandle
    var writing: Bool

    func makeUIView(context: Context) -> PageView {
        // A draft of another page is saved first, so this one can't write over it.
        if let quickNotes = JOATRuntime.shared.brain?.quickNotes {
            Notebook.recoverDraft(quickNotes: quickNotes, except: .some(noteId))
        }
        let view: PageView
        if let d = PageStore.readDraft(), d.draft.noteId == noteId {
            // This page's own draft: carry on from it.
            Log.add("notebook: page carries on from its draft (\(d.draft.typed.count) typed chars)")
            view = PageView(noteId: noteId, typed: d.draft.typed, rich: d.rich, drawing: d.drawing,
                            inkChanged: d.draft.inkChanged, unsaved: true)
        } else {
            view = PageView(noteId: noteId, typed: typed, rich: noteId.flatMap(PageStore.loadRich), drawing: drawing,
                            unsaved: unsaved)
        }
        handle.view = view
        view.writing = writing
        return view
    }

    func updateUIView(_ view: PageView, context: Context) {
        view.writing = writing
    }
}

/// A new page, or a saved one to change. Done keeps it; Cancel leaves the
/// page as it was. What's written is kept on disk as it changes (the
/// draft), so a page closed any other way isn't lost.
struct PageEditorView: View {
    let noteId: String?
    let onDone: (String?) -> Void
    private let typed: String
    private let drawing: PKDrawing
    private let unsaved: Bool
    @State private var writing = true
    @State private var handle = PageHandle()
    /// The simulator check narrows the page's view, as a narrower screen would.
    @State private var debugWidth: CGFloat?
    @Environment(\.dismiss) private var dismiss

    init(noteId: String?, typed: String, drawing: PKDrawing, onDone: @escaping (String?) -> Void) {
        self.noteId = noteId
        self.onDone = onDone
        #if DEBUG
        let start = noteId == nil ? Notebook.debugStart : nil
        Notebook.debugStart = nil
        self.typed = start?.typed ?? typed
        self.drawing = start?.drawing ?? drawing
        unsaved = start != nil
        #else
        self.typed = typed
        self.drawing = drawing
        unsaved = false
        #endif
    }

    var body: some View {
        NavigationStack {
            PageCanvas(noteId: noteId, typed: typed, drawing: drawing, unsaved: unsaved, handle: handle, writing: writing)
                .frame(maxWidth: debugWidth ?? .infinity)
                .ignoresSafeArea(.container, edges: .bottom)
                .navigationTitle(noteId == nil ? "New page" : "Page")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { cancel() } }
                    ToolbarItem(placement: .principal) {
                        Picker("Mode", selection: $writing) {
                            Text("Write").tag(true)
                            Text("Type").tag(false)
                        }
                        .pickerStyle(.segmented)
                        .frame(width: 160)
                    }
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { save() } }
                    ToolbarItem(placement: .primaryAction) {
                        // A screenshot of a slide, say: Write or Type, no
                        // "Allow Paste" alert (it's the system's button).
                        PasteButton(supportedContentTypes: [.image, .plainText]) { providers in
                            handle.view?.pasteItems(providers)
                        }
                        .labelStyle(.iconOnly)
                    }
                }
        }
        #if DEBUG
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugDone)) { _ in save() }
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugWidth)) { note in
            debugWidth = note.object as? CGFloat
        }
        // The editor going away without Done or Cancel.
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugDrop)) { _ in dismiss() }
        #endif
    }

    private func save() {
        if let quickNotes = JOATRuntime.shared.brain?.quickNotes {
            onDone(handle.view.map { $0.save(quickNotes: quickNotes) } ?? Notebook.saveDraft(quickNotes: quickNotes))
        } else {
            handle.view?.writeDraft()
            Log.add("notebook: Done with JOAT Brain not running: the page stays a draft, saved when it starts")
        }
        dismiss()
    }

    private func cancel() {
        if let view = handle.view { view.discardDraft() } else { PageStore.clearDraft() }
        dismiss()
    }
}

#if DEBUG
// MARK: - Simulator check (JOAT_DEBUG_PAGE=1; the Simulator has no Pencil
// and, headless, no screenshots: the app draws its own window to files)

extension Notebook {
    static let debugOpen = Notification.Name("joat.debug.page.open")
    static let debugDone = Notification.Name("joat.debug.page.done")
    static let debugWidth = Notification.Name("joat.debug.page.width")
    static let debugDrop = Notification.Name("joat.debug.page.drop")
    /// What the next new page starts with.
    static var debugStart: (typed: String, drawing: PKDrawing)?

    /// Ink shaped like handwriting: each line's glyph outlines in a script
    /// font, as pen strokes.
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

    /// The app's own windows (the page, the tool picker) as a PNG in Documents.
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
        if let canvas = findCanvas(in: window) {
            let f = canvas.convert(canvas.bounds, to: window)
            let d = canvas.drawing
            let ink = d.strokes.isEmpty ? "none" : "y \(Int(d.bounds.minY))–\(Int(d.bounds.maxY))"
            Log.add("debug: canvas at \(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height)), zoom \(String(format: "%.2f", canvas.zoomScale)), offset \(Int(canvas.contentOffset.y)), "
                + "\(d.strokes.count) strokes \(ink), hidden \(canvas.isHidden), alpha \(canvas.alpha), first \(canvas.isFirstResponder), "
                + "views \(canvas.subviews.map { "\(type(of: $0))\($0.isHidden ? "(hidden)" : "")" })")
        }
    }

    static func findCanvas(in view: UIView) -> PKCanvasView? {
        if let canvas = view as? PKCanvasView { return canvas }
        for sub in view.subviews { if let canvas = findCanvas(in: sub) { return canvas } }
        return nil
    }

    /// The tiles Claude is sent, as files (debug-tile-N.png).
    static func debugTiles(_ drawing: PKDrawing) {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        for (i, png) in PageStore.pngTiles(drawing).enumerated() {
            try? png.write(to: dir.appendingPathComponent("debug-tile-\(i).png"))
        }
    }

    /// The UI test (JOAT_UITEST=1) asks for each picture with a Darwin
    /// notification; the app draws its window to Documents/debug-ui-N.png.
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
        // The page's view 300 points wide, then back: a screen's width changing.
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugWidth, object: CGFloat(300)) }
        }, "com.joat.debug.narrow" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugWidth, object: nil) }
        }, "com.joat.debug.wide" as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, _, _, _ in
            Task { @MainActor in NotificationCenter.default.post(name: Notebook.debugDrop, object: nil) }
        }, "com.joat.debug.drop" as CFString, nil, .deliverImmediately)
        Log.add("debug: UI test pictures on")
    }
    private static var snapCount = 0

    /// The next of three course slides onto the pasteboard, as PNG (the UI
    /// test's own copies, from its runner, didn't load in the app).
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

    /// Notes tab › ✎, write, type, Done, as the wearer would.
    static func debugRun() {
        let drawing = debugDrawing(["Call Mike about the tag map", "on Friday at 3 pm", "Order two spare relays"], top: 140)
        debugStart = ("Site visit, Tuesday", drawing)
        debugTiles(drawing)
        NotificationCenter.default.post(name: debugOpen, object: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { debugSnapshot("1-editor") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { NotificationCenter.default.post(name: debugDone, object: nil) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 9) { debugSnapshot("2-list") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 45) { debugSnapshot("3-list-later") }
    }
}
#endif
