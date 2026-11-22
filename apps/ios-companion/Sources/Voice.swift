import Foundation
import AVFoundation

/// Spoken replies are the wearer's choice and OFF unless they turn them on
/// (2026-10-23: "I could read it on the glass ... let me choose it"): every
/// answer is on the lens either way. "JOAT, speak your answers" / "JOAT,
/// stop talking", or the switch in JOAT Brain. When on, they are spoken
/// only into AirPods or other headphones, never out of the phone's speaker.
final class Voice: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    static let spokenRepliesKey = "joat.voice.spokenReplies"
    @Published var spokenReplies: Bool = UserDefaults.standard.bool(forKey: Voice.spokenRepliesKey) {
        didSet {
            UserDefaults.standard.set(spokenReplies, forKey: Self.spokenRepliesKey)
            Log.add("spoken replies \(spokenReplies ? "on" : "off")")
        }
    }
    static let earPorts: Set<AVAudioSession.Port> = [
        .bluetoothA2DP, .bluetoothHFP, .bluetoothLE, .headphones, .carAudio, .usbAudio,
    ]

    @Published private(set) var speaking = false
    private var startedAt = Date.distantPast
    private var finishedAt = Date.distantPast
    private var synth = AVSpeechSynthesizer()

    override init() {
        super.init()
        synth.delegate = self
        synth.usesApplicationAudioSession = true
        // After a media-services reset the old synthesizer is dead: replies
        // went silent until a relaunch (review, 2026-10-30).
        NotificationCenter.default.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            Log.add("media services reset; new speech synthesizer")
            self.synth = AVSpeechSynthesizer()
            self.synth.delegate = self
            self.synth.usesApplicationAudioSession = true
            self.speaking = false
        }
    }

    func say(_ text: String) {
        guard spokenReplies else {
            Log.add("not spoken (spoken replies off): \(text.prefix(120))")
            return
        }
        speakToHeadphones(text, stopCurrent: false, tag: "speak")
    }

    /// Turn-by-turn navigation cues use their own preference (`Navigation.spokenDirections`),
    /// independent of general `spokenReplies`, while preserving the headphones-only
    /// rule and mic echo suppression (`speaking`/`startedAt`/`finishedAt`).
    func sayNavigation(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        speakToHeadphones(trimmed, stopCurrent: true, tag: "speak nav")
    }

    private func speakToHeadphones(_ text: String, stopCurrent: Bool, tag: String) {
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        let route = outputs.map { $0.portType.rawValue }.joined(separator: ",")
        guard outputs.contains(where: { Self.earPorts.contains($0.portType) }) else {
            Log.add("not spoken, no headphones (\(route)): \(text.prefix(120))")
            return
        }
        if stopCurrent, synth.isSpeaking {
            synth.stopSpeaking(at: .word)
        }
        Log.add("\(tag) (\(route)): \(text.prefix(120))")
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: "en-US")
        synth.speak(u)
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = true; self.startedAt = Date() }
    }
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = s.isSpeaking; self.finishedAt = Date() }
    }
    /// A reply cut off (a call, another app's audio) ends too: otherwise
    /// `speaking` stayed true and every note sentence and follow-up after it
    /// was dropped as JOAT's own voice (review, 2026-10-30).
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = s.isSpeaking; self.finishedAt = Date() }
    }

    /// Was the phone talking when a sentence that ended at `moment` was
    /// heard? (Speaker out loud, glasses mic close by: an echo.)
    func wasSpeaking(at moment: Date) -> Bool {
        // No reply runs a minute: a `speaking` that old was never ended.
        if speaking, Date().timeIntervalSince(startedAt) > 60, !synth.isSpeaking { speaking = false }
        if speaking { return true }
        return moment >= startedAt && moment <= finishedAt.addingTimeInterval(0.3)
    }
}
