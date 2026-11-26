import Foundation
import AVFoundation
import Speech

/// On-device speech-to-text for the glasses mic stream (16 kHz mono s16le,
/// as the plugin's `audio` frames carry it).
///
/// Uses Apple's SpeechAnalyzer (iOS 26). SpeechTranscriber is the long-form
/// model; Apple publishes no device list for it and says to check
/// `isAvailable` at runtime, so DictationTranscriber (the system dictation
/// model, "compatible with older devices") is the fallback.
final class Transcriber: ObservableObject {
    /// Which Apple engine. `dictation` is the one Apple documents custom words
    /// for (AnalysisContext.contextualStrings, DictationTranscriber docs);
    /// `speech` is the newer long-form model, which documents no such option.
    enum Kind { case dictation(words: [String]), speech }

    enum Event {
        case volatile(String)
        /// Final text, and how long after its last audio arrived it was finalised.
        case final(String, latencyMs: Int?)
    }

    @Published private(set) var engine = "not started"
    @Published private(set) var lastFinal = ""
    @Published private(set) var lastVolatile = ""
    @Published private(set) var lastLatencyMs: Int?

    var onEvent: ((Event) -> Void)?

    private let inputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                            channels: 1, interleaved: true)!
    private var analyzer: SpeechAnalyzer?
    private var builder: AsyncStream<AnalyzerInput>.Continuation?
    private var analyzerFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var resultsTask: Task<Void, Never>?
    /// (samples fed so far, wall time they arrived) — maps a result's audio
    /// end back to when that audio reached the phone.
    private var arrivals: [(samples: Int64, at: Date)] = []
    private var samplesFed: Int64 = 0
    private var lastSpeechAudioAt: Date?
    private var lastResultAt: Date = Date()
    private var startedAt: Date = Date()
    private var lastVolatileLogged = ""
    private var mediaResetObserver: NSObjectProtocol?
    private let lock = NSLock()
    private(set) var running = false
    /// Restarts since the last sentence came through (the backoff), and the
    /// engine's generation, so a cancelled engine's ending is not news.
    private var retries = 0
    private var generation = 0
    private let kind: Kind
    /// Shown in the log so two engines on the same audio can be told apart.
    let label: String

    init(kind: Kind, label: String) {
        self.kind = kind
        self.label = label
        mediaResetObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self, self.running else { return }
            self.restart("media services reset", generation: self.generation)
        }
    }

    deinit {
        if let mediaResetObserver {
            NotificationCenter.default.removeObserver(mediaResetObserver)
        }
    }

    /// Ensures speech recognition is running when `JOAT Brain` comes forward.
    func check() {
        if !running {
            start()
        }
    }

    func start() {
        #if DEBUG
        if ProcessInfo.processInfo.environment["JOAT_SKIP_SIM_PROMPTS"] == "1" {
            engine = "dictation · simulator"
            return
        }
        #endif
        guard !running else { return }
        running = true
        generation += 1
        let g = generation
        Task { await self.boot(generation: g) }
    }

    /// Speech recognition stopped: it failed to start (no model and no
    /// network at launch) or its results ended. Start again after a pause,
    /// longer each time (5 s up to 60 s), so the voice features never stay
    /// dead for the rest of a process that runs for days (review, 2026-10-30).
    private func restart(_ why: String, generation g: Int) {
        DispatchQueue.main.async { [self] in
            guard g == generation else { return }
            generation += 1
            let delay = min(60.0, 5.0 * pow(2.0, Double(retries)))
            retries += 1
            Log.add("[\(label)] speech recognition stopped (\(why)); restarting in \(Int(delay)) s")
            lock.lock(); builder?.finish(); builder = nil; lock.unlock()
            let old = analyzer
            analyzer = nil
            resultsTask?.cancel()
            resultsTask = nil
            Task { await old?.cancelAndFinishNow() }
            running = false
            engine = "restarting in \(Int(delay)) s"
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.start() }
        }
    }

    private func boot(generation g: Int) async {
        let auth = await withCheckedContinuation { c in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
        }
        Log.add("speech authorization: \(auth.rawValue) (3 = authorized)")

        let locale = Locale(identifier: "en-US")
        let module: any SpeechModule
        var name: String
        var context: AnalysisContext?
        switch kind {
        case .dictation(let words):
            // Apple's `progressiveShortDictation` preset (short form, volatile
            // results, frequent finalization, punctuation) plus audio time
            // ranges for the latency measurement. Without frequentFinalization
            // and the short-form hint, dictation held every sentence open and
            // finalised none (first run, 2026-10-22).
            let preset = DictationTranscriber.Preset.progressiveShortDictation
            var hints = preset.contentHints
            if let model = await NameModel.prepare() {
                hints.insert(.customizedLanguage(modelConfiguration: model))
            }
            let t = DictationTranscriber(locale: locale, contentHints: hints,
                                         transcriptionOptions: preset.transcriptionOptions,
                                         reportingOptions: preset.reportingOptions,
                                         attributeOptions: preset.attributeOptions.union([.audioTimeRange]))
            module = t; name = "DictationTranscriber"
            if !words.isEmpty {
                let ctx = AnalysisContext()
                ctx.contextualStrings[.general] = words
                context = ctx
                name += " + custom words \(words)"
            }
            if hints.count > preset.contentHints.count {
                name += " + pronunciation model"
            }
            resultsTask = Task { [weak self] in
                do {
                    for try await r in t.results {
                        self?.handle(text: String(r.text.characters), isFinal: r.isFinal, range: r.range)
                    }
                    self?.restart("results ended", generation: g)
                } catch {
                    Log.add("[\(self?.label ?? "?")] dictation results ended: \(error)")
                    self?.restart("results failed", generation: g)
                }
            }
        case .speech:
            guard SpeechTranscriber.isAvailable,
                  let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
                Log.add("[\(label)] SpeechTranscriber unavailable on this device")
                await MainActor.run { self.engine = "SpeechTranscriber unavailable" }
                running = false
                return
            }
            let t = SpeechTranscriber(locale: supported, transcriptionOptions: [],
                                      reportingOptions: [.volatileResults, .fastResults],
                                      attributeOptions: [.audioTimeRange])
            module = t; name = "SpeechTranscriber"
            resultsTask = Task { [weak self] in
                do {
                    for try await r in t.results {
                        self?.handle(text: String(r.text.characters), isFinal: r.isFinal, range: r.range)
                    }
                    self?.restart("results ended", generation: g)
                } catch {
                    Log.add("[\(self?.label ?? "?")] transcriber results ended: \(error)")
                    self?.restart("results failed", generation: g)
                }
            }
        }
        Log.add("[\(label)] STT engine: \(name)")
        await MainActor.run { self.engine = name }

        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
                Log.add("downloading speech model for en-US…")
                try await request.downloadAndInstall()
                Log.add("speech model installed")
            } else {
                Log.add("speech model already installed")
            }
            let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [module])
            analyzerFormat = format
            if let format, format != inputFormat {
                converter = AVAudioConverter(from: inputFormat, to: format)
            }
            Log.add("analyzer format: \(format.map { "\($0)" } ?? "nil (feeding 16 kHz Int16)")")
            let (stream, builder) = AsyncStream<AnalyzerInput>.makeStream()
            lock.lock(); self.builder = builder; lock.unlock()
            let analyzer = SpeechAnalyzer(modules: [module])
            self.analyzer = analyzer
            if let context {
                try await analyzer.setContext(context)
                Log.add("[\(label)] custom words set")
            }
            try await analyzer.start(inputSequence: stream)
            lock.lock()
            samplesFed = 0
            arrivals.removeAll()
            startedAt = Date()
            lastResultAt = Date()
            lastSpeechAudioAt = nil
            lock.unlock()
            Log.add("analyzer started")
            await MainActor.run { self.engine = name + " — listening" }
        } catch {
            Log.add("[\(label)] STT start failed: \(error)")
            restart("start failed: \(error.localizedDescription)", generation: g)
        }
    }

    /// One plugin `audio` frame: signed 16-bit samples.
    func feed(_ samples: [Int16]) {
        lock.lock(); let builder = self.builder; lock.unlock()
        guard let builder, !samples.isEmpty else { return }
        let count = AVAudioFrameCount(samples.count)
        guard let buf = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: count) else { return }
        buf.frameLength = count
        samples.withUnsafeBufferPointer { src in
            buf.int16ChannelData![0].update(from: src.baseAddress!, count: samples.count)
        }
        var out: AVAudioPCMBuffer = buf
        if let converter, let target = analyzerFormat {
            let ratio = target.sampleRate / inputFormat.sampleRate
            let cap = AVAudioFrameCount(Double(count) * ratio) + 32
            guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
            var given = false
            var err: NSError?
            converter.convert(to: converted, error: &err) { _, status in
                if given { status.pointee = .noDataNow; return nil }
                given = true; status.pointee = .haveData; return buf
            }
            if let err { Log.add("convert error: \(err)"); return }
            out = converted
        }
        var peak: Int32 = 0
        for s in samples.lazy.prefix(640) {
            let mag = abs(Int32(s))
            if mag > peak { peak = mag }
        }
        let now = Date()
        var shouldRestartStalled = false
        var stalledGen = 0
        lock.lock()
        samplesFed += Int64(samples.count)
        arrivals.append((samplesFed, now))
        if arrivals.count > 3_000 { arrivals.removeFirst(arrivals.count - 3_000) }
        if peak >= 1_400 {
            if lastSpeechAudioAt == nil { lastSpeechAudioAt = now }
            if let speechStart = lastSpeechAudioAt,
               now.timeIntervalSince(speechStart) > 25,
               now.timeIntervalSince(lastResultAt) > 25,
               now.timeIntervalSince(startedAt) > 25 {
                lastSpeechAudioAt = nil
                shouldRestartStalled = true
                stalledGen = generation
            }
        } else if let speechStart = lastSpeechAudioAt, now.timeIntervalSince(speechStart) > 4,
                  now.timeIntervalSince(lastResultAt) <= 4 {
            lastSpeechAudioAt = nil
        }
        lock.unlock()
        builder.yield(AnalyzerInput(buffer: out))
        if shouldRestartStalled {
            restart("analyzer produced no results across 25s of speech audio", generation: stalledGen)
        }
    }

    private func handle(text: String, isFinal: Bool, range: CMTimeRange) {
        lock.lock()
        lastResultAt = Date()
        lastSpeechAudioAt = nil
        lock.unlock()
        // Progressive dictation finalises a sentence's closing "?" or "." as
        // its own result, or at the head of the next one ("? Let's grab...").
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "^[\\s.,?!;:]+", with: "", options: .regularExpression)
        guard !trimmed.isEmpty else { return }
        if isFinal {
            var latency: Int?
            let endSample = Int64((range.end.seconds.isFinite ? range.end.seconds : 0) * 16_000)
            lock.lock()
            if endSample > 0, let hit = arrivals.first(where: { $0.samples >= endSample }) {
                let rawLatency = Int(Date().timeIntervalSince(hit.at) * 1000)
                if rawLatency >= 0 && rawLatency <= 5_000 {
                    latency = rawLatency
                }
            }
            lock.unlock()
            if retries > 0 { DispatchQueue.main.async { self.retries = 0 } }
            Log.add("[\(label)] STT final (\(latency.map { "\($0) ms after its audio" } ?? "latency n/a")): \(trimmed)")
            DispatchQueue.main.async { self.lastFinal = trimmed; self.lastLatencyMs = latency; self.lastVolatile = "" }
            onEvent?(.final(trimmed, latencyMs: latency))
        } else {
            if trimmed != lastVolatileLogged, trimmed.count % 12 == 0 || trimmed.hasSuffix("?") || trimmed.hasSuffix(".") {
                lastVolatileLogged = trimmed
                Log.add("[\(label)] hearing: \(trimmed.suffix(80))")
            }
            DispatchQueue.main.async { self.lastVolatile = trimmed }
            onEvent?(.volatile(trimmed))
        }
    }
}
