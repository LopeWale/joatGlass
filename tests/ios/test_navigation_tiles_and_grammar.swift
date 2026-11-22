// Standalone unit test suite for iOS Navigation Completion (Task 2):
// - Web Mercator MKMapRect & slippy-tile sub-pixel alignment vs HudCanvasRenderer.projectMercatorPanel
// - 4-bit G2 monochrome tone curve (histogram mode -> 0, strokes -> 17 * 1..11, never >= 12 * 17)
// - CPU-only slippy tile stitcher orientation & pixel accuracy
// - routeMapCentres stride (180 m walk/cycle, 360 m drive), 24-tile cap, and 30 m dedupe
// - Mode-aware voice grammar (Navigation.parsedQuery) & driving policy
// - One-shot non-destructive legacy place migration, phone CRUD, selectable NAVIGATION menu,
//   clean session replacement, and turn-by-turn spoken headphone cues.
//
// Run directly via:
//   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun swift \
//     tests/ios/test_navigation_tiles_and_grammar.swift

import Foundation

#if !JOAT_COMPILED_WITH_SOURCES
func runEntry() {
    let testFile = URL(fileURLWithPath: #filePath)
    let repoRoot = testFile
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let sourcesDir = repoRoot.appendingPathComponent("apps/ios-companion/Sources")
    let sourceFiles = [
        "Log.swift",
        "Features.swift",
        "Confirm.swift",
        "Prompts.swift",
        "WakeGate.swift",
        "PanelPublisher.swift",
        "PhoneLocation.swift",
        "NavigationMaps.swift",
        "NavigationMapTiles.swift",
        "Navigation.swift",
    ].map { sourcesDir.appendingPathComponent($0).path }

    let tempDir = FileManager.default.temporaryDirectory
        .appendingPathComponent("joat_test_nav_\(ProcessInfo.processInfo.processIdentifier)")
    try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempDir) }

    let mainSwift = tempDir.appendingPathComponent("main.swift")
    let outBinary = tempDir.appendingPathComponent("runner")
    do {
        try FileManager.default.copyItem(at: testFile, to: mainSwift)
    } catch {
        fputs("Failed to stage main.swift: \(error)\n", stderr)
        exit(1)
    }

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/xcrun")
    proc.arguments = ["swiftc", "-suppress-warnings", "-DJOAT_COMPILED_WITH_SOURCES"] + sourceFiles + [mainSwift.path, "-o", outBinary.path]
    proc.environment = ProcessInfo.processInfo.environment
    do {
        try proc.run()
        proc.waitUntilExit()
    } catch {
        fputs("Failed to launch swiftc: \(error)\n", stderr)
        exit(1)
    }
    if proc.terminationStatus != 0 {
        exit(proc.terminationStatus)
    }

    let runProc = Process()
    runProc.executableURL = outBinary
    runProc.environment = ProcessInfo.processInfo.environment
    do {
        try runProc.run()
        runProc.waitUntilExit()
        exit(runProc.terminationStatus)
    } catch {
        fputs("Failed to run compiled test binary: \(error)\n", stderr)
        exit(1)
    }
}
#else

import MapKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

struct CommandReply {
    let text: String
    let activated: Bool
}

protocol VoiceCommand: AnyObject {
    func handle(_ request: String) -> CommandReply?
}

enum Grammar {
    static func match(_ re: NSRegularExpression, _ text: String, groups: [String] = []) -> [String: String]? {
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.firstMatch(in: text, range: range),
              m.range.location == 0, m.range.length == range.length else { return nil }
        var out: [String: String] = [:]
        for g in groups {
            let r = m.range(withName: g)
            if r.location != NSNotFound, let rr = Range(r, in: text) { out[g] = String(text[rr]) }
        }
        return out
    }
}

private var passedAssertions = 0

private func expect(_ condition: @autoclosure () -> Bool, _ message: String, file: StaticString = #file, line: UInt = #line) {
    if !condition() {
        fputs("FAIL [\(file):\(line)]: \(message)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

private func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String, file: StaticString = #file, line: UInt = #line) {
    if actual != expected {
        fputs("FAIL [\(file):\(line)]: \(message)\n  actual:   \(actual)\n  expected: \(expected)\n", stderr)
        exit(1)
    }
    passedAssertions += 1
}

/// Exact port of `HudCanvasRenderer.projectMercatorPanel` in TypeScript.
private func tsProjectMercatorPanel(
    point: NavPoint,
    centre: NavPoint,
    zoom: Int,
    width: Double = 284,
    height: Double = 220
) -> (Double, Double) {
    let worldPixels = 512.0 * pow(2.0, Double(zoom))
    func toWorld(_ lat: Double, _ lon: Double) -> (Double, Double) {
        let clamped = max(-85.05112878, min(85.05112878, lat))
        let s = sin(clamped * .pi / 180.0)
        return (
            ((lon + 180.0) / 360.0) * worldPixels,
            (0.5 - log((1.0 + s) / (1.0 - s)) / (4.0 * .pi)) * worldPixels
        )
    }
    let (cx, cy) = toWorld(centre.latitude, centre.longitude)
    let (px, py) = toWorld(point.latitude, point.longitude)
    return (width / 2.0 + (px - cx), height / 2.0 + (py - cy))
}

private func invertMercatorWorldPixel(x: Double, y: Double, zoom: Int) -> NavPoint {
    let worldPixels = 512.0 * pow(2.0, Double(zoom))
    let lon = (x / worldPixels) * 360.0 - 180.0
    let n = .pi - 2.0 * .pi * (y / worldPixels)
    let lat = (180.0 / .pi) * atan(0.5 * (exp(n) - exp(-n)))
    return NavPoint(lat, lon)
}

private func testWebMercatorMapRectAndSlippyAlignment() {
    let centres = [
        NavPoint(41.8826, -87.6226),   // Chicago
        NavPoint(37.7749, -122.4194),  // San Francisco
        NavPoint(35.6812, 139.7671),   // Tokyo
        NavPoint(-33.8688, 151.2093),  // Sydney
    ]
    let cornerOffsets: [(Double, Double)] = [
        (-142.0, -110.0),
        (142.0, -110.0),
        (-142.0, 110.0),
        (142.0, 110.0),
        (0.0, 0.0),
        (73.25, -48.5),
    ]

    expectEqual(
        NavigationMapTiles.backgroundSlippyTileURL(z: 16, x: 16815, y: 24365)?.absoluteString,
        "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/16/24365/16815",
        "Background slippy URL uses key-free ArcGIS World_Dark_Gray_Base with {z}/{y}/{x} order"
    )

    for zoom in [15, 16] {
        let expectedSlippyZ = min(16, zoom + 1)
        let expectedDrawSize = zoom == 15 ? 256.0 : 512.0
        for centre in centres {
            let rect = NavigationMapTiles.mercatorMapRect(
                latitude: centre.latitude,
                longitude: centre.longitude,
                zoom: zoom,
                width: 284,
                height: 220
            )
            let (cwX, cwY) = NavigationMapTiles.mercatorWorldPixel(
                latitude: centre.latitude,
                longitude: centre.longitude,
                zoom: zoom
            )
            let placements = NavigationMapTiles.slippyPlacements(
                latitude: centre.latitude,
                longitude: centre.longitude,
                zoom: zoom,
                width: 284,
                height: 220
            )
            expect(!placements.isEmpty, "Slippy placements non-empty for z=\(zoom)")
            expectEqual(placements[0].z, expectedSlippyZ, "Slippy zoom clamped to z <= 16")
            expectEqual(placements[0].tileDrawSize, expectedDrawSize, "Slippy tileDrawSize preserves exact 512*2^zoom parity")

            for (dx, dy) in cornerOffsets {
                let pt = invertMercatorWorldPixel(x: cwX + dx, y: cwY + dy, zoom: zoom)
                let (tsX, tsY) = tsProjectMercatorPanel(point: pt, centre: centre, zoom: zoom)
                let (mkX, mkY) = NavigationMapTiles.pixelInMapRect(point: pt, rect: rect, zoom: zoom)
                let drift = hypot(tsX - mkX, tsY - mkY)
                expect(drift < 0.01, "MKMapRect pixel drift < 0.01 px at z=\(zoom) (got \(drift))")

                // Also verify slippy tile screen placement maps the world pixel identically:
                let (pwX, pwY) = NavigationMapTiles.mercatorWorldPixel(
                    latitude: pt.latitude,
                    longitude: pt.longitude,
                    zoom: zoom
                )
                let tileX = Int(floor(pwX / expectedDrawSize))
                let tileY = Int(floor(pwY / expectedDrawSize))
                if let placement = placements.first(where: { $0.rawX == tileX && $0.y == tileY }) {
                    let localX = pwX - Double(tileX) * expectedDrawSize
                    let localY = pwY - Double(tileY) * expectedDrawSize
                    let slippyPanelX = placement.screenX + localX
                    let slippyPanelY = placement.screenY + localY
                    let slippyDrift = hypot(tsX - slippyPanelX, tsY - slippyPanelY)
                    expect(slippyDrift < 0.01, "Slippy tile screen placement drift < 0.01 px (got \(slippyDrift))")
                }
            }
        }
    }
}

private func makeSyntheticRGBTilePNG(markerX: Int?, markerY: Int?) -> Data? {
    let w = 256, h = 256
    var rgba = [UInt8](repeating: 20, count: w * h * 4)
    for i in 0..<(w * h) { rgba[i * 4 + 3] = 255 }
    if let mx = markerX, let my = markerY {
        for dy in -1...1 {
            for dx in -1...1 {
                let x = mx + dx, y = my + dy
                guard (0..<w).contains(x), (0..<h).contains(y) else { continue }
                let idx = (y * w + x) * 4
                rgba[idx] = 255
                rgba[idx + 1] = 255
                rgba[idx + 2] = 255
            }
        }
    }
    let data = Data(rgba)
    guard let provider = CGDataProvider(data: data as CFData),
          let img = CGImage(
              width: w, height: h,
              bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4,
              space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
              provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
          ) else { return nil }
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, img, nil)
    guard CGImageDestinationFinalize(dest) else { return nil }
    return out as Data
}

private func testCpuSlippyStitchPixelOrientation() async {
    for zoom in [15, 16] {
        let centre = NavPoint(41.8826, -87.6226)
        let (cwX, cwY) = NavigationMapTiles.mercatorWorldPixel(latitude: centre.latitude, longitude: centre.longitude, zoom: zoom)
        let drawSize = zoom == 15 ? 256.0 : 512.0
        let scale = drawSize / 256.0
        // Align snapped centre to `scale` so target marker sits cleanly on source tile pixel grid
        let baseWorldX = floor(cwX / scale) * scale
        let baseWorldY = floor(cwY / scale) * scale
        let targetWorldX = baseWorldX + 60.0
        let targetWorldY = baseWorldY - 50.0
        let snappedCentre = invertMercatorWorldPixel(x: baseWorldX, y: baseWorldY, zoom: zoom)
        let markerPoint = invertMercatorWorldPixel(x: targetWorldX, y: targetWorldY, zoom: zoom)
        let (expectedX, expectedY) = tsProjectMercatorPanel(point: markerPoint, centre: snappedCentre, zoom: zoom)

        let targetTileX = Int(floor(targetWorldX / drawSize))
        let targetTileY = Int(floor(targetWorldY / drawSize))
        let localMarkerX = Int(round((targetWorldX - Double(targetTileX) * drawSize) / scale))
        let localMarkerY = Int(round((targetWorldY - Double(targetTileY) * drawSize) / scale))

        let stitched = await NavigationMapTiles.stitchSlippyTiles(
            latitude: snappedCentre.latitude,
            longitude: snappedCentre.longitude,
            zoom: zoom,
            width: 284,
            height: 220
        ) { _, tx, ty in
            if tx == targetTileX && ty == targetTileY {
                return makeSyntheticRGBTilePNG(markerX: localMarkerX, markerY: localMarkerY)
            }
            return makeSyntheticRGBTilePNG(markerX: nil, markerY: nil)
        }

        expect(stitched != nil, "CPU slippy stitch produced PNG Data at z=\(zoom)")
        if let png = stitched,
           let src = CGImageSourceCreateWithData(png as CFData, nil),
           let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) {
            expectEqual(cg.width, 284, "Stitched PNG width == 284")
            expectEqual(cg.height, 220, "Stitched PNG height == 220")
            expectEqual(cg.bitsPerPixel, 8, "Stitched PNG is 8-bit grayscale")

            var gray = [UInt8](repeating: 0, count: 284 * 220)
            gray.withUnsafeMutableBytes { ptr in
                if let ctx = CGContext(
                    data: ptr.baseAddress,
                    width: 284, height: 220,
                    bitsPerComponent: 8, bytesPerRow: 284,
                    space: CGColorSpaceCreateDeviceGray(),
                    bitmapInfo: CGImageAlphaInfo.none.rawValue
                ) {
                    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: 284, height: 220))
                }
            }
            let ex = Int(expectedX.rounded())
            let ey = Int(expectedY.rounded())
            let markerVal = gray[ey * 284 + ex]
            let mirroredVal = gray[(219 - ey) * 284 + ex]
            expectEqual(markerVal, 187, "Bright marker landed at exact top-down predicted pixel (\(ex), \(ey)) at z=\(zoom)")
            expectEqual(mirroredVal, 0, "Vertically mirrored Y coordinate is unlit ground (0), confirming upright orientation at z=\(zoom)")
        }
    }
}

private func testG2ToneCurve() {
    // 1. Uniform tile -> all 0 (clear glass)
    let uniform = [UInt8](repeating: 42, count: 1000)
    let uniformOut = NavigationMapTiles.applyG2ToneCurve(luminance: uniform)
    expect(uniformOut.allSatisfy { $0 == 0 }, "Uniform tile maps completely to 0")

    // 2. Realistic dark-map distribution: ground mode at 24, sub-ground noise at 12,
    //    buildings at 40, minor streets at 85, major roads at 145, street labels at 255.
    var sample = [UInt8](repeating: 24, count: 700)
    sample += [UInt8](repeating: 12, count: 50)
    sample += [40, 85, 145, 200, 255]

    let mapped = NavigationMapTiles.applyG2ToneCurve(luminance: sample)
    expectEqual(mapped[0], 0, "Histogram mode (ground=24) maps to 0")
    expectEqual(mapped[700], 0, "Sub-ground value (12 <= 24) maps to 0")

    let strokes = Array(mapped.suffix(5))
    let allowedLevels = Set((1...11).map { UInt8($0 * 17) })
    for v in strokes {
        expect(allowedLevels.contains(v), "Stroke tone \(v) is in 17*(1...11)")
        expect(v < 12 * 17, "Stroke tone \(v) < 204 so 12..15 remain reserved for route rails")
    }
    for (a, b) in zip(strokes, strokes.dropFirst()) {
        expect(a <= b, "Tone curve is monotonic (\(a) <= \(b))")
    }
    expectEqual(strokes.last, 187, "Peak white label (255) maps to ceiling 11 * 17 = 187")

    // 3. Verify 8-bit grayscale PNG encoding
    let pixels = [UInt8](repeating: 0, count: 284 * 219) + [UInt8](repeating: 187, count: 284)
    let png = NavigationMapTiles.encodeGrayscalePNG(pixels: pixels, width: 284, height: 220)
    expect(png != nil && !(png!.isEmpty), "Encoded 284x220 8-bit grayscale PNG")
}

private func testRouteMapCentresStrideCapAndDedupe() {
    let start = NavPoint(41.8800, -87.6300)
    // ~540 m north (1 deg lat ~ 111,194.9 m -> 540 m ~ 0.0048563 deg)
    let end540 = NavPoint(41.8800 + 540.0 / 111_194.9, -87.6300)
    let walkCentres = NavigationMapTiles.routeMapCentres(geometry: [start, end540], mode: "walking")
    expectEqual(walkCentres.count, 4, "540 m walking route yields 4 centres at 180 m stride")

    // ~550 m north: 3 * 180 = 540 m, and end at 550 m is 10 m (< 30 m dedupe) from 540 m
    let end550 = NavPoint(41.8800 + 550.0 / 111_194.9, -87.6300)
    let cycleCentres = NavigationMapTiles.routeMapCentres(geometry: [start, end550], mode: "cycling")
    expectEqual(cycleCentres.count, 4, "550 m cycling route dedupes end within 30 m of 540 m sample")

    // ~720 m north in driving mode (360 m stride -> 0, 360, 720 = 3 centres)
    let end720 = NavPoint(41.8800 + 720.0 / 111_194.9, -87.6300)
    let driveCentres = NavigationMapTiles.routeMapCentres(geometry: [start, end720], mode: "driving")
    expectEqual(driveCentres.count, 3, "720 m driving route yields 3 centres at 360 m stride")
    expectEqual(NavigationMapTiles.zoom(for: "driving"), 15, "Driving zoom is 15")
    expectEqual(NavigationMapTiles.zoom(for: "walking"), 16, "Walking zoom is 16")

    // 25 km highway drive -> capped at <= 24 tiles
    let end25km = NavPoint(41.8800 + 25_000.0 / 111_194.9, -87.6300)
    let longDrive = NavigationMapTiles.routeMapCentres(geometry: [start, end25km], mode: "driving")
    expect(longDrive.count <= 24, "25 km driving route capped at <= 24 tiles (got \(longDrive.count))")
    expect(NavigationMapTiles.moveMeters(longDrive.first!, start) < 1.0, "Includes route start")
    expect(NavigationMapTiles.moveMeters(longDrive.last!, end25km) < 30.0, "Covers route end")

    // Out-and-back loop returning to within 5 m of start -> deduped so no pair < 30 m
    let mid = NavPoint(41.8800 + 360.0 / 111_194.9, -87.6300)
    let nearStart = NavPoint(41.8800 + 5.0 / 111_194.9, -87.6300)
    let loopCentres = NavigationMapTiles.routeMapCentres(geometry: [start, mid, nearStart], mode: "walking")
    for i in 0..<loopCentres.count {
        for j in (i + 1)..<loopCentres.count {
            let d = NavigationMapTiles.moveMeters(loopCentres[i], loopCentres[j])
            expect(d >= 30.0, "All returned centres are >= 30 m apart (got \(d) m)")
        }
    }
}

private func testParsedQueryAndPolicies() {
    if let q1 = Navigation.parsedQuery("walk to Starbucks") {
        expectEqual(q1.destination, "Starbucks", "walk to Starbucks destination")
        expectEqual(q1.mode, "walking", "walk to Starbucks mode")
    } else {
        expect(false, "Expected match for 'walk to Starbucks'")
    }

    if let q2 = Navigation.parsedQuery("drive home") {
        expectEqual(q2.destination, "home", "drive home destination")
        expectEqual(q2.mode, "driving", "drive home mode")
    } else {
        expect(false, "Expected match for 'drive home'")
    }

    if let q3 = Navigation.parsedQuery("bike to the park") {
        expectEqual(q3.destination, "the park", "bike to the park destination")
        expectEqual(q3.mode, "cycling", "bike to the park mode")
    } else {
        expect(false, "Expected match for 'bike to the park'")
    }

    if let q4 = Navigation.parsedQuery("Hey JOAT, driving directions to 233 S Wacker Dr!") {
        expectEqual(q4.destination, "233 S Wacker Dr", "driving directions destination")
        expectEqual(q4.mode, "driving", "driving directions mode")
    } else {
        expect(false, "Expected match for 'driving directions to ...'")
    }

    if let q5 = Navigation.parsedQuery("walking directions to Union Station") {
        expectEqual(q5.destination, "Union Station", "walking directions destination")
        expectEqual(q5.mode, "walking", "walking directions mode")
    } else {
        expect(false, "Expected match for 'walking directions to ...'")
    }

    if let q6 = Navigation.parsedQuery("cycling directions to Millennium Park") {
        expectEqual(q6.destination, "Millennium Park", "cycling directions destination")
        expectEqual(q6.mode, "cycling", "cycling directions mode")
    } else {
        expect(false, "Expected match for 'cycling directions to ...'")
    }

    if let q7 = Navigation.parsedQuery("navigate to Navy Pier") {
        expectEqual(q7.destination, "Navy Pier", "navigate to Navy Pier destination")
        expectEqual(q7.mode, nil, "generic navigate leaves mode nil")
    } else {
        expect(false, "Expected match for 'navigate to Navy Pier'")
    }

    expect(Navigation.parsedQuery("drive carefully today") == nil, "Non-navigation 'drive carefully' ignored")
    expect(Navigation.parsedQuery("walk the dog") == nil, "Non-navigation 'walk the dog' ignored")

    let drivePolicy = NavigationMaps.policy("driving")
    expectEqual(drivePolicy["corridorRadiusM"] as? Double, 50.0, "Driving corridorRadiusM == 50")
    expectEqual(drivePolicy["arrivalRadiusM"] as? Double, 40.0, "Driving arrivalRadiusM == 40")
    expectEqual(drivePolicy["maxAccuracyM"] as? Double, 45.0, "Driving maxAccuracyM == 45")
    expectEqual(drivePolicy["searchForwardM"] as? Double, 1500.0, "Driving searchForwardM == 1500")
}

private final class MockPluginLink: PluginLink {
    var sent: [[String: Any]] = []
    func send(_ object: [String: Any]) {
        sent.append(object)
    }
}

private func testPlaceMigrationMenuAndSessionLifecycle() {
    let suiteName = "com.joat.test.nav.\(UUID().uuidString)"
    guard let defaults = UserDefaults(suiteName: suiteName) else {
        expect(false, "Created isolated UserDefaults suite")
        return
    }
    defer { defaults.removePersistentDomain(forName: suiteName) }

    let link = MockPluginLink()
    let nav = Navigation(link: link, defaults: defaults)
    expectEqual(nav.spokenDirections, true, "spokenDirections defaults to true")

    // 1. Legacy plugin push imports once
    let legacyHome: [String: Any] = [
        "id": "legacy-home",
        "name": "Home",
        "address": "100 Main St",
        "latitude": 41.881,
        "longitude": -87.632,
    ]
    let legacyWork: [String: Any] = [
        "id": "legacy-work",
        "name": "Work",
        "address": "233 S Wacker Dr",
        "latitude": 41.8789,
        "longitude": -87.6359,
    ]
    nav.acceptPlaces(["places": [legacyHome, legacyWork]])
    expectEqual(nav.places.map(\.id), ["legacy-home", "legacy-work"], "Imported initial legacy places")

    // 2. Empty `places: []` on reconnect never wipes phone places
    nav.acceptPlaces(["places": []])
    expectEqual(nav.places.count, 2, "Empty legacy places array ignored")

    // 3. Delete `legacy-home` on phone; reconnect with `[legacyHome, legacyWork, legacyGym]`
    //    must NOT resurrect `legacy-home`, while importing new `legacy-gym` once!
    nav.removePlace(id: "legacy-home")
    expectEqual(nav.places.map(\.id), ["legacy-work"], "Removed legacy-home")

    let legacyGym: [String: Any] = [
        "id": "legacy-gym",
        "name": "Gym",
        "address": "50 E Monroe St",
        "latitude": 41.8807,
        "longitude": -87.6258,
    ]
    nav.acceptPlaces(["places": [legacyHome, legacyWork, legacyGym]])
    expectEqual(nav.places.map(\.id), ["legacy-work", "legacy-gym"], "Deleted legacy-home never resurrected; new legacy-gym imported once")

    // 4. Phone upsert & offline guard
    let coffee = nav.upsertPlace(name: "Blue Bottle", address: "313 W Wolf Point", point: NavPoint(41.888, -87.636))
    expect(coffee != nil, "Upserted phone place")
    expectEqual(nav.places.count, 3, "Three saved places stored")

    nav.isPluginConnected = { false }
    let disconnectedErr = nav.startFromPhone(destination: "Work", mode: "driving")
    expectEqual(disconnectedErr, "Connect Even G2 in Even Hub first.", "Disconnected startFromPhone returns helpful error")

    // 5. Connected startFromPhone + clean session replacement + selectable menu rows
    nav.isPluginConnected = { true }
    let err1 = nav.startFromPhone(destination: "Work", mode: "driving")
    expectEqual(err1, nil, "First startFromPhone succeeded")
    let firstSessionId = nav.session?.sessionId
    expect(firstSessionId != nil, "Active session created")

    let activeRows = nav.pageRows()
    expect(activeRows.count == 6, "Menu has 2 active controls + 1 prompt row + 3 saved places")
    expect(activeRows[0].hasPrefix("➤ Show active guidance (Work)"), "Row 0 shows active guidance")
    expect(activeRows[1].hasPrefix("⏹ Stop navigation (Work)"), "Row 1 stops navigation")
    expectEqual(activeRows[2], "➤ Navigate to a place…", "Row 2 opens Where to? prompt")
    expect(activeRows[3].hasPrefix("➤ Work · 233 S Wacker Dr"), "Row 3 is tappable Work saved place")

    // Tapping Row 0 sends navigation.resume
    nav.selectPageRow(0)
    expect(link.sent.contains(where: { $0.string("kind") == "navigation.resume" && $0.string("sessionId") == firstSessionId }),
           "Tapping row 0 sends navigation.resume")

    // Starting a second route cleanly replaces the first instead of rejecting!
    let err2 = nav.start("Navy Pier", initialMode: "walking")
    expectEqual(err2, nil, "Second start cleanly replaced active session")
    expect(nav.session?.sessionId != firstSessionId, "New session ID assigned")
    expect(link.sent.contains(where: { $0.string("kind") == "navigation.stop" && $0.string("sessionId") == firstSessionId }),
           "Old session sent navigation.stop on replacement")

    // Tapping Row 1 on refreshed menu stops navigation in place
    _ = nav.pageRows()
    nav.selectPageRow(1)
    expect(nav.session == nil, "Tapping Stop navigation ended session")
    expect(nav.activeSummary == nil, "activeSummary cleared on stop")
    let idleRows = nav.pageRows()
    expectEqual(idleRows[0], "➤ Navigate to a place…", "Idle menu starts with Navigate to a place…")

    // 6. Tapping '➤ Navigate to a place…' opens Prompts.ask with ownerKind == "capability"
    let prompts = Prompts(link: link)
    nav.prompts = prompts
    link.sent.removeAll()
    nav.selectPageRow(0)
    let promptFrame = link.sent.first { $0.string("kind") == "prompt.present" }
    expect(promptFrame != nil, "Tapping Navigate to a place… sends prompt.present")
    expectEqual(promptFrame?.string("ownerKind"), "capability", "prompt.present uses ownerKind == capability")
    expectEqual(promptFrame?.string("question"), "Where to?", "prompt.present asks Where to?")
    expectEqual(promptFrame?.string("field"), "destination", "prompt.present sets field == destination")
    prompts.cancel("test cleanup")
}

private func testSpokenTurnByTurnCues() {
    let suiteName = "com.joat.test.cues.\(UUID().uuidString)"
    guard let defaults = UserDefaults(suiteName: suiteName) else {
        expect(false, "Created cues UserDefaults suite")
        return
    }
    defer { defaults.removePersistentDomain(forName: suiteName) }

    let link = MockPluginLink()
    let nav = Navigation(link: link, defaults: defaults)
    var cues: [String] = []
    nav.sayCue = { cues.append($0) }

    // Seed an active driving session to a coordinate candidate and simulate origin + route + state uplinks
    let dest = NavCandidate(
        choiceId: "mk-pier",
        label: "Navy Pier",
        address: "600 E Grand Ave",
        point: NavPoint(41.8917, -87.6086)
    )
    _ = nav.startFromPhone(destination: "Navy Pier", candidate: dest, mode: "driving")
    guard let session = nav.session else {
        expect(false, "Session created for cues test")
        return
    }
    let sessionId = session.sessionId

    let sampleRoute = NavRoute(
        routeId: "mk-test-route-1",
        etaMinutes: 6.0,
        mode: "driving",
        geometry: [
            NavPoint(41.8789, -87.6359),
            NavPoint(41.8917, -87.6240),
            NavPoint(41.8917, -87.6086),
        ],
        steps: [
            NavStep(id: "step-1", maneuver: "depart", instruction: "Head north on N Wacker Dr", alongM: 0),
            NavStep(id: "step-2", maneuver: "turn_right", instruction: "Turn right onto E Grand Ave", alongM: 1400),
            NavStep(id: "step-3", maneuver: "arrive", instruction: "You have arrived at your destination", alongM: 2500),
        ]
    )
    session.injectRouteForTesting(sampleRoute, origin: NavPoint(41.8789, -87.6359), installNow: true)
    expect(link.sent.contains(where: { $0.string("kind") == "navigation.route" && $0.string("routeId") == "mk-test-route-1" }),
           "Auto-installed route sent navigation.route without preview")

    // Acknowledge route installation -> speaks start cue, claims G2 lens via navigation.resume, + updates activeSummary
    nav.handle(kind: "navigation.route.ack", message: [
        "sessionId": sessionId,
        "routeId": "mk-test-route-1",
        "revision": 1,
        "outcome": "installed",
    ])
    expect(link.sent.contains(where: { $0.string("kind") == "navigation.resume" && $0.string("sessionId") == sessionId }),
           "autoInstall route ack sends navigation.resume so Start on Glasses claims G2 lens")
    expectEqual(cues.count, 1, "Spoke route start cue on install")
    expect(cues[0].contains("Starting driving directions to Navy Pier"), "Start cue mentions mode and destination")
    expectEqual(nav.activeSummary?.phase, "guidance", "activeSummary enters guidance phase")
    expectEqual(nav.activeSummary?.routeGeometry.count, 3, "activeSummary exposes routeGeometry")

    // Advance to step-2 far out (350 m > 100 m driving threshold) -> speaks once, dedupes at 310 m
    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 1,
        "state": "guidance", "stepId": "step-2", "maneuver": "turn_right",
        "instruction": "Turn right onto E Grand Ave", "maneuverDistanceM": 350.0,
        "remainingDistanceM": 1450.0, "etaMinutes": 4.0,
    ])
    expectEqual(cues.count, 2, "Spoke new step cue at 350 m")
    expect(cues[1].contains("In 350 meters, turn right onto E Grand Ave"), "Formatted distant step cue")

    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 2,
        "state": "guidance", "stepId": "step-2", "maneuver": "turn_right",
        "instruction": "Turn right onto E Grand Ave", "maneuverDistanceM": 310.0,
    ])
    expectEqual(cues.count, 2, "Did not repeat step-2 cue before approach threshold")

    // Cross <= 100 m driving threshold (90 m) -> speaks approach cue once, dedupes at 60 m
    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 3,
        "state": "guidance", "stepId": "step-2", "maneuver": "turn_right",
        "instruction": "Turn right onto E Grand Ave", "maneuverDistanceM": 90.0,
    ])
    expectEqual(cues.count, 3, "Spoke <= 100 m driving approach cue")
    expect(cues[2].contains("In 90 meters, turn right onto E Grand Ave"), "Approach cue spoken at 90 m")

    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 4,
        "state": "guidance", "stepId": "step-2", "maneuver": "turn_right",
        "instruction": "Turn right onto E Grand Ave", "maneuverDistanceM": 60.0,
    ])
    expectEqual(cues.count, 3, "Approach cue not repeated at 60 m")

    // Wrong-way + off-route + muted check + arrival
    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 5,
        "state": "wrong_way",
    ])
    expectEqual(cues.count, 4, "Spoke wrong_way alert")
    expect(cues[3].contains("Wrong direction"), "Wrong direction wording")

    nav.spokenDirections = false
    expectEqual(defaults.bool(forKey: Navigation.spokenDirectionsKey), false, "Persisted spokenDirections = false")
    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 6,
        "state": "off_route",
    ])
    expectEqual(cues.count, 4, "Muted spokenDirections suppresses off_route audio")
    expectEqual(nav.activeSummary?.phase, "rerouting", "activeSummary still updates to rerouting while muted")

    nav.spokenDirections = true
    expectEqual(defaults.bool(forKey: Navigation.spokenDirectionsKey), true, "Persisted spokenDirections = true")
    nav.handle(kind: "navigation.state", message: [
        "sessionId": sessionId, "routeId": "mk-test-route-1", "revision": 1, "sequence": 7,
        "state": "arrived",
    ])
    expectEqual(cues.count, 5, "Spoke arrival cue once re-enabled")
    expect(cues[4].contains("You have arrived at Navy Pier"), "Arrival cue names destination")
    expectEqual(nav.activeSummary?.arrived, true, "activeSummary marked arrived")
    expectEqual(nav.activeSummary?.phase, "arrived", "activeSummary phase == arrived")
    nav.stopFromPhone()
}

func runEntry() {
    let sema = DispatchSemaphore(value: 0)
    Task { @MainActor in
        testWebMercatorMapRectAndSlippyAlignment()
        await testCpuSlippyStitchPixelOrientation()
        testG2ToneCurve()
        testRouteMapCentresStrideCapAndDedupe()
        testParsedQueryAndPolicies()
        testPlaceMigrationMenuAndSessionLifecycle()
        testSpokenTurnByTurnCues()
        print("PASS: all \(passedAssertions) iOS navigation tile, Mercator, tone-curve, grammar, and place-migration assertions succeeded.")
        sema.signal()
    }
    while sema.wait(timeout: .now()) == .timedOut {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
}
#endif

runEntry()
