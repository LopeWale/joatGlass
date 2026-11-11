import Foundation
import MapKit
import CryptoKit

// Apple Maps behind the Navigation session: the phone's stand-in for the
// Mac's Mapbox adapter (apps/mac/joat/navigation/mapbox.py). Search and
// directions need no key. The plugin is unchanged: it tracks progress on the
// phone's location fixes itself, so all this hands it is a route plan.
//
// Measured with MKDirections on 2026-10-23 (San Francisco, New York and
// Chicago routes): a step's instruction is the move at the END of that step's
// polyline ("Turn right onto Post St" ends where Post St begins), step 0 is
// the start ("Start on W 33rd St", 0 m), and the last step is the arrival.
// Cycling routes came back labelled `.automobile` while following paths and
// one-way streets the wrong way for cars — the transport label is wrong, not
// the route (Apple Developer Forums thread 836096 reports the same), so it is
// logged, not trusted.

/// protocol.py NavigationCoordinate / core/navigation.py NavigationPoint.
struct NavPoint: Equatable {
    let latitude: Double
    let longitude: Double

    init(_ latitude: Double, _ longitude: Double) {
        self.latitude = latitude
        self.longitude = longitude
    }

    init(_ c: CLLocationCoordinate2D) { self.init(c.latitude, c.longitude) }

    var coordinate: CLLocationCoordinate2D { .init(latitude: latitude, longitude: longitude) }
    var wire: [String: Any] { ["latitude": latitude, "longitude": longitude] }

    var isValid: Bool {
        latitude.isFinite && longitude.isFinite && (-90...90).contains(latitude) && (-180...180).contains(longitude)
    }
}

/// core/navigation.py NavigationCandidate: one place the wearer may confirm.
struct NavCandidate {
    let choiceId: String
    let label: String
    let address: String
    let point: NavPoint
    var repairHint: String? = nil
    var distanceM: Double? = nil
}

struct NavStep {
    let id: String
    let maneuver: String
    let instruction: String
    let alongM: Double
}

/// core/navigation.py ProviderRoute: what navigation.route carries.
struct NavRoute {
    let routeId: String
    let etaMinutes: Double
    let mode: String
    let geometry: [NavPoint]
    let steps: [NavStep]

    var totalM: Double { steps.last?.alongM ?? 0 }

    /// protocol.py _navigation_route_plan_to_wire
    var plan: [String: Any] {
        [
            "mode": mode,
            "geometry": geometry.map(\.wire),
            "steps": steps.map {
                ["id": $0.id, "maneuver": $0.maneuver, "instruction": $0.instruction,
                 "distanceAlongRouteM": $0.alongM] as [String: Any]
            },
            "policy": NavigationMaps.policy(mode),
        ]
    }
}

/// Why a route failed; each one names a different next move for the wearer
/// (navigation.py _ROUTE_FAILURE_COPY).
enum NavFailure: Error {
    case noRoute, tooLong, unreachable, incomplete
    /// A route the wire would refuse (navigation.py: "rejected").
    case rejected

    var reason: String {
        switch self {
        case .rejected: return "rejected"
        case .noRoute: return "no_route"
        case .tooLong: return "too_long"
        case .unreachable: return "unreachable"
        case .incomplete: return "incomplete"
        }
    }
}

enum NavigationMaps {
    static let earthRadius = 6_371_008.8
    /// The wire's caps (mapbox.py MAX_ROUTE_COORDINATES / MAX_ROUTE_STEPS).
    static let maxCoordinates = 4096
    static let maxSteps = 512
    /// A search bias, never a filter: about a city around the wearer.
    static let searchRegionMeters = 20_000.0

    // MARK: - Policies (navigation/policies.py)

    static func policy(_ mode: String) -> [String: Any] {
        let walking = mode == "walking"
        return [
            "maxAccuracyM": walking ? 25.0 : 20.0,
            "staleAfterMs": walking ? 5000.0 : 4000.0,
            "corridorRadiusM": walking ? 25.0 : 35.0,
            "ambiguityMarginM": walking ? 6.0 : 7.0,
            "ambiguityProgressDeltaM": walking ? 80.0 : 120.0,
            "searchBackM": walking ? 100.0 : 160.0,
            "searchForwardM": walking ? 400.0 : 800.0,
            "minCourseDisplacementM": walking ? 5.0 : 8.0,
            "wrongWayAngleDeg": walking ? 120.0 : 125.0,
            "wrongWayMinBacktrackM": walking ? 12.0 : 25.0,
            "wrongWaySamples": 3,
            "offRouteSamples": 3,
            "arrivalRadiusM": walking ? 15.0 : 20.0,
            "arrivalSamples": 2,
            "uTurnSuppressionM": walking ? 40.0 : 60.0,
        ]
    }

    // MARK: - Distances

    /// Great-circle distance (navigation.py _candidate_distance).
    static func distance(_ a: NavPoint, _ b: NavPoint) -> Double {
        let la1 = a.latitude * .pi / 180, la2 = b.latitude * .pi / 180
        let dLat = la2 - la1
        let dLon = (b.longitude - a.longitude) * .pi / 180
        let h = sin(dLat / 2) * sin(dLat / 2) + cos(la1) * cos(la2) * sin(dLon / 2) * sin(dLon / 2)
        return 2 * earthRadius * asin(min(1, sqrt(h)))
    }

    static func bearing(_ a: NavPoint, _ b: NavPoint) -> Double {
        let p1 = a.latitude * .pi / 180, p2 = b.latitude * .pi / 180
        let dl = (b.longitude - a.longitude) * .pi / 180
        let y = sin(dl) * cos(p2), x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
        return (atan2(y, x) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }

    /// navigation.py _format_distance (candidate distances, not guidance).
    static func formatDistance(_ m: Double) -> String {
        m < 1000 ? "\(Int((m / 10).rounded()) * 10) m" : String(format: "%.1f km", m / 1000)
    }

    static func digest(_ text: String) -> String {
        String(ConfirmGate.sha256(text).prefix(20))
    }

    // MARK: - Search (mapbox.py search + _ranked_search_candidates)

    /// Candidates for a spoken destination, best first. A failed search is
    /// an empty list: the wearer sees "Destination not found", as on the Mac.
    /// `nearest`: the wearer said "nearest" / "near me", so when no result
    /// names the words, the closest comes first instead of Apple's order.
    static func search(_ query: String, near proximity: NavPoint?, nearest: Bool = false) async -> [NavCandidate] {
        let bounded = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !bounded.isEmpty, bounded.count <= 256 else { return [] }
        let request = MKLocalSearch.Request()
        request.naturalLanguageQuery = bounded
        request.resultTypes = [.address, .pointOfInterest]
        if let p = proximity {
            request.region = MKCoordinateRegion(center: p.coordinate, latitudinalMeters: searchRegionMeters,
                                                longitudinalMeters: searchRegionMeters)
        }
        let items: [MKMapItem]
        do {
            items = try await MKLocalSearch(request: request).start().mapItems
        } catch {
            Log.add("navigation search failed: \((error as NSError).domain) \((error as NSError).code)")
            return []
        }
        // Mapbox was asked for 10; the ranking below works on the same depth.
        let parsed = items.prefix(10).enumerated().compactMap { candidate($0.element, proximity, $0.offset) }
        // One place, one row: Apple can return the same place twice under
        // two ids ("Navy Pier · 600 E Grand Ave" both times).
        var seen = Set<String>()
        let ranked = rank(bounded, parsed, proximity, nearest: nearest).map(\.candidate).filter {
            seen.insert($0.choiceId).inserted && seen.insert(searchText("\($0.label) \($0.address)")).inserted
        }
        guard ranked.count >= 2 else { return ranked }
        return ranked.map {
            var c = $0
            c.repairHint = "street, city, or postcode"
            return c
        }
    }

    struct Ranked {
        let candidate: NavCandidate
        let names: [String]
        let categories: [String]
        let isPoi: Bool
        let index: Int
    }

    static func candidate(_ item: MKMapItem, _ proximity: NavPoint?, _ index: Int) -> Ranked? {
        let label = (item.name ?? "").collapsed
        let address = (item.addressRepresentations?.fullAddress(includingRegion: false, singleLine: true)
            ?? item.address?.fullAddress ?? item.address?.shortAddress ?? "").collapsed
        let point = NavPoint(item.location.coordinate)
        guard !label.isEmpty, !address.isEmpty, point.isValid else { return nil }
        let stable = item.identifier?.rawValue ?? "\(label)|\(point.latitude)|\(point.longitude)"
        let category = item.pointOfInterestCategory.map { categoryWords($0.rawValue) }
        return Ranked(
            candidate: NavCandidate(choiceId: "mk-" + digest(stable), label: label.utf16Prefix(96),
                                    address: address.utf16Prefix(128), point: point,
                                    distanceM: proximity.map { distance($0, point) }),
            names: [label], categories: category.map { [$0] } ?? [],
            isPoi: item.pointOfInterestCategory != nil, index: index)
    }

    /// "MKPOICategoryPublicTransport" -> "public transport"
    static func categoryWords(_ raw: String) -> String {
        let bare = raw.hasPrefix("MKPOICategory") ? String(raw.dropFirst("MKPOICategory".count)) : raw
        return bare.replacingOccurrences(of: "([a-z])([A-Z])", with: "$1 $2", options: .regularExpression).lowercased()
    }

    /// Prefer semantic POI matches, then measured distance; with no lexical
    /// match at all, keep Apple's own order (misspellings, aliases).
    static func rank(_ query: String, _ items: [Ranked], _ proximity: NavPoint?, nearest: Bool = false) -> [Ranked] {
        guard !items.isEmpty else { return [] }
        var ranked = items.map { ($0, matchRank(query, $0)) }
        let best = ranked.map(\.1).min() ?? 5
        if best <= 1 { ranked = ranked.filter { $0.1 <= 1 } }
        else if best <= 3 { ranked = ranked.filter { $0.1 <= 3 } }
        else if nearest { ranked = ranked.map { ($0.0, 5) } }
        else { return items }
        ranked.sort { a, b in
            if a.1 != b.1 { return a.1 < b.1 }
            if proximity != nil, let da = a.0.candidate.distanceM, let db = b.0.candidate.distanceM, da != db {
                return da < db
            }
            return a.0.index < b.0.index
        }
        return ranked.map(\.0)
    }

    static func matchRank(_ query: String, _ r: Ranked) -> Int {
        let q = searchText(query)
        let qTokens = tokens(query)
        let names = r.names.map(searchText)
        let categories = r.categories.map(searchText)
        let address = searchText(r.candidate.address)
        let isPoi = r.isPoi || r.names.count > 1 || !categories.isEmpty
        // "the Ferry Building" names the Ferry Building, not the tram stop
        // whose name merely contains it: equal words count as equal.
        if isPoi, (names + categories).contains(q) || names.contains(where: { tokens($0) == qTokens }) { return 0 }
        if isPoi, !qTokens.isEmpty, qTokens.isSubset(of: tokens((names + categories).joined(separator: " "))) { return 1 }
        if (names + [address]).contains(q) { return 2 }
        if !qTokens.isEmpty, qTokens.isSubset(of: tokens((names + [address]).joined(separator: " "))) { return 3 }
        return isPoi ? 4 : 5
    }

    static func searchText(_ value: String) -> String {
        value.lowercased().replacingOccurrences(of: #"[\W_]+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    static func tokens(_ value: String) -> Set<String> {
        Set(searchText(value).split(separator: " ").map(String.init).filter { !["a", "an", "the"].contains($0) })
    }

    // MARK: - Where am I

    /// The address Apple gives for a point, with a place name when there is
    /// one ("Bethesda Fountain, …"); nil when it cannot be looked up.
    static func address(at point: NavPoint) async -> String? {
        guard let request = MKReverseGeocodingRequest(location: CLLocation(latitude: point.latitude,
                                                                           longitude: point.longitude)) else { return nil }
        let items: [MKMapItem]
        do {
            items = try await request.mapItems
        } catch {
            Log.add("reverse geocoding failed: \((error as NSError).domain) \((error as NSError).code)")
            return nil
        }
        guard let item = items.first else { return nil }
        let short = (item.address?.shortAddress ?? "").collapsed
        let full = (item.addressRepresentations?.fullAddress(includingRegion: false, singleLine: true) ?? "").collapsed
        let address = short.isEmpty ? full : short
        let name = (item.name ?? "").collapsed
        if address.isEmpty { return name.isEmpty ? nil : name }
        if name.isEmpty || address.lowercased().contains(name.lowercased()) { return address }
        return "\(name), \(address)"
    }

    /// Where a point is, for Claude: "5800 Will Rogers Rd, Oklahoma City,
    /// United States (35.44690, -97.42109)", the city name and the ISO
    /// country code.
    static func area(at point: NavPoint) async -> (context: String, city: String?, country: String?)? {
        guard let request = MKReverseGeocodingRequest(location: CLLocation(latitude: point.latitude,
                                                                           longitude: point.longitude)),
              let item = try? await request.mapItems.first else { return nil }
        let r = item.addressRepresentations
        let street = (item.address?.shortAddress ?? "").collapsed
        let name = (item.name ?? "").collapsed
        var parts: [String] = []
        if !name.isEmpty, !street.lowercased().contains(name.lowercased()) { parts.append(name) }
        if !street.isEmpty { parts.append(street) }
        if let city = r?.cityWithContext ?? r?.cityName, !parts.joined().contains(city) { parts.append(city) }
        guard !parts.isEmpty else { return nil }
        let context = parts.joined(separator: ", ")
            + String(format: " (%.5f, %.5f)", point.latitude, point.longitude)
        return (context, r?.cityName, r?.region?.identifier)
    }

    // MARK: - Directions (mapbox.py route + _route_steps)

    static func route(from origin: NavPoint, to destination: NavCandidate, mode: String) async throws -> NavRoute {
        let request = MKDirections.Request()
        request.source = MKMapItem(location: CLLocation(latitude: origin.latitude, longitude: origin.longitude),
                                   address: nil)
        request.destination = MKMapItem(location: CLLocation(latitude: destination.point.latitude,
                                                             longitude: destination.point.longitude),
                                        address: nil)
        request.transportType = mode == "walking" ? .walking : .cycling
        request.requestsAlternateRoutes = false
        let response: MKDirections.Response
        do {
            response = try await MKDirections(request: request).calculate()
        } catch {
            let ns = error as NSError
            Log.add("navigation directions failed: \(ns.domain) \(ns.code)")
            if ns.domain == MKErrorDomain, ns.code == Int(MKError.directionsNotFound.rawValue) { throw NavFailure.noRoute }
            throw NavFailure.unreachable
        }
        guard let route = response.routes.first else { throw NavFailure.noRoute }
        if route.transportType != request.transportType {
            Log.add("navigation: asked for \(mode), Apple labelled the route \(route.transportType.rawValue)")
        }
        return try plan(route, mode: mode)
    }

    /// Steps and geometry from one MKRoute. Exposed for the harness.
    static func plan(_ route: MKRoute, mode: String) throws -> NavRoute {
        let raw = route.steps.map { (coordinates($0.polyline), $0.instructions) }
        return try plan(steps: raw, eta: route.expectedTravelTime, mode: mode)
    }

    static func plan(steps raw: [([CLLocationCoordinate2D], String)], eta seconds: TimeInterval,
                     mode: String) throws -> NavRoute {
        guard !raw.isEmpty else { throw NavFailure.incomplete }
        if raw.count > maxSteps { throw NavFailure.tooLong }
        // One polyline from the steps, so every maneuver's distance is
        // measured on the very line the plugin tracks against.
        var points: [NavPoint] = []
        var ends: [Int] = []
        for (coords, _) in raw {
            for c in coords {
                let p = NavPoint(c)
                guard p.isValid else { throw NavFailure.incomplete }
                if points.last != p { points.append(p) }
            }
            ends.append(max(0, points.count - 1))
        }
        guard points.count >= 2, seconds.isFinite, seconds >= 0 else { throw NavFailure.incomplete }
        var along = [0.0]
        for i in 1..<points.count { along.append(along[i - 1] + distance(points[i - 1], points[i])) }
        var steps: [NavStep] = []
        for (i, (_, text)) in raw.enumerated() {
            let instruction = text.collapsed.isEmpty ? "Continue" : text.collapsed.utf16Prefix(128)
            let turn = turnAngle(points, along, at: ends[i])
            steps.append(NavStep(id: "step-\(i + 1)",
                                 maneuver: maneuver(instruction, index: i, count: raw.count, turn: turn),
                                 instruction: instruction, alongM: along[ends[i]]))
        }
        if steps.last?.maneuver != "arrive" {
            steps.append(NavStep(id: "step-\(steps.count + 1)", maneuver: "arrive",
                                 instruction: "You have arrived at your destination", alongM: along.last ?? 0))
        }
        let geometry = thinned(points, to: maxCoordinates)
        let key = geometry.map { String(format: "%.6f,%.6f", $0.latitude, $0.longitude) }.joined(separator: ";")
        return NavRoute(routeId: "mk-" + digest(key), etaMinutes: seconds / 60, mode: mode,
                        geometry: geometry, steps: steps)
    }

    static func coordinates(_ line: MKPolyline) -> [CLLocationCoordinate2D] {
        var c = [CLLocationCoordinate2D](repeating: .init(), count: line.pointCount)
        line.getCoordinates(&c, range: NSRange(location: 0, length: line.pointCount))
        return c
    }

    /// The heading change where a step ends: about 10 m in, about 10 m out.
    static func turnAngle(_ points: [NavPoint], _ along: [Double], at index: Int) -> Double? {
        guard index > 0, index < points.count - 1 else { return nil }
        var a = index - 1
        while a > 0, along[index] - along[a] < 10 { a -= 1 }
        var b = index + 1
        while b < points.count - 1, along[b] - along[index] < 10 { b += 1 }
        var d = bearing(points[index], points[b]) - bearing(points[a], points[index])
        while d > 180 { d -= 360 }
        while d < -180 { d += 360 }
        return d
    }

    /// The plugin's eight maneuvers. Apple gives words, not a maneuver
    /// type, so the words decide (in English); anything else falls back to
    /// how sharply the line itself turns.
    static func maneuver(_ text: String, index: Int, count: Int, turn: Double?) -> String {
        if index == count - 1 { return "arrive" }
        if index == 0 { return "depart" }
        let t = text.lowercased()
        if t.contains("u-turn") || t.contains("u turn") || t.contains("uturn") { return "u_turn" }
        let left = t.range(of: #"\bleft\b"#, options: .regularExpression) != nil
        let right = t.range(of: #"\bright\b"#, options: .regularExpression) != nil
        if left != right {
            let slight = t.range(of: #"\b(slight|bear|keep)\b"#, options: .regularExpression) != nil
            if left { return slight ? "slight_left" : "turn_left" }
            return slight ? "slight_right" : "turn_right"
        }
        if !left, !right,
           t.range(of: #"\b(continue|straight|proceed|stairs|start|head|follow)\b"#, options: .regularExpression) != nil {
            return "continue"
        }
        guard let a = turn else { return "continue" }
        switch abs(a) {
        case ..<25: return "continue"
        case ..<60: return a < 0 ? "slight_left" : "slight_right"
        case ..<155: return a < 0 ? "turn_left" : "turn_right"
        default: return "u_turn"
        }
    }

    /// Evenly thin a polyline, keeping both ends (mapbox.py _downsampled).
    static func thinned(_ points: [NavPoint], to limit: Int) -> [NavPoint] {
        guard limit >= 2, points.count > limit else { return points }
        let last = points.count - 1
        let stride = Double(last) / Double(limit - 1)
        return (0..<(limit - 1)).map { points[min(last, Int((Double($0) * stride).rounded()))] } + [points[last]]
    }
}

extension String {
    /// Whitespace runs to one space, trimmed (Python's " ".join(s.split())).
    var collapsed: String { split(whereSeparator: { $0.isWhitespace }).joined(separator: " ") }
}
