import Foundation

/// One place the wearer saved on the plugin's phone page (protocol.py SavedPlace).
struct SavedPlace: Codable, Equatable {
    let id: String
    let name: String
    let address: String?
    let latitude: Double?
    let longitude: Double?
}

/// Navigation on the phone: the Mac's spoken door, saved places, the
/// Navigation page and one route session at a time (services/navigation.py,
/// capabilities.py _show_navigation, __main__.py make_navigation_session).
///
/// The plugin is unchanged. It still owns guidance: it tracks the phone's
/// location fixes against the route plan and draws the turn pages itself.
/// This side finds and confirms the place, the mode and the route, and
/// answers a reroute. Apple Maps replaces Mapbox (NavigationMaps.swift).
final class Navigation: BrainFeature, VoiceCommand {
    let name = "navigation"
    let uplinkKinds: Set<String> = [
        "navigation.action", "navigation.view.ack", "navigation.origin", "navigation.origin.failed",
        "navigation.route.ack", "navigation.state", "navigation.reroute.request", "places", "location",
    ]

    static let placesKey = "joat.navigation.places.v1"
    /// session.py MAX_NAVIGATION_DESTINATION_CHARS
    static let maxDestinationChars = 200
    /// hub_gateway.py DEFAULT_LOCATION_REFRESH_TIMEOUT_S
    static let refreshSeconds: TimeInterval = 7
    /// A fix this old still says which city the wearer is in: a search bias.
    /// "Nearest" asks the phone for a fresh one first (hub_gateway uses 120 s).
    static let biasMaxAge: TimeInterval = 3600
    static let nearbyMaxAge: TimeInterval = 120

    private let link: PluginLink
    private(set) var places: [SavedPlace]
    private var fix: (point: NavPoint, at: Date, accuracy: Double?)?
    private var refreshes: [String: (NavPoint?) -> Void] = [:]
    private(set) var session: NavigationSession?
    /// Brain.ask: show and say a question; the next sentence answers it.
    var askFollowUp: ((String, @escaping (String) -> Void) -> Void)?
    /// A short answer over the current page, also said aloud.
    var receipt: ((String) -> Void)?
    /// Brain: the question on the conversation page now, the answer later
    /// (shown and said). "Where am I" waits for a fresh fix.
    var onAsked: ((String) -> Void)?
    var onAnswer: ((String) -> Void)?
    private var areaCache: (point: NavPoint, at: Date, value: (context: String, city: String?, country: String?))?

    /// The city the wearer is in, for Claude's answers and its web search:
    /// looked up again after 30 minutes or 5 km (Apple asks for at most one
    /// geocoding request a minute).
    @MainActor
    func area() async -> (context: String, city: String?, country: String?)? {
        guard let point = recentFix(maxAge: Self.biasMaxAge) else { return nil }
        if let c = areaCache, Date().timeIntervalSince(c.at) < 600, NavigationMaps.distance(c.point, point) < 150 {
            return c.value
        }
        // Without an address the coordinates still say where the wearer is:
        // Claude must never answer that the location is unknown when the
        // phone has a fix (2026-10-23, it "corrected" the phone's address).
        let fallback = (context: String(format: "latitude %.5f, longitude %.5f", point.latitude, point.longitude),
                        city: areaCache?.value.city, country: areaCache?.value.country)
        guard let value = await NavigationMaps.area(at: point) else { return fallback }
        areaCache = (point, Date(), value)
        return value
    }

    init(link: PluginLink) {
        self.link = link
        if let data = UserDefaults.standard.data(forKey: Self.placesKey),
           let saved = try? JSONDecoder().decode([SavedPlace].self, from: data) {
            places = saved
        } else {
            places = []
        }
    }

    var active: Bool { session != nil }

    // MARK: - BrainFeature

    /// A new plugin connection means the old one is gone, and with it the
    /// route the plugin was tracking (__main__.py on_disconnect / hello).
    func pluginConnected() {
        if let s = session {
            Log.add("navigation: plugin reconnected, ending \(s.sessionId)")
            s.end()
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

    // MARK: - Location (hub_gateway.py fresh_location / request_location_refresh)

    func recentFix(maxAge: TimeInterval) -> NavPoint? {
        guard let fix, Date().timeIntervalSince(fix.at) <= maxAge else { return nil }
        return fix.point
    }

    /// One high-accuracy fix from the plugin, or nil after 7 s.
    func refreshFix(_ done: @escaping (NavPoint?) -> Void) {
        let id = "location-" + ConfirmGate.hex(9)
        refreshes[id] = done
        link.send(["kind": "location.refresh", "requestId": id])
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.refreshSeconds) { [weak self] in
            self?.refreshes.removeValue(forKey: id)?(nil)
        }
    }

    // MARK: - Saved places (protocol.py _saved_place)

    private func acceptPlaces(_ message: [String: Any]) {
        guard let raw = message["places"] as? [Any], raw.count <= 50 else {
            Log.add("places update rejected: not a list of at most 50")
            return
        }
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
        places = parsed
        if let data = try? JSONEncoder().encode(parsed) { UserDefaults.standard.set(data, forKey: Self.placesKey) }
        Log.add("places: \(parsed.count) saved")
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

    // MARK: - Starting a route (session.py start_navigation)

    /// nil = the session started; otherwise the receipt that says why not.
    @discardableResult
    func start(_ destination: String) -> String? {
        let d = destination.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !d.isEmpty, d.count <= Self.maxDestinationChars, !d.contains("\u{0}") else {
            return "I didn't catch a destination."
        }
        guard session == nil else { return "Finish the current session first." }
        let s = NavigationSession(query: d, link: link, sessionId: "nav-" + ConfirmGate.hex(10),
                                  proximity: recentFix(maxAge: Self.biasMaxAge), owner: self)
        session = s
        s.onEnd = { [weak self, weak s] in
            guard let self, let s, self.session === s else { return }
            self.session = nil
            Log.add("navigation \(s.sessionId) ended (\(s.arrived ? "arrived" : "cancelled"))")
        }
        Log.add("navigation \(s.sessionId) started (\(d.count) chars)")
        s.start()
        return nil
    }

    // MARK: - The Navigation page (capabilities.py _show_navigation)

    /// The plugin draws NAVIGATION as one firmware text page, and a tap on a
    /// text page always arrives as row zero (CapabilityMenu.ts
    /// TEXT_PAGE_TITLES). So the question comes first: on the Mac the saved
    /// places came first, and the tap could only ever pick the first place.
    /// The places are listed by name to say; "Home" resolves to its saved
    /// coordinates.
    func pageRows() -> [String] {
        var rows = ["➤ Navigate to a place"]
        let names = places.prefix(16).map(\.name)
        var line = ""
        for name in names {
            let next = line.isEmpty ? "Saved: \(name)" : "\(line) · \(name)"
            if next.utf16.count > 64, !line.isEmpty {
                rows.append(line)
                line = name
            } else {
                line = next
            }
        }
        if !line.isEmpty { rows.append(line) }
        rows.append("I’ll confirm the place. Then choose Walk or Cycle.")
        return rows
    }

    func selectPageRow(_ index: Int) {
        guard index == 0 else { return }
        guard session == nil else {
            receipt?("Finish the current session first.")
            return
        }
        guard let ask = askFollowUp else {
            receipt?("Navigation cannot be started from this phone.")
            return
        }
        ask("Where to?") { [weak self] answer in
            guard let self else { return }
            if let why = self.start(Self.destination(fromAnswer: answer)) { self.receipt?(why) }
        }
    }

    // MARK: - Voice

    /// "navigate to the library": the command chain's door (navigation_query).
    /// And "where am I", which the phone answers itself: the Mac's assistant
    /// answered it with a location tool, and the phone already has the fix.
    func handle(_ request: String) -> CommandReply? {
        if Self.isWhereAmI(request) {
            onAsked?(request)
            describeLocation { [weak self] answer in self?.onAnswer?(answer) }
            return CommandReply(text: "", activated: false)
        }
        guard let destination = Self.query(request) else { return nil }
        if let why = start(destination) { return CommandReply(text: why, activated: false) }
        // The navigation views are the answer; nothing to say over them.
        return CommandReply(text: "", activated: true)
    }

    /// session.py handle_transcript while a route session is live: the
    /// cancel words are heard with or without the name; everything that
    /// steers needs the name, except the destination JOAT just asked for.
    func hear(_ text: String, addressed: String?) {
        guard let s = session else { return }
        if Self.isCancel(text) || addressed.map(Self.isCancel) == true {
            Log.add("navigation cancelled by voice")
            s.end()
            return
        }
        if let a = addressed, !a.isEmpty {
            if !s.consume(a) { Log.add("navigation active — not routed to the assistant (\(a.count) chars)") }
            return
        }
        if addressed == nil, s.awaitingDestinationNow {
            _ = s.consume(text)
            return
        }
        Log.add("navigation active — unaddressed speech ignored (\(text.count) chars)")
    }

    /// session.py's whole-utterance escape hatch, apostrophes out.
    static let sessionCancel: Set<String> = [
        "stop", "cancel", "stop it", "cancel that", "cancel it",
        "stop the debate", "stop the brainstorm", "end the brainstorm",
        "stop the session", "end the session", "never mind", "nevermind",
        "thats enough", "that is enough", "enough",
        "were done", "we are done", "done",
        "forget it", "abort", "quit", "end it", "stop this",
        "cancel this", "cancel the brainstorm",
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

    private static let door = try! NSRegularExpression(
        pattern: #"^(?:navigate(?:\s+me)?|directions|take me|guide me|get me|help me get|show me (?:the )?way)(?:\s+to)\s+(.+)$"#,
        options: [.caseInsensitive])
    private static let doorQuestion = try! NSRegularExpression(
        pattern: #"^(?:how do i get|what(?:['’]s| is) the (?:best )?way)(?:\s+to)\s+(.+)$"#,
        options: [.caseInsensitive])
    /// "Navigate home" has no "to" on the Mac's grammar, where the Mac's
    /// assistant tool caught it; the phone has no such tool, so it is here.
    private static let doorHome = try! NSRegularExpression(
        pattern: #"^(?:navigate(?:\s+me)?|take me|guide me|get me)\s+(home)$"#, options: [.caseInsensitive])

    /// What a spoken answer to "Where to?" names. The recogniser often turns
    /// "Hey JOAT" into something else ("Bijo take me to the nearest coffee
    /// shop", 2026-10-23), so a door phrase anywhere in the answer wins;
    /// otherwise the whole answer is the place.
    static func destination(fromAnswer text: String) -> String {
        if let d = query(text) { return d }
        let range = NSRange(text.startIndex..., in: text)
        if let m = looseDoor.firstMatch(in: text, range: range), let r = Range(m.range(at: 1), in: text) {
            let d = text[r].trimmingCharacters(in: CharacterSet(charactersIn: " .!?"))
            if !d.isEmpty { return d }
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let looseDoor = try! NSRegularExpression(
        pattern: #"\b(?:navigate(?:\s+me)?|directions|take me|guide me|get me|help me get|show me (?:the )?way|how do i get)\s+to\s+(.+)$"#,
        options: [.caseInsensitive])

    // MARK: - Where am I

    private static let whereAmIPattern = try! NSRegularExpression(
        pattern: #"^(?:(?:can|could|would) you\s+)?(?:please\s+)?(?:tell me\s+|show me\s+|give me\s+)?(?:where am i|where are we|what(?:['’]s| is) my (?:current |exact )?(?:location|address)|my (?:current |exact )?location|what (?:street|road) am i on|what city am i in)(?:\s+right now)?(?:\s+please)?$"#,
        options: [.caseInsensitive])

    static func isWhereAmI(_ text: String) -> Bool {
        var t = text.collapsed
        while let last = t.last, ".,!?".contains(last) { t.removeLast() }
        return Grammar.match(whereAmIPattern, t) != nil
    }

    /// A fresh fix from the glasses' phone, then Apple's address for it.
    func describeLocation(_ done: @escaping (String) -> Void) {
        refreshFix { [weak self] fresh in
            guard let self else { return }
            guard let point = fresh ?? self.recentFix(maxAge: Self.biasMaxAge), let fix = self.fix else {
                done("I don't have your location yet. Open the Even app and allow it Location, so the glasses can share it.")
                return
            }
            let minutesOld = fresh == nil ? Int(Date().timeIntervalSince(fix.at) / 60) : 0
            let rough = (fix.accuracy ?? 0) > 50
            Task { @MainActor in
                var answer: String
                if let place = await NavigationMaps.address(at: point) {
                    answer = "You're \(rough ? "near" : "at") \(place)."
                } else {
                    answer = String(format: "You're at %.4f, %.4f. I couldn't look up the address.",
                                    point.latitude, point.longitude)
                }
                if minutesOld >= 2 { answer += " That was \(minutesOld) minutes ago." }
                Log.add("where am I answered (fresh=\(fresh != nil), accuracy=\(Int(fix.accuracy ?? -1)) m)")
                done(answer)
            }
        }
    }

    /// navigation.py navigation_query: an explicit destination request, or nil.
    static func query(_ text: String) -> String? {
        var request = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = request.last, ".!?".contains(last) { request.removeLast() }
        request = request.replacingOccurrences(of: #"^joat(?:\s*[,:-])?\s+"#, with: "",
                                               options: [.regularExpression, .caseInsensitive])
        request = request.replacingOccurrences(of: #"^(?:(?:can|could|would)\s+you\s+|please\s+)"#, with: "",
                                               options: [.regularExpression, .caseInsensitive])
        for re in [door, doorQuestion, doorHome] {
            let range = NSRange(request.startIndex..., in: request)
            if let m = re.firstMatch(in: request, range: range), let r = Range(m.range(at: 1), in: request) {
                let destination = request[r].trimmingCharacters(in: .whitespaces)
                return destination.isEmpty ? nil : destination
            }
        }
        return nil
    }
}

/// One confirm-place, choose-mode, route lifecycle; one HUD owner. A port of
/// services/navigation.py NavigationSession. Every input is bound to the
/// view generation it answers, so a late tap or a stale route cannot act.
final class NavigationSession {
    let sessionId: String
    private let link: PluginLink
    private weak var owner: Navigation?
    var onEnd: (() -> Void)?

    private let initialQuery: String
    private var lastQuery: String
    private var proximity: NavPoint?
    private var searchProximity: NavPoint?
    private var nearbySearch = false
    private var generation = 0
    private var searchGeneration = 0
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
    private var lastStateSequence = 0
    private var installAttempt: (routeId: String, revision: Int)?
    private var routeSentAt = Date()
    private var pendingRerouteRequestId: String?
    private var rerouteAfterAck: (origin: NavPoint, generation: Int, revision: Int, requestId: String)?
    private(set) var arrived = false
    private(set) var ended = false
    private var awaitingDestination = false
    private var awaitingUntil = Date.distantPast
    /// destination, search_location, origin, route, install
    private var errorRecovery: String?
    private var searchTask: Task<Void, Never>?
    private var routeTask: Task<Void, Never>?

    /// _MIN_LOCATING_VISIBLE_SECONDS
    static let minLocatingVisible: TimeInterval = 0.8
    /// The phone's own convention for a question JOAT asked: the next
    /// sentence answers it without the name, for this long.
    static let answerWindow: TimeInterval = 15

    init(query: String, link: PluginLink, sessionId: String, proximity: NavPoint?, owner: Navigation) {
        initialQuery = query
        lastQuery = query
        self.link = link
        self.sessionId = sessionId
        self.proximity = proximity
        self.owner = owner
    }

    var awaitingDestinationNow: Bool { awaitingDestination && Date() < awaitingUntil }

    func start() { startSearch(initialQuery) }

    /// cancel(), finish, a rejected view, link loss: the route ends on the
    /// glasses too.
    func end() {
        guard !ended else { return }
        ended = true
        awaitingDestination = false
        errorRecovery = nil
        locatingSince = nil
        rerouteAfterAck = nil
        searchTask?.cancel()
        routeTask?.cancel()
        routeToken += 1
        link.send(["kind": "navigation.stop", "sessionId": sessionId])
        Log.add("tx navigation.stop \(sessionId)")
        onEnd?()
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
            handleState(routeId: routeId, revision: revision, sequence: sequence, state: state)
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

    // MARK: - Voice (consume_transcript / _voice_action)

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
    static let startWords: Set<String> = ["start", "start navigation", "go", "begin", "begin navigation"]
    static let changeModeWords: Set<String> = ["change mode", "change the mode"]
    static let finishWords: Set<String> = ["finish", "done", "end navigation"]
    static let cancelWords: Set<String> = [
        "stop", "cancel", "stop navigation", "cancel navigation", "end directions", "stop directions",
    ]

    /// _normalise_navigation_voice: case, spaces and end punctuation only.
    static func normalize(_ text: String) -> String {
        var t = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = t.last, ".!?".contains(last) { t.removeLast() }
        return t.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    /// One explicit, state-bound voice command. Never a shortcut around the
    /// place confirmation: each sentence maps only to what the visible view
    /// permits.
    func consume(_ text: String) -> Bool {
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, !ended else { return false }
        if let replacement = Navigation.query(query) {
            awaitingDestination = false
            replaceDestination(replacement)
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
            replaceDestination(correction)
            return true
        }
        if awaitingDestination {
            awaitingDestination = false
            replaceDestination(Navigation.destination(fromAnswer: query))
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

    /// "No, I meant OnCue, the gas station" -> "OnCue gas station".
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

    /// _destination_search: the place words, and whether "nearest" / "near
    /// me" asked for the wearer's own surroundings.
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

    // MARK: - Destination search

    private func replaceDestination(_ query: String) {
        // Old guidance must never outlive the route generation behind it.
        if routeInstalled || installAttempt != nil {
            link.send(["kind": "navigation.stop", "sessionId": sessionId])
        }
        startSearch(query)
    }

    private func startSearch(_ query: String) {
        let request = Self.destinationSearch(query)
        lastQuery = query
        awaitingDestination = false
        searchTask?.cancel()
        routeTask?.cancel()
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
        arrived = false
        searchGeneration += 1
        let gen = searchGeneration
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
                    // The wearer named the place; keep that name on the lens.
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
            sendView(Self.view("error", "Phone location unavailable",
                               "I need your phone location to find the nearest match",
                               hint: "tap/say retry · swipe cancel"), "search-location-error")
            return
        }
        guard let found, !found.isEmpty else {
            errorRecovery = "destination"
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

    /// _candidate_option
    private func option(_ c: NavCandidate, near: NavPoint?) -> String {
        let away = near.map { " · about \(NavigationMaps.formatDistance(distance(to: c, from: $0)))" } ?? ""
        let value = "\(c.label)\(away) · \(c.address)"
        if value.utf16.count <= 64 { return value }
        return value.utf16Prefix(61).trimmingCharacters(in: .whitespaces) + "..."
    }

    private func showDestination(_ c: NavCandidate) {
        var secondary = c.address
        if nearbySearch, let near = searchProximity {
            secondary = "About \(NavigationMaps.formatDistance(distance(to: c, from: near))) away · \(c.address)"
        }
        sendView(Self.view("destination", c.label, secondary, hint: "tap/say confirm · swipe retry"), "destination")
    }

    private func showMode() {
        guard let destination else { return }
        sendView(Self.view("mode", "Choose travel mode", destination.label,
                           hint: "tap choose · swipe select · say walk/cycle",
                           options: ["Walk", "Cycle"], choiceIds: ["walking", "cycling"]), "mode")
    }

    private func showRetryPrompt() {
        awaitingDestination = true
        awaitingUntil = Date().addingTimeInterval(Self.answerWindow)
        sendView(Self.view("repair", "Say the destination again",
                           "Keep confirmed parts; correct only what was wrong",
                           hint: "say the destination again",
                           options: ["Waiting for destination"], choiceIds: ["retry"]), "retry")
    }

    // MARK: - Actions

    private func handleAction(requestId: String, generation: Int, action: String, choiceId: String?) {
        guard let v = currentView, requestId == v.requestId, generation == v.generation else { return }
        // A left-temple double press stops from any view, waits included.
        if action == "cancel" {
            end()
            return
        }
        switch (v.view, action) {
        case ("destination", "confirm_destination"):
            showMode()
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
            guard let choiceId, ["walking", "cycling"].contains(choiceId) else { return }
            mode = choiceId
            requestOrigin()
        case ("preview", "start"):
            sendRoute()
        case ("preview", "change_mode"):
            showMode()
        case ("preview", "change_destination"):
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
                // The Mac left the error up while it retried; say so instead.
                showRouting()
                startRoute(from: origin!, generation: self.generation, revision: routeRevision + 1,
                           installNow: false)
            case "install":
                sendRoute()
            default:
                showRetryPrompt()
            }
        default:
            break
        }
    }

    // MARK: - Origin

    private func requestOrigin() {
        guard destination != nil, let mode else { return }
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
              destination != nil, mode != nil, point.isValid else { return }
        originRequest = nil
        locatingSince = nil
        Log.add("navigation origin received")
        origin = point
        showRouting()
        startRoute(from: point, generation: self.generation, revision: routeRevision + 1, installNow: false)
    }

    private func showRouting() {
        guard let mode else { return }
        sendView(Self.view("uncertain", "Finding a \(mode) route", "Checking the route to your destination",
                           hint: "Route search in progress · say stop", interaction: "none", mode: mode), "routing")
    }

    private func handleOriginFailure(requestId: String, generation: Int) {
        guard let request = originRequest, request.requestId == requestId, request.generation == generation else { return }
        // A refusal that comes back at once must not erase "Getting your
        // location" before the wearer can see it.
        let shown = locatingSince.map { Date().timeIntervalSince($0) } ?? Self.minLocatingVisible
        locatingSince = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, Self.minLocatingVisible - shown)) { [weak self] in
            guard let self, !self.ended, self.originRequest?.requestId == requestId else { return }
            Log.add("navigation origin unavailable")
            self.originRequest = nil
            self.errorRecovery = "origin"
            self.sendView(Self.view("error", "Phone location unavailable", "Allow Location for Even Hub, then retry",
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

    /// protocol.py _navigation_hud_to_wire, cut to its limits instead of
    /// refused: the wire carries no empty text.
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

    /// Whole minutes for the lens: the Mac's raw float printed as
    /// "17.833333333333332 min" on the Starting page.
    static func minutes(_ eta: Double) -> Double { max(1, eta.rounded()) }

    // MARK: - Route

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
                // The Mac suggested cycling even to a cyclist.
                copy = ("No \(travel) route from here",
                        travel == "cycling" ? "Try walking or a closer destination" : "Try cycling or a closer destination")
            case .tooLong: copy = ("Route too long for the HUD", "Choose a closer destination")
            case .unreachable: copy = ("Can't reach the map service", "Check the phone's internet, then retry")
            case .incomplete: copy = ("Route data was incomplete", "Try again or change destination")
            case .rejected: copy = ("Route unavailable", "Try again or change destination")
            }
            Log.add("navigation route failed: \((failure ?? .rejected).reason)")
            sendView(Self.view("error", copy.0, copy.1, hint: "tap/say retry · swipe cancel"), "route-error")
            return
        }
        route = r
        routeRevision = revision
        self.generation = max(self.generation, generation)
        Log.add("navigation route ready: \(Int(r.totalM)) m, \(r.steps.count) steps, \(r.geometry.count) points, "
                + String(format: "%.1f min", r.etaMinutes))
        if installNow {
            sendRoute()
            return
        }
        sendView(Self.view("preview", destination?.label ?? "Destination",
                           "\(travel.capitalized) · \(Int(Self.minutes(r.etaMinutes))) min",
                           hint: "tap/say start · swipe up/down to edit", mode: travel, maneuver: "depart",
                           remaining: r.totalM, eta: Self.minutes(r.etaMinutes), progress: 0), "preview")
    }

    /// _provider_route_is_usable: what the wire would refuse, refused here.
    private func usable(_ r: NavRoute) -> Bool {
        guard r.mode == mode, destination != nil, (2...4096).contains(r.geometry.count),
              (1...512).contains(r.steps.count), r.etaMinutes.isFinite, (0...1_000_000).contains(r.etaMinutes),
              r.geometry.allSatisfy(\.isValid) else { return false }
        return zip(r.steps, r.steps.dropFirst()).allSatisfy { $0.alongM <= $1.alongM }
            && r.steps.allSatisfy { $0.alongM.isFinite && (0...10_000_000).contains($0.alongM) && !$0.instruction.isEmpty }
    }

    private func sendRoute() {
        guard let r = route, currentView != nil, let destination, installAttempt == nil, !routeInstalled,
              !arrived else { return }
        // Replace the preview first: it says what is happening and makes a
        // second tap inert while the phone installs the route.
        if currentView?.view == "preview" {
            sendView(Self.view("rerouting", "Starting navigation", "Preparing directions on your glasses",
                               hint: "Directions begin when ready · say stop", interaction: "none", mode: mode,
                               remaining: r.totalM, eta: Self.minutes(r.etaMinutes), progress: 0), "starting")
        }
        guard let v = currentView else { return }
        routeInstalled = false
        installAttempt = (r.routeId, routeRevision)
        routeSentAt = Date()
        link.send([
            "kind": "navigation.route", "sessionId": sessionId, "requestId": v.requestId,
            "generation": v.generation, "routeId": r.routeId, "revision": routeRevision,
            "destinationLabel": destination.label.utf16Prefix(96), "etaMinutes": r.etaMinutes, "plan": r.plan,
        ])
        Log.add("tx navigation.route revision=\(routeRevision) \(r.routeId)")
    }

    private func handleRouteAck(routeId: String, revision: Int, outcome: String) {
        guard let r = route, let attempt = installAttempt, attempt.routeId == routeId, attempt.revision == revision,
              r.routeId == routeId, revision == routeRevision else { return }
        installAttempt = nil
        Log.add("navigation route \(outcome) after " + String(format: "%.1fs", Date().timeIntervalSince(routeSentAt)))
        if outcome == "installed" {
            routeInstalled = true
            lastStateSequence = 0
            pendingRerouteRequestId = nil
            return
        }
        routeInstalled = false
        pendingRerouteRequestId = nil
        errorRecovery = "install"
        sendView(Self.view("error", "Navigation could not start", "The route was not installed on the phone",
                           hint: "tap/say retry · swipe cancel"), "install-error")
    }

    private func handleState(routeId: String, revision: Int, sequence: Int, state: String) {
        guard !arrived, routeInstalled, let r = route, r.routeId == routeId, revision == routeRevision,
              sequence > lastStateSequence else { return }
        lastStateSequence = sequence
        if state == "arrived" {
            arrived = true
            routeInstalled = false
            installAttempt = nil
            pendingRerouteRequestId = nil
            Log.add("navigation arrived")
        }
    }

    private func handleReroute(routeId: String, baseRevision: Int, requestId: String, point: NavPoint) {
        guard !arrived, routeInstalled, let r = route, r.routeId == routeId, baseRevision == routeRevision,
              pendingRerouteRequestId == nil, point.isValid else { return }
        pendingRerouteRequestId = requestId
        routeInstalled = false
        lastStateSequence = 0
        sendView(Self.view("rerouting", "Rerouting", "Finding a safer path", hint: "Old directions paused · say stop",
                           interaction: "none", mode: mode), "rerouting")
        guard let v = currentView else { return }
        // Route only once the paused page is on the glass.
        rerouteAfterAck = (point, v.generation, routeRevision + 1, v.requestId)
    }
}
