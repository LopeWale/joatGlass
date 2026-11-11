import Foundation
import PDFKit
import UIKit

/// Plain text from what the wearer shares or picks: a PDF, a text, Markdown,
/// RTF or web page file, or a link (the page is fetched and its text kept).
enum PrepExtract {
    enum Failure: Error, CustomStringConvertible {
        case unreadable(String)
        case empty
        var description: String {
            switch self {
            case .unreadable(let what): return "JOAT can't read \(what) yet. Share it as a PDF or text."
            case .empty: return "There is no text in it."
            }
        }
    }

    /// Prep text is capped: enough for a briefing, small enough to send
    /// with every cue request (it is cached on Claude's side).
    static let maxChars = 60_000

    static func text(fromFile url: URL) throws -> String {
        let ext = url.pathExtension.lowercased()
        var text = ""
        switch ext {
        case "pdf":
            guard let doc = PDFDocument(url: url) else { throw Failure.unreadable("this PDF") }
            text = doc.string ?? ""
        case "rtf", "rtfd":
            text = try NSAttributedString(url: url, options: [.documentType: NSAttributedString.DocumentType.rtf],
                                          documentAttributes: nil).string
        case "html", "htm":
            text = try html(Data(contentsOf: url))
        case "txt", "text", "md", "markdown", "csv", "json", "log", "":
            text = try String(contentsOf: url, encoding: .utf8)
        default:
            if let s = try? String(contentsOf: url, encoding: .utf8) { text = s }
            else { throw Failure.unreadable("a .\(ext) file") }
        }
        return try tidy(text)
    }

    static func text(fromLink url: URL) async throws -> (title: String, text: String) {
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        let type = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type")?.lowercased() ?? ""
        if type.contains("pdf") {
            guard let doc = PDFDocument(data: data) else { throw Failure.unreadable("this PDF") }
            return (url.lastPathComponent, try tidy(doc.string ?? ""))
        }
        let raw = String(decoding: data, as: UTF8.self)
        let title = raw.range(of: "<title[^>]*>([^<]*)</title>", options: [.regularExpression, .caseInsensitive])
            .map { String(raw[$0]).replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression) }?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return ((title?.isEmpty == false ? title! : url.host ?? "Link"), try tidy(html(data)))
    }

    /// Scripts and styles out, then the system's HTML reader for the rest.
    static func html(_ data: Data) throws -> String {
        var s = String(decoding: data, as: UTF8.self)
        for tag in ["script", "style", "noscript", "svg"] {
            s = s.replacingOccurrences(of: "<\(tag)[\\s\\S]*?</\(tag)>", with: " ", options: [.regularExpression, .caseInsensitive])
        }
        let page = s
        // NSAttributedString's HTML reader must run on the main thread.
        let read = { () -> String in
            (try? NSAttributedString(data: Data(page.utf8),
                                     options: [.documentType: NSAttributedString.DocumentType.html,
                                               .characterEncoding: String.Encoding.utf8.rawValue],
                                     documentAttributes: nil).string)
                ?? page.replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
        }
        return Thread.isMainThread ? read() : DispatchQueue.main.sync(execute: read)
    }

    static func tidy(_ text: String) throws -> String {
        let lines = text.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        var out: [String] = []
        for line in lines where !(line.isEmpty && out.last?.isEmpty == true) { out.append(line) }
        let joined = out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !joined.isEmpty else { throw Failure.empty }
        return String(joined.prefix(maxChars))
    }
}
