// Unit tests for Notes insights in JOAT Brain (iOS), design 2026-11-23 Goal 7:
// - handwriting is read after a short writing pause (≈4 s), not 25 s
// - pinch zoom goes OUT below width-fit (fit × 0.5) and IN (≥ 4×), page centred when smaller
// - handwriting OCR provider order: Claude first whenever a Claude key exists, Gemini fallback
// - inline insight status (reading / summarizing / no key / ready / stale)
// - list-row summary line, and coalescing of concurrent summary runs
//
// Run directly via:
//   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift \
//     tests/ios/test_notes_insights.swift

import Foundation

#if !JOAT_COMPILED_WITH_SOURCES
func runEntry() {
    let testFile = URL(fileURLWithPath: #filePath)
    let repoRoot = testFile.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let sourcesDir = repoRoot.appendingPathComponent("apps/ios-companion/Sources")
    let sourceFiles = ["NotesInsights.swift"].map { sourcesDir.appendingPathComponent($0).path }

    let tempDir = FileManager.default.temporaryDirectory
        .appendingPathComponent("joat_test_notes_insights_\(ProcessInfo.processInfo.processIdentifier)")
    try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempDir) }
    let mainSwift = tempDir.appendingPathComponent("main.swift")
    let outBinary = tempDir.appendingPathComponent("runner")
    do { try FileManager.default.copyItem(at: testFile, to: mainSwift) } catch {
        fputs("Failed to stage main.swift: \(error)\n", stderr); exit(1)
    }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    proc.arguments = ["swiftc", "-suppress-warnings", "-DJOAT_COMPILED_WITH_SOURCES"] + sourceFiles + [mainSwift.path, "-o", outBinary.path]
    proc.environment = ProcessInfo.processInfo.environment
    do { try proc.run(); proc.waitUntilExit() } catch { fputs("Failed to launch swiftc: \(error)\n", stderr); exit(1) }
    if proc.terminationStatus != 0 { exit(proc.terminationStatus) }
    let runProc = Process()
    runProc.executableURL = outBinary
    do { try runProc.run(); runProc.waitUntilExit(); exit(runProc.terminationStatus) } catch {
        fputs("Failed to run compiled test binary: \(error)\n", stderr); exit(1)
    }
}
runEntry()
#else

private var passed = 0
private func expect(_ c: @autoclosure () -> Bool, _ m: String, line: UInt = #line) {
    if !c() { fputs("FAIL [line \(line)]: \(m)\n", stderr); exit(1) }
    passed += 1
}
private func expectEqual<T: Equatable>(_ a: T, _ e: T, _ m: String, line: UInt = #line) {
    if a != e { fputs("FAIL [line \(line)]: \(m)\n  actual:   \(a)\n  expected: \(e)\n", stderr); exit(1) }
    passed += 1
}
private func near(_ a: Double, _ b: Double) -> Bool { abs(a - b) < 0.0001 }

// MARK: Writing pause

expect(NotesInsights.idleSeconds >= 3 && NotesInsights.idleSeconds <= 5, "handwriting/summary runs after a ≈4 s writing pause")
expect(NotesInsights.idleSeconds < 25, "the old 25 s idle is gone")

// MARK: Zoom limits (compact phone, unfolded Duo, wide page)

do {
    let phoneFit = 390.0 / 612.0
    let z = NotesInsights.zoomLimits(fit: phoneFit)
    expect(near(z.min, phoneFit * 0.5), "zoom OUT to half of width-fit on the phone")
    expect(z.min < phoneFit, "minimum is below width-fit")
    expect(z.max >= 4, "zoom IN to at least 4×")
    let duo = NotesInsights.zoomLimits(fit: 1100.0 / 612.0)
    expect(duo.min < 1100.0 / 612.0 && duo.max >= 4 && duo.max > duo.min, "unfolded Duo: out and in")
    expect(near(NotesInsights.clampZoom(0.01, fit: phoneFit), phoneFit * 0.5), "clamps to minimum")
    expect(near(NotesInsights.clampZoom(100, fit: phoneFit), z.max), "clamps to maximum")
    expect(near(NotesInsights.clampZoom(phoneFit * 0.7, fit: phoneFit), phoneFit * 0.7), "keeps a zoomed-out scale")
}

// MARK: Centring a zoomed-out page

do {
    let c = NotesInsights.centringInset(content: (width: 200, height: 300), bounds: (width: 400, height: 800))
    expect(near(c.left, 100) && near(c.top, 250), "page smaller than the screen sits in the middle")
    let none = NotesInsights.centringInset(content: (width: 800, height: 2000), bounds: (width: 400, height: 800))
    expect(near(none.left, 0) && near(none.top, 0), "no centring once the page is larger than the screen")
}

// MARK: Handwriting OCR provider order

expectEqual(NotesInsights.ocrOrder(hasClaude: true, hasGemini: true), [.claude, .gemini], "Claude first, Gemini fallback")
expectEqual(NotesInsights.ocrOrder(hasClaude: true, hasGemini: false), [.claude], "Claude only")
expectEqual(NotesInsights.ocrOrder(hasClaude: false, hasGemini: true), [.gemini], "Gemini-only still works")
expectEqual(NotesInsights.ocrOrder(hasClaude: false, hasGemini: false), [], "nothing configured")

// MARK: Inline status

expectEqual(NotesInsights.status(reading: true, summarizing: false, configured: true, hasSummary: false, stale: false, hasContent: true),
            .readingHandwriting, "reading wins")
expectEqual(NotesInsights.status(reading: false, summarizing: true, configured: true, hasSummary: true, stale: true, hasContent: true),
            .summarizing, "summarizing")
expectEqual(NotesInsights.status(reading: false, summarizing: false, configured: false, hasSummary: false, stale: false, hasContent: true),
            .noKey, "clear message without an AI key")
expectEqual(NotesInsights.status(reading: false, summarizing: false, configured: true, hasSummary: true, stale: false, hasContent: true),
            .ready, "ready")
expectEqual(NotesInsights.status(reading: false, summarizing: false, configured: true, hasSummary: true, stale: true, hasContent: true),
            .stale, "edited since the summary")
expectEqual(NotesInsights.status(reading: false, summarizing: false, configured: true, hasSummary: false, stale: false, hasContent: false),
            .empty, "blank page")
expectEqual(NotesInsights.status(reading: false, summarizing: false, configured: true, hasSummary: false, stale: false, hasContent: true),
            .pending, "content waiting for the pause")
expect(NotesInsights.Status.readingHandwriting.label.contains("Reading handwriting"), "reading label")
expect(NotesInsights.Status.summarizing.label.contains("Summarizing"), "summarizing label")
expect(NotesInsights.Status.noKey.label.lowercased().contains("key"), "no-key label names the key")

// MARK: List row summary line

expectEqual(NotesInsights.rowLine(summary: "  Call Mike Friday.  ", keyPoints: ["x"], paragraphs: ["raw"]), "Call Mike Friday.", "summary first")
expectEqual(NotesInsights.rowLine(summary: nil, keyPoints: ["Spare relays"], paragraphs: ["raw"]), "Spare relays", "then key point")
expectEqual(NotesInsights.rowLine(summary: "", keyPoints: [], paragraphs: ["", "first words"]), "first words", "then text")
expectEqual(NotesInsights.rowLine(summary: nil, keyPoints: [], paragraphs: []), nil, "nothing")

// MARK: Coalescing summary runs

do {
    var q = NotesInsights.RunQueue()
    expect(q.begin("a"), "first run starts")
    expect(!q.begin("a"), "second concurrent run does not start")
    expect(q.isRunning("a"), "running")
    expect(q.begin("b"), "other notes are independent")
    expect(q.finish("a"), "a request while running asks for one rerun")
    expect(!q.isRunning("a"), "finished")
    expect(q.begin("a"), "rerun starts")
    expect(!q.finish("a"), "no further rerun")
    expect(!q.finish("b"), "b had no queued request")
}

print("test_notes_insights: \(passed) assertions passed")
#endif
