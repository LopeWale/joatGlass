import Foundation

/// JOAT's own notifications on the phone, and the resting dashboard's margin
/// facts. Ports, by file:
///   channels/cards.py     Card, CardLog (25 kept, read/unread by card id,
///                         persistent origin tombstones across reconnects)
///   services/session.py   the Main card, the Tray's two previous rows,
///                         notification_stack → hud.notifications
///   services/ambient.py   hud.ambient: temperature, sky word, unread count
///   capabilities.py       the Notifications page (list, detail, clear)
///
/// What the phone cannot do: read other apps' notifications. These cards are
/// the ones JOAT itself raises (Pomodoro, reminders), Gmail arrivals, and
/// texts relayed from the Mac or Shortcuts automations (Messages.swift).
final class Cards: BrainFeature {
    let name = "cards"
    let uplinkKinds: Set<String> = ["input", "location"]

    struct Card: Codable, Equatable {
        let id: String
        let source: String
        let channel: String
        let summary: String
        let body: String
        let at: Date
        var originId: String? = nil

        /// cards.py Card.render: who, then what.
        var render: String {
            var header = source.isEmpty ? channel : source
            if !channel.isEmpty, channel.lowercased() != header.lowercased() { header += " · \(channel)" }
            return "\(Hud.shorten(header))\n\(Hud.shorten(summary))"
        }

        /// cards.py Card.home_row: "Source · age · headline".
        func homeRow(now: Date = Date()) -> String {
            let headline = String(summary.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(96))
            return "\(source.isEmpty ? channel : source) · \(Cards.age(at, now: now)) · \(headline)"
        }
    }

    private struct PersistedStore: Codable {
        var entries: [Card]
        var acknowledged: [String]
        var dismissedOriginIds: [String]
    }

    static let storeKey = "joat.cards.store.v2"
    /// Matched 1:1 across `hud.notifications`, `hud.ambient`, and `NOTIFICATIONS` list.
    static let keep = 25
    static let dismissedOriginKeep = 200

    private let link: PluginLink
    private let defaults: UserDefaults
    private(set) var entries: [Card] = []
    private var acknowledged: Set<String> = []
    /// Bounded FIFO tombstones of dismissed `originId`s so reconnecting the
    /// plugin or restarting JOAT Brain never resurrects dismissed notifications.
    private(set) var dismissedOriginIds: [String] = []
    /// The card Main is showing; the tray lists the ones before it.
    private var mainCardId: String?

    init(link: PluginLink, defaults: UserDefaults = .standard) {
        self.link = link
        self.defaults = defaults
        loadPersisted()
    }

    private func loadPersisted() {
        guard let data = defaults.data(forKey: Self.storeKey) else { return }
        do {
            let decoded = try JSONDecoder().decode(PersistedStore.self, from: data)
            entries = Array(decoded.entries.suffix(Self.keep))
            acknowledged = Set(decoded.acknowledged).intersection(entries.map(\.id))
            var seen = Set<String>()
            var dedupedOrigins: [String] = []
            for raw in decoded.dismissedOriginIds.reversed() {
                let clean = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                if !clean.isEmpty, seen.insert(clean).inserted {
                    dedupedOrigins.append(clean)
                }
            }
            dismissedOriginIds = Array(dedupedOrigins.reversed().suffix(Self.dismissedOriginKeep))
        } catch {
            if defaults === UserDefaults.standard {
                SafeStore.quarantine(defaultsKey: Self.storeKey, data, error)
            } else {
                defaults.removeObject(forKey: Self.storeKey)
            }
        }
    }

    private func recordDismissedOrigin(_ originId: String?) {
        guard let clean = originId?.trimmingCharacters(in: .whitespacesAndNewlines),
              !clean.isEmpty else { return }
        if let idx = dismissedOriginIds.firstIndex(of: clean) {
            dismissedOriginIds.remove(at: idx)
        }
        dismissedOriginIds.append(clean)
        if dismissedOriginIds.count > Self.dismissedOriginKeep {
            dismissedOriginIds.removeFirst(dismissedOriginIds.count - Self.dismissedOriginKeep)
        }
    }

    private func persist() {
        let payload = PersistedStore(
            entries: entries,
            acknowledged: Array(acknowledged).sorted(),
            dismissedOriginIds: dismissedOriginIds
        )
        if let data = try? JSONEncoder().encode(payload) {
            defaults.set(data, forKey: Self.storeKey)
        }
    }

    // MARK: - Raising a card

    func post(
        source: String,
        channel: String,
        summary: String,
        body: String = "",
        at: Date = Date(),
        originId: String? = nil
    ) {
        let cleanOrigin: String? = {
            guard let raw = originId?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !raw.isEmpty else { return nil }
            return raw
        }()
        if let cleanOrigin {
            if dismissedOriginIds.contains(cleanOrigin) || entries.contains(where: { $0.originId == cleanOrigin }) {
                Log.add("cards: skipped duplicate/dismissed origin \(cleanOrigin)")
                return
            }
        }
        let card = Card(
            id: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
            source: source,
            channel: channel,
            summary: summary,
            body: body,
            at: at,
            originId: cleanOrigin
        )
        entries.append(card)
        if entries.count > Self.keep { entries.removeFirst(entries.count - Self.keep) }
        acknowledged.formIntersection(entries.map(\.id))
        persist()
        mainCardId = card.id
        // Publish `hud.notifications` + `hud.ambient` BEFORE `hud.text` so the
        // plugin's `notificationStack` already holds the card's true `at`
        // timestamp, full `body`, and 1:1 unread count when `popNotification` renders!
        publish()
        link.send(["kind": "hud.text", "containerId": 2, "text": card.render,
                   "textColor": 3, "contentKind": "notification", "traceId": card.id])
        Log.add("tx card \(card.source) · \(card.channel): \(card.summary)")
    }

    var active: [Card] { entries.filter { !acknowledged.contains($0.id) } }

    @discardableResult
    func acknowledge(_ id: String) -> Bool {
        guard let card = entries.first(where: { $0.id == id }) else { return false }
        let inserted = acknowledged.insert(id).inserted
        recordDismissedOrigin(card.originId)
        if inserted || card.originId != nil {
            persist()
        }
        return inserted
    }

    /// Clear what the page showed, never a card that arrived after it.
    func dismiss(_ ids: [String]) {
        let idSet = Set(ids)
        let wasActive = Set(active.map(\.id))
        for card in entries where idSet.contains(card.id) {
            acknowledged.insert(card.id)
            recordDismissedOrigin(card.originId)
        }
        persist()
        ids.filter(wasActive.contains).forEach(retire)
        publish()
    }

    /// Retire only this card's Main presentation (capabilities._retire_notification).
    func retire(_ id: String) {
        link.send(["kind": "hud.text", "containerId": 2, "text": "", "contentKind": "notification", "traceId": id])
        if mainCardId == id { mainCardId = nil }
    }

    /// hud.notifications, the tray and the unread count, after any change.
    func publish() {
        let unread = active
        link.send(["kind": "hud.notifications",
                   "cards": unread.reversed().prefix(Self.keep).map { c -> [String: Any] in
                       var body = (c.body.isEmpty ? c.summary : c.body).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
                       if body.count > 600 { body = String(body.prefix(599)).trimmingCharacters(in: .whitespaces) + "…" }
                       return ["id": c.id, "app": String(c.channel.prefix(48)), "sender": String(c.source.prefix(48)),
                               "body": body, "at": Int(c.at.timeIntervalSince1970 * 1000), "urgent": false]
                   }])
        let previous = unread.reversed().filter { $0.id != mainCardId }.prefix(2)
        link.send(["kind": "hud.text", "containerId": 6, "text": previous.map { $0.homeRow() }.joined(separator: "\n")])
        publishAmbient()
    }

    // MARK: - BrainFeature

    func pluginConnected() {
        // A new connection has no identified Main card (reset_home_notification_identity).
        mainCardId = nil
        publish()
    }

    func handle(kind: String, message: [String: Any]) {
        switch kind {
        case "input":
            guard let event = message.object("event"), event.string("containerName") == "notification-dismiss",
                  let id = event.string("traceId") else { return }
            if acknowledge(id) {
                retire(id)
                publish()
            }
        case "location":
            if let lat = (message["latitude"] as? NSNumber)?.doubleValue,
               let lon = (message["longitude"] as? NSNumber)?.doubleValue {
                acceptPosition(lat, lon)
            }
        default:
            break
        }
    }

    // MARK: - Weather (services/ambient.py)

    static let refreshSeconds: TimeInterval = 900
    static let positionMaxAge: TimeInterval = 3600
    private var position: (Double, Double)?
    private var positionAt: Date?
    private var temperature = ""
    private var condition = ""
    private var fetchedAt: Date?
    private var fetching = false

    private func acceptPosition(_ lat: Double, _ lon: Double) {
        position = (lat, lon)
        positionAt = Date()
        if !fetching, fetchedAt.map({ Date().timeIntervalSince($0) >= Self.refreshSeconds }) ?? true {
            refreshWeather()
        }
    }

    private func publishAmbient() {
        if let at = positionAt, Date().timeIntervalSince(at) > Self.positionMaxAge {
            temperature = ""
            condition = ""
        }
        link.send(["kind": "hud.ambient", "temperature": temperature, "condition": condition,
                   "unread": max(0, min(999, active.count))])
    }

    private func refreshWeather() {
        guard let (lat, lon) = position else { return }
        fetching = true
        var c = URLComponents(string: "https://api.open-meteo.com/v1/forecast")!
        c.queryItems = [
            URLQueryItem(name: "latitude", value: String(format: "%.4f", lat)),
            URLQueryItem(name: "longitude", value: String(format: "%.4f", lon)),
            URLQueryItem(name: "current", value: "temperature_2m,weather_code"),
            URLQueryItem(name: "temperature_unit", value: "fahrenheit"),
        ]
        var request = URLRequest(url: c.url!, timeoutInterval: 8)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("JOAT-G2/0.1", forHTTPHeaderField: "User-Agent")
        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.fetching = false
                guard error == nil, let data,
                      let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let current = doc["current"] as? [String: Any],
                      let raw = (current["temperature_2m"] as? NSNumber)?.doubleValue else {
                    Log.add("ambient weather unavailable: \(error.map { "\($0)" } ?? "no temperature")")
                    return
                }
                self.temperature = "\(Int(raw.rounded()))f"
                self.condition = Self.sky[(current["weather_code"] as? NSNumber)?.intValue ?? -1] ?? ""
                self.fetchedAt = Date()
                Log.add("ambient weather \(self.temperature) \(self.condition)")
                self.publishAmbient()
            }
        }.resume()
    }

    /// ambient.py _SKY: WMO codes reduced to one margin word.
    static let sky: [Int: String] = [
        0: "clear", 1: "clear", 2: "cloudy", 3: "overcast", 45: "fog", 48: "fog",
        51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "freezing", 57: "freezing",
        61: "rain", 63: "rain", 65: "heavy rain", 66: "freezing", 67: "freezing",
        71: "snow", 73: "snow", 75: "heavy snow", 77: "snow",
        80: "showers", 81: "showers", 82: "storm", 85: "snow", 86: "snow",
        95: "storm", 96: "storm", 99: "storm",
    ]

    /// capabilities._age
    static func age(_ at: Date, now: Date = Date()) -> String {
        let mins = max(0, Int(now.timeIntervalSince(at) / 60))
        if mins < 1 { return "now" }
        if mins < 60 { return "\(mins)m" }
        let hours = mins / 60
        return hours < 24 ? "\(hours)h" : "\(hours / 24)d"
    }
}
