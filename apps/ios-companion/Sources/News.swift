import Foundation

/// The News rail tab on the phone: services/news.py NewsService and the
/// news half of services/panel_commands.py. A story is listed only once
/// its page was read in full here, so every row opens; the list is shared
/// evenly across the wearer's subjects, newest first, twenty at most.
final class News: PanelProvider, VoiceCommand {
    let tab = "news"

    static let maxHeadlines = 20
    static let minAttempts = 10
    static let attemptFactor = 3
    static let verifyWorkers = 8
    static let verifyPerHost = 2
    static let verifyDeadline: TimeInterval = 45
    static let firstPassDeadline: TimeInterval = 20
    static let catchUpDelay: TimeInterval = 60
    static let maxCatchUps = 3
    static let badArticleTTL: TimeInterval = 30 * 60
    static let maxCachedArticles = 600
    static let feedFailuresToBench = 3
    static let feedBenchBase: TimeInterval = 3600
    static let feedBenchMax: TimeInterval = 12 * 3600
    static let feedUnreadableToBench = 6
    static let feedUnreadableBench: TimeInterval = 24 * 3600
    static let userAgent = "JOAT-G2/0.1 (+personal reader)"

    enum Status { case read, unreadable, transient }
    struct Article { let title: String; let text: String; let status: Status; var at = Date() }
    struct Health { var failures = 0; var unreadable = 0; var benchedUntil = Date.distantPast; var reason = "" }

    private(set) var headlines: [Headline] = []
    private var fetchedAt: Date?
    private var lastError: String?
    private var noReadable = false
    private var incomplete = false
    private var articles: [String: Article] = [:]
    private var health: [String: Health] = [:]
    private var running = false
    private var passing = false

    /// Push the tab; `activate` brings it up on the glasses (a voice open).
    var publish: ((_ activate: Bool) -> Void)?
    /// Send an article reader under the tab's current request id.
    var sendReader: ((_ list: [String: Any]) -> Void)?
    /// Speak through the phone.
    var say: ((String) -> Void)?

    private let feedSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 12
        c.httpAdditionalHeaders = ["User-Agent": News.userAgent]
        return URLSession(configuration: c)
    }()
    private let pageSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 12
        c.httpAdditionalHeaders = ["User-Agent": News.userAgent]
        // Story pages are the heavy part: Low Data Mode skips them.
        c.allowsConstrainedNetworkAccess = false
        return URLSession(configuration: c)
    }()

    // MARK: - Polling (NewsService.run)

    func start() {
        guard !running else { return }
        running = true
        Task { @MainActor in
            var first = true
            var catchUps = 0
            while self.running {
                let changed = await self.refreshChanged(force: false)
                if changed || first { self.publish?(false) }
                first = false
                if self.incomplete, catchUps < Self.maxCatchUps {
                    catchUps += 1
                    try? await Task.sleep(nanoseconds: UInt64(Self.catchUpDelay * 1e9))
                } else {
                    catchUps = 0
                    try? await Task.sleep(nanoseconds: UInt64(Double(NewsConfig.load().refreshMinutes) * 60 * 1e9))
                }
            }
        }
    }

    // MARK: - One pass (_refresh_changed)

    @MainActor
    func refreshChanged(force: Bool) async -> Bool {
        while passing { try? await Task.sleep(nanoseconds: 200_000_000) }
        passing = true
        defer { passing = false }
        let started = Date()
        let config = NewsConfig.load()
        let feeds = config.feeds
        let now = Date()
        if force { for k in health.keys { health[k]?.failures = 0; health[k]?.unreadable = 0; health[k]?.benchedUntil = .distantPast } }
        let live = feeds.filter { !benched($0.1, now) }
        var names: [String: String] = [:]
        for s in config.enabledSources { names[s.url] = s.name }

        var results: [(String, String, Result<[Headline], Error>)] = []
        await withTaskGroup(of: (String, String, Result<[Headline], Error>).self) { group in
            for (category, url) in live {
                group.addTask { (category, url, await self.fetchFeed(url, category: category)) }
            }
            for await r in group { results.append(r) }
        }
        var candidates: [Headline] = []
        var failed: [String] = []
        for (category, url, result) in results {
            switch result {
            case .failure(let e):
                failed.append(url)
                Log.add("feed unavailable category=\(category) host=\(URL(string: url)?.host ?? "?") (\(e))")
            case .success(let list):
                health[url, default: Health()].failures = 0
                candidates += list.map { var h = $0; h.source = names[url] ?? h.source; h.feed = url; return h }
            }
        }
        if !live.isEmpty, failed.count < live.count { failed.forEach { feedFailed($0, now) } }
        let allFailed = !feeds.isEmpty && failed.count == live.count
        let before = (headlines.map(\.guid), lastError, noReadable)
        if allFailed {
            lastError = "feeds unavailable"
            incomplete = false
        } else {
            let deadline = (force || headlines.isEmpty) ? Self.firstPassDeadline : Self.verifyDeadline
            let bySubject = groupBySubject(candidates, config: config, now: now)
            let (listed, complete) = await select(bySubject, deadline: deadline, now: now)
            incomplete = !complete
            if !listed.isEmpty || complete || headlines.isEmpty {
                headlines = listed
                noReadable = !candidates.isEmpty && listed.isEmpty && complete
                fetchedAt = Date()
                lastError = nil
                prune(Set(bySubject.flatMap { $0.1.map(\.link) }), now: now)
            }
        }
        let after = (headlines.map(\.guid), lastError, noReadable)
        Log.add("news pass feeds=\(feeds.count) answered=\(live.count - failed.count) benched=\(feeds.count - live.count) listed=\(headlines.count) incomplete=\(incomplete) in \(String(format: "%.1f", Date().timeIntervalSince(started)))s")
        return before.0 != after.0 || before.1 != after.1 || before.2 != after.2
    }

    /// fetch_feed: one feed → its headlines, newest first.
    private func fetchFeed(_ url: String, category: String) async -> Result<[Headline], Error> {
        do {
            let data = try await get(url, session: feedSession,
                                     accept: "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
                                     limit: 2_000_000)
            let (feedTitle, entries) = FeedParser.parse(data)
            let now = Date()
            var out: [Headline] = []
            for e in entries {
                var title = NewsTidy.visible(e.title).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
                title = HTMLText.decode(title)
                let link = e.link.trimmingCharacters(in: .whitespacesAndNewlines)
                if title.isEmpty || link.isEmpty || NewsTidy.search(NewsTidy.promo, title) || NewsTidy.search(NewsTidy.promoLink, link) { continue }
                let published = FeedParser.date(e.date) ?? FeedParser.date(e.updated) ?? now
                if !e.source.isEmpty { title = NewsTidy.stripPublisher(title) }
                out.append(Headline(title: title, link: link, source: Self.sourceName(feedTitle, e.source),
                                    category: category, publishedAt: published, guid: e.id.isEmpty ? link : e.id))
            }
            return .success(out.sorted { $0.publishedAt > $1.publishedAt })
        } catch {
            return .failure(error)
        }
    }

    struct HTTPStatus: Error { let code: Int }
    struct TooLarge: Error {}

    private func get(_ url: String, session: URLSession, accept: String, limit: Int) async throws -> Data {
        guard let u = URL(string: url) else { throw URLError(.badURL) }
        var request = URLRequest(url: u)
        request.setValue(accept, forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request, delegate: SecureRedirects.shared)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) { throw HTTPStatus(code: http.statusCode) }
        if data.count > limit { throw TooLarge() }
        return data
    }

    /// fetch_article_text: one story page → (title, tidied text).
    private func fetchArticle(_ url: String) async throws -> (String, String) {
        let data = try await get(url, session: pageSession, accept: "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", limit: 8_000_000)
        let page = String(decoding: data, as: UTF8.self)
        let extracted = HTMLText.extract(page)
        let title = NewsTidy.title(extracted.title, site: extracted.site)
        return (title, NewsTidy.article(extracted.text, title: title))
    }

    /// _blames_the_page: a 4xx other than "slow down", or the size cap.
    private func blamesPage(_ e: Error) -> Bool {
        if let s = e as? HTTPStatus { return (400..<500).contains(s.code) && ![408, 425, 429].contains(s.code) }
        return e is TooLarge
    }

    private func readArticle(_ story: Headline) async -> Article {
        do {
            let (title, raw) = try await fetchArticle(story.link)
            let text = NewsTidy.trimLead(raw, title: story.title)
            return Article(title: title, text: text, status: NewsTidy.readable(text) ? .read : .unreadable)
        } catch {
            return Article(title: "", text: "", status: blamesPage(error) ? .unreadable : .transient)
        }
    }

    // MARK: - Choosing the list (_select)

    private func groupBySubject(_ candidates: [Headline], config: NewsConfig, now: Date) -> [(String, [Headline])] {
        // Once each: a category listed twice in news.json made select()'s
        // Dictionary(uniqueKeysWithValues:) trap on every pass (review, 2026-10-30).
        var order: [String] = []
        for c in config.categories + ["custom"] where !order.contains(c) { order.append(c) }
        var map: [String: [Headline]] = [:]
        var seen = Set<String>()
        for var h in candidates.sorted(by: { $0.publishedAt > $1.publishedAt }) {
            let words = NewsTidy.stripPublisher(h.title).lowercased()
                .split(whereSeparator: { !($0.isASCII && ($0.isLetter || $0.isNumber)) }).prefix(8).joined(separator: " ")
            let key = words.isEmpty ? h.link : words
            if seen.contains(key) { continue }
            seen.insert(key)
            if h.publishedAt > now.addingTimeInterval(3600) { h.publishedAt = now }
            if !order.contains(h.category) { order.append(h.category) }
            map[h.category, default: []].append(h)
        }
        return order.compactMap { s in map[s].flatMap { $0.isEmpty ? nil : (s, $0) } }
    }

    private func targets(_ subjects: [String], kept: [String: [Headline]], exhausted: Set<String>) -> [String: Int] {
        let held = exhausted.reduce(0) { $0 + (kept[$1]?.count ?? 0) }
        let live = subjects.filter { !exhausted.contains($0) }
        guard !live.isEmpty else { return [:] }
        let total = max(0, Self.maxHeadlines - held)
        let share = total / live.count, extra = total % live.count
        var out: [String: Int] = [:]
        for (i, s) in live.enumerated() { out[s] = share + (i < extra ? 1 : 0) }
        return out
    }

    @MainActor
    private func select(_ bySubject: [(String, [Headline])], deadline: TimeInterval, now: Date) async -> ([Headline], Bool) {
        let subjects = bySubject.map(\.0)
        guard !subjects.isEmpty else { return ([], true) }
        var queues = Dictionary(uniqueKeysWithValues: bySubject)
        var kept: [String: [Headline]] = Dictionary(uniqueKeysWithValues: subjects.map { ($0, []) })
        var taken: [String: [String: Int]] = Dictionary(uniqueKeysWithValues: subjects.map { ($0, [:]) })
        var tried: [String: Int] = Dictionary(uniqueKeysWithValues: subjects.map { ($0, 0) })
        let share = (Self.maxHeadlines + subjects.count - 1) / subjects.count
        let attempts = max(Self.minAttempts, Self.attemptFactor * share)
        let feedCap = max(2, (share + 2) / 3)
        var exhausted = Set<String>()
        let stopAt = Date().addingTimeInterval(deadline)
        var complete = true
        while exhausted.count < subjects.count {
            for s in subjects { queues[s] = queues[s]?.filter { !benched($0.feed, now) } }
            let t = targets(subjects, kept: kept, exhausted: exhausted)
            let ranDry = t.filter { s, target in target > (kept[s]?.count ?? 0) && ((tried[s] ?? 0) >= attempts || (queues[s] ?? []).isEmpty) }.map(\.key)
            if !ranDry.isEmpty { exhausted.formUnion(ranDry); continue }
            var picks: [(String, Headline)] = []
            for (s, target) in t {
                let want = min(target - (kept[s]?.count ?? 0), attempts - (tried[s] ?? 0))
                guard want > 0 else { continue }
                var q = queues[s] ?? []
                var tk = taken[s] ?? [:]
                let picked = Self.pick(&q, want, feedCap, &tk)
                queues[s] = q
                taken[s] = tk
                tried[s, default: 0] += picked.count
                picks += picked.map { (s, $0) }
            }
            if picks.isEmpty { break }
            let outcomes = await readWave(picks.map(\.1), stopAt: stopAt)
            for ((s, story), outcome) in zip(picks, outcomes) {
                guard let (article, fresh) = outcome else {
                    taken[s]?[story.feed, default: 1] -= 1
                    complete = false
                    continue
                }
                if fresh, article.status != .transient { articles[story.link] = Article(title: article.title, text: article.text, status: article.status, at: now) }
                noteRead(story.feed, article, fresh: fresh, now: now)
                if article.status == .read { kept[s, default: []].append(story) } else { taken[s]?[story.feed, default: 1] -= 1 }
            }
            if !complete { break }
        }
        let listed = kept.values.flatMap { $0 }.sorted { $0.publishedAt > $1.publishedAt }
        return (Array(listed.prefix(Self.maxHeadlines)), complete)
    }

    /// _pick: newest first, at most feedCap from one feed while others have stories.
    private static func pick(_ queue: inout [Headline], _ count: Int, _ feedCap: Int, _ taken: inout [String: Int]) -> [Headline] {
        var chosen: [Int] = []
        for relaxed in [false, true] {
            for (i, story) in queue.enumerated() {
                if chosen.count >= count { break }
                if chosen.contains(i) || (!relaxed && (taken[story.feed] ?? 0) >= feedCap) { continue }
                chosen.append(i)
                taken[story.feed, default: 0] += 1
            }
        }
        let set = Set(chosen)
        let picked = chosen.sorted().map { queue[$0] }
        queue = queue.enumerated().filter { !set.contains($0.offset) }.map(\.element)
        return picked
    }

    /// _read_wave: at most 8 reads at once, 2 per publisher; none starts after the deadline.
    @MainActor
    private func readWave(_ stories: [Headline], stopAt: Date) async -> [(Article, Bool)?] {
        var results = [(Article, Bool)?](repeating: nil, count: stories.count)
        var pending: [Int] = []
        for (i, s) in stories.enumerated() {
            if let c = cached(s.link) { results[i] = (c, false) } else { pending.append(i) }
        }
        var active = 0
        var perHost: [String: Int] = [:]
        await withTaskGroup(of: (Int, Article).self) { group in
            var queue = pending
            func launch() {
                var i = 0
                while i < queue.count, active < Self.verifyWorkers, Date() < stopAt {
                    let index = queue[i]
                    let host = URL(string: stories[index].link)?.host ?? ""
                    if perHost[host, default: 0] >= Self.verifyPerHost { i += 1; continue }
                    queue.remove(at: i)
                    active += 1
                    perHost[host, default: 0] += 1
                    let story = stories[index]
                    group.addTask { (index, await self.readArticle(story)) }
                }
            }
            launch()
            for await (index, article) in group {
                results[index] = (article, true)
                active -= 1
                let host = URL(string: stories[index].link)?.host ?? ""
                perHost[host, default: 1] -= 1
                launch()
            }
        }
        return results
    }

    private func cached(_ link: String) -> Article? {
        guard let a = articles[link] else { return nil }
        return a.status == .read || Date().timeIntervalSince(a.at) < Self.badArticleTTL ? a : nil
    }

    private func prune(_ live: Set<String>, now: Date) {
        for (link, a) in articles {
            let keep = a.status == .read ? live.contains(link) : now.timeIntervalSince(a.at) < Self.badArticleTTL
            if !keep { articles.removeValue(forKey: link) }
        }
        let overflow = articles.count - Self.maxCachedArticles
        if overflow > 0 {
            for link in articles.sorted(by: { $0.value.at < $1.value.at }).prefix(overflow).map(\.key) { articles.removeValue(forKey: link) }
        }
    }

    private func benched(_ url: String, _ now: Date) -> Bool { (health[url]?.benchedUntil ?? .distantPast) > now }

    private func feedFailed(_ url: String, _ now: Date) {
        var h = health[url] ?? Health()
        h.failures += 1
        if h.failures >= Self.feedFailuresToBench {
            let wait = min(Self.feedBenchMax, Self.feedBenchBase * pow(2, Double(h.failures - Self.feedFailuresToBench)))
            h.benchedUntil = now.addingTimeInterval(wait)
            h.reason = "unavailable (\(h.failures) failures in a row)"
            Log.add("feed benched for \(Int(wait / 60))m host=\(URL(string: url)?.host ?? "?")")
        }
        health[url] = h
    }

    private func noteRead(_ url: String, _ a: Article, fresh: Bool, now: Date) {
        guard fresh, !url.isEmpty, a.status != .transient else { return }
        var h = health[url] ?? Health()
        if a.status == .read { h.unreadable = 0; health[url] = h; return }
        h.unreadable += 1
        if h.unreadable >= Self.feedUnreadableToBench {
            h.benchedUntil = now.addingTimeInterval(Self.feedUnreadableBench)
            h.reason = "its stories cannot be read in full"
            h.unreadable = 0
            Log.add("feed benched for 24h host=\(URL(string: url)?.host ?? "?") (stories not readable in full)")
        }
        health[url] = h
    }

    // MARK: - The tab (snapshot, status_rows, open)

    var statusRows: Int { !headlines.isEmpty && lastError != nil && fetchedAt != nil ? 1 : 0 }

    func snapshot() -> PanelSnapshot {
        let now = Date()
        if headlines.isEmpty {
            let empty = lastError != nil ? "Feeds unavailable — hold to retry."
                : noReadable ? "No stories readable in full right now — hold to retry."
                : "No headlines yet — long-press to refresh."
            return PanelSnapshot(rows: [], empty: empty)
        }
        let rows = headlines.map { $0.row(now: now) }
        if lastError != nil, let at = fetchedAt {
            return PanelSnapshot(rows: ["[!] Feeds unavailable · showing headlines from \(Self.relativeAge(at, now: now)) ago"] + rows)
        }
        return PanelSnapshot(rows: rows)
    }

    func refresh(completion: @escaping () -> Void) {
        Task { @MainActor in _ = await self.refreshChanged(force: false); completion() }
    }

    /// NewsService.open: the reader, from the read kept when the list was built.
    func open(index: Int, completion: @escaping (Bool) -> Void) {
        guard headlines.indices.contains(index) else { completion(false); return }
        let h = headlines[index]
        Task { @MainActor in
            var title: String, text: String
            if let a = self.articles[h.link], a.status == .read {
                title = a.title; text = a.text
            } else {
                do { (title, text) = try await self.fetchArticle(h.link) } catch { title = h.title; text = "" }
                text = NewsTidy.trimLead(text, title: h.title)
            }
            let body = text.isEmpty ? "The article text could not be read on the phone." : text
            let metadata = "\(h.source) · \(Self.relativeAge(h.publishedAt))"
            let label = metadata.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            let headline = title.isEmpty ? h.title : title
            self.sendReader?([
                "kind": "hud.list", "items": [String](), "selectable": false,
                "body": "\(headline)\n\(label.isEmpty ? "Article" : label)\n\n\(body)",
                "title": "NEWS · ARTICLE", "hint": CapabilityMenu.backHint,
            ])
            completion(true)
        }
    }

    // MARK: - Voice (panel_commands.py NewsCommands)

    static let tellRe = Grammar.regex(#"^(?:what(?:'s| is| are)(?: in)? the news|any news|(?:read|tell) me the news|(?:read|tell me) the headlines|(?:what are|read) the (?:latest )?headlines|(?:the )?latest news|news headlines|what(?:'s| is) (?:the )?(?:latest|new)(?: in the news)?)$"#)
    static let refreshRe = Grammar.regex(#"^(?:refresh|update|check|reload) (?:the |my )?(?:news|headlines|feeds?)$"#)
    static let openRe = Grammar.regex(#"^(?:open|show)(?: me)? (?:the |my )?(?:news|headlines)$"#)
    static let ordinal = #"(?<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d{1,2}(?:st|nd|rd|th)?|one|two|three|four|five|six|seven|eight|nine|ten)"#
    static let readNthRe = Grammar.regex(#"^(?:read|open)(?: me)? (?:the )?"# + ordinal + #" (?:story|headline|article|one)$"#)
    static let ordinals: [String: Int] = [
        "first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
        "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4, "sixth": 5, "6th": 5, "six": 5,
        "seventh": 6, "7th": 6, "seven": 6, "eighth": 7, "8th": 7, "eight": 7, "ninth": 8, "9th": 8, "nine": 8,
        "tenth": 9, "10th": 9, "ten": 9, "last": -1,
    ]
    static func ordinalIndex(_ token: String) -> Int? {
        if let v = ordinals[token.lowercased()] { return v }
        let digits = token.prefix(while: \.isNumber)
        return Int(digits).map { $0 - 1 }
    }

    func handle(_ request: String) -> CommandReply? {
        let t = Grammar.normalize(request)
        if Grammar.match(Self.openRe, t) != nil {
            publish?(true)
            return CommandReply(text: "Opening the news.", activated: true)
        }
        if Grammar.match(Self.tellRe, t) != nil {
            publish?(true)
            return CommandReply(text: spokenHeadlines(5), activated: true)
        }
        if Grammar.match(Self.refreshRe, t) != nil {
            Task { @MainActor in
                _ = await self.refreshChanged(force: true)
                self.publish?(true)
                let n = self.headlines.count
                let text = n == 0 ? "I couldn't get any headlines right now."
                    : self.lastError != nil ? "The feeds didn't answer, so these are the \(n) headlines from earlier. \(self.spokenHeadlines(3))"
                    : "News updated: \(n) headlines. \(self.spokenHeadlines(3))"
                self.say?(text)
            }
            return CommandReply(text: "", activated: true)
        }
        if let g = Grammar.match(Self.readNthRe, t, groups: ["ordinal"]), let token = g["ordinal"], var i = Self.ordinalIndex(token) {
            if i < 0 { i = headlines.count - 1 }
            guard headlines.indices.contains(i) else { return CommandReply(text: "There is no story at that number.", activated: false) }
            let h = headlines[i]
            publish?(true)
            open(index: i) { _ in }
            return CommandReply(text: "Opening \(h.title), from \(h.source).", activated: true)
        }
        return nil
    }

    func spokenHeadlines(_ limit: Int) -> String {
        guard !headlines.isEmpty else { return "I don't have any headlines yet." }
        let parts = headlines.prefix(limit).enumerated().map { n, h in "\(n + 1). \(h.title)\(h.source.isEmpty ? "" : " from \(h.source)")." }
        return "Here are the headlines. " + parts.joined(separator: " ")
    }

    // MARK: - Shared text helpers (news.py relative_age, shorten, _source_name)

    static func relativeAge(_ at: Date, now: Date = Date()) -> String {
        let e = max(0, now.timeIntervalSince(at))
        if e < 60 { return "now" }
        if e < 3600 { return "\(Int(e / 60))m" }
        if e < 86_400 { return "\(Int(e / 3600))h" }
        return "\(Int(e / 86_400))d"
    }

    static func shorten(_ text: String, _ limit: Int) -> String {
        let t = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if t.count <= limit { return t }
        let head = String(t.prefix(max(1, limit - 1)))
        var cut = head.contains(" ") ? String(head[..<head.lastIndex(of: " ")!]) : head
        if cut.isEmpty { cut = head }
        while let l = cut.last, ",.;: ".contains(l) { cut.removeLast() }
        return cut + "…"
    }

    static let sourceNames: [(String, String)] = [
        ("bbc", "BBC"), ("npr", "NPR"), ("google news", "Google News"), ("guardian", "Guardian"),
        ("new york times", "NYT"), ("nyt", "NYT"), ("al jazeera", "Al Jazeera"), ("reuters", "Reuters"),
        ("associated press", "AP"), ("ars technica", "Ars Technica"), ("hacker news", "Hacker News"), ("espn", "ESPN"),
    ]

    static func sourceName(_ feedTitle: String, _ entrySource: String) -> String {
        let candidate = entrySource.isEmpty ? feedTitle : entrySource
        let lowered = candidate.lowercased()
        for (k, name) in sourceNames where lowered.contains(k) { return name }
        let head = candidate.components(separatedBy: try! NSRegularExpression(pattern: #"\s+[-–|:]\s+"#)).first ?? candidate
        return shorten(head.trimmingCharacters(in: .whitespaces), 18)
    }
}

private extension String {
    func components(separatedBy re: NSRegularExpression) -> [String] {
        var out: [String] = []
        var last = startIndex
        for m in re.matches(in: self, range: NSRange(startIndex..., in: self)) {
            guard let r = Range(m.range, in: self) else { continue }
            out.append(String(self[last..<r.lowerBound]))
            last = r.upperBound
        }
        out.append(String(self[last...]))
        return out
    }
}

/// Some publishers answer https with a redirect to plain http
/// (techradar.com/rss and tomshardware.com/feeds/all both 301 to
/// http://…/feeds.xml), and iOS refuses that hop (App Transport Security,
/// NSURLErrorDomain -1022). The same address over https answers, so a
/// redirect is followed on https instead.
final class SecureRedirects: NSObject, URLSessionTaskDelegate {
    static let shared = SecureRedirects()

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, url.scheme?.lowercased() == "http",
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            completionHandler(request)
            return
        }
        parts.scheme = "https"
        if parts.port == 80 { parts.port = nil }
        var secure = request
        secure.url = parts.url
        completionHandler(secure)
    }
}
