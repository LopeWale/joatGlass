import Foundation

/// One place the wearer saved in JOAT Brain (or migrated once from Even Hub).
struct SavedPlace: Identifiable, Codable, Equatable, Hashable {
    let id: String
    let name: String
    let address: String?
    let latitude: Double?
    let longitude: Double?

    init(id: String, name: String, address: String?, latitude: Double?, longitude: Double?) {
        self.id = id
        self.name = name
        self.address = address
        self.latitude = latitude
        self.longitude = longitude
    }

    var point: NavPoint? {
        guard let latitude, let longitude else { return nil }
        let p = NavPoint(latitude, longitude)
        return p.isValid ? p : nil
    }
}

/// Navigation on the phone: voice commands, Saved Places CRUD, selectable
/// `NAVIGATION` glasses list, `PhoneLocation` GPS streaming, `NavigationMapTiles`
/// 4-bit street tile prefetch, spoken headphone cues, and live phone state.
final class Navigation: ObservableObject, BrainFeature, VoiceCommand {
    let name = "navigation"
    let uplinkKinds: Set<String> = [
        "navigation.action", "navigation.view.ack", "navigation.origin", "navigation.origin.failed",
        "navigation.route.ack", "navigation.state", "navigation.reroute.request", "places", "location",
    ]

    static let placesKey = "joat.navigation.places.v1"
    static let migratedPluginPlaceIdsKey = "joat.navigation.migratedPluginPlaceIds.v1"
    static let spokenDirectionsKey = "joat.navigation.voiceCues"
    /// session.py MAX_NAVIGATION_DESTINATION_CHARS
    static let maxDestinationChars = 200
    /// hub_gateway.py DEFAULT_LOCATION_REFRESH_TIMEOUT_S
    static let refreshSeconds: TimeInterval = 7
    /// A fix this old still says which city the wearer is in: a search bias.
    static let biasMaxAge: TimeInterval = 3600
    static let nearbyMaxAge: TimeInterval = 120

    struct ActiveRouteSummary: Equatable {
        let sessionId: String
        let destinationLabel: String
        let destinationAddress: String
        let destinationPoint: NavPoint?
        let mode: String           // "walking" | "cycling" | "driving"
        let phase: String          // "searching" | "confirming" | "routing" | "preview" | "guidance" | "rerouting" | "arrived" | "error"
        let instruction: String
        let maneuver: String?
        let maneuverDistanceM: Double?
        let remainingM: Double?
        let etaMinutes: Double?
        let arrived: Bool
        let routeGeometry: [NavPoint]
    }

    private enum MenuRowAction {
        case showActiveGuidance
        case stopNavigation
        case askDestination
        case startSavedPlace(SavedPlace)
    }

    private let link: PluginLink
    private let defaults: UserDefaults
    @Published private(set) var places: [SavedPlace]
    @Published private(set) var activeSummary: ActiveRouteSummary?
    @Published var spokenDirections: Bool {
        didSet {
            defaults.set(spokenDirections, forKey: Self.spokenDirectionsKey)
            Log.add("navigation spoken directions \(spokenDirections ? "on" : "off")")
        }
    }

    private var fix: (point: NavPoint, at: Date, accuracy: Double?)?
    private var refreshes: [String: (NavPoint?) -> Void] = [:]
    private(set) var session: NavigationSession?
    private var menuActions: [MenuRowAction] = []

    /// Glasses speech-in-text prompt controller (`ownerKind: "capability"`).
    var prompts: Prompts?
    /// Whether Even Hub is currently connected to LoopbackServer.
    var isPluginConnected: () -> Bool = { true }
    /// Speaks concise turn-by-turn cues into connected headphones (`Voice.sayNavigation`).
    var sayCue: ((String) -> Void)?
    /// Refreshes the open `NAVIGATION` firmware list when places or session state change.
    var onMenuChanged: (() -> Void)?
    /// Retires `CapabilityMenu` ownership when `navigation.view` or `navigation.resume` claims the lens.
    var onClaimedGlass: (() -> Void)?
    /// Brain.ask fallback when `prompts` is not wired.
    var askFollowUp: ((String, @escaping (String) -> Void) -> Void)?
    /// A short answer over the current page, also said aloud.
    var receipt: ((String) -> Void)?
    /// Brain: the question on the conversation page now, the answer later.
    var onAsked: ((String) -> Void)?
    var onAnswer: ((String) -> Void)?

    private var areaCache: (point: NavPoint, at: Date, value: (context: String, city: String?, country: String?))?
    private var lastAreaRefreshAt = Date.distantPast

    init(link: PluginLink, defaults: UserDefaults = .standard) {
        self.link = link
        self.defaults = defaults
        if let data = defaults.data(forKey: Self.placesKey),
           let saved = try? JSONDecoder().decode([SavedPlace].self, from: data) {
            places = saved
        } else {
            places = []
        }
        spokenDirections = (defaults.object(forKey: Self.spokenDirectionsKey) as? Bool) ?? true
    }

    var active: Bool { session != nil }

    // MARK: - Area Context for Streaming Assistant

    func cachedArea() -> (context: String, city: String?, country: String?)? {
        guard let point = recentFix(maxAge: Self.biasMaxAge) else { return nil }
        let now = Date()
        if let c = areaCache, now.timeIntervalSince(c.at) < 600, NavigationMaps.distance(c.point, point) < 150 {
            return c.value
        }
        if now.timeIntervalSince(lastAreaRefreshAt) >= 30 {
            lastAreaRefreshAt = now
            Task { @MainActor [weak self] in
                _ = await self?.area()
            }
        }
        if let c = areaCache, now.timeIntervalSince(c.at) < Self.biasMaxAge, NavigationMaps.distance(c.point, point) < 5_000 {
            return c.value
        }
        return (context: String(format: "latitude %.5f, longitude %.5f", point.latitude, point.longitude),
                city: areaCache?.value.city, country: areaCache?.value.country)
    }

    @MainActor
    func area() async -> (context: String, city: String?, country: String?)? {
        guard let point = recentFix(maxAge: Self.biasMaxAge) else { return nil }
        if let c = areaCache, Date().timeIntervalSince(c.at) < 600, NavigationMaps.distance(c.point, point) < 150 {
            return c.value
        }
        let fallback = (context: String(format: "latitude %.5f, longitude %.5f", point.latitude, point.longitude),
                        city: areaCache?.value.city, country: areaCache?.value.country)
        guard let value = await NavigationMaps.area(at: point) else { return fallback }
        areaCache = (point, Date(), value)
        return value
    }

    // MARK: - BrainFeature

    func pluginConnected() {
        if let s = session {
            Log.add("navigation: plugin reconnected, ending \(s.sessionId)")
            session = nil
            s.end()
            activeSummary = nil
        }
        let waiting = refreshes
        refreshes = [:]
        waiting.values.forEach { $0(nil) }
    }

    func handle(kind: String, message: [String: Any]) {
        switch kind {
        case "location":
            guard let lat = (message["latitude"] as? NSNumber)?.doubleValue,
                  let lon = (message["longitude"] as? NSNumber)?.doubleValue else { return }
            let point = NavPoint(lat, lon)
            guard point.isValid else { return }
            fix = (point, Date(), (message["accuracy"] as? NSNumber)?.doubleValue)
            if let id = message.string("refreshId"), let done = refreshes.removeValue(forKey: id) { done(point) }
        case "places":
            acceptPlaces(message)
        default:
            session?.accept(kind: kind, message: message)
        }
    }

    // MARK: - Location (PhoneLocation first, Even Hub fallback)

    fileprivate func notePhoneFix(_ phoneFix: PhoneLocation.Fix) {
        fix = (phoneFix.point, phoneFix.at, phoneFix.accuracy)
    }

    func recentFix(maxAge: TimeInterval) -> NavPoint? {
        let now = Date()
        var best: (point: NavPoint, at: Date)?
        if let fix, now.timeIntervalSince(fix.at) <= maxAge {
            best = (fix.point, fix.at)
        }
        if Thread.isMainThread {
            if let latest = MainActor.assumeIsolated({ PhoneLocation.shared.latest }),
               now.timeIntervalSince(latest.at) <= maxAge {
                if best == nil || latest.at > best!.at {
                    best = (latest.point, latest.at)
                }
            }
        }
        return best?.point
    }

    /// Fresh fix from native `PhoneLocation` when authorized, falling back to
    /// Even Hub's `location.refresh` (7 s ceiling).
    func refreshFix(_ done: @escaping (NavPoint?) -> Void) {
        Task { @MainActor [weak self] in
            guard let self else { done(nil); return }
            if PhoneLocation.shared.isAuthorized,
               let pt = await PhoneLocation.shared.requestOneShot(timeout: 2.5) {
                if let latest = PhoneLocation.shared.latest {
                    self.notePhoneFix(latest)
                } else {
                    self.fix = (pt, Date(), nil)
                }
                done(pt)
                return
            }
            self.refreshFixFromPlugin(done)
        }
    }

    private func refreshFixFromPlugin(_ done: @escaping (NavPoint?) -> Void) {
        let id = "location-" + ConfirmGate.hex(9)
        refreshes[id] = done
        link.send(["kind": "location.refresh", "requestId": id])
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.refreshSeconds) { [weak self] in
            self?.refreshes.removeValue(forKey: id)?(nil)
        }
    }

    // MARK: - Saved Places CRUD & One-Shot Legacy Migration

    private func persistPlaces() {
        if let data = try? JSONEncoder().encode(places) {
            defaults.set(data, forKey: Self.placesKey)
        }
    }

    @discardableResult
    func upsertPlace(id: String? = nil, name: String, address: String?, point: NavPoint?) -> SavedPlace? {
        let cleanName = name.collapsed.utf16Prefix(40)
        guard !cleanName.isEmpty else { return nil }
        let rawAddress = address?.collapsed.utf16Prefix(120) ?? ""
        let cleanAddress: String? = rawAddress.isEmpty ? nil : rawAddress
        let validPoint: NavPoint?
        if let point {
            guard point.isValid else { return nil }
            validPoint = point
        } else {
            validPoint = nil
        }
        guard cleanAddress != nil || validPoint != nil else { return nil }

        let trimmedId = id?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let resolvedId: String
        if !trimmedId.isEmpty {
            resolvedId = String(trimmedId.prefix(64))
        } else if let existing = places.first(where: { Self.placeName($0.name) == Self.placeName(cleanName) }) {
            resolvedId = existing.id
        } else {
            resolvedId = "place-" + ConfirmGate.hex(8)
        }

        let saved = SavedPlace(
            id: resolvedId,
            name: cleanName,
            address: cleanAddress,
            latitude: validPoint?.latitude,
            longitude: validPoint?.longitude
        )

        if let idx = places.firstIndex(where: { $0.id == resolvedId }) {
            places[idx] = saved
        } else {
            places.append(saved)
            if places.count > 50 {
                places = Array(places.prefix(50))
            }
        }
        persistPlaces()
        onMenuChanged?()
        return saved
    }

    func removePlace(id: String) {
        let before = places.count
        places.removeAll { $0.id == id }
        guard places.count != before else { return }
        persistPlaces()
        onMenuChanged?()
    }

    /// One-shot migration for legacy Even Hub WebView `places` uplinks:
    /// - Ignores empty `places: []` payloads so phone-saved places are never wiped.
    /// - Tracks `joat.navigation.migratedPluginPlaceIds.v1` so each legacy ID is
    ///   imported at most once and never resurrects after `removePlace(id:)`.
    func acceptPlaces(_ message: [String: Any]) {
        guard let raw = message["places"] as? [Any], raw.count <= 50 else {
            Log.add("places update rejected: not a list of at most 50")
            return
        }
        guard !raw.isEmpty else { return }

        var parsed: [SavedPlace] = []
        for item in raw {
            guard let p = item as? [String: Any], let place = Self.place(p) else {
                Log.add("places update rejected: one entry is invalid")
                return
            }
            parsed.append(place)
        }
        guard Set(parsed.map(\.id)).count == parsed.count else {
            Log.add("places update rejected: an id repeats")
            return
        }

        var migrated = Set(defaults.stringArray(forKey: Self.migratedPluginPlaceIdsKey) ?? [])
        var changed = false
        var migratedChanged = false

        for candidate in parsed {
            if migrated.contains(candidate.id) {
                continue
            }
            migrated.insert(candidate.id)
            migratedChanged = true
            if !places.contains(where: { $0.id == candidate.id }), places.count < 50 {
                places.append(candidate)
                changed = true
            }
        }

        if migratedChanged {
            defaults.set(Array(migrated).sorted(), forKey: Self.migratedPluginPlaceIdsKey)
        }
        if changed {
            persistPlaces()
            onMenuChanged?()
            Log.add("places: migrated legacy entries (total=\(places.count))")
        }
    }

    static func place(_ raw: [String: Any]) -> SavedPlace? {
        let allowed: Set<String> = ["id", "name", "address", "latitude", "longitude"]
        guard Set(raw.keys).isSubset(of: allowed), let id = raw["id"] as? String,
              (1...64).contains(id.count), let rawName = raw["name"] as? String else { return nil }
        let name = rawName.collapsed
        guard (1...40).contains(name.count) else { return nil }
        var address: String?
        if let a = raw["address"], !(a is NSNull) {
            guard let s = a as? String else { return nil }
            let c = s.collapsed
            address = c.isEmpty ? nil : c
            if let address, address.count > 120 { return nil }
        }
        let lat = (raw["latitude"] as? NSNumber)?.doubleValue
        let lon = (raw["longitude"] as? NSNumber)?.doubleValue
        guard (lat == nil) == (lon == nil) else { return nil }
        if let lat, let lon, !NavPoint(lat, lon).isValid { return nil }
        guard address != nil || lat != nil else { return nil }
        return SavedPlace(id: id, name: name, address: address, latitude: lat, longitude: lon)
    }

    /// navigation.py _saved_place_for: "my home", "the office." and "Home"
    /// name the same saved place.
    func savedPlace(for text: String) -> SavedPlace? {
        let wanted = Self.placeName(text)
        guard !wanted.isEmpty else { return nil }
        return places.first { Self.placeName($0.name) == wanted }
    }

    static func placeName(_ text: String) -> String {
        var words = text.lowercased().replacingOccurrences(of: #"[^\w\s'-]"#, with: " ", options: .regularExpression)
            .split(whereSeparator: { $0.isWhitespace }).map(String.init)
        while let first = words.first, ["my", "the", "our"].contains(first) { words.removeFirst() }
        return words.joined(separator: " ")
    }

    // MARK: - Starting, Stopping & Resuming Routes

    fileprivate func updateSummary(from source: NavigationSession, _ summary: ActiveRouteSummary?) {
        guard session === source else { return }
        activeSummary = summary
    }

    fileprivate func speakCueIfEnabled(_ text: String) {
        guard spokenDirections else { return }
        sayCue?(text)
    }

    private func endExistingSessionForReplacement() {
        guard let old = session else { return }
        session = nil
        Log.add("navigation replacing active session \(old.sessionId)")
        old.end()
        activeSummary = nil
    }

    /// Starts a voice or glasses-menu route, cleanly ending any prior session.
    @discardableResult
    func start(
        _ destination: String,
        initialCandidate: NavCandidate? = nil,
        initialMode: String? = nil,
        autoInstall: Bool = false
    ) -> String? {
        let d = destination.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !d.isEmpty, d.count <= Self.maxDestinationChars, !d.contains("\u{0}") else {
            return "I didn't catch a destination."
        }
        endExistingSessionForReplacement()
        onClaimedGlass?()

        let cleanMode = initialMode.flatMap { ["walking", "cycling", "driving"].contains($0) ? $0 : nil }
        let s = NavigationSession(
            query: d,
            link: link,
            sessionId: "nav-" + ConfirmGate.hex(10),
            proximity: recentFix(maxAge: Self.biasMaxAge),
            owner: self,
            initialCandidate: initialCandidate,
            initialMode: cleanMode,
            autoInstall: autoInstall
        )
        session = s
        s.onEnd = { [weak self, weak s] in
            guard let self, let s, self.session === s else { return }
            self.session = nil
            self.activeSummary = nil
            self.onMenuChanged?()
            Log.add("navigation \(s.sessionId) ended (\(s.arrived ? "arrived" : "cancelled"))")
        }
        Log.add("navigation \(s.sessionId) started (\(d.count) chars, mode=\(cleanMode ?? "ask"), auto=\(autoInstall))")
        s.start()
        return nil
    }

    /// Launches turn-by-turn directions onto Even G2 directly from JOAT Brain.
    /// When `candidate` or a coordinate-backed `SavedPlace` is supplied with `mode`,
    /// skips destination confirmation & preview on the glasses (`installNow = true`).
    @discardableResult
    func startFromPhone(destination: String, candidate: NavCandidate? = nil, mode: String) -> String? {
        guard isPluginConnected() else {
            return "Connect Even G2 in Even Hub first."
        }
        let cleanMode = ["walking", "cycling", "driving"].contains(mode) ? mode : "walking"
        let trimmed = destination.trimmingCharacters(in: .whitespacesAndNewlines)
        let queryText = trimmed.isEmpty ? (candidate?.label.trimmingCharacters(in: .whitespacesAndNewlines) ?? "") : trimmed
        guard !queryText.isEmpty, queryText.count <= Self.maxDestinationChars, !queryText.contains("\u{0}") else {
            return "I didn't catch a destination."
        }

        var directCandidate: NavCandidate?
        if let candidate, candidate.point.isValid {
            directCandidate = candidate
        } else if let saved = savedPlace(for: queryText), let pt = saved.point {
            directCandidate = NavCandidate(
                choiceId: "place:\(saved.id)",
                label: saved.name,
                address: saved.address ?? "Saved place",
                point: pt
            )
        }

        return start(
            directCandidate?.label ?? queryText,
            initialCandidate: directCandidate,
            initialMode: cleanMode,
            autoInstall: directCandidate != nil
        )
    }

    func stopFromPhone() {
        if let s = session {
            session = nil
            s.end()
        }
        activeSummary = nil
        onMenuChanged?()
    }

    func resumeGuidanceOnGlasses() {
        guard let s = session else { return }
        onClaimedGlass?()
        link.send(["kind": "navigation.resume", "sessionId": s.sessionId])
        Log.add("tx navigation.resume \(s.sessionId)")
    }

    #if DEBUG
    func debugSeedSampleState() {
        if places.isEmpty {
            places = [
                SavedPlace(
                    id: "seed-home",
                    name: "Home",
                    address: "333 W Wolf Point Plaza, Chicago, IL",
                    latitude: 41.8885,
                    longitude: -87.6354
                ),
                SavedPlace(
                    id: "seed-work",
                    name: "Work",
                    address: "233 S Wacker Dr, Chicago, IL",
                    latitude: 41.8789,
                    longitude: -87.6359
                ),
                SavedPlace(
                    id: "seed-park",
                    name: "Millennium Park",
                    address: "201 E Randolph St, Chicago, IL",
                    latitude: 41.8826,
                    longitude: -87.6226
                ),
            ]
        }
        if ProcessInfo.processInfo.environment["JOAT_DEBUG_ACTIVE_ROUTE"] != "0" {
            let geometry: [NavPoint] = [
                NavPoint(41.8789, -87.6359),
                NavPoint(41.8818, -87.6358),
                NavPoint(41.8842, -87.6357),
                NavPoint(41.8843, -87.6312),
                NavPoint(41.8843, -87.6268),
                NavPoint(41.8843, -87.6244),
                NavPoint(41.8832, -87.6244),
                NavPoint(41.8826, -87.6226),
            ]
            activeSummary = ActiveRouteSummary(
                sessionId: "nav-debug-seed",
                destinationLabel: "Millennium Park",
                destinationAddress: "201 E Randolph St, Chicago, IL",
                destinationPoint: NavPoint(41.8826, -87.6226),
                mode: "driving",
                phase: "guidance",
                instruction: "Turn right onto N Michigan Ave",
                maneuver: "turn_right",
                maneuverDistanceM: 180,
                remainingM: 1250,
                etaMinutes: 4,
                arrived: false,
                routeGeometry: geometry
            )
        }
    }
    #endif

    // MARK: - Selectable `NAVIGATION` Firmware List

    func pageRows() -> [String] {
        var rows: [String] = []
        var actions: [MenuRowAction] = []

        if let s = session {
            let dest = s.displayDestination
            rows.append("➤ Show active guidance (\(dest))")
            actions.append(.showActiveGuidance)
            rows.append("⏹ Stop navigation (\(dest))")
            actions.append(.stopNavigation)
        }

        rows.append("➤ Navigate to a place…")
        actions.append(.askDestination)

        let remainingSlots = max(0, 20 - rows.count)
        for place in places.prefix(remainingSlots) {
            let suffix = place.address.flatMap { $0.isEmpty ? nil : " · \($0)" } ?? ""
            rows.append("➤ \(place.name)\(suffix)")
            actions.append(.startSavedPlace(place))
        }

        menuActions = actions
        return rows
    }

    func selectPageRow(_ index: Int) {
        if menuActions.isEmpty {
            _ = pageRows()
        }
        guard menuActions.indices.contains(index) else {
            onMenuChanged?()
            return
        }
        switch menuActions[index] {
        case .showActiveGuidance:
            resumeGuidanceOnGlasses()

        case .stopNavigation:
            if let s = session {
                session = nil
                s.end()
            }
            activeSummary = nil
            onMenuChanged?()

        case .askDestination:
            if let prompts {
                let ownerId = "nav-" + ConfirmGate.hex(8)
                prompts.ask("Where to?", ownerKind: "capability", ownerId: ownerId, field: "destination") { [weak self] answer in
                    guard let self, let answer else { return }
                    let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !trimmed.isEmpty else { return }
                    let parsed = Self.parsedAnswer(trimmed)
                    if let why = self.start(parsed.destination, initialMode: parsed.mode) {
                        self.receipt?(why)
                    }
                }
            } else if let ask = askFollowUp {
                ask("Where to?") { [weak self] answer in
                    guard let self else { return }
                    let parsed = Self.parsedAnswer(answer)
                    if let why = self.start(parsed.destination, initialMode: parsed.mode) {
                        self.receipt?(why)
                    }
                }
            } else {
                receipt?("Navigation cannot be started from this phone.")
            }

        case .startSavedPlace(let place):
            if let pt = place.point {
                let candidate = NavCandidate(
                    choiceId: "place:\(place.id)",
                    label: place.name,
                    address: place.address ?? "Saved place",
                    point: pt
                )
                if let why = start(place.name, initialCandidate: candidate, initialMode: nil, autoInstall: false) {
                    receipt?(why)
                }
            } else {
                if let why = start(place.name) {
                    receipt?(why)
                }
            }
        }
    }

    // MARK: - Voice Grammar & Session Steering

    func handle(_ request: String) -> CommandReply? {
        if Self.isWhereAmI(request) {
            onAsked?(request)
            describeLocation { [weak self] answer in self?.onAnswer?(answer) }
            return CommandReply(text: "", activated: false)
        }
        guard let parsed = Self.parsedQuery(request) else { return nil }
        if let why = start(parsed.destination, initialMode: parsed.mode) {
            return CommandReply(text: why, activated: false)
        }
        return CommandReply(text: "", activated: true)
    }

    @discardableResult
    func hear(_ text: String, addressed: String?) -> Bool {
        guard let s = session else { return false }
        if Self.isCancel(text) || addressed.map(Self.isCancel) == true {
            Log.add("navigation cancelled by voice")
            session = nil
            s.end()
            activeSummary = nil
            onMenuChanged?()
            return true
        }
        if let a = addressed {
            if a.isEmpty {
                Log.add("navigation active — forwarding standalone wake call to assistant")
                return false
            }
            if s.consume(a) { return true }
            Log.add("navigation active — forwarding addressed query to assistant (\(a.count) chars)")
            return false
        }
        if s.awaitingDestinationNow {
            _ = s.consume(text)
            return true
        }
        Log.add("navigation active — unaddressed speech ignored (\(text.count) chars)")
        return true
    }

    static let sessionCancel: Set<String> = [
        "stop", "cancel", "stop it", "cancel that", "cancel it",
        "stop the session", "end the session", "never mind", "nevermind",
        "thats enough", "that is enough", "enough",
        "were done", "we are done", "done",
        "forget it", "abort", "quit", "end it", "stop this",
        "cancel this",
    ]

    static func isCancel(_ text: String) -> Bool {
        var t = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = t.last, ".!?".contains(last) { t.removeLast() }
        for wake in ["joat,", "joat"] where t.hasPrefix(wake + " ") {
            t = String(t.dropFirst(wake.count)).trimmingCharacters(in: .whitespaces)
            break
        }
        t = t.replacingOccurrences(of: "’", with: "").replacingOccurrences(of: "'", with: "")
        return sessionCancel.contains(t)
    }

    private static let walkDoor = try! NSRegularExpression(
        pattern: #"^(?:walk(?:\s+me)?|(?:(?:give|get|show)(?:\s+me)?\s+)?walking\s+(?:directions|route))\s+to\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let walkHomeDoor = try! NSRegularExpression(
        pattern: #"^walk(?:\s+me)?\s+(home)$"#,
        options: [.caseInsensitive])

    private static let cycleDoor = try! NSRegularExpression(
        pattern: #"^(?:(?:bike|cycle|bicycle)(?:\s+me)?|(?:(?:give|get|show)(?:\s+me)?\s+)?(?:biking|cycling|bicycle)\s+(?:directions|route))\s+to\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let cycleHomeDoor = try! NSRegularExpression(
        pattern: #"^(?:bike|cycle|bicycle)(?:\s+me)?\s+(home)$"#,
        options: [.caseInsensitive])

    private static let driveDoor = try! NSRegularExpression(
        pattern: #"^(?:drive(?:\s+me)?|(?:(?:give|get|show)(?:\s+me)?\s+)?driving\s+(?:directions|route))\s+to\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let driveHomeDoor = try! NSRegularExpression(
        pattern: #"^drive(?:\s+me)?\s+(home)$"#,
        options: [.caseInsensitive])

    private static let door = try! NSRegularExpression(
        pattern: #"^(?:(?:give|get|show)(?:\s+me)?\s+)?(?:navigate(?:\s+me)?|directions|route|take me|guide me|get me|help me get|show me (?:the )?way)(?:\s+to)\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let doorQuestion = try! NSRegularExpression(
        pattern: #"^(?:how do i get|what(?:['’]s| is) the (?:best )?way)(?:\s+to)\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let doorHome = try! NSRegularExpression(
        pattern: #"^(?:navigate(?:\s+me)?|take me|guide me|get me)\s+(home)$"#,
        options: [.caseInsensitive])

    private static let looseDoor = try! NSRegularExpression(
        pattern: #"\b(?:navigate(?:\s+me)?|directions|take me|guide me|get me|help me get|show me (?:the )?way|how do i get|walk(?:\s+me)?|bike(?:\s+me)?|cycle(?:\s+me)?|drive(?:\s+me)?|(?:walking|biking|cycling|driving)\s+directions)\s+to\s+(.+)$"#,
        options: [.caseInsensitive])

    /// Mode-aware spoken destination parser:
    /// - `"walk to Starbucks"` -> `("Starbucks", "walking")`
    /// - `"bike to the park"` -> `("the park", "cycling")`
    /// - `"drive home"` -> `("home", "driving")`
    /// - `"navigate to Navy Pier"` -> `("Navy Pier", nil)`
    static func parsedQuery(_ text: String) -> (destination: String, mode: String?)? {
        var request = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = request.last, ".!?".contains(last) { request.removeLast() }
        request = request.replacingOccurrences(of: #"^(?:hey\s+)?joat(?:\s*[,:-])?\s+"#, with: "",
                                               options: [.regularExpression, .caseInsensitive])
        request = request.replacingOccurrences(of: #"^(?:(?:can|could|would)\s+you\s+|please\s+)"#, with: "",
                                               options: [.regularExpression, .caseInsensitive])
        let rules: [(NSRegularExpression, String?)] = [
            (walkDoor, "walking"),
            (walkHomeDoor, "walking"),
            (cycleDoor, "cycling"),
            (cycleHomeDoor, "cycling"),
            (driveDoor, "driving"),
            (driveHomeDoor, "driving"),
            (door, nil),
            (doorQuestion, nil),
            (doorHome, nil),
        ]
        let range = NSRange(request.startIndex..., in: request)
        for (re, mode) in rules {
            if let m = re.firstMatch(in: request, range: range), let r = Range(m.range(at: 1), in: request) {
                let destination = request[r].trimmingCharacters(in: .whitespaces)
                if !destination.isEmpty {
                    return (destination, mode)
                }
            }
        }
        return nil
    }

    static func query(_ text: String) -> String? {
        parsedQuery(text)?.destination
    }

    static func parsedAnswer(_ text: String) -> (destination: String, mode: String?) {
        if let parsed = parsedQuery(text) {
            return parsed
        }
        let range = NSRange(text.startIndex..., in: text)
        if let m = looseDoor.firstMatch(in: text, range: range), let r = Range(m.range(at: 1), in: text) {
            let d = text[r].trimmingCharacters(in: CharacterSet(charactersIn: " .!?"))
            if !d.isEmpty {
                let lower = text.lowercased()
                let inferredMode: String?
                if lower.range(of: #"\bwalk(?:ing)?\b"#, options: .regularExpression) != nil {
                    inferredMode = "walking"
                } else if lower.range(of: #"\b(?:bike|biking|cycle|cycling|bicycle)\b"#, options: .regularExpression) != nil {
                    inferredMode = "cycling"
                } else if lower.range(of: #"\bdriv(?:e|ing)\b"#, options: .regularExpression) != nil {
                    inferredMode = "driving"
                } else {
                    inferredMode = nil
                }
                return (d, inferredMode)
            }
        }
        return (text.trimmingCharacters(in: .whitespacesAndNewlines), nil)
    }

    static func destination(fromAnswer text: String) -> String {
        parsedAnswer(text).destination
    }

    // MARK: - Where am I

    private static let whereAmIPattern = try! NSRegularExpression(
        pattern: #"^(?:(?:can|could|would) you\s+)?(?:please\s+)?(?:tell me\s+|show me\s+|give me\s+)?(?:where am i|where are we|what(?:['’]s| is) my (?:current |exact )?(?:location|address)|my (?:current |exact )?location|what (?:street|road) am i on|what city am i in)(?:\s+right now)?(?:\s+please)?$"#,
        options: [.caseInsensitive])

    static func isWhereAmI(_ text: String) -> Bool {
        var t = text.collapsed
        while let last = t.last, ".,!?".contains(last) { t.removeLast() }
        return Grammar.match(whereAmIPattern, t) != nil
    }

    func describeLocation(_ done: @escaping (String) -> Void) {
        refreshFix { [weak self] fresh in
            guard let self else { return }
            guard let point = fresh ?? self.recentFix(maxAge: Self.biasMaxAge) else {
                done("I don't have your location yet. Allow Location in JOAT Brain or Even Hub so I can see where you are.")
                return
            }
            let fixMeta = self.fix
            let minutesOld = (fresh == nil && fixMeta != nil) ? Int(Date().timeIntervalSince(fixMeta!.at) / 60) : 0
            let rough = (fixMeta?.accuracy ?? 0) > 50
            Task { @MainActor in
                var answer: String
                if let place = await NavigationMaps.address(at: point) {
                    answer = "You're \(rough ? "near" : "at") \(place)."
                } else {
                    answer = String(format: "You're at %.4f, %.4f. I couldn't look up the address.",
                                    point.latitude, point.longitude)
                }
                if minutesOld >= 2 { answer += " That was \(minutesOld) minutes ago." }
                Log.add("where am I answered (fresh=\(fresh != nil), accuracy=\(Int(fixMeta?.accuracy ?? -1)) m)")
                done(answer)
            }
        }
    }
}

/// One confirm-place, choose-mode, route, map-prefetch, GPS-stream, and voice-cue
/// lifecycle; one HUD owner.
final class NavigationSession {
    let sessionId: String
    private let link: PluginLink
    private weak var owner: Navigation?
    var onEnd: (() -> Void)?

    private let initialQuery: String
    private let initialCandidate: NavCandidate?
    private var initialMode: String?
    private var autoInstall: Bool
    private var lastQuery: String
    private var proximity: NavPoint?
    private var searchProximity: NavPoint?
    private var nearbySearch = false
    private var generation = 0
    private var searchGeneration = 0
    private var originGeneration = 0
    private var routeToken = 0

    struct View {
        let requestId: String
        let generation: Int
        let view: String
        let choiceIds: [String]
    }
    private(set) var currentView: View?
    private var originRequest: (requestId: String, generation: Int)?
    private var locatingSince: Date?
    private var candidates: [String: NavCandidate] = [:]
    private var destination: NavCandidate?
    private var mode: String?
    private var origin: NavPoint?
    private var route: NavRoute?
    private var routeRevision = 0
    private var routeInstalled = false
    private var usingPhoneFixes = false
    private var phoneStreamActive = false
    private var lastStateSequence = 0
    private var installAttempt: (routeId: String, revision: Int)?
    private var routeSentAt = Date()
    private var pendingRerouteRequestId: String?
    private var rerouteAfterAck: (origin: NavPoint, generation: Int, revision: Int, requestId: String)?
    private(set) var arrived = false
    private(set) var ended = false
    private var awaitingDestination = false
    private var awaitingUntil = Date.distantPast
    private var errorRecovery: String?

    private var searchTask: Task<Void, Never>?
    private var originTask: Task<Void, Never>?
    private var routeTask: Task<Void, Never>?
    private var mapTask: Task<Void, Never>?
    private var mapCentres: [(point: NavPoint, mode: String)] = []
    private var mapQueue: [(point: NavPoint, mode: String)] = []

    private var lastSpokenStepId: String?
    private var spokenApproachStepIds: Set<String> = []
    private var lastSpokenAlert: String?

    static let minLocatingVisible: TimeInterval = 0.8
    static let answerWindow: TimeInterval = 15

    init(
        query: String,
        link: PluginLink,
        sessionId: String,
        proximity: NavPoint?,
        owner: Navigation,
        initialCandidate: NavCandidate? = nil,
        initialMode: String? = nil,
        autoInstall: Bool = false
    ) {
        self.initialQuery = query
        self.lastQuery = query
        self.link = link
        self.sessionId = sessionId
        self.proximity = proximity
        self.owner = owner
        self.initialCandidate = initialCandidate
        self.initialMode = initialMode
        self.autoInstall = autoInstall
    }

    var awaitingDestinationNow: Bool { awaitingDestination && Date() < awaitingUntil }

    var displayDestination: String {
        let raw = destination?.label ?? initialCandidate?.label ?? lastQuery
        return raw.collapsed.utf16Prefix(32)
    }

    func start() {
        if let candidate = initialCandidate {
            destination = candidate
            if let initialMode {
                mode = initialMode
                publishSummary(phase: "routing", instruction: "Getting your location…")
                requestOrigin()
            } else {
                publishSummary(phase: "confirming", instruction: "Choose Walk, Cycle, or Drive on glasses")
                showMode()
            }
            return
        }
        startSearch(initialQuery)
    }

    func injectRouteForTesting(_ testRoute: NavRoute, origin testOrigin: NavPoint, installNow: Bool) {
        originTask?.cancel()
        routeTask?.cancel()
        origin = testOrigin
        showRouting()
        routeToken += 1
        routeReady(token: routeToken, generation: generation, result: .success(testRoute), revision: 1, installNow: installNow)
    }

    func end() {
        guard !ended else { return }
        ended = true
        awaitingDestination = false
        errorRecovery = nil
        locatingSince = nil
        rerouteAfterAck = nil
        searchTask?.cancel()
        originTask?.cancel()
        routeTask?.cancel()
        cancelMapPrefetch()
        stopPhoneFixesIfActive()
        routeToken += 1
        link.send(["kind": "navigation.stop", "sessionId": sessionId])
        Log.add("tx navigation.stop \(sessionId)")
        onEnd?()
    }

    private func publishSummary(
        phase: String,
        instruction: String,
        maneuver: String? = nil,
        maneuverDistanceM: Double? = nil,
        remainingM: Double? = nil,
        etaMinutes: Double? = nil,
        arrivedOverride: Bool? = nil
    ) {
        let summary = Navigation.ActiveRouteSummary(
            sessionId: sessionId,
            destinationLabel: destination?.label ?? initialCandidate?.label ?? lastQuery,
            destinationAddress: destination?.address ?? initialCandidate?.address ?? "",
            destinationPoint: destination?.point ?? initialCandidate?.point,
            mode: mode ?? initialMode ?? "walking",
            phase: phase,
            instruction: instruction,
            maneuver: maneuver,
            maneuverDistanceM: maneuverDistanceM,
            remainingM: remainingM,
            etaMinutes: etaMinutes,
            arrived: arrivedOverride ?? arrived,
            routeGeometry: route?.geometry ?? []
        )
        owner?.updateSummary(from: self, summary)
    }

    // MARK: - Uplinks

    func accept(kind: String, message m: [String: Any]) {
        guard !ended, m.string("sessionId") == sessionId else { return }
        switch kind {
        case "navigation.action":
            guard let requestId = m.string("requestId"), let generation = m.int("generation"),
                  let action = m.string("action") else { return }
            handleAction(requestId: requestId, generation: generation, action: action, choiceId: m.string("choiceId"))
        case "navigation.view.ack":
            guard let requestId = m.string("requestId"), let generation = m.int("generation"),
                  let outcome = m.string("outcome") else { return }
            handleViewAck(requestId: requestId, generation: generation, outcome: outcome)
        case "navigation.origin":
            guard let requestId = m.string("requestId"), let generation = m.int("generation"),
                  let lat = (m["latitude"] as? NSNumber)?.doubleValue,
                  let lon = (m["longitude"] as? NSNumber)?.doubleValue else { return }
            handleOrigin(requestId: requestId, generation: generation, point: NavPoint(lat, lon))
        case "navigation.origin.failed":
            guard let requestId = m.string("requestId"), let generation = m.int("generation"),
                  m.string("reason") == "unavailable" else { return }
            handleOriginFailure(requestId: requestId, generation: generation)
        case "navigation.route.ack":
            guard let routeId = m.string("routeId"), let revision = m.int("revision"),
                  let outcome = m.string("outcome") else { return }
            handleRouteAck(routeId: routeId, revision: revision, outcome: outcome)
        case "navigation.state":
            guard let routeId = m.string("routeId"), let revision = m.int("revision"),
                  let sequence = m.int("sequence"), let state = m.string("state") else { return }
            handleState(routeId: routeId, revision: revision, sequence: sequence, state: state, message: m)
        case "navigation.reroute.request":
            guard let routeId = m.string("routeId"), let base = m.int("baseRevision"),
                  let requestId = m.string("requestId"),
                  let lat = (m["latitude"] as? NSNumber)?.doubleValue,
                  let lon = (m["longitude"] as? NSNumber)?.doubleValue else { return }
            handleReroute(routeId: routeId, baseRevision: base, requestId: requestId, point: NavPoint(lat, lon))
        default:
            break
        }
    }

    // MARK: - Voice Steering

    static let confirmWords: Set<String> = ["confirm", "yes", "correct", "continue", "that one"]
    static let retryWords: Set<String> = ["retry", "try again", "retry navigation"]
    static let repairWords: Set<String> = [
        "retry", "try again", "change destination", "change the destination",
        "different destination", "not this one", "no",
    ]
    static let firstWords: Set<String> = ["first", "the first", "option one", "option 1"]
    static let secondWords: Set<String> = ["second", "the second", "option two", "option 2"]
    static let walkWords: Set<String> = ["walk", "walking", "walking mode"]
    static let cycleWords: Set<String> = ["cycle", "cycling", "bike", "bicycle", "cycling mode"]
    static let driveWords: Set<String> = ["drive", "driving", "car", "by car", "driving mode"]
    static let startWords: Set<String> = ["start", "start navigation", "go", "begin", "begin navigation"]
    static let changeModeWords: Set<String> = ["change mode", "change the mode"]
    static let finishWords: Set<String> = ["finish", "done", "end navigation"]
    static let cancelWords: Set<String> = [
        "stop", "cancel", "stop navigation", "cancel navigation", "end directions", "stop directions",
    ]

    static func normalize(_ text: String) -> String {
        var t = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = t.last, ".!?".contains(last) { t.removeLast() }
        return t.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    func consume(_ text: String) -> Bool {
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, !ended else { return false }
        if let replacement = Navigation.parsedQuery(query) {
            awaitingDestination = false
            initialMode = replacement.mode
            autoInstall = false
            replaceDestination(replacement.destination)
            return true
        }
        let spoken = Self.normalize(query)
        if Self.cancelWords.contains(spoken) {
            end()
            return true
        }
        if let v = currentView, ["destination", "repair", "error"].contains(v.view),
           let correction = Self.correction(query) {
            awaitingDestination = false
            autoInstall = false
            replaceDestination(correction)
            return true
        }
        if awaitingDestination {
            awaitingDestination = false
            let parsed = Navigation.parsedAnswer(query)
            if let m = parsed.mode { initialMode = m }
            autoInstall = false
            replaceDestination(parsed.destination)
            return true
        }
        guard let v = currentView, let (action, choice) = voiceAction(v, spoken) else { return false }
        handleAction(requestId: v.requestId, generation: v.generation, action: action, choiceId: choice)
        return true
    }

    private func voiceAction(_ v: View, _ spoken: String) -> (String, String?)? {
        switch v.view {
        case "destination":
            if Self.confirmWords.contains(spoken) { return ("confirm_destination", nil) }
            if Self.repairWords.contains(spoken) { return ("retry_destination", nil) }
            return nil
        case "repair":
            if Self.repairWords.contains(spoken) { return ("select_destination", "retry") }
            if Self.firstWords.contains(spoken), v.choiceIds.count >= 1 { return ("select_destination", v.choiceIds[0]) }
            if Self.secondWords.contains(spoken), v.choiceIds.count >= 2 { return ("select_destination", v.choiceIds[1]) }
            for id in v.choiceIds {
                if let c = candidates[id], [Self.normalize(c.label), Self.normalize(c.address)].contains(spoken) {
                    return ("select_destination", id)
                }
            }
            return nil
        case "mode":
            if Self.walkWords.contains(spoken) { return ("select_mode", "walking") }
            if Self.cycleWords.contains(spoken) { return ("select_mode", "cycling") }
            if Self.driveWords.contains(spoken) { return ("select_mode", "driving") }
            return nil
        case "preview":
            if Self.startWords.contains(spoken) { return ("start", nil) }
            if Self.changeModeWords.contains(spoken) { return ("change_mode", nil) }
            if Self.repairWords.contains(spoken) { return ("change_destination", nil) }
            return nil
        default:
            if v.view == "error", Self.retryWords.contains(spoken) { return ("retry", nil) }
            if arrived, Self.finishWords.contains(spoken) { return ("finish", nil) }
            return nil
        }
    }

    static func correction(_ text: String) -> String? {
        var request = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = request.last, ".!?".contains(last) { request.removeLast() }
        request = request.collapsed
        let re = try! NSRegularExpression(pattern: #"^(?:no\s*[,;:-]?\s*)?i\s+(?:mean|meant)\s+(.+)$"#,
                                          options: [.caseInsensitive])
        let range = NSRange(request.startIndex..., in: request)
        guard let m = re.firstMatch(in: request, range: range), m.range.length == range.length,
              let r = Range(m.range(at: 1), in: request) else { return nil }
        var destination = String(request[r]).replacingOccurrences(of: #",\s*(?:the\s+)?"#, with: " ",
                                                                  options: .regularExpression)
        destination = destination.collapsed.trimmingCharacters(in: CharacterSet(charactersIn: " ,"))
        return destination.isEmpty ? nil : destination
    }

    static func destinationSearch(_ query: String) -> (text: String, nearby: Bool) {
        var text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = text.last, ".!?".contains(last) { text.removeLast() }
        text = text.collapsed
        var nearby = false
        let prefix = #"^(?:the\s+)?(?:nearest|closest)\s+"#
        let suffix = #"\s*,?\s*(?:near(?:\s+to)?\s+(?:me|here|us)|around\s+(?:me|here|us)|close\s+by|nearby|to\s+(?:me|here|us))$"#
        if let r = text.range(of: prefix, options: [.regularExpression, .caseInsensitive]) {
            text.removeSubrange(r)
            nearby = true
        }
        if let r = text.range(of: suffix, options: [.regularExpression, .caseInsensitive]) {
            text.removeSubrange(r)
            nearby = true
        }
        text = text.trimmingCharacters(in: CharacterSet(charactersIn: " ,"))
        return (text.isEmpty ? query.trimmingCharacters(in: .whitespacesAndNewlines) : text, nearby)
    }

    // MARK: - Destination Search

    private func replaceDestination(_ query: String) {
        if routeInstalled || installAttempt != nil {
            link.send(["kind": "navigation.stop", "sessionId": sessionId])
        }
        stopPhoneFixesIfActive()
        cancelMapPrefetch()
        startSearch(query)
    }

    private func startSearch(_ query: String) {
        let request = Self.destinationSearch(query)
        lastQuery = query
        awaitingDestination = false
        searchTask?.cancel()
        originTask?.cancel()
        routeTask?.cancel()
        cancelMapPrefetch()
        stopPhoneFixesIfActive()
        routeToken += 1
        destination = nil
        mode = nil
        origin = nil
        route = nil
        originRequest = nil
        locatingSince = nil
        currentView = nil
        installAttempt = nil
        pendingRerouteRequestId = nil
        rerouteAfterAck = nil
        routeInstalled = false
        usingPhoneFixes = false
        arrived = false
        searchGeneration += 1
        let gen = searchGeneration
        publishSummary(phase: "searching", instruction: "Searching for \(request.text)…")
        Log.add("navigation destination search chars=\(query.count) generation=\(gen) nearby=\(request.nearby)")
        searchTask = Task { @MainActor [weak self] in
            guard let self else { return }
            var near = self.proximity
            if request.nearby {
                let refreshed: NavPoint? = await withCheckedContinuation { done in
                    guard let owner = self.owner else { return done.resume(returning: nil) }
                    owner.refreshFix { done.resume(returning: $0) }
                }
                near = refreshed ?? self.owner?.recentFix(maxAge: Navigation.nearbyMaxAge)
            }
            guard !Task.isCancelled, gen == self.searchGeneration, !self.ended else { return }
            if request.nearby, near == nil {
                self.searchReady(gen, nil, nil, nearby: true, locationFailed: true)
                return
            }
            var found: [NavCandidate]
            let saved = self.owner?.savedPlace(for: request.text)
            if let saved, let lat = saved.latitude, let lon = saved.longitude {
                found = [NavCandidate(choiceId: "place:\(saved.id)", label: saved.name,
                                      address: saved.address ?? "Saved place", point: NavPoint(lat, lon))]
            } else {
                found = await NavigationMaps.search(saved?.address ?? request.text, near: near,
                                                   nearest: request.nearby)
                if let saved, let first = found.first {
                    found = [NavCandidate(choiceId: first.choiceId, label: saved.name, address: first.address,
                                          point: first.point, repairHint: first.repairHint,
                                          distanceM: first.distanceM)]
                }
            }
            guard !Task.isCancelled, gen == self.searchGeneration, !self.ended else { return }
            self.searchReady(gen, found, near, nearby: request.nearby, locationFailed: false)
        }
    }

    private func searchReady(_ gen: Int, _ found: [NavCandidate]?, _ near: NavPoint?, nearby: Bool,
                             locationFailed: Bool) {
        guard gen == searchGeneration else { return }
        if let near { proximity = near }
        searchProximity = near
        nearbySearch = nearby
        if locationFailed {
            errorRecovery = "search_location"
            publishSummary(phase: "error", instruction: "Phone location unavailable")
            sendView(Self.view("error", "Phone location unavailable",
                               "I need your phone location to find the nearest match",
                               hint: "tap/say retry · swipe cancel"), "search-location-error")
            return
        }
        guard let found, !found.isEmpty else {
            errorRecovery = "destination"
            publishSummary(phase: "error", instruction: "Destination not found")
            sendView(Self.view("error", "Destination not found", "Try the address again",
                               hint: "tap/say retry · swipe cancel"), "search-error")
            return
        }
        let bounded = Array(found.prefix(2))
        candidates = Dictionary(bounded.map { ($0.choiceId, $0) }, uniquingKeysWith: { a, _ in a })
        if found.count == 1 {
            destination = found[0]
            showDestination(found[0])
            return
        }
        publishSummary(phase: "confirming", instruction: "Choose destination on glasses")
        let hint = bounded.compactMap(\.repairHint).first
        let options = bounded.map { option($0, near: nearby ? near : nil) } + ["None of these · retry"]
        sendView(Self.view("repair", "Which destination?",
                           nearby ? "Closest matches near you"
                               : (hint.map { "Check \($0)" } ?? "Choose the canonical place"),
                           hint: "tap choose · swipe select · say first/second",
                           options: options, choiceIds: bounded.map(\.choiceId) + ["retry"]), "repair")
    }

    private func distance(to c: NavCandidate, from near: NavPoint) -> Double {
        if let d = c.distanceM, d.isFinite, d >= 0 { return d }
        return NavigationMaps.distance(near, c.point)
    }

    private func option(_ c: NavCandidate, near: NavPoint?) -> String {
        let away = near.map { " · about \(NavigationMaps.formatDistance(distance(to: c, from: $0)))" } ?? ""
        let value = "\(c.label)\(away) · \(c.address)"
        if value.utf16.count <= 64 { return value }
        return value.utf16Prefix(61).trimmingCharacters(in: .whitespaces) + "..."
    }

    private func showDestination(_ c: NavCandidate) {
        publishSummary(phase: "confirming", instruction: "Confirm destination on glasses")
        var secondary = c.address
        if nearbySearch, let near = searchProximity {
            secondary = "About \(NavigationMaps.formatDistance(distance(to: c, from: near))) away · \(c.address)"
        }
        sendView(Self.view("destination", c.label, secondary, hint: "tap/say confirm · swipe retry"), "destination")
    }

    private func showMode() {
        guard let destination else { return }
        publishSummary(phase: "confirming", instruction: "Choose Walk, Cycle, or Drive on glasses")
        sendView(Self.view("mode", "Choose travel mode", destination.label,
                           hint: "tap choose · swipe select · say walk/cycle/drive",
                           options: ["Walk", "Cycle", "Drive"],
                           choiceIds: ["walking", "cycling", "driving"]), "mode")
    }

    private func showRetryPrompt() {
        awaitingDestination = true
        awaitingUntil = Date().addingTimeInterval(Self.answerWindow)
        publishSummary(phase: "confirming", instruction: "Say the destination again")
        sendView(Self.view("repair", "Say the destination again",
                           "Keep confirmed parts; correct only what was wrong",
                           hint: "say the destination again",
                           options: ["Waiting for destination"], choiceIds: ["retry"]), "retry")
    }

    // MARK: - Actions

    private func handleAction(requestId: String, generation: Int, action: String, choiceId: String?) {
        guard let v = currentView, requestId == v.requestId, generation == v.generation else { return }
        if action == "cancel" {
            end()
            return
        }
        switch (v.view, action) {
        case ("destination", "confirm_destination"):
            if let initialMode {
                mode = initialMode
                requestOrigin()
            } else {
                showMode()
            }
        case ("destination", "retry_destination"):
            showRetryPrompt()
        case ("repair", "select_destination"):
            if choiceId == "retry" {
                showRetryPrompt()
            } else if let c = candidates[choiceId ?? ""] {
                destination = c
                showDestination(c)
            }
        case ("mode", "select_mode"):
            guard let choiceId, ["walking", "cycling", "driving"].contains(choiceId) else { return }
            mode = choiceId
            initialMode = choiceId
            requestOrigin()
        case ("preview", "start"):
            sendRoute()
        case ("preview", "change_mode"):
            initialMode = nil
            showMode()
        case ("preview", "change_destination"):
            autoInstall = false
            showRetryPrompt()
        case (_, "finish") where arrived:
            end()
        case ("error", "retry"):
            switch errorRecovery {
            case "search_location":
                startSearch(lastQuery)
            case "origin":
                requestOrigin()
            case "route" where origin != nil:
                showRouting()
                startRoute(from: origin!, generation: self.generation, revision: routeRevision + 1,
                           installNow: autoInstall)
            case "install":
                sendRoute()
            default:
                showRetryPrompt()
            }
        default:
            break
        }
    }

    // MARK: - Origin (PhoneLocation First, Even Hub Fallback)

    private func requestOrigin() {
        guard destination != nil, let mode else { return }
        originTask?.cancel()
        originGeneration += 1
        let originGen = originGeneration
        errorRecovery = nil
        publishSummary(phase: "routing", instruction: "Getting your location…")

        originTask = Task { @MainActor [weak self] in
            guard let self, !self.ended, originGen == self.originGeneration else { return }
            if PhoneLocation.shared.isAuthorized {
                if PhoneLocation.shared.latest == nil {
                    self.sendView(
                        Self.view("uncertain", "Getting your location", "Reading iPhone GPS",
                                  hint: "Finding phone location · say stop", interaction: "none", mode: mode),
                        "locating"
                    )
                }
                if let phonePoint = await PhoneLocation.shared.requestOneShot(timeout: 2.5),
                   !Task.isCancelled, !self.ended, originGen == self.originGeneration, phonePoint.isValid {
                    if let latest = PhoneLocation.shared.latest {
                        self.owner?.notePhoneFix(latest)
                    }
                    self.usingPhoneFixes = true
                    self.originRequest = nil
                    self.locatingSince = nil
                    self.origin = phonePoint
                    Log.add("navigation origin received from PhoneLocation")
                    self.showRouting()
                    self.startRoute(
                        from: phonePoint,
                        generation: self.generation,
                        revision: self.routeRevision + 1,
                        installNow: self.autoInstall
                    )
                    return
                }
            }
            guard !Task.isCancelled, !self.ended, originGen == self.originGeneration else { return }
            self.requestOriginFromPlugin(mode: mode)
        }
    }

    private func requestOriginFromPlugin(mode: String) {
        usingPhoneFixes = false
        sendView(Self.view("uncertain", "Getting your location", "Keep phone foreground and unlocked",
                           hint: "Finding phone location · say stop", interaction: "none", mode: mode), "locating")
        locatingSince = Date()
        generation += 1
        let request = (requestId: "\(sessionId):origin:\(generation)", generation: generation)
        originRequest = request
        errorRecovery = nil
        link.send(["kind": "navigation.origin.request", "sessionId": sessionId,
                   "requestId": request.requestId, "generation": request.generation])
        Log.add("tx navigation.origin.request gen=\(generation)")
    }

    private func handleOrigin(requestId: String, generation: Int, point: NavPoint) {
        guard let request = originRequest, request.requestId == requestId, request.generation == generation,
              destination != nil, let mode, point.isValid else { return }
        originRequest = nil
        locatingSince = nil
        usingPhoneFixes = false
        Log.add("navigation origin received from plugin")
        origin = point
        showRouting()
        startRoute(from: point, generation: self.generation, revision: routeRevision + 1, installNow: autoInstall)
    }

    private func showRouting() {
        guard let mode else { return }
        publishSummary(phase: "routing", instruction: "Finding a \(mode) route…")
        sendView(Self.view("uncertain", "Finding a \(mode) route", "Checking the route to your destination",
                           hint: "Route search in progress · say stop", interaction: "none", mode: mode), "routing")
    }

    private func handleOriginFailure(requestId: String, generation: Int) {
        guard let request = originRequest, request.requestId == requestId, request.generation == generation else { return }
        let shown = locatingSince.map { Date().timeIntervalSince($0) } ?? Self.minLocatingVisible
        locatingSince = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, Self.minLocatingVisible - shown)) { [weak self] in
            guard let self, !self.ended, self.originRequest?.requestId == requestId else { return }
            Log.add("navigation origin unavailable")
            self.originRequest = nil
            self.errorRecovery = "origin"
            self.publishSummary(phase: "error", instruction: "Phone location unavailable")
            self.sendView(Self.view("error", "Phone location unavailable", "Allow Location in JOAT Brain or Even Hub",
                                    hint: "tap/say retry · swipe cancel"), "origin-error")
        }
    }

    // MARK: - Views

    private func handleViewAck(requestId: String, generation: Int, outcome: String) {
        guard let v = currentView, v.requestId == requestId, v.generation == generation else { return }
        if outcome == "rejected" {
            rerouteAfterAck = nil
            Log.add("navigation HUD view rejected generation=\(generation)")
            end()
            return
        }
        guard let pending = rerouteAfterAck, pending.requestId == requestId else { return }
        rerouteAfterAck = nil
        startRoute(from: pending.origin, generation: pending.generation, revision: pending.revision, installNow: true)
    }

    private func sendView(_ navigation: [String: Any], _ phase: String) {
        let view = navigation["view"] as? String ?? ""
        if view != "error" { errorRecovery = nil }
        generation += 1
        let requestId = "\(sessionId):\(phase):\(generation)"
        currentView = View(requestId: requestId, generation: generation, view: view,
                           choiceIds: navigation["choiceIds"] as? [String] ?? [])
        link.send(["kind": "navigation.view", "sessionId": sessionId, "requestId": requestId,
                   "generation": generation, "navigation": navigation])
        Log.add("tx navigation.view \(view) \(phase) gen=\(generation)")
    }

    static func view(_ view: String, _ primary: String, _ secondary: String, hint: String,
                     interaction: String = "stationary", options: [String] = [], choiceIds: [String]? = nil,
                     mode: String? = nil, maneuver: String? = nil, remaining: Double? = nil,
                     eta: Double? = nil, progress: Double? = nil) -> [String: Any] {
        func text(_ s: String, _ n: Int) -> String {
            let t = s.collapsed.utf16Prefix(n)
            return t.isEmpty ? "—" : t
        }
        let shown = Array(options.prefix(3)).map { text($0, 64) }
        var out: [String: Any] = [
            "view": view, "primary": text(primary, 96), "secondary": text(secondary, 128),
            "hint": text(hint, 80), "interaction": interaction, "options": shown, "selectedIndex": 0,
        ]
        if let choiceIds { out["choiceIds"] = Array(choiceIds.prefix(shown.count)).map { String($0.prefix(64)) } }
        if let mode { out["mode"] = mode }
        if let maneuver { out["maneuver"] = maneuver }
        if let remaining, remaining.isFinite { out["remainingDistanceM"] = min(max(0, remaining), 10_000_000) }
        if let eta, eta.isFinite { out["etaMinutes"] = min(max(0, eta), 1_000_000) }
        if let progress, progress.isFinite { out["progress"] = min(max(0, progress), 1) }
        return out
    }

    static func minutes(_ eta: Double) -> Double { max(1, eta.rounded()) }

    // MARK: - Route Calculation, Map Prefetch & Phone GPS Streaming

    private func startRoute(from origin: NavPoint, generation: Int, revision: Int, installNow: Bool) {
        guard let destination, let mode else { return }
        routeTask?.cancel()
        routeToken += 1
        let token = routeToken
        routeTask = Task { @MainActor [weak self] in
            let result: Result<NavRoute, NavFailure>
            do {
                result = .success(try await NavigationMaps.route(from: origin, to: destination, mode: mode))
            } catch let failure as NavFailure {
                result = .failure(failure)
            } catch {
                result = .failure(.unreachable)
            }
            guard let self, !Task.isCancelled else { return }
            self.routeReady(token: token, generation: generation, result: result, revision: revision,
                            installNow: installNow)
        }
    }

    private func routeReady(token: Int, generation: Int, result: Result<NavRoute, NavFailure>, revision: Int,
                            installNow: Bool) {
        guard token == routeToken, generation == self.generation, !ended else { return }
        let travel = mode ?? "walking"
        let failure: NavFailure?
        switch result {
        case .success(let r): failure = usable(r) ? nil : .rejected
        case .failure(let f): failure = f
        }
        guard case .success(let r) = result, failure == nil else {
            pendingRerouteRequestId = nil
            errorRecovery = "route"
            let copy: (String, String)
            switch failure ?? .rejected {
            case .noRoute:
                let suggestion: String
                switch travel {
                case "driving": suggestion = "Try walking or check the destination"
                case "cycling": suggestion = "Try walking or driving"
                default: suggestion = "Try cycling, driving, or a closer destination"
                }
                copy = ("No \(travel) route from here", suggestion)
            case .tooLong: copy = ("Route too long for the HUD", "Choose a closer destination")
            case .unreachable: copy = ("Can't reach the map service", "Check the phone's internet, then retry")
            case .incomplete: copy = ("Route data was incomplete", "Try again or change destination")
            case .rejected: copy = ("Route unavailable", "Try again or change destination")
            }
            Log.add("navigation route failed: \((failure ?? .rejected).reason)")
            publishSummary(phase: "error", instruction: "\(copy.0) — \(copy.1)")
            sendView(Self.view("error", copy.0, copy.1, hint: "tap/say retry · swipe cancel"), "route-error")
            return
        }
        route = r
        routeRevision = revision
        self.generation = max(self.generation, generation)
        Log.add("navigation route ready: \(Int(r.totalM)) m, \(r.steps.count) steps, \(r.geometry.count) points, "
                + String(format: "%.1f min", r.etaMinutes))

        // No street-tile prefetch: since 2026-11-23 the glasses draw a route
        // schematic from the route and the live fix alone, and ignore any
        // `navigation.map` raster that still arrives.

        if installNow {
            sendRoute()
            return
        }
        publishSummary(
            phase: "preview",
            instruction: "Tap on glasses to start",
            maneuver: "depart",
            remainingM: r.totalM,
            etaMinutes: Self.minutes(r.etaMinutes)
        )
        sendView(Self.view("preview", destination?.label ?? "Destination",
                           "\(travel.capitalized) · \(Int(Self.minutes(r.etaMinutes))) min",
                           hint: "tap/say start · swipe up/down to edit", mode: travel, maneuver: "depart",
                           remaining: r.totalM, eta: Self.minutes(r.etaMinutes), progress: 0), "preview")
    }

    private func usable(_ r: NavRoute) -> Bool {
        guard ["walking", "cycling", "driving"].contains(r.mode), r.mode == mode, destination != nil,
              (2...4096).contains(r.geometry.count), (1...512).contains(r.steps.count),
              r.etaMinutes.isFinite, (0...1_000_000).contains(r.etaMinutes),
              r.geometry.allSatisfy(\.isValid) else { return false }
        return zip(r.steps, r.steps.dropFirst()).allSatisfy { $0.alongM <= $1.alongM }
            && r.steps.allSatisfy { $0.alongM.isFinite && (0...10_000_000).contains($0.alongM) && !$0.instruction.isEmpty }
    }

    private func sendRoute() {
        guard let r = route, currentView != nil, let destination, installAttempt == nil, !routeInstalled,
              !arrived else { return }
        if currentView?.view == "preview" {
            sendView(Self.view("rerouting", "Starting navigation", "Preparing directions on your glasses",
                               hint: "Directions begin when ready · say stop", interaction: "none", mode: mode,
                               remaining: r.totalM, eta: Self.minutes(r.etaMinutes), progress: 0), "starting")
        }
        guard let v = currentView else { return }
        routeInstalled = false
        installAttempt = (r.routeId, routeRevision)
        routeSentAt = Date()
        var wire: [String: Any] = [
            "kind": "navigation.route", "sessionId": sessionId, "requestId": v.requestId,
            "generation": v.generation, "routeId": r.routeId, "revision": routeRevision,
            "destinationLabel": destination.label.utf16Prefix(96), "etaMinutes": r.etaMinutes, "plan": r.plan,
        ]
        if usingPhoneFixes {
            wire["phoneFixes"] = true
        }
        link.send(wire)
        Log.add("tx navigation.route revision=\(routeRevision) \(r.routeId) phoneFixes=\(usingPhoneFixes)")
    }

    private func handleRouteAck(routeId: String, revision: Int, outcome: String) {
        guard let r = route, let attempt = installAttempt, attempt.routeId == routeId, attempt.revision == revision,
              r.routeId == routeId, revision == routeRevision else { return }
        installAttempt = nil
        Log.add("navigation route \(outcome) after " + String(format: "%.1fs", Date().timeIntervalSince(routeSentAt)))
        if outcome == "installed" {
            let isInitialInstall = revision == 1
            routeInstalled = true
            lastStateSequence = 0
            pendingRerouteRequestId = nil
            lastSpokenStepId = nil
            spokenApproachStepIds.removeAll()
            lastSpokenAlert = nil

            let leadStep = r.steps.first(where: { $0.maneuver != "depart" && $0.maneuver != "arrive" }) ?? r.steps.first
            publishSummary(
                phase: "guidance",
                instruction: leadStep?.instruction ?? "Follow the route",
                maneuver: leadStep?.maneuver ?? "continue",
                remainingM: r.totalM,
                etaMinutes: Self.minutes(r.etaMinutes)
            )
            owner?.onMenuChanged?()

            if let first = r.steps.first {
                lastSpokenStepId = first.id
                let destName = destination?.label ?? "your destination"
                let travel = mode ?? "walking"
                if isInitialInstall {
                    owner?.speakCueIfEnabled("Starting \(travel) directions to \(destName). \(first.instruction)")
                } else {
                    owner?.speakCueIfEnabled("Route updated. \(first.instruction)")
                }
            }

            let claimLens = isInitialInstall && autoInstall
            if usingPhoneFixes {
                startPhoneFixStream(mode: mode ?? "walking", claimLensOnStart: claimLens)
            } else if claimLens {
                owner?.onClaimedGlass?()
                link.send(["kind": "navigation.resume", "sessionId": sessionId])
                Log.add("tx navigation.resume \(sessionId) (autoInstall)")
            }
            return
        }
        routeInstalled = false
        pendingRerouteRequestId = nil
        stopPhoneFixesIfActive()
        errorRecovery = "install"
        publishSummary(phase: "error", instruction: "Navigation could not start")
        sendView(Self.view("error", "Navigation could not start", "The route was not installed on the phone",
                           hint: "tap/say retry · swipe cancel"), "install-error")
    }

    private func startPhoneFixStream(mode: String, claimLensOnStart: Bool = false) {
        Task { @MainActor [weak self] in
            guard let self, !self.ended, self.routeInstalled, self.usingPhoneFixes else { return }
            self.phoneStreamActive = true
            PhoneLocation.shared.startNavigationUpdates(mode: mode) { [weak self] fix in
                guard let self, !self.ended, self.routeInstalled, self.usingPhoneFixes else { return }
                self.owner?.notePhoneFix(fix)
                self.sendPhoneFix(fix)
            }
            // Deliver an immediate initial fix so Even Hub paints the active guidance
            // screen + street map right away even while stationary.
            let seedFix: PhoneLocation.Fix?
            if let latest = PhoneLocation.shared.latest, Date().timeIntervalSince(latest.at) <= 30 {
                seedFix = latest
            } else if let origin = self.origin {
                seedFix = PhoneLocation.Fix(point: origin, accuracy: 10.0, heading: nil, speed: nil, at: Date())
            } else {
                seedFix = nil
            }
            if let seedFix, !self.ended, self.routeInstalled {
                self.sendPhoneFix(seedFix)
            }
            if claimLensOnStart, !self.ended, self.routeInstalled {
                self.owner?.onClaimedGlass?()
                self.link.send(["kind": "navigation.resume", "sessionId": self.sessionId])
                Log.add("tx navigation.resume \(self.sessionId) (autoInstall)")
            }
        }
    }

    private func sendPhoneFix(_ fix: PhoneLocation.Fix) {
        var wire: [String: Any] = [
            "kind": "navigation.fix",
            "sessionId": sessionId,
            "latitude": fix.point.latitude,
            "longitude": fix.point.longitude,
            "timestamp": Int(fix.at.timeIntervalSince1970 * 1000),
        ]
        if let accuracy = fix.accuracy, accuracy.isFinite { wire["accuracy"] = accuracy }
        if let heading = fix.heading, heading.isFinite { wire["heading"] = heading }
        if let speed = fix.speed, speed.isFinite { wire["speed"] = speed }
        link.send(wire)
    }

    private func stopPhoneFixesIfActive() {
        guard phoneStreamActive else { return }
        phoneStreamActive = false
        Task { @MainActor in
            PhoneLocation.shared.stopNavigationUpdates()
        }
    }

    // MARK: - Street Tile Prefetch (retired)

    /// Retired with the raster map pane (2026-11-23): nothing queues tiles
    /// any more; this only clears state a pre-schematic build left behind.
    private func cancelMapPrefetch() {
        mapTask?.cancel()
        mapTask = nil
        mapQueue.removeAll()
        mapCentres.removeAll()
    }

    // MARK: - Live Progress States & Spoken Headphone Cues

    private func handleState(routeId: String, revision: Int, sequence: Int, state: String, message m: [String: Any]) {
        guard !arrived, routeInstalled, let r = route, r.routeId == routeId, revision == routeRevision,
              sequence > lastStateSequence else { return }
        lastStateSequence = sequence

        let maneuver = m.string("maneuver")
        let maneuverDist = (m["maneuverDistanceM"] as? NSNumber)?.doubleValue
        let instruction = m.string("instruction")
        let stepId = m.string("stepId")
        let remaining = (m["remainingDistanceM"] as? NSNumber)?.doubleValue
        let eta = (m["etaMinutes"] as? NSNumber)?.doubleValue

        switch state {
        case "guidance":
            lastSpokenAlert = nil
            let currentSummary = owner?.activeSummary
            publishSummary(
                phase: "guidance",
                instruction: instruction ?? currentSummary?.instruction ?? "Continue",
                maneuver: maneuver ?? currentSummary?.maneuver,
                maneuverDistanceM: maneuverDist,
                remainingM: remaining ?? currentSummary?.remainingM ?? r.totalM,
                etaMinutes: eta ?? currentSummary?.etaMinutes ?? Self.minutes(r.etaMinutes)
            )
            if let instruction, !instruction.isEmpty, maneuver != "arrive" {
                let key = stepId ?? instruction
                let approachThresholdM = (mode == "driving") ? 100.0 : 40.0
                if let dist = maneuverDist, dist <= approachThresholdM, !spokenApproachStepIds.contains(key) {
                    spokenApproachStepIds.insert(key)
                    lastSpokenStepId = key
                    if dist >= 15 {
                        let rounded = Int((dist / 10.0).rounded()) * 10
                        owner?.speakCueIfEnabled("In \(rounded) meters, \(Self.lowercasedLead(instruction))")
                    } else {
                        owner?.speakCueIfEnabled(instruction)
                    }
                } else if key != lastSpokenStepId {
                    lastSpokenStepId = key
                    if let dist = maneuverDist, dist > approachThresholdM {
                        owner?.speakCueIfEnabled("In \(Self.spokenDistance(dist)), \(Self.lowercasedLead(instruction))")
                    } else {
                        owner?.speakCueIfEnabled(instruction)
                    }
                }
            }

        case "wrong_way":
            let currentSummary = owner?.activeSummary
            publishSummary(
                phase: "guidance",
                instruction: "Wrong direction — turn around when safe",
                maneuver: "u_turn",
                remainingM: remaining ?? currentSummary?.remainingM,
                etaMinutes: eta ?? currentSummary?.etaMinutes
            )
            if lastSpokenAlert != "wrong_way" {
                lastSpokenAlert = "wrong_way"
                owner?.speakCueIfEnabled("Wrong direction. Turn around when safe.")
            }

        case "off_route":
            let currentSummary = owner?.activeSummary
            publishSummary(
                phase: "rerouting",
                instruction: "Off route — rerouting…",
                remainingM: remaining ?? currentSummary?.remainingM,
                etaMinutes: eta ?? currentSummary?.etaMinutes
            )
            if lastSpokenAlert != "off_route" {
                lastSpokenAlert = "off_route"
                owner?.speakCueIfEnabled("Off route. Rerouting.")
            }

        case "uncertain":
            let currentSummary = owner?.activeSummary
            publishSummary(
                phase: "guidance",
                instruction: currentSummary?.instruction ?? "Finding your position…",
                maneuver: currentSummary?.maneuver,
                maneuverDistanceM: currentSummary?.maneuverDistanceM,
                remainingM: remaining ?? currentSummary?.remainingM,
                etaMinutes: eta ?? currentSummary?.etaMinutes
            )

        case "arrived":
            arrived = true
            routeInstalled = false
            installAttempt = nil
            pendingRerouteRequestId = nil
            stopPhoneFixesIfActive()
            cancelMapPrefetch()
            publishSummary(
                phase: "arrived",
                instruction: "You have arrived",
                maneuver: "arrive",
                maneuverDistanceM: 0,
                remainingM: 0,
                etaMinutes: 0,
                arrivedOverride: true
            )
            let destName = destination?.label ?? "your destination"
            owner?.speakCueIfEnabled("You have arrived at \(destName).")
            Log.add("navigation arrived")

        default:
            break
        }
    }

    private static func lowercasedLead(_ text: String) -> String {
        guard let first = text.first else { return text }
        let rest = text.dropFirst()
        if let second = rest.first, second.isUppercase {
            return text
        }
        return String(first).lowercased() + rest
    }

    private static func spokenDistance(_ meters: Double) -> String {
        if meters < 1000 {
            let rounded = max(10, Int((meters / 10.0).rounded()) * 10)
            return "\(rounded) meters"
        }
        return String(format: "%.1f kilometers", meters / 1000.0)
    }

    private func handleReroute(routeId: String, baseRevision: Int, requestId: String, point: NavPoint) {
        guard !arrived, routeInstalled, let r = route, r.routeId == routeId, baseRevision == routeRevision,
              pendingRerouteRequestId == nil, point.isValid else { return }
        pendingRerouteRequestId = requestId
        routeInstalled = false
        lastStateSequence = 0
        publishSummary(phase: "rerouting", instruction: "Rerouting…")
        sendView(Self.view("rerouting", "Rerouting", "Finding a safer path", hint: "Old directions paused · say stop",
                           interaction: "none", mode: mode), "rerouting")
        guard let v = currentView else { return }
        rerouteAfterAck = (point, v.generation, routeRevision + 1, v.requestId)
    }
}
