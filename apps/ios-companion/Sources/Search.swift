import Foundation

/// The Search tab on the phone: services/search.py SearchService and the
/// search grammar in panel_commands.py. Key-free, like the Mac's ddgs (the
/// wearer's ruling: no paid API): DuckDuckGo's HTML results page. Opening
/// a result reads that page's text with the same extractor as News.
final class Search: PanelProvider, VoiceCommand {
    let tab = "search"
    let statusRows = 0

    struct Result { let title: String; let url: String; let snippet: String
        var domain: String {
            let host = URL(string: url)?.host?.lowercased() ?? ""
            return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
        }
        /// SearchResult.row: "Title · domain".
        var row: String {
            let suffix = domain.isEmpty ? "" : " · \(domain)"
            return News.shorten(title, max(12, 64 - suffix.count)) + suffix
        }
    }

    static let maxResults = 8
    static let controlRow = "⌕ Look something up"

    private(set) var query = ""
    private(set) var results: [Result] = []
    private var failed = false

    var publish: ((_ activate: Bool) -> Void)?
    var sendReader: ((_ list: [String: Any]) -> Void)?
    var say: ((String) -> Void)?
    /// Ask by voice; the next sentence is the answer (the control row).
    var askFollowUp: ((_ question: String, _ answer: @escaping (String) -> Void) -> Void)?

    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 8
        return URLSession(configuration: c)
    }()

    // MARK: - Lookup

    /// SearchService.lookup: run the query, return what JOAT should say.
    func lookup(_ raw: String, completion: @escaping (String) -> Void) {
        let cleaned = raw.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: " ?.!"))
        guard !cleaned.isEmpty else { completion("What should I look up?"); return }
        query = cleaned
        var request = URLRequest(url: URL(string: "https://html.duckduckgo.com/html/")!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
                         forHTTPHeaderField: "User-Agent")
        var form = URLComponents()
        form.queryItems = [URLQueryItem(name: "q", value: cleaned)]
        request.httpBody = form.percentEncodedQuery?.data(using: .utf8)
        session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                guard error == nil, let data, (response as? HTTPURLResponse)?.statusCode == 200 else {
                    Log.add("search unavailable: \(error.map { "\($0)" } ?? "status \((response as? HTTPURLResponse)?.statusCode ?? 0)")")
                    self.results = []
                    self.failed = true
                    completion("I couldn't reach a search engine for \(cleaned).")
                    return
                }
                self.results = Array(Self.parse(String(decoding: data, as: UTF8.self)).prefix(Self.maxResults))
                self.failed = false
                completion(self.spokenResults(3))
            }
        }.resume()
    }

    private static let resultRe = try! NSRegularExpression(
        pattern: #"<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]*class="result__a"|$)"#,
        options: [.caseInsensitive, .dotMatchesLineSeparators])
    private static let snippetRe = try! NSRegularExpression(
        pattern: #"class="result__snippet"[^>]*>(.*?)</a>"#, options: [.caseInsensitive, .dotMatchesLineSeparators])
    private static let tagRe = try! NSRegularExpression(pattern: "<[^>]+>")

    static func plain(_ html: String) -> String {
        let stripped = tagRe.stringByReplacingMatches(in: html, range: NSRange(html.startIndex..., in: html), withTemplate: "")
        return HTMLText.decode(stripped).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    static func parse(_ html: String) -> [Result] {
        var out: [Result] = []
        for m in resultRe.matches(in: html, range: NSRange(html.startIndex..., in: html)) {
            guard let h = Range(m.range(at: 1), in: html), let t = Range(m.range(at: 2), in: html),
                  let rest = Range(m.range(at: 3), in: html) else { continue }
            var url = HTMLText.decode(String(html[h]))
            // A redirect link carries the target in `uddg`.
            if let c = URLComponents(string: url.hasPrefix("//") ? "https:" + url : url),
               let target = c.queryItems?.first(where: { $0.name == "uddg" })?.value { url = target }
            if url.hasPrefix("//") { url = "https:" + url }
            let title = plain(String(html[t]))
            guard !title.isEmpty, url.hasPrefix("http://") || url.hasPrefix("https://"),
                  !(URL(string: url)?.host ?? "").contains("duckduckgo.com") else { continue }
            let tail = String(html[rest])
            let snippet = snippetRe.firstMatch(in: tail, range: NSRange(tail.startIndex..., in: tail))
                .flatMap { Range($0.range(at: 1), in: tail) }.map { plain(String(tail[$0])) } ?? ""
            out.append(Result(title: title, url: url, snippet: snippet))
        }
        return out
    }

    func spokenResults(_ limit: Int) -> String {
        guard !query.isEmpty else { return "Nothing has been looked up yet." }
        guard !results.isEmpty else { return "I found nothing for \(query)." }
        let parts = results.prefix(limit).enumerated().map { n, r in
            "\(n + 1). \(r.title), \(r.domain)\(r.snippet.isEmpty ? "" : ": \(News.shorten(r.snippet, 140))")."
        }
        return "For \(query), " + parts.joined(separator: " ")
    }

    // MARK: - PanelProvider

    func snapshot() -> PanelSnapshot {
        if query.isEmpty { return PanelSnapshot(rows: [Self.controlRow]) }
        if results.isEmpty {
            return PanelSnapshot(rows: [Self.controlRow], headline: "\(query) — \(failed ? "search unavailable" : "no results")")
        }
        return PanelSnapshot(rows: results.map(\.row) + [Self.controlRow], headline: query)
    }

    func refresh(completion: @escaping () -> Void) {
        guard !query.isEmpty else { completion(); return }
        lookup(query) { _ in completion() }
    }

    func open(index: Int, completion: @escaping (Bool) -> Void) {
        if index == results.count {
            // The control row: ask, then look up what was said.
            askFollowUp?("What should I look up?") { [weak self] answer in
                guard let self else { return }
                self.lookup(answer) { spoken in
                    self.publish?(true)
                    self.say?(spoken)
                }
            }
            completion(false)
            return
        }
        guard results.indices.contains(index) else { completion(false); return }
        let r = results[index]
        Task { @MainActor in
            var title = r.title, text = ""
            if let u = URL(string: r.url) {
                var request = URLRequest(url: u, timeoutInterval: 12)
                request.setValue("text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", forHTTPHeaderField: "Accept")
                request.setValue(News.userAgent, forHTTPHeaderField: "User-Agent")
                if let (data, response) = try? await URLSession.shared.data(for: request),
                   ((response as? HTTPURLResponse)?.statusCode ?? 0) / 100 == 2, data.count <= 8_000_000 {
                    let e = HTMLText.extract(String(decoding: data, as: UTF8.self))
                    let t = NewsTidy.title(e.title, site: e.site)
                    if !t.isEmpty { title = t }
                    text = NewsTidy.article(e.text, title: t)
                }
            }
            let body = text.isEmpty ? (r.snippet.isEmpty ? "The page text could not be read on the phone." : r.snippet) : text
            self.sendReader?([
                "kind": "hud.list", "items": [String](), "selectable": false,
                "body": "\(title)\n\(r.domain.isEmpty ? "Result" : r.domain)\n\n\(body)",
                "title": "SEARCH · RESULT", "hint": CapabilityMenu.backHint,
            ])
            completion(true)
        }
    }

    // MARK: - Voice (panel_commands.py SearchCommands)

    static let searchRe = Grammar.regex(#"^(?:look up|search(?: the web)?(?: for)?|google (?:for|search for)|web search(?: for)?|find out about|what does the internet say about) (?<query>.{2,120})$"#)
    static let openNthRe = Grammar.regex(#"^(?:open|read)(?: me)? (?:the )?"# + News.ordinal + #" (?:result|link|hit)$"#)
    static let openRe = Grammar.regex(#"^(?:open|show)(?: me)? (?:the |my )?(?:search|search results|results)$"#)

    func handle(_ request: String) -> CommandReply? {
        let t = Grammar.normalize(request)
        if Grammar.match(Self.openRe, t) != nil {
            publish?(true)
            return CommandReply(text: "Opening search. " + spokenResults(2), activated: true)
        }
        if let g = Grammar.match(Self.searchRe, t, groups: ["query"]), let q = g["query"] {
            // The wearer's own casing: the same span from the original words.
            var original = request.trimmingCharacters(in: .whitespacesAndNewlines)
            while let l = original.last, ".,!?".contains(l) { original.removeLast() }
            let query = String(original.suffix(q.count))
            lookup(query) { [weak self] spoken in
                self?.publish?(true)
                self?.say?(spoken)
            }
            return CommandReply(text: "", activated: true)
        }
        if let g = Grammar.match(Self.openNthRe, t, groups: ["ordinal"]), let token = g["ordinal"], var i = News.ordinalIndex(token) {
            if i < 0 { i = results.count - 1 }
            guard results.indices.contains(i) else { return CommandReply(text: "There is no result at that number.", activated: false) }
            let r = results[i]
            publish?(true)
            open(index: i) { _ in }
            return CommandReply(text: "Opening \(r.title), from \(r.domain).", activated: true)
        }
        return nil
    }
}
