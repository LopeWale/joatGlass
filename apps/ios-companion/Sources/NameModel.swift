import Foundation
import Speech

/// Teaches Apple's dictation engine the assistant's name, the way the
/// wearer says it: a custom language model (iOS 17+) with a custom
/// pronunciation for "JOAT" and weighted phrases that use it.
///
/// Apple: SFCustomLanguageModelData (CustomPronunciation, PhraseCount,
/// export(to:)), SFSpeechLanguageModel.prepareCustomLanguageModel, and
/// DictationTranscriber.ContentHint.customizedLanguage(modelConfiguration:).
///
/// Every pronunciation ends in /t/. None ends in /k/, so "joke" stays
/// "joke" and "that's a joke" cannot wake JOAT.
enum NameModel {
    /// X-SAMPA spellings of "JOAT" in Apple's en-US inventory
    /// (`supportedPhonemes`, logged at startup): there "o" is the GOAT vowel,
    /// the "oh" glide as in "go", and "O" the open o; a leading `"` marks
    /// primary stress. Unstressed and stressed forms of both. Every one ends
    /// in /t/, so "Joe" and "joke" stay what they are.
    /// (Run 1 also tried the textbook spellings "oU", "@U", "o:", which are
    /// not in Apple's inventory and were dropped.)
    static let pronunciations = [#"dZ "o t"#, #"dZ "O t"#, "dZ o t", "dZ O t"]

    /// Phrases the wearer actually says, weighted toward the name.
    static let phrases: [(String, Int)] = [
        ("Hey JOAT", 600), ("JOAT", 600),
        ("Hey JOAT what time is it", 60), ("JOAT what time is it", 60),
        ("Hey JOAT what is my location", 60), ("JOAT what is my current location", 60),
        ("Hey JOAT remind me to", 60), ("JOAT remind me to", 100), ("JOAT remind me to call", 60),
        ("Hey JOAT open quick notes", 10), ("JOAT open agent terminal", 10),
        ("JOAT what's on my calendar today", 10), ("Hey JOAT read my messages", 10),
    ]

    static func prepare() async -> SFSpeechLanguageModel.Configuration? {
        let locale = Locale(identifier: "en-US")
        let supported = Set(SFCustomLanguageModelData.supportedPhonemes(locale: locale))
        Log.add("name model: \(supported.count) supported X-SAMPA symbols: \(supported.sorted().joined(separator: " "))")
        let usable = pronunciations.filter {
            $0.split(separator: " ").allSatisfy { supported.contains(String($0)) }
        }
        Log.add("name model: JOAT pronunciations used \(usable), dropped \(pronunciations.filter { !usable.contains($0) })")

        let data = SFCustomLanguageModelData(locale: locale, identifier: "com.joat.brain.name", version: "3")
        if !usable.isEmpty {
            data.insert(term: SFCustomLanguageModelData.CustomPronunciation(grapheme: "JOAT", phonemes: usable))
        }
        for (phrase, count) in phrases {
            data.insert(phraseCount: SFCustomLanguageModelData.PhraseCount(phrase: phrase, count: count))
        }

        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("joat-name-model", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dataURL = dir.appendingPathComponent("training.bin")
        let config = SFSpeechLanguageModel.Configuration(
            languageModel: dir.appendingPathComponent("model.lm"),
            vocabulary: dir.appendingPathComponent("model.vocab"))
        let started = Date()
        do {
            try await data.export(to: dataURL)
            let prepared = await withTaskGroup(of: Result<Void, Error>?.self) { group -> Bool in
                group.addTask {
                    do {
                        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
                            SFSpeechLanguageModel.prepareCustomLanguageModel(
                                for: dataURL, clientIdentifier: "com.joat.brain",
                                configuration: config, ignoresCache: true
                            ) { error in
                                if let error { c.resume(throwing: error) } else { c.resume() }
                            }
                        }
                        return .success(())
                    } catch {
                        return .failure(error)
                    }
                }
                group.addTask {
                    try? await Task.sleep(nanoseconds: 3_500_000_000)
                    return nil
                }
                let first = await group.next() ?? nil
                group.cancelAll()
                switch first {
                case .some(.success):
                    return true
                case .some(.failure(let err)):
                    Log.add("name model failed: \(err)")
                    return false
                case .none:
                    Log.add("name model timed out after 3500 ms; continuing without custom pronunciation")
                    return false
                }
            }
            guard prepared else { return nil }
            Log.add("name model ready in \(Int(Date().timeIntervalSince(started) * 1000)) ms")
            return config
        } catch {
            Log.add("name model failed: \(error)")
            return nil
        }
    }
}
