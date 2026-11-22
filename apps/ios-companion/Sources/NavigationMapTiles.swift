import Foundation
import MapKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
#if canImport(UIKit)
import UIKit
#endif

struct RenderedMapTile: Equatable {
    let latitude: Double
    let longitude: Double
    let zoom: Int
    let width: Int
    let height: Int
    let png: Data
}

/// Key-free street map tiles for Even G2 navigation:
/// - Foreground (`UIApplication.shared.applicationState == .active`): Apple Maps
///   dark `MKMapSnapshotter` at 1x scale over the exact 512*2^zoom Web Mercator
///   `MKMapRect` expected by `HudCanvasRenderer.projectMercatorPanel`, immediately
///   cancelled on `willResignActiveNotification` so Metal is never touched in the
///   background (`gpus_ReturnNotPermittedKillClient`).
/// - Background / inactive: pure-CPU (`URLSession` + `CGImageSource` + `CGContext`,
///   zero Metal) stitch of key-free ArcGIS World Dark Gray Base slippy tiles
///   (`.../World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, clamped to `z <= 16`
///   with exact `tileDrawSize` scaling so `512 * 2^zoom` alignment holds at all zooms).
/// - Both paths pass through JOAT's 4-bit G2 monochrome tone curve (`0` for
///   `<= histogramMode`, levels `1..11 * 17` above, reserving `12..15` for route rails)
///   and encode an 8-bit grayscale PNG.
@MainActor
final class NavigationMapTiles {
    static let shared = NavigationMapTiles()

    nonisolated static let defaultWidth = 284
    nonisolated static let defaultHeight = 220
    nonisolated static let maxTilesDefault = 24
    nonisolated static let dedupeMeters = 30.0
    nonisolated static let maxMercatorLatitude = 85.05112878
    nonisolated static let maxSlippyZoom = 16

    struct SlippyPlacement: Equatable {
        let z: Int
        let x: Int
        let y: Int
        let rawX: Int
        let screenX: Double
        let screenY: Double
        let tileDrawSize: Double
    }

    private let maxCachedPanels = 64
    private let maxCachedSlippyTiles = 128
    private var panelCache: [String: Data] = [:]
    private var panelOrder: [String] = []
    private var slippyCache: [String: Data] = [:]
    private var slippyOrder: [String] = []

    private let tileSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3.0
        config.timeoutIntervalForResource = 3.0
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    #if os(iOS)
    private var activeSnapshotters: [UUID: MKMapSnapshotter] = [:]
    private var lifecycleObservers: [NSObjectProtocol] = []
    #endif

    init() {
        #if os(iOS)
        let nc = NotificationCenter.default
        lifecycleObservers.append(
            nc.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.cancelActiveSnapshotters(reason: "willResignActive")
                }
            }
        )
        lifecycleObservers.append(
            nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.cancelActiveSnapshotters(reason: "didEnterBackground")
                }
            }
        )
        #endif
    }

    #if os(iOS)
    private func cancelActiveSnapshotters(reason: String) {
        guard !activeSnapshotters.isEmpty else { return }
        let count = activeSnapshotters.count
        let running = Array(activeSnapshotters.values)
        activeSnapshotters.removeAll()
        for snap in running {
            snap.cancel()
        }
        Log.add("navigation map cancelled \(count) active snapshotter(s) (\(reason))")
    }
    #endif

    // MARK: - Pure Projection, Route Stride & G2 Tone Curve Math

    nonisolated static func zoom(for mode: String) -> Int {
        mode == "driving" ? 15 : 16
    }

    nonisolated static func strideMeters(for mode: String) -> Double {
        mode == "driving" ? 360.0 : 180.0
    }

    /// Equirectangular great-circle-ish distance matching `map_image.py` `map_move_meters`.
    nonisolated static func moveMeters(_ a: NavPoint, _ b: NavPoint) -> Double {
        let meanLat = ((a.latitude + b.latitude) / 2.0) * .pi / 180.0
        let dx = (b.longitude - a.longitude) * .pi / 180.0 * cos(meanLat)
        let dy = (b.latitude - a.latitude) * .pi / 180.0
        return hypot(dx, dy) * 6_371_000.0
    }

    /// World pixel coordinates under the 512-px Web Mercator tile scheme used by
    /// `HudCanvasRenderer.projectMercatorPanel`.
    nonisolated static func mercatorWorldPixel(latitude: Double, longitude: Double, zoom: Int) -> (x: Double, y: Double) {
        let worldPixels = 512.0 * pow(2.0, Double(zoom))
        let clamped = max(-maxMercatorLatitude, min(maxMercatorLatitude, latitude))
        let sinLat = sin(clamped * .pi / 180.0)
        let x = ((longitude + 180.0) / 360.0) * worldPixels
        let y = (0.5 - log((1.0 + sinLat) / (1.0 - sinLat)) / (4.0 * .pi)) * worldPixels
        return (x, y)
    }

    /// Exact port of `HudCanvasRenderer.projectMercatorPanel` for panel-local or HUD-global coordinates.
    nonisolated static func projectMercatorPanel(
        point: NavPoint,
        centre: NavPoint,
        zoom: Int,
        boundsX: Double = 0,
        boundsY: Double = 0,
        width: Int = defaultWidth,
        height: Int = defaultHeight
    ) -> (x: Double, y: Double) {
        let (cx, cy) = mercatorWorldPixel(latitude: centre.latitude, longitude: centre.longitude, zoom: zoom)
        let (px, py) = mercatorWorldPixel(latitude: point.latitude, longitude: point.longitude, zoom: zoom)
        return (
            boundsX + Double(width) / 2.0 + (px - cx),
            boundsY + Double(height) / 2.0 + (py - cy)
        )
    }

    /// Exact Web Mercator `MKMapRect` matching `512 * 2^zoom` pixel scale at `scale = 1.0`.
    nonisolated static func mercatorMapRect(
        latitude: Double,
        longitude: Double,
        zoom: Int,
        width: Int = defaultWidth,
        height: Int = defaultHeight
    ) -> MKMapRect {
        let clamped = max(-maxMercatorLatitude, min(maxMercatorLatitude, latitude))
        let mapPointsPerPixel = MKMapSize.world.width / (512.0 * pow(2.0, Double(zoom)))
        let centre = MKMapPoint(CLLocationCoordinate2D(latitude: clamped, longitude: longitude))
        let mapWidth = Double(width) * mapPointsPerPixel
        let mapHeight = Double(height) * mapPointsPerPixel
        return MKMapRect(
            x: centre.x - mapWidth / 2.0,
            y: centre.y - mapHeight / 2.0,
            width: mapWidth,
            height: mapHeight
        )
    }

    /// Pixel position `(0..<width, 0..<height)` of `point` inside `mercatorMapRect`.
    nonisolated static func pixelInMapRect(point: NavPoint, rect: MKMapRect, zoom: Int) -> (x: Double, y: Double) {
        let clamped = max(-maxMercatorLatitude, min(maxMercatorLatitude, point.latitude))
        let mapPointsPerPixel = MKMapSize.world.width / (512.0 * pow(2.0, Double(zoom)))
        let mp = MKMapPoint(CLLocationCoordinate2D(latitude: clamped, longitude: point.longitude))
        return (
            (mp.x - rect.origin.x) / mapPointsPerPixel,
            (mp.y - rect.origin.y) / mapPointsPerPixel
        )
    }

    /// Key-free ArcGIS World Dark Gray Base tile URL (`{z}/{y}/{x}` row-before-column order).
    nonisolated static func backgroundSlippyTileURL(z: Int, x: Int, y: Int) -> URL? {
        URL(string: "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/\(z)/\(y)/\(x)")
    }

    /// Web Mercator slippy tile placements clamped to `z = min(16, zoom + 1)`
    /// (`World_Dark_Gray_Base` native pyramid ceiling) with `tileDrawSize` scaled so
    /// `tileDrawSize * 2^z == 512 * 2^zoom` (`256.0` at `zoom == 15`, `512.0` at `zoom == 16`).
    nonisolated static func slippyPlacements(
        latitude: Double,
        longitude: Double,
        zoom: Int,
        width: Int = defaultWidth,
        height: Int = defaultHeight
    ) -> [SlippyPlacement] {
        let idealZ = zoom + 1
        let z = min(maxSlippyZoom, idealZ)
        let tileDrawSize = 256.0 * pow(2.0, Double(max(0, idealZ - z)))
        let numTiles = 1 << z
        let (centreX, centreY) = mercatorWorldPixel(latitude: latitude, longitude: longitude, zoom: zoom)
        let topLeftX = centreX - Double(width) / 2.0
        let topLeftY = centreY - Double(height) / 2.0
        let bottomRightX = topLeftX + Double(width)
        let bottomRightY = topLeftY + Double(height)

        let minTileX = Int(floor(topLeftX / tileDrawSize))
        let maxTileX = Int(floor((bottomRightX - 1e-9) / tileDrawSize))
        let minTileY = max(0, Int(floor(topLeftY / tileDrawSize)))
        let maxTileY = min(numTiles - 1, Int(floor((bottomRightY - 1e-9) / tileDrawSize)))
        guard minTileX <= maxTileX, minTileY <= maxTileY else { return [] }

        var out: [SlippyPlacement] = []
        for ty in minTileY...maxTileY {
            for rawX in minTileX...maxTileX {
                let wrappedX = ((rawX % numTiles) + numTiles) % numTiles
                out.append(
                    SlippyPlacement(
                        z: z,
                        x: wrappedX,
                        y: ty,
                        rawX: rawX,
                        screenX: Double(rawX) * tileDrawSize - topLeftX,
                        screenY: Double(ty) * tileDrawSize - topLeftY,
                        tileDrawSize: tileDrawSize
                    )
                )
            }
        }
        return out
    }

    /// Sample street tile centres along a route polyline:
    /// 180 m stride for `walking`/`cycling`, 360 m stride for `driving`,
    /// capped at `maxTiles` (24), with 30 m deduplication.
    nonisolated static func routeMapCentres(
        geometry: [NavPoint],
        mode: String,
        maxTiles: Int = maxTilesDefault
    ) -> [NavPoint] {
        guard maxTiles > 0, !geometry.isEmpty else { return [] }
        if geometry.count < 2 || maxTiles == 1 {
            return [geometry[0]]
        }

        var legs: [Double] = []
        legs.reserveCapacity(geometry.count - 1)
        for (a, b) in zip(geometry, geometry.dropFirst()) {
            legs.append(moveMeters(a, b))
        }
        let total = legs.reduce(0, +)
        let baseStride = strideMeters(for: mode)
        let stride = total > 0 ? max(baseStride, total / Double(max(1, maxTiles - 1))) : baseStride

        var centres: [NavPoint] = [geometry[0]]
        var nextAt = stride
        var walked = 0.0

        for (pair, leg) in zip(zip(geometry, geometry.dropFirst()), legs) {
            let (a, b) = pair
            while leg > 0, nextAt <= walked + leg {
                let fraction = (nextAt - walked) / leg
                let candidate = NavPoint(
                    a.latitude + (b.latitude - a.latitude) * fraction,
                    a.longitude + (b.longitude - a.longitude) * fraction
                )
                if !centres.contains(where: { moveMeters($0, candidate) < dedupeMeters }) {
                    if centres.count < maxTiles {
                        centres.append(candidate)
                    }
                }
                nextAt += stride
            }
            walked += leg
        }

        let end = geometry[geometry.count - 1]
        if !centres.contains(where: { moveMeters($0, end) < dedupeMeters }) {
            if centres.count < maxTiles {
                centres.append(end)
            } else {
                centres[maxTiles - 1] = end
            }
        }
        return Array(centres.prefix(maxTiles))
    }

    nonisolated static func luminance(r: UInt8, g: UInt8, b: UInt8) -> UInt8 {
        UInt8(clamping: Int((0.299 * Double(r) + 0.587 * Double(g) + 0.114 * Double(b)).rounded()))
    }

    /// 4-bit Even G2 monochrome tone curve (`map_image.py` `_tone_curve`):
    /// - 256-bin histogram mode `ground` -> `0` (transparent glass)
    /// - `> ground` -> `round(1 + ((v - ground) / max(1, 255 - ground))^0.5 * 10) * 17`
    ///   (levels `1..11` -> `17..187`, reserving `12..15` for route rails & wearer dot).
    nonisolated static func applyG2ToneCurve(luminance: [UInt8]) -> [UInt8] {
        guard !luminance.isEmpty else { return [] }
        var histogram = [Int](repeating: 0, count: 256)
        for v in luminance {
            histogram[Int(v)] &+= 1
        }
        var ground = 0
        var bestCount = -1
        for i in 0..<256 where histogram[i] > bestCount {
            bestCount = histogram[i]
            ground = i
        }

        let denom = Double(max(1, 255 - ground))
        var lut = [UInt8](repeating: 0, count: 256)
        for v in 0..<256 {
            if v <= ground {
                lut[v] = 0
            } else {
                let normalized = pow(Double(v - ground) / denom, 0.5)
                let level = min(11, max(1, Int((1.0 + normalized * 10.0).rounded())))
                lut[v] = UInt8(clamping: level * 17)
            }
        }
        return luminance.map { lut[Int($0)] }
    }

    nonisolated static func encodeGrayscalePNG(pixels: [UInt8], width: Int, height: Int) -> Data? {
        guard width > 0, height > 0, pixels.count == width * height else { return nil }
        let rawData = Data(pixels)
        guard let provider = CGDataProvider(data: rawData as CFData),
              let cgImage = CGImage(
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 8,
                  bytesPerRow: width,
                  space: CGColorSpaceCreateDeviceGray(),
                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              ) else {
            return nil
        }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else {
            return nil
        }
        CGImageDestinationAddImage(dest, cgImage, nil)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
    }

    nonisolated static func prepareGrayscalePNG(from cgImage: CGImage, width: Int, height: Int) -> Data? {
        guard width > 0, height > 0 else { return nil }
        let bytesPerRow = width * 4
        var rgba = [UInt8](repeating: 0, count: bytesPerRow * height)
        let ok: Bool = rgba.withUnsafeMutableBytes { ptr in
            guard let base = ptr.baseAddress,
                  let ctx = CGContext(
                      data: base,
                      width: width,
                      height: height,
                      bitsPerComponent: 8,
                      bytesPerRow: bytesPerRow,
                      space: CGColorSpaceCreateDeviceRGB(),
                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
                  ) else {
                return false
            }
            ctx.interpolationQuality = .high
            ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard ok else { return nil }

        var lum = [UInt8](repeating: 0, count: width * height)
        for i in 0..<(width * height) {
            let o = i * 4
            lum[i] = luminance(r: rgba[o], g: rgba[o + 1], b: rgba[o + 2])
        }
        let toned = applyG2ToneCurve(luminance: lum)
        return encodeGrayscalePNG(pixels: toned, width: width, height: height)
    }

    /// Pure-CPU slippy tile stitch + G2 tone curve + 8-bit grayscale PNG encoding (zero Metal).
    nonisolated static func stitchSlippyTiles(
        latitude: Double,
        longitude: Double,
        zoom: Int,
        width: Int = defaultWidth,
        height: Int = defaultHeight,
        tileLoader: (Int, Int, Int) async -> Data?
    ) async -> Data? {
        let placements = slippyPlacements(latitude: latitude, longitude: longitude, zoom: zoom, width: width, height: height)
        guard !placements.isEmpty, width > 0, height > 0 else { return nil }

        var decodedImages: [String: CGImage] = [:]
        for p in placements {
            if Task.isCancelled { return nil }
            let key = "\(p.z)/\(p.x)/\(p.y)"
            if decodedImages[key] != nil { continue }
            guard let raw = await tileLoader(p.z, p.x, p.y),
                  let src = CGImageSourceCreateWithData(raw as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary) else {
                return nil
            }
            decodedImages[key] = img
        }

        let bytesPerRow = width * 4
        var rgba = [UInt8](repeating: 0, count: bytesPerRow * height)
        let drew: Bool = rgba.withUnsafeMutableBytes { ptr in
            guard let base = ptr.baseAddress,
                  let ctx = CGContext(
                      data: base,
                      width: width,
                      height: height,
                      bitsPerComponent: 8,
                      bytesPerRow: bytesPerRow,
                      space: CGColorSpaceCreateDeviceRGB(),
                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
                  ) else {
                return false
            }
            ctx.setFillColor(red: 0, green: 0, blue: 0, alpha: 1)
            ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
            ctx.interpolationQuality = .high
            for p in placements {
                let key = "\(p.z)/\(p.x)/\(p.y)"
                guard let img = decodedImages[key] else { return false }
                // CoreGraphics bitmap context user-space has origin at bottom-left (+Y up),
                // whereas `screenY` is measured from top-left (+Y down).
                let cgY = Double(height) - p.screenY - p.tileDrawSize
                ctx.draw(img, in: CGRect(x: p.screenX, y: cgY, width: p.tileDrawSize, height: p.tileDrawSize))
            }
            return true
        }
        guard drew else { return nil }

        var lum = [UInt8](repeating: 0, count: width * height)
        for i in 0..<(width * height) {
            let o = i * 4
            lum[i] = luminance(r: rgba[o], g: rgba[o + 1], b: rgba[o + 2])
        }
        let toned = applyG2ToneCurve(luminance: lum)
        return encodeGrayscalePNG(pixels: toned, width: width, height: height)
    }

    // MARK: - Foreground / Background Safe Tile Rendering & Cache

    func renderTile(
        latitude: Double,
        longitude: Double,
        mode: String,
        width: Int = defaultWidth,
        height: Int = defaultHeight
    ) async -> RenderedMapTile? {
        let z = Self.zoom(for: mode)
        let key = String(format: "%.5f,%.5f,%d,%dx%d", latitude, longitude, z, width, height)
        if let cached = cachedPanel(forKey: key) {
            return RenderedMapTile(latitude: latitude, longitude: longitude, zoom: z, width: width, height: height, png: cached)
        }

        var pngData: Data?
        #if os(iOS)
        if UIApplication.shared.applicationState == .active {
            pngData = await renderForegroundSnapshot(latitude: latitude, longitude: longitude, zoom: z, width: width, height: height)
        }
        #endif

        if pngData == nil, !Task.isCancelled {
            pngData = await renderBackgroundSlippyTile(latitude: latitude, longitude: longitude, zoom: z, width: width, height: height)
        }

        guard let png = pngData, !Task.isCancelled else { return nil }
        storePanel(png, forKey: key)
        return RenderedMapTile(latitude: latitude, longitude: longitude, zoom: z, width: width, height: height, png: png)
    }

    #if os(iOS)
    private func renderForegroundSnapshot(
        latitude: Double,
        longitude: Double,
        zoom: Int,
        width: Int,
        height: Int
    ) async -> Data? {
        // Strict Metal GPU guard: NEVER instantiate or start MKMapSnapshotter when not `.active`.
        guard UIApplication.shared.applicationState == .active, !Task.isCancelled else { return nil }

        let options = MKMapSnapshotter.Options()
        options.mapRect = Self.mercatorMapRect(latitude: latitude, longitude: longitude, zoom: zoom, width: width, height: height)
        options.size = CGSize(width: width, height: height)
        options.scale = 1.0
        options.traitCollection = UITraitCollection(userInterfaceStyle: .dark)
        options.pointOfInterestFilter = .excludingAll
        options.showsBuildings = true

        let id = UUID()
        let snapshotter = MKMapSnapshotter(options: options)
        activeSnapshotters[id] = snapshotter

        let cgImage: CGImage? = await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                // Re-verify `.active` right before starting on the main actor.
                guard UIApplication.shared.applicationState == .active else {
                    self.activeSnapshotters.removeValue(forKey: id)
                    continuation.resume(returning: nil)
                    return
                }
                snapshotter.start(with: .main) { [weak self] snapshot, error in
                    MainActor.assumeIsolated {
                        self?.activeSnapshotters.removeValue(forKey: id)
                        guard UIApplication.shared.applicationState == .active,
                              error == nil,
                              let cg = snapshot?.image.cgImage else {
                            continuation.resume(returning: nil)
                            return
                        }
                        continuation.resume(returning: cg)
                    }
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                if let s = self?.activeSnapshotters.removeValue(forKey: id) {
                    s.cancel()
                }
            }
        }

        guard let cgImage, !Task.isCancelled else { return nil }
        return await Task.detached(priority: .utility) {
            Self.prepareGrayscalePNG(from: cgImage, width: width, height: height)
        }.value
    }
    #endif

    private func renderBackgroundSlippyTile(
        latitude: Double,
        longitude: Double,
        zoom: Int,
        width: Int,
        height: Int
    ) async -> Data? {
        await Self.stitchSlippyTiles(latitude: latitude, longitude: longitude, zoom: zoom, width: width, height: height) { [weak self] z, x, y in
            guard let self else { return nil }
            return await self.fetchBackgroundSlippyTile(z: z, x: x, y: y)
        }
    }

    private func fetchBackgroundSlippyTile(z: Int, x: Int, y: Int) async -> Data? {
        let key = "\(z)/\(x)/\(y)"
        if let cached = cachedSlippyTile(forKey: key) {
            return cached
        }
        guard let url = Self.backgroundSlippyTileURL(z: z, x: x, y: y) else {
            return nil
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 3.0
        request.setValue("JOATBrain/1.0", forHTTPHeaderField: "User-Agent")
        request.setValue("image/png,image/*;q=0.8", forHTTPHeaderField: "Accept")
        do {
            let (data, response) = try await tileSession.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, !data.isEmpty else {
                return nil
            }
            storeSlippyTile(data, forKey: key)
            return data
        } catch {
            return nil
        }
    }

    private func cachedPanel(forKey key: String) -> Data? {
        guard let data = panelCache[key] else { return nil }
        if let idx = panelOrder.firstIndex(of: key) {
            panelOrder.remove(at: idx)
        }
        panelOrder.append(key)
        return data
    }

    private func storePanel(_ data: Data, forKey key: String) {
        panelCache[key] = data
        if let idx = panelOrder.firstIndex(of: key) {
            panelOrder.remove(at: idx)
        }
        panelOrder.append(key)
        while panelOrder.count > maxCachedPanels {
            let oldest = panelOrder.removeFirst()
            panelCache.removeValue(forKey: oldest)
        }
    }

    private func cachedSlippyTile(forKey key: String) -> Data? {
        guard let data = slippyCache[key] else { return nil }
        if let idx = slippyOrder.firstIndex(of: key) {
            slippyOrder.remove(at: idx)
        }
        slippyOrder.append(key)
        return data
    }

    private func storeSlippyTile(_ data: Data, forKey key: String) {
        slippyCache[key] = data
        if let idx = slippyOrder.firstIndex(of: key) {
            slippyOrder.remove(at: idx)
        }
        slippyOrder.append(key)
        while slippyOrder.count > maxCachedSlippyTiles {
            let oldest = slippyOrder.removeFirst()
            slippyCache.removeValue(forKey: oldest)
        }
    }
}
