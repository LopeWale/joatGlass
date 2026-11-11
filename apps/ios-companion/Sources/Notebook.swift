import SwiftUI
import PencilKit

/// JOAT Brain's notebook (the wearer, 2026-11-09, after TypeDraw): a page you
/// type on and write or draw on with Apple Pencil or a finger, in one view.
/// A saved page is a QuickNote (QuickNotes.savePage): the glasses list and
/// read it, Claude summarizes it and offers its next steps as reminders, and
/// it can be the prep notes the AI cues answer from. Claude reads the
/// handwriting into text (ClaudeLink.readHandwriting); the ink itself is kept
/// in Documents/pages/<id>.drawing.
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

// MARK: - Saving a page

@MainActor
enum Notebook {
    private static let claude = ClaudeLink()
    /// Pages Claude is reading now (the note's screen says so).
    static private(set) var reading: Set<String> = []

    /// Done on the page: the words and the ink are kept at once; the
    /// handwriting is read when the ink changed. Nil when nothing was saved.
    @discardableResult
    static func save(noteId: String?, typed rawTyped: String, drawing: PKDrawing, inkChanged: Bool,
                     quickNotes: QuickNotes) -> String? {
        let typed = rawTyped.trimmingCharacters(in: .whitespacesAndNewlines)
        let ink = !drawing.strokes.isEmpty
        if noteId == nil, typed.isEmpty, !ink { return nil }
        let id = quickNotes.savePage(id: noteId, typed: typed, inkChanged: inkChanged)
        if inkChanged || noteId == nil { PageStore.save(drawing, id) }
        if ink, inkChanged { read(id, drawing, quickNotes: quickNotes) } else { quickNotes.pageReady(id) }
        return id
    }

    /// Claude reads the handwriting into the page's text.
    static func read(_ id: String, _ drawing: PKDrawing, quickNotes: QuickNotes) {
        let tiles = PageStore.pngTiles(drawing)
        guard !tiles.isEmpty, !reading.contains(id) else { return }
        guard claude.configured else {
            Log.add("notebook: handwriting not read: no Claude key on this phone")
            quickNotes.pageReady(id)
            return
        }
        reading.insert(id)
        Log.add("notebook: reading page \(id.prefix(6)), \(tiles.count) image\(tiles.count == 1 ? "" : "s")")
        claude.readHandwriting(tiles) { result in
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
final class PageView: UIView, UITextViewDelegate, PKCanvasViewDelegate, PKToolPickerObserver {
    private let textScroll = UIScrollView()
    let textView = UITextView()
    let canvas = PKCanvasView()
    private let toolPicker = PKToolPicker()
    private lazy var textScrollDelegate = TextScrollDelegate(page: self)
    var onTyped: ((String) -> Void)?
    var onDrawing: ((PKDrawing) -> Void)?
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

    init(typed: String, drawing: PKDrawing) {
        super.init(frame: .zero)
        backgroundColor = .systemBackground
        for scroller in [textScroll, canvas] as [UIScrollView] {
            scroller.alwaysBounceVertical = true
            scroller.minimumZoomScale = 1
            scroller.maximumZoomScale = 4
        }
        textScroll.keyboardDismissMode = .interactive
        textScroll.delegate = textScrollDelegate
        textView.text = typed
        textView.font = .preferredFont(forTextStyle: .body)
        textView.adjustsFontForContentSizeCategory = true
        textView.isScrollEnabled = false
        textView.backgroundColor = .clear
        textView.textContainerInset = UIEdgeInsets(top: 20, left: 16, bottom: 20, right: 16)
        textView.delegate = self
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
        if toolPicker.selectedToolItem is PKToolPickerInkingItem { lastInk = toolPicker.selectedToolItem }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        // First responders need a window: the tool picker follows the canvas.
        if window != nil { DispatchQueue.main.async { [weak self] in self?.applyMode() } }
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
            + "Pencil only: \(!fingerDraws), tools shown: \(toolPicker.isVisible))")
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        textScroll.frame = bounds
        canvas.frame = bounds
        let width = bounds.width
        guard width > 0 else { return }
        let textHeight = textView.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height
        let drawing = pendingDrawing ?? canvas.drawing
        let ink = drawing.strokes.isEmpty ? 0 : drawing.bounds.maxY
        // Room to keep writing below the last line.
        let size = CGSize(width: width, height: ceil(max(bounds.height * 1.5, textHeight + 240, ink + 400)))
        guard size != pageSize else { return }
        pageSize = size
        // Both in zoomed points, as Apple's PencilKit sample sets the canvas.
        let zoom = canvas.zoomScale
        let zoomed = CGSize(width: size.width * zoom, height: size.height * zoom)
        textView.bounds = CGRect(origin: .zero, size: size)
        textView.center = CGPoint(x: zoomed.width / 2, y: zoomed.height / 2)
        textScroll.contentSize = zoomed
        canvas.contentSize = zoomed
        if let pending = pendingDrawing {
            pendingDrawing = nil
            settingDrawing = true
            canvas.drawing = pending
            settingDrawing = false
        }
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
        let scale = traitCollection.displayScale * textScroll.zoomScale
        func apply(_ view: UIView) {
            view.contentScaleFactor = scale
            view.setNeedsDisplay()
            view.subviews.forEach(apply)
        }
        apply(textView)
        Log.add("notebook: zoom \(String(format: "%.1f", canvas.zoomScale))x")
    }

    #if DEBUG
    deinit { Log.add("notebook: page view closed") }

    /// The UI test picks the eraser (JOAT_UITEST=1, Darwin "com.joat.debug.eraser").
    func debugSelectEraser() {
        guard let eraser = toolPicker.toolItems.first(where: { $0 is PKToolPickerEraserItem }) else { return }
        toolPicker.selectedToolItem = eraser
        Log.add("debug: eraser picked")
    }
    #endif

    func textViewDidChange(_ textView: UITextView) {
        onTyped?(textView.text)
        setNeedsLayout()
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
        onDrawing?(canvasView.drawing)
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

struct PageCanvas: UIViewRepresentable {
    @Binding var typed: String
    @Binding var drawing: PKDrawing
    var writing: Bool

    func makeUIView(context: Context) -> PageView {
        let view = PageView(typed: typed, drawing: drawing)
        let typedBinding = $typed
        let drawingBinding = $drawing
        view.onTyped = { typedBinding.wrappedValue = $0 }
        view.onDrawing = { drawingBinding.wrappedValue = $0 }
        view.writing = writing
        return view
    }

    func updateUIView(_ view: PageView, context: Context) {
        view.writing = writing
    }
}

/// A new page, or a saved one to change. Done keeps it; Cancel leaves the
/// page as it was.
struct PageEditorView: View {
    let noteId: String?
    let onDone: (String?) -> Void
    @State private var typed: String
    @State private var drawing: PKDrawing
    @State private var writing = true
    @Environment(\.dismiss) private var dismiss
    private let originalInk: Data

    init(noteId: String?, typed: String, drawing: PKDrawing, onDone: @escaping (String?) -> Void) {
        self.noteId = noteId
        self.onDone = onDone
        #if DEBUG
        let start = noteId == nil ? Notebook.debugStart : nil
        Notebook.debugStart = nil
        _typed = State(initialValue: start?.typed ?? typed)
        _drawing = State(initialValue: start?.drawing ?? drawing)
        #else
        _typed = State(initialValue: typed)
        _drawing = State(initialValue: drawing)
        #endif
        originalInk = noteId == nil ? PKDrawing().dataRepresentation() : drawing.dataRepresentation()
    }

    var body: some View {
        NavigationStack {
            PageCanvas(typed: $typed, drawing: $drawing, writing: writing)
                .ignoresSafeArea(.container, edges: .bottom)
                .navigationTitle(noteId == nil ? "New page" : "Page")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .principal) {
                        Picker("Mode", selection: $writing) {
                            Text("Write").tag(true)
                            Text("Type").tag(false)
                        }
                        .pickerStyle(.segmented)
                        .frame(width: 160)
                    }
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { save() } }
                }
        }
        #if DEBUG
        .onReceive(NotificationCenter.default.publisher(for: Notebook.debugDone)) { _ in save() }
        #endif
    }

    private func save() {
        if let quickNotes = JOATRuntime.shared.brain?.quickNotes {
            let id = Notebook.save(noteId: noteId, typed: typed, drawing: drawing,
                                   inkChanged: drawing.dataRepresentation() != originalInk, quickNotes: quickNotes)
            onDone(id)
        }
        dismiss()
    }
}

#if DEBUG
// MARK: - Simulator check (JOAT_DEBUG_PAGE=1; the Simulator has no Pencil
// and, headless, no screenshots: the app draws its own window to files)

extension Notebook {
    static let debugOpen = Notification.Name("joat.debug.page.open")
    static let debugDone = Notification.Name("joat.debug.page.done")
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
        Log.add("debug: UI test pictures on")
    }
    private static var snapCount = 0

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
