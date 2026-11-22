import Foundation

/// What the glasses' mic actually sent while a note recorded.
///
/// 2026-11-12: a chapter read aloud by the car's speakers reached the note
/// with whole stretches missing (42% of the words), while the wearer's own
/// voice comes through whole. Even says the G2's four mics "isolate the
/// wearer's voice from background noise", so the glasses may cut the car's
/// audio before JOAT ever gets it. This shows it rather than guesses it:
/// - once a second while a note records, the log gets how many frames came
///   and how loud they were (RMS and peak, dBFS): if the level drops while
///   the car is still reading, the glasses cut it; if the level holds and no
///   words come, the speech engine missed them;
/// - with "Save note audio" on (Diagnostics), the audio itself is kept as a
///   WAV per note, to listen to and to run through the speech engine again.
/// Called on the main queue.
final class AudioCheck {
    static let keepKey = "joat.diagnostics.keepNoteAudio"
    static var keepAudio: Bool { UserDefaults.standard.bool(forKey: keepKey) }

    /// Documents/Note audio: the saved WAVs (16 kHz, 16-bit, mono).
    static var folder: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Note audio", isDirectory: true)
    }

    private var windowStart: Date?
    private var frames = 0
    private var samples = 0
    private var sumSquares: Double = 0
    private var peak: Int32 = 0
    private var quiet = 0

    private let fileQueue = DispatchQueue(label: "com.joatlabs.joatbrain.audiocheck")
    private var file: FileHandle?
    private var fileNote: String?
    private var fileBytes = 0

    /// One frame of glasses audio, on the main queue. `note`: the open note
    /// while its lane records, else nil.
    func frame(_ pcm: [Int16], note: String?) {
        guard let note else {
            flush(now: Date(), final: true)
            return
        }
        let now = Date()
        if windowStart == nil { windowStart = now }
        frames += 1
        samples += pcm.count
        var sum: Double = 0
        var top: Int32 = 0
        for s in pcm {
            let v = Double(s)
            sum += v * v
            top = max(top, abs(Int32(s)))
        }
        sumSquares += sum
        peak = max(peak, top)
        // A frame under -60 dBFS RMS: as good as silence.
        if pcm.isEmpty || sqrt(sum / Double(pcm.count)) < 32.8 { quiet += 1 }
        if let start = windowStart, now.timeIntervalSince(start) >= 1 { flush(now: now, final: false) }
        if Self.keepAudio { write(pcm, note: note) }
    }

    /// The note's lane closed (pause, stop, a hold let go): the last part
    /// of the second goes to the log, and the WAV's header is brought up to date.
    func laneClosed() {
        flush(now: Date(), final: true)
        finishFile(close: false)
    }

    /// The note was saved or discarded.
    func noteEnded() {
        flush(now: Date(), final: true)
        finishFile(close: true)
    }

    private func flush(now: Date, final: Bool) {
        guard let start = windowStart, frames > 0 else { windowStart = nil; return }
        let rms = samples > 0 ? sqrt(sumSquares / Double(samples)) : 0
        let line = String(format: "quicknotes audio %.1f s: %d frames, %.1f s of sound, rms %@ dBFS, peak %@ dBFS%@",
                          now.timeIntervalSince(start), frames, Double(samples) / 16_000,
                          Self.db(rms), Self.db(Double(peak)),
                          quiet > 0 ? ", \(quiet) silent frame\(quiet == 1 ? "" : "s")" : "")
        Log.add(line + (final ? " (lane closed)" : ""))
        windowStart = final ? nil : now
        frames = 0; samples = 0; sumSquares = 0; peak = 0; quiet = 0
    }

    private static func db(_ amplitude: Double) -> String {
        amplitude <= 0 ? "-inf" : String(format: "%.0f", 20 * log10(amplitude / 32_768))
    }

    // MARK: The WAV

    private func write(_ pcm: [Int16], note: String) {
        if fileNote != note { finishFile(close: true); open(note: note) }
        guard let file else { return }
        let data = pcm.withUnsafeBufferPointer { Data(buffer: $0) }   // little-endian on iPhone
        fileBytes += data.count
        fileQueue.async { try? file.write(contentsOf: data) }
    }

    private func open(note: String) {
        let fm = FileManager.default
        try? fm.createDirectory(at: Self.folder, withIntermediateDirectories: true)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH.mm.ss"
        let url = Self.folder.appendingPathComponent("Note \(f.string(from: Date())).wav")
        fm.createFile(atPath: url.path, contents: Self.header(dataBytes: 0))
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        _ = try? h.seekToEnd()
        file = h
        fileNote = note
        fileBytes = 0
        Log.add("quicknotes audio: saving to \(url.lastPathComponent)")
    }

    private func finishFile(close: Bool) {
        guard let h = file else { return }
        let bytes = fileBytes
        fileQueue.async {
            // The sizes in the header, so the file plays as it is now.
            _ = try? h.seek(toOffset: 0)
            try? h.write(contentsOf: Self.header(dataBytes: bytes))
            _ = try? h.seekToEnd()
            if close { try? h.close() }
        }
        if close {
            file = nil
            fileNote = nil
            fileBytes = 0
        }
    }

    /// A 44-byte WAV header: PCM, 16 kHz, 16-bit, mono.
    private static func header(dataBytes: Int) -> Data {
        var d = Data()
        func u32(_ v: Int) { var x = UInt32(v).littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: Int) { var x = UInt16(v).littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + dataBytes)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(16_000); u32(32_000); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        return d
    }

    /// The saved WAVs, newest first.
    static func saved() -> [URL] {
        let urls = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.creationDateKey])) ?? []
        return urls.filter { $0.pathExtension == "wav" }.sorted { $0.lastPathComponent > $1.lastPathComponent }
    }
}
