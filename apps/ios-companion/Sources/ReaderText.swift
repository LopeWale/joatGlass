import Foundation

/// services/reader.py paginate_reader_text, exactly: lossless slices of the
/// source, at most 62 mono columns a row, ten rows a page. Wide and astral
/// characters cost two columns, a tab eight, everything else one. CRLF, CR
/// and LF end a row and stay in the slice, so the plugin
/// (EvenFont.ts readerParagraphs) can join the rows back into paragraphs.
enum ReaderText {
    static let rowColumns = 62
    static let pageRows = 10

    /// The pages, each up to ten rows.
    static func pages(_ text: String) -> [[String]] {
        let all = rows(text)
        return stride(from: 0, to: all.count, by: pageRows).map { Array(all[$0..<min($0 + pageRows, all.count)]) }
    }

    /// Every row, unpaged.
    static func rows(_ text: String) -> [String] {
        let scalars = Array(text.unicodeScalars)
        var out: [String] = []
        for (p0, p1, ending) in paragraphs(scalars) {
            let stop = p1 - ending
            if stop == p0 {
                out.append(string(scalars[p0..<p1]))
                continue
            }
            var start = p0
            while start < stop {
                var columns = 0
                var end = start
                while end < stop {
                    let w = width(scalars[end])
                    if columns + w > rowColumns { break }
                    columns += w
                    end += 1
                }
                if end == stop {
                    out.append(string(scalars[start..<p1]))
                    break
                }
                // str.rfind(" ", start, end)
                if let space = (start..<end).last(where: { scalars[$0] == " " }), space > start {
                    end = space + 1
                }
                var safeEnd = end
                while safeEnd > start, continuesGroup(scalars, safeEnd, start) { safeEnd -= 1 }
                end = safeEnd > start ? safeEnd : end
                out.append(string(scalars[start..<end]))
                start = end
            }
        }
        return out
    }

    /// re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text) or [""], as
    /// (start, end, ending length) over the scalars.
    private static func paragraphs(_ s: [Unicode.Scalar]) -> [(Int, Int, Int)] {
        var out: [(Int, Int, Int)] = []
        var i = 0
        let n = s.count
        while i < n {
            var j = i
            while j < n, s[j] != "\r", s[j] != "\n" { j += 1 }
            if j == n {
                out.append((i, n, 0))
                break
            }
            let ending = (s[j] == "\r" && j + 1 < n && s[j + 1] == "\n") ? 2 : 1
            out.append((i, j + ending, ending))
            i = j + ending
        }
        return out.isEmpty ? [(0, 0, 0)] : out
    }

    /// reader._columns. East Asian Wide/Fullwidth uses the same ranges as the
    /// plugin's readerColumns (EvenFont.ts), so both sides agree.
    static func width(_ c: Unicode.Scalar) -> Int {
        if c == "\t" { return 8 }
        let p = c.value
        if p > 0xFFFF { return 2 }
        if (0x1100...0x115F).contains(p) || (0x2E80...0xA4CF).contains(p) || (0xAC00...0xD7A3).contains(p)
            || (0xF900...0xFAFF).contains(p) || (0xFE30...0xFE4F).contains(p) || (0xFF00...0xFF60).contains(p)
            || (0xFFE0...0xFFE6).contains(p) { return 2 }
        return 1
    }

    /// reader._continues_group: never strand a combining mark, a joined
    /// emoji, a skin tone or half a flag at a row's start.
    private static func continuesGroup(_ s: [Unicode.Scalar], _ boundary: Int, _ rowStart: Int) -> Bool {
        let c = s[boundary]
        switch c.properties.generalCategory {
        case .nonspacingMark, .spacingMark, .enclosingMark: return true
        default: break
        }
        if c.value == 0x200D || s[boundary - 1].value == 0x200D || (0x1F3FB...0x1F3FF).contains(c.value) { return true }
        if (0x1F1E6...0x1F1FF).contains(c.value) {
            var previous = boundary - 1
            while previous >= rowStart, (0x1F1E6...0x1F1FF).contains(s[previous].value) { previous -= 1 }
            return (boundary - previous - 1) % 2 == 1
        }
        return false
    }

    private static func string(_ slice: ArraySlice<Unicode.Scalar>) -> String {
        var v = String.UnicodeScalarView()
        v.append(contentsOf: slice)
        return String(v)
    }
}
