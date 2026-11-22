import SwiftUI
import MapKit
import CoreLocation
import UIKit

// MARK: - Apple Maps Autocomplete & Resolution

struct PlaceSuggestion: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    fileprivate let completion: MKLocalSearchCompletion
}

@MainActor
final class PlaceSearchModel: NSObject, ObservableObject, MKLocalSearchCompleterDelegate {
    @Published private(set) var suggestions: [PlaceSuggestion] = []
    @Published private(set) var isCompleting = false
    @Published private(set) var lastError: String?

    private let completer: MKLocalSearchCompleter
    private var debounceTask: Task<Void, Never>?

    override init() {
        let c = MKLocalSearchCompleter()
        c.resultTypes = [.address, .pointOfInterest]
        self.completer = c
        super.init()
        self.completer.delegate = self
    }

    func updateQuery(_ raw: String, near proximity: NavPoint? = nil) {
        debounceTask?.cancel()
        lastError = nil
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            suggestions = []
            isCompleting = false
            return
        }
        if let p = proximity, p.isValid {
            completer.region = MKCoordinateRegion(
                center: p.coordinate,
                latitudinalMeters: NavigationMaps.searchRegionMeters,
                longitudinalMeters: NavigationMaps.searchRegionMeters
            )
        }
        isCompleting = true
        debounceTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 140_000_000)
            guard let self, !Task.isCancelled else { return }
            self.completer.queryFragment = trimmed
        }
    }

    func clear() {
        debounceTask?.cancel()
        suggestions = []
        isCompleting = false
        lastError = nil
    }

    func resolve(_ suggestion: PlaceSuggestion, near proximity: NavPoint? = nil) async -> NavCandidate? {
        lastError = nil
        let request = MKLocalSearch.Request(completion: suggestion.completion)
        if let p = proximity, p.isValid {
            request.region = MKCoordinateRegion(
                center: p.coordinate,
                latitudinalMeters: NavigationMaps.searchRegionMeters,
                longitudinalMeters: NavigationMaps.searchRegionMeters
            )
        }
        do {
            let response = try await MKLocalSearch(request: request).start()
            if let item = response.mapItems.first,
               let candidate = Self.makeCandidate(
                   from: item,
                   fallbackTitle: suggestion.title,
                   fallbackSubtitle: suggestion.subtitle,
                   near: proximity
               ) {
                return candidate
            }
        } catch {
            Log.add("place completion resolve failed: \((error as NSError).domain) \((error as NSError).code)")
        }
        let combined = [suggestion.title, suggestion.subtitle]
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: ", ")
        return await resolveQuery(combined.isEmpty ? suggestion.title : combined, near: proximity)
    }

    func resolveQuery(_ query: String, near proximity: NavPoint? = nil) async -> NavCandidate? {
        lastError = nil
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        let ranked = await NavigationMaps.search(trimmed, near: proximity)
        if let first = ranked.first {
            return first
        }

        let request = MKLocalSearch.Request()
        request.naturalLanguageQuery = String(trimmed.prefix(256))
        request.resultTypes = [.address, .pointOfInterest]
        if let p = proximity, p.isValid {
            request.region = MKCoordinateRegion(
                center: p.coordinate,
                latitudinalMeters: NavigationMaps.searchRegionMeters,
                longitudinalMeters: NavigationMaps.searchRegionMeters
            )
        }
        do {
            let response = try await MKLocalSearch(request: request).start()
            if let item = response.mapItems.first {
                return Self.makeCandidate(
                    from: item,
                    fallbackTitle: trimmed,
                    fallbackSubtitle: trimmed,
                    near: proximity
                )
            }
        } catch {
            lastError = "Could not search Apple Maps right now."
        }
        return nil
    }

    private static func makeCandidate(
        from item: MKMapItem,
        fallbackTitle: String,
        fallbackSubtitle: String,
        near proximity: NavPoint?
    ) -> NavCandidate? {
        let point = NavPoint(item.location.coordinate)
        guard point.isValid else { return nil }

        let itemName = (item.name ?? "").collapsed
        let cleanFallbackTitle = fallbackTitle.collapsed
        let labelSource = itemName.isEmpty ? cleanFallbackTitle : itemName
        guard !labelSource.isEmpty else { return nil }

        let fullAddress = (
            item.addressRepresentations?.fullAddress(includingRegion: false, singleLine: true)
            ?? item.address?.fullAddress
            ?? item.address?.shortAddress
            ?? fallbackSubtitle
        ).collapsed
        let addressSource = fullAddress.isEmpty ? labelSource : fullAddress

        let stable = item.identifier?.rawValue ?? "\(labelSource)|\(point.latitude)|\(point.longitude)"
        return NavCandidate(
            choiceId: "mk-" + NavigationMaps.digest(stable),
            label: Self.clipped(labelSource, maxChars: 96),
            address: Self.clipped(addressSource, maxChars: 128),
            point: point,
            distanceM: proximity.map { NavigationMaps.distance($0, point) }
        )
    }

    fileprivate static func clipped(_ text: String, maxChars: Int) -> String {
        guard text.count > maxChars else { return text }
        return String(text.prefix(maxChars)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func syncCompleterResults() {
        isCompleting = false
        var seen = Set<String>()
        var mapped: [PlaceSuggestion] = []
        for item in completer.results.prefix(8) {
            let title = item.title.collapsed
            let subtitle = item.subtitle.collapsed
            guard !title.isEmpty else { continue }
            let key = "\(title.lowercased())|\(subtitle.lowercased())"
            guard seen.insert(key).inserted else { continue }
            mapped.append(
                PlaceSuggestion(
                    id: key,
                    title: title,
                    subtitle: subtitle,
                    completion: item
                )
            )
            if mapped.count >= 5 { break }
        }
        suggestions = mapped
    }

    private func handleCompleterFailure() {
        isCompleting = false
    }

    nonisolated func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        Task { @MainActor [weak self] in
            self?.syncCompleterResults()
        }
    }

    nonisolated func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        Task { @MainActor [weak self] in
            self?.handleCompleterFailure()
        }
    }
}

// MARK: - Shared Formatting Helpers

enum NavFormatters {
    static let modeOrder: [String] = ["walking", "cycling", "driving"]

    static func modeShortTitle(_ mode: String) -> String {
        switch mode.lowercased() {
        case "cycling": return "Cycle"
        case "driving": return "Drive"
        default: return "Walk"
        }
    }

    static func modeDisplayTitle(_ mode: String) -> String {
        switch mode.lowercased() {
        case "cycling": return "Cycling"
        case "driving": return "Driving"
        default: return "Walking"
        }
    }

    static func modeIcon(_ mode: String) -> String {
        switch mode.lowercased() {
        case "cycling": return "bicycle"
        case "driving": return "car.fill"
        default: return "figure.walk"
        }
    }

    static func maneuverIcon(for maneuver: String?, arrived: Bool = false) -> String {
        if arrived { return "flag.checkered" }
        switch (maneuver ?? "").lowercased() {
        case "arrive": return "flag.checkered"
        case "depart": return "location.north.line.fill"
        case "turn_left": return "arrow.turn.up.left"
        case "turn_right": return "arrow.turn.up.right"
        case "slight_left": return "arrow.up.left"
        case "slight_right": return "arrow.up.right"
        case "u_turn": return "arrow.uturn.down"
        default: return "arrow.up"
        }
    }

    static func placeIcon(for name: String) -> String {
        let lower = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch lower {
        case "home", "house", "apartment":
            return "house.fill"
        case "work", "office", "hq", "studio":
            return "briefcase.fill"
        case "gym", "fitness":
            return "dumbbell.fill"
        case "coffee", "cafe":
            return "cup.and.saucer.fill"
        case "airport":
            return "airplane"
        case "school", "campus", "university":
            return "graduationcap.fill"
        default:
            return "mappin.and.ellipse"
        }
    }

    static func distance(_ meters: Double?) -> String? {
        guard let m = meters, m.isFinite, m >= 0 else { return nil }
        if m < 100 {
            return "\(Int(m.rounded())) m"
        }
        if m < 1000 {
            return "\(Int((m / 10).rounded()) * 10) m"
        }
        if m >= 100_000 {
            return String(format: "%.0f km", m / 1000)
        }
        return String(format: "%.1f km", m / 1000)
    }

    static func eta(_ minutes: Double?) -> String? {
        guard let mins = minutes, mins.isFinite, mins >= 0 else { return nil }
        let rounded = Int(mins.rounded())
        if rounded < 1 { return "< 1 min" }
        if rounded < 60 { return "\(rounded) min" }
        let hours = rounded / 60
        let rem = rounded % 60
        return rem == 0 ? "\(hours) hr" : "\(hours) hr \(rem) min"
    }

    static func phaseBadge(for summary: Navigation.ActiveRouteSummary) -> (label: String, color: Color)? {
        if summary.arrived || summary.phase == "arrived" {
            return ("Arrived", JoatTheme.accent)
        }
        switch summary.phase.lowercased() {
        case "searching":
            return ("Searching", JoatTheme.amber)
        case "confirming":
            return ("Confirm on G2", JoatTheme.amber)
        case "routing":
            return ("Calculating", JoatTheme.amber)
        case "preview":
            return ("Route Preview", JoatTheme.accent)
        case "rerouting":
            return ("Rerouting", JoatTheme.amber)
        case "error":
            return ("Needs Attention", JoatTheme.amber)
        case "guidance":
            return ("Live on G2", JoatTheme.accent)
        default:
            return nil
        }
    }
}

// MARK: - Navigation & Places Hub (Adaptive Duo Split + Compact Stack)

struct NavigationPlacesView: View {
    @ObservedObject var navigation: Navigation
    @ObservedObject private var phoneLocation = PhoneLocation.shared
    @ObservedObject private var server = JOATRuntime.shared.server
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    @StateObject private var searchModel = PlaceSearchModel()
    @AppStorage("joat.navigation.preferredMode") private var selectedMode = "walking"

    @State private var searchText = ""
    @State private var suppressSearchUpdate = false
    @State private var selectedCandidate: NavCandidate?
    @State private var isResolvingCandidate = false
    @State private var isStartingRoute = false
    @State private var statusBanner: StatusBanner?
    @State private var placeSheetTarget: SavedPlaceSheetTarget?
    @State private var focusedPlaceId: String?
    @State private var cameraPosition: MapCameraPosition = .automatic
    @FocusState private var searchFocused: Bool

    private struct StatusBanner: Equatable {
        let text: String
        let isError: Bool
    }

    private var glassesConnected: Bool {
        server.authenticated && server.clients > 0
    }

    private var placesWithCoordinates: [SavedPlace] {
        navigation.places.filter { place in
            guard let lat = place.latitude, let lon = place.longitude else { return false }
            return NavPoint(lat, lon).isValid
        }
    }

    private var activeRouteCoordinates: [CLLocationCoordinate2D] {
        guard let geometry = navigation.activeSummary?.routeGeometry, geometry.count >= 2 else {
            return []
        }
        return geometry.filter(\.isValid).map(\.coordinate)
    }

    private var searchBiasPoint: NavPoint? {
        if phoneLocation.isAuthorized,
           let coord = CLLocationManager().location?.coordinate {
            let pt = NavPoint(coord)
            if pt.isValid { return pt }
        }
        if let firstRoutePt = navigation.activeSummary?.routeGeometry.first, firstRoutePt.isValid {
            return firstRoutePt
        }
        if let firstSaved = placesWithCoordinates.first,
           let lat = firstSaved.latitude,
           let lon = firstSaved.longitude {
            return NavPoint(lat, lon)
        }
        return nil
    }

    var body: some View {
        Group {
            if horizontalSizeClass == .regular {
                duoSplitLayout
            } else {
                compactLayout
            }
        }
        .navigationTitle("Navigation & Places")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    placeSheetTarget = .add(presetName: nil, candidate: nil)
                } label: {
                    Label("Add Place", systemImage: "plus.circle.fill")
                }
            }
        }
        .sheet(item: $placeSheetTarget) { target in
            AddSavedPlaceSheet(
                navigation: navigation,
                target: target,
                biasPoint: searchBiasPoint
            ) { saved in
                if let lat = saved.latitude, let lon = saved.longitude {
                    focusedPlaceId = saved.id
                    focusCamera(on: NavPoint(lat, lon), meters: 900)
                }
                statusBanner = StatusBanner(text: "Saved “\(saved.name)”.", isError: false)
            }
        }
        .onAppear {
            syncInitialCamera(animated: false)
        }
        .onChange(of: navigation.activeSummary?.sessionId) { _, _ in
            syncInitialCamera(animated: true)
        }
        .onChange(of: navigation.activeSummary?.routeGeometry) { _, _ in
            if (navigation.activeSummary?.routeGeometry.count ?? 0) >= 2 {
                fitActiveRoute(animated: true)
            }
        }
        .onChange(of: navigation.activeSummary?.destinationPoint) { _, newPoint in
            if (navigation.activeSummary?.routeGeometry.count ?? 0) < 2, let newPoint {
                focusCamera(on: newPoint, meters: 1_200)
            }
        }
        .onChange(of: navigation.places) { _, _ in
            if navigation.activeSummary == nil && selectedCandidate == nil {
                syncInitialCamera(animated: true)
            }
        }
    }

    // MARK: - Adaptive Layouts

    private var duoSplitLayout: some View {
        HStack(spacing: 0) {
            ScrollView {
                controlsColumn
                    .padding(16)
            }
            .frame(width: 400)
            .background(JoatTheme.surface)

            Divider()

            interactiveMapPane(isFullHeight: true)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(JoatTheme.surface.ignoresSafeArea())
    }

    private var compactLayout: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                interactiveMapPane(isFullHeight: false)
                    .frame(height: 220)
                    .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                            .strokeBorder(JoatTheme.heroBorder, lineWidth: 1)
                    )

                controlsColumn
            }
            .padding(16)
        }
        .background(JoatTheme.surface.ignoresSafeArea())
    }

    private var controlsColumn: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let banner = statusBanner {
                statusBannerCard(banner)
            }

            if !phoneLocation.isAuthorized {
                locationPermissionCard
            }

            if let active = navigation.activeSummary {
                activeRouteCard(active)
            }

            quickStartCard

            savedPlacesSection

            guidanceAudioCard
        }
    }

    // MARK: - Interactive Map Pane

    private func interactiveMapPane(isFullHeight: Bool) -> some View {
        ZStack(alignment: .topTrailing) {
            Map(position: $cameraPosition) {
                UserAnnotation()

                if activeRouteCoordinates.count >= 2 {
                    MapPolyline(coordinates: activeRouteCoordinates)
                        .stroke(
                            JoatTheme.accent.opacity(0.28),
                            style: StrokeStyle(lineWidth: 11, lineCap: .round, lineJoin: .round)
                        )
                    MapPolyline(coordinates: activeRouteCoordinates)
                        .stroke(
                            JoatTheme.accent,
                            style: StrokeStyle(lineWidth: 5, lineCap: .round, lineJoin: .round)
                        )
                }

                if let firstCoord = activeRouteCoordinates.first {
                    Annotation("", coordinate: firstCoord, anchor: .center) {
                        ZStack {
                            Circle()
                                .fill(JoatTheme.card)
                                .frame(width: 14, height: 14)
                            Circle()
                                .fill(JoatTheme.accent)
                                .frame(width: 8, height: 8)
                        }
                        .shadow(color: .black.opacity(0.25), radius: 3, y: 1)
                    }
                }

                ForEach(placesWithCoordinates) { place in
                    if let lat = place.latitude, let lon = place.longitude {
                        Annotation(place.name, coordinate: CLLocationCoordinate2D(latitude: lat, longitude: lon), anchor: .bottom) {
                            SavedPlaceMapPin(
                                icon: NavFormatters.placeIcon(for: place.name),
                                isFocused: focusedPlaceId == place.id
                            )
                            .onTapGesture {
                                focusedPlaceId = place.id
                                focusCamera(on: NavPoint(lat, lon), meters: 900)
                            }
                        }
                    }
                }

                if let active = navigation.activeSummary,
                   let destPoint = active.destinationPoint,
                   destPoint.isValid {
                    Annotation(active.destinationLabel, coordinate: destPoint.coordinate, anchor: .bottom) {
                        HighlightedMapPin(
                            icon: active.arrived ? "flag.checkered" : NavFormatters.modeIcon(active.mode),
                            tint: JoatTheme.accent
                        )
                    }
                }

                if let candidate = selectedCandidate,
                   candidate.point.isValid,
                   !isSameDestination(candidate.point, navigation.activeSummary?.destinationPoint) {
                    Annotation(candidate.label, coordinate: candidate.point.coordinate, anchor: .bottom) {
                        HighlightedMapPin(
                            icon: "mappin.and.ellipse",
                            tint: JoatTheme.accent
                        )
                    }
                }
            }
            .mapStyle(.standard(elevation: .realistic))
            .mapControls {
                MapCompass()
                if isFullHeight {
                    MapScaleView()
                }
            }

            VStack(alignment: .trailing, spacing: 8) {
                if (navigation.activeSummary?.routeGeometry.count ?? 0) >= 2 {
                    Button {
                        fitActiveRoute(animated: true)
                    } label: {
                        Label("Fit Route", systemImage: "point.topleft.down.curvedto.point.bottomright.up")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(.ultraThinMaterial, in: Capsule())
                            .overlay(
                                Capsule()
                                    .strokeBorder(JoatTheme.heroBorder, lineWidth: 0.8)
                            )
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(JoatTheme.accent)
                } else if let candidate = selectedCandidate {
                    Button {
                        focusCamera(on: candidate.point, meters: 900)
                    } label: {
                        Label("Center Pin", systemImage: "scope")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(.ultraThinMaterial, in: Capsule())
                            .overlay(
                                Capsule()
                                    .strokeBorder(JoatTheme.cardBorder, lineWidth: 0.8)
                            )
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.primary)
                }

                if phoneLocation.isAuthorized {
                    Button {
                        Task {
                            if let pt = await phoneLocation.requestOneShot(timeout: 2.0) {
                                focusCamera(on: pt, meters: 900)
                            } else {
                                withAnimation(.easeInOut(duration: 0.3)) {
                                    cameraPosition = .userLocation(fallback: .automatic)
                                }
                            }
                        }
                    } label: {
                        Image(systemName: "location.fill")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(JoatTheme.accent)
                            .frame(width: 32, height: 32)
                            .background(.ultraThinMaterial, in: Circle())
                            .overlay(
                                Circle()
                                    .strokeBorder(JoatTheme.cardBorder, lineWidth: 0.8)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Center on current location")
                }
            }
            .padding(12)
        }
    }

    // MARK: - Banners & Location Permission

    private func statusBannerCard(_ banner: StatusBanner) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: banner.isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .foregroundStyle(banner.isError ? JoatTheme.amber : JoatTheme.accent)
                .font(.subheadline)
                .padding(.top, 1)

            Text(banner.text)
                .font(.footnote)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            Spacer(minLength: 4)

            Button {
                withAnimation(.snappy(duration: 0.2)) {
                    statusBanner = nil
                }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
        }
        .joatCardContainer(cornerRadius: 14, padding: 12)
    }

    private var locationPermissionCard: some View {
        HStack(alignment: .center, spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(JoatTheme.amber.opacity(0.16))
                    .frame(width: 36, height: 36)
                Image(systemName: "location.slash.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(JoatTheme.amber)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text("iPhone GPS Off")
                    .font(.subheadline.weight(.semibold))
                Text("Enable Location for 1 Hz lock-screen tracking and nearby search.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 6)

            Button(phoneLocation.authorization == .notDetermined ? "Allow" : "Settings") {
                Task {
                    if phoneLocation.authorization == .notDetermined {
                        _ = await phoneLocation.requestPermission()
                    } else if let url = URL(string: UIApplication.openSettingsURLString) {
                        await UIApplication.shared.open(url)
                    }
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(JoatTheme.accent)
            .controlSize(.small)
        }
        .joatCardContainer(cornerRadius: 16, padding: 12)
    }

    // MARK: - Active Route Card

    private func activeRouteCard(_ active: Navigation.ActiveRouteSummary) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                JoatStatusPill(
                    label: NavFormatters.modeDisplayTitle(active.mode),
                    color: JoatTheme.accent,
                    icon: NavFormatters.modeIcon(active.mode)
                )
                if let badge = NavFormatters.phaseBadge(for: active) {
                    JoatStatusPill(label: badge.label, color: badge.color)
                }
                Spacer()
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(active.destinationLabel)
                    .font(.title3.weight(.bold))
                    .foregroundStyle(.primary)
                if !active.destinationAddress.isEmpty,
                   active.destinationAddress.lowercased() != active.destinationLabel.lowercased() {
                    Text(active.destinationAddress)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            HStack(alignment: .center, spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(JoatTheme.accent.opacity(0.16))
                        .frame(width: 42, height: 42)
                    Image(systemName: NavFormatters.maneuverIcon(for: active.maneuver, arrived: active.arrived))
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(JoatTheme.accent)
                }

                VStack(alignment: .leading, spacing: 3) {
                    if let stepDist = NavFormatters.distance(active.maneuverDistanceM),
                       !active.arrived,
                       (active.maneuverDistanceM ?? 0) > 0 {
                        Text("In \(stepDist)")
                            .font(.caption.weight(.bold).monospacedDigit())
                            .foregroundStyle(JoatTheme.accent)
                    }
                    Text(active.instruction.isEmpty ? "Following route on glasses" : active.instruction)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(JoatTheme.card.opacity(0.78))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
            )

            let remText = NavFormatters.distance(active.remainingM)
            let etaText = NavFormatters.eta(active.etaMinutes)
            if remText != nil || etaText != nil {
                HStack(spacing: 16) {
                    if let etaText {
                        VStack(alignment: .leading, spacing: 1) {
                            Text("ETA")
                                .font(.caption2.weight(.medium))
                                .foregroundStyle(.secondary)
                            Text(etaText)
                                .font(.subheadline.weight(.bold).monospacedDigit())
                                .foregroundStyle(.primary)
                        }
                    }
                    if let remText {
                        VStack(alignment: .leading, spacing: 1) {
                            Text("Remaining")
                                .font(.caption2.weight(.medium))
                                .foregroundStyle(.secondary)
                            Text(remText)
                                .font(.subheadline.weight(.bold).monospacedDigit())
                                .foregroundStyle(.primary)
                        }
                    }
                    Spacer()
                }
            }

            HStack(spacing: 10) {
                Button {
                    navigation.resumeGuidanceOnGlasses()
                    statusBanner = StatusBanner(
                        text: glassesConnected
                            ? "Showing active guidance on Even G2."
                            : "Connect Even G2 in Even Hub to view guidance on the lens.",
                        isError: !glassesConnected
                    )
                } label: {
                    Label("Show on Glasses", systemImage: "eyeglasses")
                        .font(.footnote.weight(.semibold))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(JoatTheme.accent)
                .controlSize(.regular)

                Button(role: .destructive) {
                    navigation.stopFromPhone()
                    statusBanner = nil
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                        .font(.footnote.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .controlSize(.regular)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(JoatTheme.heroGradient)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(JoatTheme.heroBorder, lineWidth: 1)
        )
    }

    // MARK: - Quick Start Card

    private var quickStartCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Quick Start", systemImage: "paperplane.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                Spacer()
                JoatStatusPill(
                    label: glassesConnected ? "G2 Linked" : "G2 Standby",
                    color: glassesConnected ? JoatTheme.accent : JoatTheme.amber,
                    icon: "eyeglasses"
                )
            }

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)

                TextField("Search Apple Maps or address…", text: $searchText)
                    .focused($searchFocused)
                    .submitLabel(.search)
                    .autocorrectionDisabled()
                    .onSubmit {
                        Task { await resolveTypedDestination() }
                    }
                    .onChange(of: searchText) { _, newValue in
                        if suppressSearchUpdate {
                            suppressSearchUpdate = false
                            return
                        }
                        selectedCandidate = nil
                        searchModel.updateQuery(newValue, near: searchBiasPoint)
                    }

                if isResolvingCandidate || searchModel.isCompleting {
                    ProgressView()
                        .controlSize(.small)
                }

                if !searchText.isEmpty {
                    Button {
                        clearSearch()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear search")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(JoatTheme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(searchFocused ? JoatTheme.accent.opacity(0.6) : JoatTheme.cardBorder, lineWidth: 1)
            )

            if !searchModel.suggestions.isEmpty {
                VStack(spacing: 0) {
                    ForEach(Array(searchModel.suggestions.enumerated()), id: \.element.id) { idx, suggestion in
                        if idx > 0 {
                            Divider()
                        }
                        Button {
                            Task { await pickSuggestion(suggestion) }
                        } label: {
                            HStack(alignment: .top, spacing: 10) {
                                Image(systemName: "mappin.circle.fill")
                                    .foregroundStyle(JoatTheme.accent)
                                    .font(.body)
                                    .padding(.top, 1)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(suggestion.title)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(.primary)
                                        .lineLimit(1)
                                    if !suggestion.subtitle.isEmpty {
                                        Text(suggestion.subtitle)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                    }
                                }
                                Spacer(minLength: 4)
                                Image(systemName: "arrow.up.left")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .padding(.vertical, 9)
                            .padding(.horizontal, 10)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(JoatTheme.surface)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
                )
            }

            if let candidate = selectedCandidate {
                selectedCandidateChip(candidate)
            }

            travelModeSelector

            Button {
                Task { await startQuickRoute() }
            } label: {
                HStack(spacing: 8) {
                    if isStartingRoute {
                        ProgressView()
                            .tint(.white)
                            .controlSize(.small)
                    } else {
                        Image(systemName: "eyeglasses")
                    }
                    Text("Start on Glasses")
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
            }
            .buttonStyle(.borderedProminent)
            .tint(JoatTheme.accent)
            .controlSize(.regular)
            .disabled(
                isStartingRoute || (
                    selectedCandidate == nil
                    && searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                )
            )
        }
        .joatCardContainer(cornerRadius: 18, padding: 14)
    }

    private func selectedCandidateChip(_ candidate: NavCandidate) -> some View {
        let alreadySaved = navigation.places.contains { place in
            if let lat = place.latitude, let lon = place.longitude {
                return NavigationMaps.distance(NavPoint(lat, lon), candidate.point) < 35
            }
            return place.name.caseInsensitiveCompare(candidate.label) == .orderedSame
        }

        return HStack(alignment: .center, spacing: 10) {
            ZStack {
                Circle()
                    .fill(JoatTheme.accent.opacity(0.16))
                    .frame(width: 32, height: 32)
                Image(systemName: "mappin.and.ellipse")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(JoatTheme.accent)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(candidate.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(candidate.address)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer(minLength: 4)

            if alreadySaved {
                JoatStatusPill(label: "Saved", color: JoatTheme.accent, icon: "checkmark")
            } else {
                Button {
                    placeSheetTarget = .add(presetName: nil, candidate: candidate)
                } label: {
                    Label("Save Place", systemImage: "bookmark.fill")
                        .font(.caption.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .tint(JoatTheme.accent)
                .controlSize(.mini)
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(JoatTheme.accent.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(JoatTheme.accent.opacity(0.28), lineWidth: 1)
        )
    }

    private var travelModeSelector: some View {
        HStack(spacing: 6) {
            ForEach(NavFormatters.modeOrder, id: \.self) { mode in
                let active = selectedMode == mode
                Button {
                    withAnimation(.snappy(duration: 0.18)) {
                        selectedMode = mode
                    }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: NavFormatters.modeIcon(mode))
                            .font(.system(size: 12, weight: .semibold))
                        Text(NavFormatters.modeShortTitle(mode))
                            .font(.footnote.weight(active ? .semibold : .medium))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .foregroundStyle(active ? Color.white : .primary)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(active ? JoatTheme.accent : Color.clear)
                    )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(4)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(JoatTheme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
        )
    }

    // MARK: - Saved Places Section

    private var savedPlacesSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Saved Places")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)

                if !navigation.places.isEmpty {
                    Text("\(navigation.places.count)")
                        .font(.caption2.weight(.bold).monospacedDigit())
                        .foregroundStyle(JoatTheme.accent)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(JoatTheme.accent.opacity(0.14), in: Capsule())
                }

                Spacer()

                Button {
                    placeSheetTarget = .add(presetName: nil, candidate: nil)
                } label: {
                    Label("Add Place", systemImage: "plus")
                        .font(.caption.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .tint(JoatTheme.accent)
                .controlSize(.small)
            }

            if navigation.places.isEmpty {
                emptySavedPlacesCard
            } else {
                let missingPresets = ["Home", "Work"].filter { preset in
                    !navigation.places.contains { $0.name.caseInsensitiveCompare(preset) == .orderedSame }
                }
                if !missingPresets.isEmpty {
                    HStack(spacing: 8) {
                        ForEach(missingPresets, id: \.self) { preset in
                            Button {
                                placeSheetTarget = .add(presetName: preset, candidate: nil)
                            } label: {
                                Label("+ Add \(preset)", systemImage: NavFormatters.placeIcon(for: preset))
                                    .font(.caption.weight(.semibold))
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 6)
                                    .background(JoatTheme.card, in: Capsule())
                                    .overlay(
                                        Capsule()
                                            .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
                                    )
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(JoatTheme.accent)
                        }
                        Spacer()
                    }
                }

                VStack(spacing: 10) {
                    ForEach(navigation.places) { place in
                        savedPlaceCard(place)
                    }
                }
            }
        }
    }

    private var emptySavedPlacesCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(JoatTheme.accent.opacity(0.14))
                        .frame(width: 36, height: 36)
                    Image(systemName: "bookmark.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(JoatTheme.accent)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Pin Home, Work & Favorites")
                        .font(.subheadline.weight(.semibold))
                    Text("Saved places appear directly in the glasses NAVIGATION menu for 1-tap routing.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 8) {
                Button {
                    placeSheetTarget = .add(presetName: "Home", candidate: nil)
                } label: {
                    Label("+ Add Home", systemImage: "house.fill")
                        .font(.caption.weight(.semibold))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(JoatTheme.accent)
                .controlSize(.small)

                Button {
                    placeSheetTarget = .add(presetName: "Work", candidate: nil)
                } label: {
                    Label("+ Add Work", systemImage: "briefcase.fill")
                        .font(.caption.weight(.semibold))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(JoatTheme.accent)
                .controlSize(.small)
            }
        }
        .joatCardContainer(cornerRadius: 16, padding: 14)
    }

    private func savedPlaceCard(_ place: SavedPlace) -> some View {
        let subtitle: String = {
            if let addr = place.address, !addr.isEmpty {
                return addr
            }
            if let lat = place.latitude, let lon = place.longitude {
                return String(format: "%.4f, %.4f", lat, lon)
            }
            return "Saved destination"
        }()

        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                Button {
                    if let lat = place.latitude, let lon = place.longitude {
                        focusedPlaceId = place.id
                        focusCamera(on: NavPoint(lat, lon), meters: 900)
                    }
                } label: {
                    HStack(alignment: .center, spacing: 10) {
                        ZStack {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(JoatTheme.accent.opacity(0.14))
                                .frame(width: 36, height: 36)
                            Image(systemName: NavFormatters.placeIcon(for: place.name))
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(JoatTheme.accent)
                        }

                        VStack(alignment: .leading, spacing: 2) {
                            Text(place.name)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            Text(subtitle)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                }
                .buttonStyle(.plain)

                Spacer(minLength: 4)

                Menu {
                    Button {
                        placeSheetTarget = .edit(place)
                    } label: {
                        Label("Edit Place", systemImage: "pencil")
                    }
                    if let lat = place.latitude, let lon = place.longitude {
                        Button {
                            focusedPlaceId = place.id
                            focusCamera(on: NavPoint(lat, lon), meters: 900)
                        } label: {
                            Label("Show on Map", systemImage: "map")
                        }
                    }
                    Divider()
                    Button(role: .destructive) {
                        if focusedPlaceId == place.id { focusedPlaceId = nil }
                        navigation.removePlace(id: place.id)
                    } label: {
                        Label("Delete Place", systemImage: "trash")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 4)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Options for \(place.name)")
            }

            HStack(spacing: 8) {
                ForEach(NavFormatters.modeOrder, id: \.self) { mode in
                    Button {
                        launchSavedPlace(place, mode: mode)
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: NavFormatters.modeIcon(mode))
                                .font(.system(size: 11, weight: .semibold))
                            Text(NavFormatters.modeShortTitle(mode))
                                .font(.caption.weight(.semibold))
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                        .foregroundStyle(JoatTheme.accent)
                        .background(
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .fill(JoatTheme.accent.opacity(0.12))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .strokeBorder(JoatTheme.accent.opacity(0.24), lineWidth: 0.8)
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .joatCardContainer(cornerRadius: 16, padding: 12)
        .contextMenu {
            Button {
                placeSheetTarget = .edit(place)
            } label: {
                Label("Edit Place", systemImage: "pencil")
            }
            Button(role: .destructive) {
                if focusedPlaceId == place.id { focusedPlaceId = nil }
                navigation.removePlace(id: place.id)
            } label: {
                Label("Delete Place", systemImage: "trash")
            }
        }
    }

    // MARK: - Guidance Audio Card

    private var guidanceAudioCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle(isOn: $navigation.spokenDirections) {
                VStack(alignment: .leading, spacing: 3) {
                    Label("Spoken directions in headphones", systemImage: "headphones")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Text("Speaks approaching turns, reroutes, and arrival into AirPods or Bluetooth audio.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .tint(JoatTheme.accent)
        }
        .joatCardContainer(cornerRadius: 16, padding: 14)
    }

    // MARK: - Actions & Camera Management

    private func clearSearch() {
        suppressSearchUpdate = true
        searchText = ""
        selectedCandidate = nil
        searchModel.clear()
    }

    private func pickSuggestion(_ suggestion: PlaceSuggestion) async {
        searchFocused = false
        isResolvingCandidate = true
        let resolved = await searchModel.resolve(suggestion, near: searchBiasPoint)
        isResolvingCandidate = false
        searchModel.clear()
        guard let resolved else {
            statusBanner = StatusBanner(text: "Could not resolve that Apple Maps place.", isError: true)
            return
        }
        suppressSearchUpdate = true
        searchText = resolved.label
        selectedCandidate = resolved
        focusCamera(on: resolved.point, meters: 900)
    }

    private func resolveTypedDestination() async {
        let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        searchFocused = false
        isResolvingCandidate = true
        let resolved = await searchModel.resolveQuery(trimmed, near: searchBiasPoint)
        isResolvingCandidate = false
        searchModel.clear()
        if let resolved {
            suppressSearchUpdate = true
            searchText = resolved.label
            selectedCandidate = resolved
            focusCamera(on: resolved.point, meters: 900)
        } else {
            statusBanner = StatusBanner(text: "No Apple Maps match found for “\(trimmed)”.", isError: true)
        }
    }

    private func startQuickRoute() async {
        searchFocused = false
        statusBanner = nil

        var candidate = selectedCandidate
        let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)

        if candidate == nil, !trimmed.isEmpty {
            isStartingRoute = true
            candidate = await searchModel.resolveQuery(trimmed, near: searchBiasPoint)
            isStartingRoute = false
            searchModel.clear()
            if let candidate {
                suppressSearchUpdate = true
                searchText = candidate.label
                selectedCandidate = candidate
                focusCamera(on: candidate.point, meters: 900)
            }
        }

        let destinationText = candidate?.label ?? trimmed
        guard !destinationText.isEmpty else { return }

        if let error = navigation.startFromPhone(
            destination: destinationText,
            candidate: candidate,
            mode: selectedMode
        ) {
            statusBanner = StatusBanner(text: error, isError: true)
        } else {
            statusBanner = StatusBanner(
                text: "Starting \(NavFormatters.modeDisplayTitle(selectedMode).lowercased()) directions to \(destinationText) on Even G2.",
                isError: false
            )
        }
    }

    private func launchSavedPlace(_ place: SavedPlace, mode: String) {
        selectedMode = mode
        let candidate: NavCandidate?
        if let lat = place.latitude, let lon = place.longitude, NavPoint(lat, lon).isValid {
            let pt = NavPoint(lat, lon)
            focusedPlaceId = place.id
            focusCamera(on: pt, meters: 900)
            candidate = NavCandidate(
                choiceId: "place:\(place.id)",
                label: place.name,
                address: place.address ?? "Saved place",
                point: pt
            )
        } else {
            candidate = nil
        }

        let query = place.address?.isEmpty == false ? place.name : place.name
        if let error = navigation.startFromPhone(destination: query, candidate: candidate, mode: mode) {
            statusBanner = StatusBanner(text: error, isError: true)
        } else {
            statusBanner = StatusBanner(
                text: "Starting \(NavFormatters.modeDisplayTitle(mode).lowercased()) directions to \(place.name) on Even G2.",
                isError: false
            )
        }
    }

    private func syncInitialCamera(animated: Bool) {
        if (navigation.activeSummary?.routeGeometry.count ?? 0) >= 2 {
            fitActiveRoute(animated: animated)
            return
        }
        if let dest = navigation.activeSummary?.destinationPoint, dest.isValid {
            focusCamera(on: dest, meters: 1_200, animated: animated)
            return
        }
        if let candidate = selectedCandidate, candidate.point.isValid {
            focusCamera(on: candidate.point, meters: 900, animated: animated)
            return
        }
        let savedPoints = placesWithCoordinates.compactMap { place -> NavPoint? in
            guard let lat = place.latitude, let lon = place.longitude else { return nil }
            return NavPoint(lat, lon)
        }
        if savedPoints.count >= 2,
           let region = Self.boundingRegion(for: savedPoints, paddingFactor: 1.6, minSpan: 0.015),
           region.span.latitudeDelta < 1.5,
           region.span.longitudeDelta < 1.5 {
            applyCamera(.region(region), animated: animated)
            return
        }
        if let first = savedPoints.first {
            focusCamera(on: first, meters: 2_200, animated: animated)
            return
        }
        if phoneLocation.isAuthorized {
            applyCamera(.userLocation(fallback: .automatic), animated: animated)
        }
    }

    private func fitActiveRoute(animated: Bool) {
        guard let active = navigation.activeSummary else { return }
        var pts = active.routeGeometry.filter(\.isValid)
        if let dest = active.destinationPoint, dest.isValid {
            pts.append(dest)
        }
        guard let region = Self.boundingRegion(for: pts, paddingFactor: 1.48, minSpan: 0.0045) else { return }
        applyCamera(.region(region), animated: animated)
    }

    private func focusCamera(on point: NavPoint, meters: CLLocationDistance, animated: Bool = true) {
        guard point.isValid else { return }
        let region = MKCoordinateRegion(
            center: point.coordinate,
            latitudinalMeters: meters,
            longitudinalMeters: meters
        )
        applyCamera(.region(region), animated: animated)
    }

    private func applyCamera(_ next: MapCameraPosition, animated: Bool) {
        if animated {
            withAnimation(.easeInOut(duration: 0.35)) {
                cameraPosition = next
            }
        } else {
            cameraPosition = next
        }
    }

    private func isSameDestination(_ a: NavPoint, _ b: NavPoint?) -> Bool {
        guard let b, a.isValid, b.isValid else { return false }
        return NavigationMaps.distance(a, b) < 12
    }

    private static func boundingRegion(
        for points: [NavPoint],
        paddingFactor: Double,
        minSpan: CLLocationDegrees
    ) -> MKCoordinateRegion? {
        let valid = points.filter(\.isValid)
        guard let first = valid.first else { return nil }
        var minLat = first.latitude
        var maxLat = first.latitude
        var minLon = first.longitude
        var maxLon = first.longitude
        for p in valid.dropFirst() {
            minLat = min(minLat, p.latitude)
            maxLat = max(maxLat, p.latitude)
            minLon = min(minLon, p.longitude)
            maxLon = max(maxLon, p.longitude)
        }
        let center = CLLocationCoordinate2D(
            latitude: (minLat + maxLat) / 2.0,
            longitude: (minLon + maxLon) / 2.0
        )
        let latSpan = min(120.0, max(minSpan, (maxLat - minLat) * paddingFactor))
        let lonSpan = min(120.0, max(minSpan, (maxLon - minLon) * paddingFactor))
        return MKCoordinateRegion(
            center: center,
            span: MKCoordinateSpan(latitudeDelta: latSpan, longitudeDelta: lonSpan)
        )
    }
}

// MARK: - Map Pin Views

private struct SavedPlaceMapPin: View {
    let icon: String
    let isFocused: Bool

    var body: some View {
        ZStack {
            Circle()
                .fill(isFocused ? JoatTheme.accent : JoatTheme.card)
                .frame(width: isFocused ? 34 : 28, height: isFocused ? 34 : 28)
            Circle()
                .strokeBorder(JoatTheme.accent, lineWidth: isFocused ? 2 : 1.4)
                .frame(width: isFocused ? 34 : 28, height: isFocused ? 34 : 28)
            Image(systemName: icon)
                .font(.system(size: isFocused ? 14 : 12, weight: .bold))
                .foregroundStyle(isFocused ? Color.white : JoatTheme.accent)
        }
        .shadow(color: .black.opacity(0.22), radius: 4, y: 2)
    }
}

private struct HighlightedMapPin: View {
    let icon: String
    let tint: Color

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Circle()
                    .fill(tint.opacity(0.24))
                    .frame(width: 44, height: 44)
                Circle()
                    .fill(tint)
                    .frame(width: 32, height: 32)
                Circle()
                    .strokeBorder(Color.white.opacity(0.9), lineWidth: 1.5)
                    .frame(width: 32, height: 32)
                Image(systemName: icon)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(.white)
            }
            Image(systemName: "arrowtriangle.down.fill")
                .font(.system(size: 9))
                .foregroundStyle(tint)
                .offset(y: -4)
        }
        .shadow(color: .black.opacity(0.28), radius: 5, y: 2)
    }
}

// MARK: - Add / Edit Saved Place Sheet

enum SavedPlaceSheetTarget: Identifiable {
    case add(presetName: String?, candidate: NavCandidate?)
    case edit(SavedPlace)

    var id: String {
        switch self {
        case .add(let preset, let candidate):
            return "add:\(preset ?? "custom"):\(candidate?.choiceId ?? "blank")"
        case .edit(let place):
            return "edit:\(place.id)"
        }
    }
}

struct AddSavedPlaceSheet: View {
    @ObservedObject var navigation: Navigation
    let target: SavedPlaceSheetTarget
    let biasPoint: NavPoint?
    var onSaved: (SavedPlace) -> Void = { _ in }

    @ObservedObject private var phoneLocation = PhoneLocation.shared
    @StateObject private var searchModel = PlaceSearchModel()
    @Environment(\.dismiss) private var dismiss

    @State private var name: String
    @State private var address: String
    @State private var pinnedPoint: NavPoint?
    @State private var suppressAddressCompletion = false
    @State private var isLocatingCurrent = false
    @State private var isSaving = false
    @State private var errorMessage: String?

    private static let presets = ["Home", "Work", "Gym", "Coffee", "Airport"]

    init(
        navigation: Navigation,
        target: SavedPlaceSheetTarget,
        biasPoint: NavPoint?,
        onSaved: @escaping (SavedPlace) -> Void = { _ in }
    ) {
        self.navigation = navigation
        self.target = target
        self.biasPoint = biasPoint
        self.onSaved = onSaved

        switch target {
        case .add(let presetName, let candidate):
            let initialName = presetName ?? candidate?.label ?? ""
            _name = State(initialValue: initialName)
            _address = State(initialValue: candidate?.address ?? "")
            _pinnedPoint = State(initialValue: candidate?.point)
        case .edit(let place):
            _name = State(initialValue: place.name)
            _address = State(initialValue: place.address ?? "")
            if let lat = place.latitude, let lon = place.longitude {
                _pinnedPoint = State(initialValue: NavPoint(lat, lon))
            } else {
                _pinnedPoint = State(initialValue: nil)
            }
        }
    }

    private var editingPlaceId: String? {
        if case .edit(let place) = target { return place.id }
        return nil
    }

    private var canSave: Bool {
        let hasName = !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let hasLocation = !address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pinnedPoint != nil
        return hasName && hasLocation && !isSaving && !isLocatingCurrent
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Label") {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(Self.presets, id: \.self) { preset in
                                let selected = name.caseInsensitiveCompare(preset) == .orderedSame
                                Button {
                                    name = preset
                                } label: {
                                    HStack(spacing: 5) {
                                        Image(systemName: NavFormatters.placeIcon(for: preset))
                                            .font(.system(size: 11, weight: .semibold))
                                        Text(preset)
                                            .font(.caption.weight(.semibold))
                                    }
                                    .padding(.horizontal, 11)
                                    .padding(.vertical, 6)
                                    .foregroundStyle(selected ? Color.white : JoatTheme.accent)
                                    .background(
                                        Capsule()
                                            .fill(selected ? JoatTheme.accent : JoatTheme.accent.opacity(0.12))
                                    )
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(.vertical, 2)
                    }

                    TextField("Place name (e.g. Home, Work, Studio)", text: $name)
                        .autocorrectionDisabled()
                }

                Section {
                    HStack(spacing: 8) {
                        TextField("Search street address or place…", text: $address)
                            .autocorrectionDisabled()
                            .onChange(of: address) { _, newValue in
                                if suppressAddressCompletion {
                                    suppressAddressCompletion = false
                                    return
                                }
                                pinnedPoint = nil
                                searchModel.updateQuery(newValue, near: biasPoint)
                            }

                        if !address.isEmpty {
                            Button {
                                suppressAddressCompletion = true
                                address = ""
                                pinnedPoint = nil
                                searchModel.clear()
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }

                    if !searchModel.suggestions.isEmpty {
                        ForEach(searchModel.suggestions) { suggestion in
                            Button {
                                Task { await applySuggestion(suggestion) }
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(suggestion.title)
                                        .font(.subheadline.weight(.medium))
                                        .foregroundStyle(.primary)
                                    if !suggestion.subtitle.isEmpty {
                                        Text(suggestion.subtitle)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                .padding(.vertical, 2)
                            }
                        }
                    }

                    Button {
                        Task { await useCurrentIPhoneLocation() }
                    } label: {
                        HStack(spacing: 8) {
                            if isLocatingCurrent {
                                ProgressView()
                                    .controlSize(.small)
                            } else {
                                Image(systemName: "location.fill")
                                    .foregroundStyle(JoatTheme.accent)
                            }
                            Text("Use Current iPhone Location")
                                .fontWeight(.medium)
                        }
                    }
                    .disabled(isLocatingCurrent || isSaving)

                    if let point = pinnedPoint, point.isValid {
                        HStack {
                            JoatStatusPill(
                                label: String(format: "Pinned · %.4f, %.4f", point.latitude, point.longitude),
                                color: JoatTheme.accent,
                                icon: "checkmark.circle.fill"
                            )
                            Spacer()
                            if !address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                Button("Clear Pin") {
                                    pinnedPoint = nil
                                }
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            }
                        }
                    }
                } header: {
                    Text("Address or GPS Pin")
                } footer: {
                    Text("Pinned coordinates start turn-by-turn guidance immediately without an extra search step.")
                }

                if let errorMessage, !errorMessage.isEmpty {
                    Section {
                        Text(errorMessage)
                            .font(.footnote)
                            .foregroundStyle(JoatTheme.amber)
                    }
                }
            }
            .navigationTitle(editingPlaceId == nil ? "Add Saved Place" : "Edit Saved Place")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isSaving ? "Saving…" : "Save") {
                        Task { await savePlace() }
                    }
                    .fontWeight(.semibold)
                    .disabled(!canSave)
                }
            }
        }
        .tint(JoatTheme.accent)
    }

    private func applySuggestion(_ suggestion: PlaceSuggestion) async {
        errorMessage = nil
        if let resolved = await searchModel.resolve(suggestion, near: biasPoint) {
            suppressAddressCompletion = true
            address = resolved.address
            pinnedPoint = resolved.point
            if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                name = resolved.label
            }
        } else {
            let combined = [suggestion.title, suggestion.subtitle]
                .filter { !$0.isEmpty }
                .joined(separator: ", ")
            suppressAddressCompletion = true
            address = combined
        }
        searchModel.clear()
    }

    private func useCurrentIPhoneLocation() async {
        errorMessage = nil
        isLocatingCurrent = true
        defer { isLocatingCurrent = false }

        if !phoneLocation.isAuthorized {
            _ = await phoneLocation.requestPermission()
        }
        guard let point = await phoneLocation.requestOneShot() else {
            errorMessage = "Could not read your iPhone's current GPS position. Ensure Location is allowed."
            return
        }
        pinnedPoint = point
        searchModel.clear()
        if let resolvedAddr = await NavigationMaps.address(at: point), !resolvedAddr.isEmpty {
            suppressAddressCompletion = true
            address = resolvedAddr
        }
    }

    private func savePlace() async {
        errorMessage = nil
        let cleanName = PlaceSearchModel.clipped(name.collapsed, maxChars: 40)
        var cleanAddress = PlaceSearchModel.clipped(address.collapsed, maxChars: 120)
        guard !cleanName.isEmpty else {
            errorMessage = "Enter a name for this place."
            return
        }

        isSaving = true
        var finalPoint = pinnedPoint
        if finalPoint == nil, !cleanAddress.isEmpty {
            if let resolved = await searchModel.resolveQuery(cleanAddress, near: biasPoint) {
                finalPoint = resolved.point
                if cleanAddress.count < 6 {
                    cleanAddress = PlaceSearchModel.clipped(resolved.address, maxChars: 120)
                }
            }
        }
        isSaving = false

        guard !cleanAddress.isEmpty || finalPoint != nil else {
            errorMessage = "Enter an address or use your current iPhone location."
            return
        }

        guard let saved = navigation.upsertPlace(
            id: editingPlaceId,
            name: cleanName,
            address: cleanAddress.isEmpty ? nil : cleanAddress,
            point: finalPoint
        ) else {
            errorMessage = "Could not save place. Check the name and address."
            return
        }

        onSaved(saved)
        dismiss()
    }
}
