import Foundation

/// The News pipeline's parts that do not depend on the glasses: the source
/// catalog and the wearer's choice, feed parsing (RSS 2.0, RDF, Atom), page
/// text extraction, and the Mac's tidy rules. Ports services/news.py; where
/// the Mac uses a Python library (feedparser, trafilatura) this is the
/// phone's own substitute, noted at each.

// MARK: - Catalog and config (news.py _load_catalog, NewsConfig)

struct NewsSource {
    let id: String
    let category: String
    let name: String
    let url: String
    let isDefault: Bool
}

enum NewsCatalog {
    /// news_sources.json, the same file the Mac engine and the Mac app's
    /// News page read (bundled from apps/mac/joat/services by project.yml).
    static let loaded: (byCategory: [String: [NewsSource]], order: [String], defaults: [String]) = {
        guard let url = Bundle.main.url(forResource: "news_sources", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let categories = raw["categories"] as? [[String: Any]] else {
            Log.add("news catalog missing from the app bundle")
            return ([:], [], [])
        }
        var map: [String: [NewsSource]] = [:]
        var order: [String] = []
        for c in categories {
            guard let id = c["id"] as? String, let sources = c["sources"] as? [[String: Any]] else { continue }
            order.append(id)
            map[id] = sources.compactMap { s in
                guard let sid = s["id"] as? String, let name = s["name"] as? String, let u = s["url"] as? String else { return nil }
                return NewsSource(id: sid, category: id, name: name, url: u, isDefault: (s["default"] as? Bool) ?? false)
            }
        }
        let defaults = ((raw["defaultCategories"] as? [String]) ?? []).filter { map[$0] != nil }
        return (map, order, defaults)
    }()

    static var byId: [String: NewsSource] {
        var out: [String: NewsSource] = [:]
        for list in loaded.byCategory.values { for s in list { out[s.id] = s } }
        return out
    }
}

struct NewsConfig {
    var categories: [String]
    var customFeeds: [String] = []
    var refreshMinutes = 15
    var sources: [String]? = nil

    static let defaults = NewsConfig(categories: NewsCatalog.loaded.defaults)

    /// The phone's own copy (Documents/news.json), else the Mac app's choice
    /// as it was at build time (news-config.json, copied by build_ios.sh),
    /// else the catalog's defaults. Same schema as the Mac's news.json.
    static func load() -> NewsConfig {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("news.json")
        for url in [docs, Bundle.main.url(forResource: "news-config", withExtension: "json")].compactMap({ $0 }) {
            if let data = try? Data(contentsOf: url), let raw = try? JSONSerialization.jsonObject(with: data) {
                return from(raw)
            }
        }
        return defaults
    }

    /// NewsConfig.from_dict
    static func from(_ raw: Any) -> NewsConfig {
        guard let d = raw as? [String: Any] else { return defaults }
        let catalog = NewsCatalog.loaded.byCategory
        let cats = ((d["categories"] as? [Any]) ?? []).compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespaces) }
            .filter { catalog[$0] != nil }
        let custom = ((d["customFeeds"] as? [Any]) ?? []).compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespaces) }
            .filter { $0.hasPrefix("http://") || $0.hasPrefix("https://") }
        var minutes = 15
        if let n = d["refreshMinutes"] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { minutes = n.intValue }
        let ids = NewsCatalog.byId
        let sources = (d["sources"] as? [Any]).map { $0.compactMap { $0 as? String }.filter { ids[$0] != nil } }
        return NewsConfig(categories: cats.isEmpty ? (custom.isEmpty ? NewsCatalog.loaded.defaults : []) : cats,
                          customFeeds: custom, refreshMinutes: max(5, min(1440, minutes)), sources: sources)
    }

    /// In each enabled category the sources the wearer chose there, or its defaults.
    var enabledSources: [NewsSource] {
        let chosen = Set(sources ?? [])
        var out: [NewsSource] = []
        for c in categories {
            let all = NewsCatalog.loaded.byCategory[c] ?? []
            let picked = all.filter { chosen.contains($0.id) }
            out += picked.isEmpty ? all.filter(\.isDefault) : picked
        }
        return out
    }

    /// (category, url), custom feeds last.
    var feeds: [(String, String)] {
        enabledSources.map { ($0.category, $0.url) } + customFeeds.map { ("custom", $0) }
    }
}

// MARK - Headlines

struct Headline {
    var title: String
    let link: String
    var source: String
    let category: String
    var publishedAt: Date
    let guid: String
    var feed: String = ""

    /// Headline.row: "[3m] Headline · BBC".
    func row(now: Date = Date()) -> String {
        let age = News.relativeAge(publishedAt, now: now)
        let suffix = source.isEmpty ? "" : " · \(source)"
        let budget = 64 - "[\(age)] ".count - suffix.count
        return "[\(age)] \(News.shorten(title, max(12, budget)))\(suffix)"
    }
}

// MARK: - Feed parsing (feedparser's place)

/// RSS 2.0, RDF/RSS 1.0 and Atom with XMLParser. A feed XMLParser rejects
/// (feedparser reads those too, setting `bozo`) falls back to a tolerant
/// scan of its <item>/<entry> blocks.
final class FeedParser: NSObject, XMLParserDelegate {
    struct Entry { var title = "", link = "", date = "", updated = "", id = "", source = "" }
    private(set) var feedTitle = ""
    private(set) var entries: [Entry] = []
    private var entry: Entry?
    private var text = ""
    private var stack: [String] = []
    private var inSource = false

    static func parse(_ data: Data) -> (String, [Entry]) {
        let p = FeedParser()
        let x = XMLParser(data: data)
        x.delegate = p
        x.shouldProcessNamespaces = false
        if x.parse() || !p.entries.isEmpty { return (p.feedTitle, p.entries) }
        return lenient(String(decoding: data, as: UTF8.self))
    }

    func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName q: String?, attributes a: [String: String] = [:]) {
        let tag = name.lowercased()
        stack.append(tag)
        text = ""
        if tag == "item" || tag == "entry" { entry = Entry(); return }
        guard entry != nil else { return }
        if tag == "source" { inSource = true }
        if tag == "link", let href = a["href"] {
            // Atom: the alternate link (rel absent means alternate).
            let rel = a["rel"] ?? "alternate"
            if rel == "alternate", entry!.link.isEmpty { entry!.link = href }
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }
    func parser(_ parser: XMLParser, foundCDATA block: Data) { text += String(decoding: block, as: UTF8.self) }

    func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName q: String?) {
        let tag = name.lowercased()
        defer { if !stack.isEmpty { stack.removeLast() } }
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if tag == "item" || tag == "entry" {
            if let e = entry { entries.append(e) }
            entry = nil
            return
        }
        guard var e = entry else {
            // The channel's / feed's own title, not an image's.
            if tag == "title", feedTitle.isEmpty, !stack.contains("image") { feedTitle = value }
            return
        }
        switch tag {
        case "title":
            if inSource { e.source = value } else if e.title.isEmpty { e.title = value }
        case "link":
            if e.link.isEmpty, !value.isEmpty { e.link = value }
        case "pubdate", "published", "issued":
            if e.date.isEmpty { e.date = value }
        case "updated", "modified", "dc:date", "dcterms:modified":
            if e.updated.isEmpty { e.updated = value }
        case "guid", "id":
            if e.id.isEmpty, !inSource { e.id = value }
        case "source":
            if e.source.isEmpty, !value.isEmpty { e.source = value }
            inSource = false
        default:
            break
        }
        entry = e
    }

    /// A feed with a stray "&" or an unclosed tag: read what can be read.
    private static func lenient(_ s: String) -> (String, [Entry]) {
        func first(_ tag: String, in block: String) -> String {
            let pattern = "<\(tag)\\b[^>]*>(?:<!\\[CDATA\\[)?(.*?)(?:\\]\\]>)?</\(tag)>"
            guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive, .dotMatchesLineSeparators]),
                  let m = re.firstMatch(in: block, range: NSRange(block.startIndex..., in: block)),
                  let r = Range(m.range(at: 1), in: block) else { return "" }
            return HTMLText.decode(String(block[r])).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let blockRe = try! NSRegularExpression(pattern: "<(item|entry)\\b[^>]*>(.*?)</\\1>", options: [.caseInsensitive, .dotMatchesLineSeparators])
        var out: [Entry] = []
        for m in blockRe.matches(in: s, range: NSRange(s.startIndex..., in: s)) {
            guard let r = Range(m.range(at: 2), in: s) else { continue }
            let b = String(s[r])
            var e = Entry()
            e.title = first("title", in: b)
            e.link = first("link", in: b)
            if e.link.isEmpty, let href = b.range(of: "href=\"[^\"]+\"", options: .regularExpression) {
                e.link = String(b[href]).dropFirst(6).dropLast().description
            }
            e.date = first("pubDate", in: b)
            if e.date.isEmpty { e.date = first("published", in: b) }
            e.updated = first("updated", in: b)
            if e.updated.isEmpty { e.updated = first("dc:date", in: b) }
            e.id = first("guid", in: b)
            if e.id.isEmpty { e.id = first("id", in: b) }
            out.append(e)
        }
        let title = (try? NSRegularExpression(pattern: "<title\\b[^>]*>(.*?)</title>", options: [.caseInsensitive, .dotMatchesLineSeparators]))
            .flatMap { $0.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) }
            .flatMap { Range($0.range(at: 1), in: s) }.map { String(s[$0]) } ?? ""
        return (title, out)
    }

    /// RFC 822 (RSS) and W3CDTF / ISO 8601 (Atom, dc:date), to an instant.
    static func date(_ raw: String) -> Date? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        for options: ISO8601DateFormatter.Options in [[.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime], [.withFullDate]] {
            iso.formatOptions = options
            if let d = iso.date(from: s) { return d }
        }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        for pattern in ["EEE, d MMM yyyy HH:mm:ss Z", "EEE, d MMM yyyy HH:mm:ss zzz", "d MMM yyyy HH:mm:ss Z",
                        "EEE, d MMM yyyy HH:mm Z", "EEE, d MMM yyyy HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ssZ",
                        "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss Z", "EEE, d MMM yy HH:mm:ss Z"] {
            f.dateFormat = pattern
            if let d = f.date(from: s) { return d }
        }
        return nil
    }
}

// MARK: - Page text (trafilatura's place)

/// The story's own text out of a news page: the <article> (or <main>, or
/// <body>) with the most paragraph text, its paragraphs, headings, quotes
/// and list items one per line, page furniture blocks removed. Not
/// trafilatura: which stories pass the readability gate can differ.
enum HTMLText {
    private static func re(_ p: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: p, options: [.caseInsensitive, .dotMatchesLineSeparators])
    }
    private static let drop = re(#"<(script|style|noscript|svg|template|iframe|object|canvas)\b.*?</\1\s*>|<!--.*?-->"#)
    private static let furniture = re(#"<(nav|header|footer|aside|form|figure|button|select|dialog)\b.*?</\1\s*>"#)
    /// Share rows, bylines and author boxes, sign-up and related blocks, by
    /// their class or id. A nested block closes early, so this removes at
    /// least its head; the line rules below catch what is left.
    private static let marked = re(#"<(ul|ol|div|section|p|span|small|address)\b[^>]*\b(?:class|id)\s*=\s*["'][^"']*(?:share|social|byline|author|related|newsletter|promo|subscribe|advert|comment|signup|sign-up|tags|breadcrumb)[^"']*["'][^>]*>.*?</\1\s*>"#)
    private static let regionArticle = re(#"<article\b[^>]*>(.*?)</article\s*>"#)
    private static let regionMain = re(#"<main\b[^>]*>(.*?)</main\s*>"#)
    private static let regionBody = re(#"<body\b[^>]*>(.*)</body\s*>"#)
    private static let blocks = re(#"<(p|h[1-6]|li|blockquote|pre)\b[^>]*>(.*?)</\1\s*>"#)
    private static let tags = re(#"<[^>]+>"#)
    private static let paragraph = re(#"<p\b[^>]*>(.*?)</p\s*>"#)

    static func extract(_ html: String) -> (title: String, site: String, text: String) {
        let title = meta(html, "og:title") ?? firstGroup(re(#"<title\b[^>]*>(.*?)</title\s*>"#), html) ?? ""
        let site = meta(html, "og:site_name") ?? meta(html, "application-name") ?? ""
        let clean = replace(drop, in: html, with: " ")
        var region = clean
        let articles = all(regionArticle, clean)
        if let best = articles.max(by: { paragraphText($0) < paragraphText($1) }), paragraphText(best) > 200 {
            region = best
        } else if let main = firstGroup(regionMain, clean) {
            region = main
        } else if let body = firstGroup(regionBody, clean) {
            region = body
        }
        region = replace(furniture, in: region, with: " ")
        region = replace(marked, in: region, with: " ")
        var lines: [String] = []
        for m in blocks.matches(in: region, range: NSRange(region.startIndex..., in: region)) {
            guard let tagR = Range(m.range(at: 1), in: region), let innerR = Range(m.range(at: 2), in: region) else { continue }
            var inner = String(region[innerR]).replacingOccurrences(of: "<br", with: " <br", options: .caseInsensitive)
            inner = replace(tags, in: inner, with: "")
            let line = decode(inner).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            guard !line.isEmpty else { continue }
            if region[tagR].lowercased() == "li" {
                // A share button or a menu entry is a word or two; a point is a sentence.
                guard line.split(separator: " ").count >= 4 else { continue }
                lines.append("- " + line)
            } else {
                lines.append(line)
            }
        }
        return (decode(title).trimmingCharacters(in: .whitespacesAndNewlines),
                decode(site).trimmingCharacters(in: .whitespacesAndNewlines),
                lines.joined(separator: "\n"))
    }

    private static func paragraphText(_ s: String) -> Int {
        paragraph.matches(in: s, range: NSRange(s.startIndex..., in: s)).reduce(0) { $0 + $1.range.length }
    }

    private static func meta(_ html: String, _ name: String) -> String? {
        let n = NSRegularExpression.escapedPattern(for: name)
        for p in [#"<meta\b[^>]*(?:property|name)\s*=\s*["']"# + n + #"["'][^>]*content\s*=\s*["']([^"']*)["']"#,
                  #"<meta\b[^>]*content\s*=\s*["']([^"']*)["'][^>]*(?:property|name)\s*=\s*["']"# + n + #"["']"#] {
            if let v = firstGroup(re(p), html), !v.isEmpty { return v }
        }
        return nil
    }

    private static func firstGroup(_ r: NSRegularExpression, _ s: String) -> String? {
        guard let m = r.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)), let g = Range(m.range(at: 1), in: s) else { return nil }
        return String(s[g])
    }

    private static func all(_ r: NSRegularExpression, _ s: String) -> [String] {
        r.matches(in: s, range: NSRange(s.startIndex..., in: s)).compactMap { Range($0.range(at: 1), in: s).map { String(s[$0]) } }
    }

    private static func replace(_ r: NSRegularExpression, in s: String, with t: String) -> String {
        r.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: t)
    }

    static let named: [String: String] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": " ", "ndash": "–", "mdash": "—",
        "hellip": "…", "lsquo": "‘", "rsquo": "’", "ldquo": "“", "rdquo": "”", "laquo": "«", "raquo": "»",
        "copy": "©", "reg": "®", "trade": "™", "deg": "°", "middot": "·", "bull": "•", "eacute": "é", "egrave": "è",
        "aacute": "á", "agrave": "à", "iacute": "í", "oacute": "ó", "uacute": "ú", "ntilde": "ñ", "ccedil": "ç",
        "uuml": "ü", "ouml": "ö", "auml": "ä", "szlig": "ß", "euro": "€", "pound": "£", "times": "×", "shy": "\u{AD}",
        "zwj": "\u{200D}", "zwnj": "\u{200C}", "thinsp": " ", "ensp": " ", "emsp": " ", "prime": "′", "Prime": "″",
    ]

    /// HTML character references: numeric, and the common named ones.
    static func decode(_ s: String) -> String {
        guard s.contains("&") else { return s }
        var out = ""
        var i = s.startIndex
        while i < s.endIndex {
            if s[i] == "&", let semi = s[i...].prefix(12).firstIndex(of: ";") {
                let name = String(s[s.index(after: i)..<semi])
                var replacement: String?
                if name.hasPrefix("#x") || name.hasPrefix("#X") {
                    replacement = UInt32(name.dropFirst(2), radix: 16).flatMap(Unicode.Scalar.init).map { String($0) }
                } else if name.hasPrefix("#") {
                    replacement = UInt32(name.dropFirst()).flatMap(Unicode.Scalar.init).map { String($0) }
                } else {
                    replacement = named[name]
                }
                if let r = replacement {
                    out += r
                    i = s.index(after: semi)
                    continue
                }
            }
            out.append(s[i])
            i = s.index(after: i)
        }
        return out
    }
}

// MARK: - Tidy rules (news.py tidy_title, tidy_article, is_readable)

enum NewsTidy {
    private static func re(_ p: String, _ o: NSRegularExpression.Options = [.caseInsensitive]) -> NSRegularExpression {
        try! NSRegularExpression(pattern: p, options: o)
    }

    static let invisible = re("[\u{00AD}\u{200B}-\u{200F}\u{202A}-\u{202E}\u{2060}-\u{2064}\u{FEFF}]", [])
    static func visible(_ s: String) -> String {
        invisible.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: "")
    }

    static let furniture = re(#"""
        ^(?:
            [-–•]?\s*(?:published|updated)(?:[\s\d:].{0,40})?
          | related\s+topics
          | you\s+must\s+confirm\s+your\s+public\s+display\s+name.*
          | please\s+log\s*out\s+and\s+then\s+log\s*in\s+again.*
          | (?:log|sign)\s+in\s+to\s+comment.*
          | a\s+free\s+press\s+is\s+a\s+cornerstone.* | support\s+trusted\s+journalism.* | donate\s+now
          | loading\.{0,3}
          | recommended\s+stories | list\s+of\s+\d+\s+items?
          | (?:watch|listen|read)\s+(?:on|more|now|next)\b.*
          | popular\s+on\s+\S+.*
          | advertisement | sponsored(?:\s+content)?
          | [-–•]?\s*share(?:\s+this(?:\s+(?:article|story))?)?:? | [-–•]?\s*full\s+story
          | copy\s+(?:url|link).*
          | credit:? | image\s+(?:source|credit):?.* | photo(?:graph)?:.* | getty\s+images
          | (?:edited|written|produced|reported)\s+by:.*
          | (?:sign\s+up|subscribe)\b.*(?:newsletter|inbox|email).*
          | .{0,120}\bsubscribe\s+to\s+(?:\S+\s+){0,5}newsletter\.?
          | if\s+you\s+rely\s+on\s+our\s+team.*
          | related(?:\s+(?:articles?|stories|coverage))?:?
          | more\s+top\s+stories
          | (?-i:FOR\s?SUBSCRIBERS).*
          | npr\s+does\s+not\s+offer\s+or\s+accept\s+money.* | anyone\s+who\s+makes\s+such\s+an\s+offer.*
          | this\s+article\s+is\s+for\s+informational\s+purposes\s+only.*
          | .*\|\s*brought\s+to\s+you\s+by\b.*
          | .*</?script\b.*
          | sign\s+up | sign\s+up\s+today\b.* | .{0,40}\bnewsletter
          | \((?:soundbite|music|archived\s+recording|laughter|applause)\b[^)]*\) | [\w\ .'-]{2,40}:\s*\(singing\).*
          | every\s+day\s+we\s+strive\s+to\s+produce\s+journalism.* | .*\bpossible\s+without\s+your\s+support\.?
          | discover\s+more\s+from\s+.* | latest\s+headlines | you\s+are\s+here
          | we['’]ve\s+got\s+the\s+edge\b.* | join\s+our\s+(?:whatsapp|telegram)\s+channel
          | follow\s+us\s+on\b.* | please\s+follow\s+and\s+like\s+us:? | details\s+later\.{0,3}
          | pay\s+attention:.* | photo\s+caption:.* | featured\s+image:.* | last\s+updated:.*
          | go\s+to\s+\S+\s+for\s+more\s+.* | to\s+stay\s+updated\s+on\b.*
          | (?:also\s+read|read\s+also|see\s+also):?.* | source:\s*legit\.ng
          | dive\s+brief:? | table\s+of\s+contents | (?-i:Topics\s+(?:[A-Z][\w&]*\s?){1,4})
          | [-•]?\s*.+\s›
          | ©\s*\d{4}.* | \(c\)\s+copyright.* | .*all\s+rights\s+reserved.*
          | ftc:\s+we\s+use.* | illustration:.* | timely,\s+incisive\s+articles.*
          | stay\s+up\s+to\s+date\s+with\b.*crunchbase.*
          | from\s+the\s+[\w-]+\s+dept | filed\s+under:.* | companies:\s+[\w@&,.\ -]{1,120}
          | breaking\s+cybersecurity\s+news,.* | dr\s+global\s+europe | save\s+your\s+seat
          | \d{1,2}\s+[a-z]+\s+\d{4}:\s*(?:published|updated).* | (?:latest|previous|next)\s+post\b.*
          | significant\s+revisions
          | \(what['’]s\s+moving\s+sensex.* | subscribe\s+to\s+et\s+prime.* | top\s+trending\s+stocks:.*
          | listen\s+to\s+this\s+article.* | follow\s+\S+\s+on\s+google\s+news.*
        )$
        """#, [.caseInsensitive, .allowCommentsAndWhitespace])
    static let labelledValue = re(#"^[-–•]?\s*(?:date|source):?$"#)
    static let labelOnly = re(#"^[-–•]?\s*summary:?$"#)
    static let role = re(#"^(?:scientific|chief|senior|managing|associate|contributing|deputy|science)?\s*editor$"#)
    static let bareDate = re(#"^(?:[A-Z][a-z]+\.? \d{1,2}, \d{4}|\d{1,2} [A-Z][a-z]+ \d{4})$"#, [])
    static let byline = re(#"^(?i:by|from)\s+[A-Z][^.!?]{1,80}$"#, [])
    static let authorBio = re(#"^[A-Z][^.]{0,60}\b(?:is (?:an? |the )?(?:[\w-]+ ){0,4}(?:[Rr]eporter|[Ww]riter|[Ee]ditor|[Cc]orrespondent|[Jj]ournalist|[Cc]olumnist|[Oo]wner)\b|earned (?:her|his|their|a) (?:PhD|Ph\.D\.|doctorate|master's|degree)\b)"#, [])
    static let credit = re(#"^(?:(?:with\s+)?additional\s+reporting\s+(?:by|from)\b.*|design\s+and\s+development\s+by\b.*|information\s+from\b.*\bwas\s+used\s+in\s+this\s+report.*|this\s+(?:newsletter|story|article|episode)\s+was\s+(?:edited|produced)\s+by\b.*|.*\bproduced\s+(?:and\s+edited\s+)?(?:this|the)\s+(?:interview|segment|video|audio|story)\b.*|.*\badapted\s+it\s+for\s+the\s+web\.?|source:\s*\S[^.]{0,40}|.*\(email hidden\).*)$"#)
    static let endMatter = re(#"^(?:story\s+source:?|cite\s+this\s+page:?|journal\s+reference:?|references?|publication\s+details|more\s+information:?|provided\s+by\b.*|copyright\s*(?:©|\(c\)).*|get\s+in\s+touch|copy\s+to\s+clipboard|sign\s+in\s+or\s+register|.*\(\d{4}[a-z]?\)\.\s.*\bdoi\.org/\S+)$"#)
    static let hardEnd = re(#"^(?:copy\s+to\s+clipboard|sign\s+in\s+or\s+register|republish\s+this\s+(?:story|article)|more\s+from\s+\S.*|we\s+have\s+recently\s+deactivated\s+our\s+website.*|update\s+news:?|press\s+information|bottom\s+line|about\s+the\s+author|leaders\s+relevant\s+to\s+this\s+article:?|voice\s+your\s+opinion!?|comment\s+about\s+the\s+article|related\s+(?:content|videos)|join\s+the\s+conversation|more\s+top\s+reads\s+from\s+.*|\(reporting\s+by\b.*|(?-i:.{5,300}[a-z.!?)”"]Reply))$|</?(?:p|a|div|img|span|script|iframe|figure|h[1-6])\b[^>]*>"#)
    static let promo = re(#"\bpromo\s+code\b|\bbonus\s+bets?\b|^deals?:"#)
    static let promoLink = re(#"/(?:select/)?shopping/|/deals/"#)
    static let paywall = re(#"subscribe to (?:continue|read|unlock)|subscribers only|for subscribers|sign in to (?:read|continue)|create (?:a free )?account to (?:read|continue)|already a subscriber|(?:article|story) is (?:for|available to) (?:paid )?(?:subscribers|members)|unlock this (?:article|story)|premium content|(?:reached|used) your (?:free|monthly)|become a (?:paid )?(?:member|subscriber)|join (?:now )?to (?:read|continue)|members[- ]only|log in to (?:read|continue)|register (?:for free )?to (?:read|continue)"#)
    static let publisherSuffix = re(#"\s+[-–|]\s+([^-–|]{2,40})$"#, [])
    static let credLine = re(#"^credit:?$"#)
    static let wireCredit = re(#"^[A-Z]{2,6}(?:/[A-Z]{2,6})+$"#, [])

    static func fullMatch(_ r: NSRegularExpression, _ s: String) -> Bool {
        let range = NSRange(s.startIndex..., in: s)
        guard let m = r.firstMatch(in: s, options: [.anchored], range: range) else { return false }
        return m.range.length == range.length
    }
    /// Python re.match: anchored at the start only.
    static func matches(_ r: NSRegularExpression, _ s: String) -> Bool {
        r.firstMatch(in: s, options: [.anchored], range: NSRange(s.startIndex..., in: s)) != nil
    }
    static func search(_ r: NSRegularExpression, _ s: String) -> Bool {
        r.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    /// news.tidy_title: the headline without the publisher tacked on.
    static func title(_ raw: String, site: String) -> String {
        let t = visible(raw).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        let r = try! NSRegularExpression(pattern: #"^(.*\S)\s+[-–|]\s+([^-–|]{2,40})$"#)
        guard !site.isEmpty, let m = r.firstMatch(in: t, range: NSRange(t.startIndex..., in: t)),
              let head = Range(m.range(at: 1), in: t), let tail = Range(m.range(at: 2), in: t) else { return t }
        let a = t[tail].lowercased(), b = site.lowercased()
        return (b.contains(a) || a.contains(b)) ? String(t[head]) : t
    }

    /// news._key: letters and digits only, case-folded.
    static func key(_ line: String) -> String {
        String(line.lowercased().unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.map(Character.init))
    }

    static func words(_ line: String) -> Set<String> {
        Set(line.lowercased().split(whereSeparator: { !($0.isLetter || $0.isNumber || $0 == "_") }).map(String.init))
    }

    /// news._restates: a second wording of the headline.
    static func restates(_ line: String, _ title: String) -> Bool {
        if title.isEmpty || line.count > 200 || line.trimmingCharacters(in: .whitespaces).hasSuffix(".") { return false }
        let mine = words(line), theirs = words(title)
        let shorter = min(mine.count, theirs.count)
        return shorter >= 4 && Double(mine.intersection(theirs).count) >= 0.6 * Double(shorter)
    }

    /// news.tidy_article
    static func article(_ raw: String, title: String) -> String {
        let text = visible(raw).replacingOccurrences(of: "[email protected]", with: "(email hidden)")
        var lines = text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
            .map { $0.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ") }
            .filter { !$0.isEmpty }
        var end = lines.count
        if let i = lines.indices.first(where: { Double($0) >= Double(lines.count) * 0.6 && fullMatch(endMatter, lines[$0]) }) { end = i }
        if let i = lines.prefix(end).indices.first(where: { $0 >= 2 && search(hardEnd, lines[$0]) }) { end = i }
        lines = Array(lines.prefix(end))
        var kept: [String] = []
        var recent: [String] = []
        let titleKey = key(title)
        var skipNext = false, dekNext = false
        for (index, original) in lines.enumerated() {
            var line = original
            if skipNext { skipNext = false; continue }
            let following = index + 1 < lines.count ? lines[index + 1] : ""
            if fullMatch(labelledValue, line) { skipNext = true; continue }
            if fullMatch(labelOnly, line) { dekNext = true; continue }
            if dekNext {
                line = line.replacingOccurrences(of: #"^[-–•]\s+"#, with: "", options: .regularExpression)
                dekNext = false
            }
            if !title.isEmpty, line.count > title.count + 20, line.hasPrefix(title) {
                line = String(line.dropFirst(title.count)).trimmingCharacters(in: CharacterSet(charactersIn: " :–-"))
            }
            if fullMatch(credLine, following) { continue }
            if fullMatch(credLine, line) { skipNext = following.count <= 60; continue }
            if fullMatch(furniture, line) || line.filter({ $0.isLetter || $0.isNumber }).count < 2 { continue }
            if fullMatch(wireCredit, line) { continue }
            if matches(role, line) {
                if let last = kept.last, last.split(separator: " ").count <= 4, !last.hasSuffix(".") { kept.removeLast() }
                continue
            }
            if kept.count < 3, matches(bareDate, line) || restates(line, title) { continue }
            if kept.count < 2, matches(byline, line), line.split(separator: " ").count <= 9 { continue }
            if kept.isEmpty, line.split(separator: " ").count <= 3,
               !(line.hasSuffix(".") || line.hasSuffix("!") || line.hasSuffix("?") || line.hasSuffix("”") || line.hasSuffix("\"")),
               following.split(separator: " ").count >= 5 { continue }
            let k = key(line)
            if k.isEmpty || k == titleKey || recent.contains(k) { continue }
            recent = Array((recent + [k]).suffix(8))
            kept.append(line.replacingOccurrences(of: #"^[–•]\s+"#, with: "- ", options: .regularExpression))
        }
        while kept.count > 1, let last = kept.last, matches(credit, last) { kept.removeLast() }
        for start in max(1, kept.count - 2)..<max(max(1, kept.count - 2), kept.count) where matches(authorBio, kept[start]) {
            kept.removeSubrange(start...)
            break
        }
        return kept.joined(separator: "\n\n")
    }

    /// news.is_readable: long enough, and no paywall wording at its end.
    static func readable(_ text: String) -> Bool {
        let size = max(text.split(whereSeparator: { $0.isWhitespace }).count, text.count / 6)
        if size < 120 { return false }
        let tail = String(text.suffix(500))
        return !(search(paywall, tail) || (size < 300 && search(paywall, text)))
    }

    /// news._trim_lead: drop a first paragraph that only restates the headline.
    static func trimLead(_ text: String, title: String) -> String {
        guard let r = text.range(of: "\n\n") else { return text }
        let lead = String(text[..<r.lowerBound]), rest = String(text[r.upperBound...])
        if !rest.isEmpty, key(lead) == key(title) || restates(lead, title) { return rest }
        return text
    }

    /// news._strip_publisher: "Apple raises prices - Reuters" → "Apple raises prices".
    static func stripPublisher(_ title: String) -> String {
        guard let m = publisherSuffix.firstMatch(in: title, range: NSRange(title.startIndex..., in: title)),
              let r = Range(m.range, in: title) else { return title }
        let head = title[..<r.lowerBound].trimmingCharacters(in: .whitespaces)
        return head.split(separator: " ").count >= 3 ? head : title
    }
}
