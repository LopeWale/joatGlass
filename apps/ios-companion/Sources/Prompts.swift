import Foundation

/// One question the glasses ask for a feature, answered by the wearer's
/// next sentence: the Mac's prompt wire (prompted_input.py, the plugin's
/// PromptedInputController). The plugin draws the question card, opens the
/// glasses mic for it and acknowledges; the words so far show under the
/// question (`prompt.partial`); a tap on the card takes them as they are
/// (`prompt-commit`). Either side can cancel. Used by Messages: "What should
/// I say to Mom?" after a tap on a conversation (2026-10-30: the glasses had
/// no way to reply at all).
final class Prompts: BrainFeature {
    let name = "prompts"
    let uplinkKinds: Set<String> = ["prompt.ack", "prompt.cancel", "input", "device"]

    private struct Open {
        let id: String
        let answer: (String?) -> Void
        var partial = ""
        let timeout: DispatchWorkItem
        /// Answered, the card stays until the next question replaces it.
        let hold: Bool
    }

    private let link: PluginLink
    /// The plugin echoes it: one value per plugin connection.
    private var generation = "phone-" + ConfirmGate.hex(8)
    private var open: Open?
    /// An answered question still on the glasses (`hold`): its card shows
    /// what was heard while the owner works out the next question. A new
    /// question replaces it there; `cancel` takes it down.
    private var held: String?
    /// The mic the plugin streams now (`device.micSource`): a question must
    /// name it, or the plugin refuses it (AudioStreamer.openPromptMicrophone,
    /// the Mac's hub_gateway prompt_audio_source).
    private var micSource = "glasses"

    var active: Bool { open != nil }

    init(link: PluginLink) { self.link = link }

    func pluginConnected() {
        generation = "phone-" + ConfirmGate.hex(8)
        micSource = "glasses"
        held = nil
        finish(nil, notify: false, why: "plugin reconnected")
    }

    /// Show `question` and hand the next sentence (or nil: cancelled, timed
    /// out, the mic refused) to `answer`, once, on the main queue. `hold`:
    /// the answered card stays up until the owner asks again or cancels
    /// (Messages' "Who should I text?" while the contact is looked up: the
    /// card closing first left the glasses on the list, and a tap there
    /// cancelled the draft before the next question came, 2026-11-11).
    func ask(_ question: String, ownerKind: String, ownerId: String, field: String,
             seconds: Int = 30, hold: Bool = false, answer: @escaping (String?) -> Void) {
        finish(nil, notify: true, why: "replaced")
        // The glasses replace a held card with this question themselves.
        held = nil
        let id = ConfirmGate.hex(16)
        let timeout = DispatchWorkItem { [weak self] in
            guard self?.open?.id == id else { return }
            self?.finish(nil, notify: true, why: "no answer in \(seconds) s")
        }
        open = Open(id: id, answer: answer, timeout: timeout, hold: hold)
        DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(seconds), execute: timeout)
        link.send([
            "kind": "prompt.present", "promptId": id, "question": String(question.prefix(1000)),
            "presentationBinding": ConfirmGate.sha256("\(id)|\(question)|\(generation)"),
            "connectionGeneration": generation, "source": micSource,
            "expiresInMs": seconds * 1000, "ownerKind": ownerKind, "ownerId": ownerId, "field": field,
        ])
        Log.add("tx prompt.present \(field) for \(ownerId): \(question)")
        #if DEBUG
        DebugSeed.asked(self)
        #endif
    }

    /// The question is no longer wanted (the wearer left, a newer one),
    /// nor a held card.
    func cancel(_ why: String) {
        finish(nil, notify: true, why: why)
        guard let id = held else { return }
        held = nil
        link.send(["kind": "prompt.cancel", "promptId": id, "connectionGeneration": generation])
        Log.add("prompt \(id.prefix(6)) taken down: \(why)")
    }

    /// Words under the held card ("Mike · looking up…").
    func note(_ text: String) {
        guard let id = held else { return }
        link.send(["kind": "prompt.partial", "promptId": id, "connectionGeneration": generation,
                   "text": String(text.suffix(600))])
    }

    /// A finished sentence: the answer, when a question is open.
    func heard(_ text: String) -> Bool {
        guard open != nil else { return false }
        let words = WakeGate.strip(text).flatMap { $0.isEmpty ? nil : $0 } ?? text
        Log.add("prompt answered (\(words.count) chars)")
        finish(words, notify: true, why: "answered")
        return true
    }

    /// Words so far, under the question.
    func hearing(_ text: String) {
        guard var o = open else { return }
        o.partial = text
        open = o
        link.send(["kind": "prompt.partial", "promptId": o.id, "connectionGeneration": generation,
                   "text": String(text.suffix(600))])
    }

    func handle(kind: String, message: [String: Any]) {
        if kind == "device" {
            if let source = message.string("micSource"), source == "glasses" || source == "phone" { micSource = source }
            return
        }
        if kind == "prompt.cancel", let id = held, message.string("promptId") == id {
            held = nil
            Log.add("prompt \(id.prefix(6)) closed on the glasses while held")
            return
        }
        guard let o = open else { return }
        switch kind {
        case "prompt.ack":
            guard message.string("promptId") == o.id else { return }
            #if DEBUG
            // The Even Hub simulator streams "glasses" audio under a phone-mic
            // stream, so the plugin drops every question at once; the seed's
            // answer still arrives (DebugSeed).
            if DebugSeed.answersQuestions, message.bool("success") == false { return }
            #endif
            if message.bool("success") == false {
                finish(nil, notify: false, why: "the glasses could not open the mic")
            } else {
                Log.add("prompt \(o.id.prefix(6)) on the glasses, listening")
            }
        case "prompt.cancel":
            guard message.string("promptId") == o.id else { return }
            #if DEBUG
            if DebugSeed.answersQuestions { Log.add("debug: the simulator closed the question; the seed answers it"); return }
            #endif
            finish(nil, notify: false, why: "cancelled on the glasses")
        case "input":
            // A tap on the listening card: take what was heard now.
            guard let event = message.object("event"), event.string("containerName") == "prompt-commit",
                  event.string("selectedName") == o.id else { return }
            let said = o.partial.trimmingCharacters(in: .whitespacesAndNewlines)
            if said.isEmpty { Log.add("prompt commit with nothing heard yet"); return }
            finish(said, notify: true, why: "tapped")
        default:
            break
        }
    }

    private func finish(_ said: String?, notify: Bool, why: String) {
        guard let o = open else { return }
        open = nil
        o.timeout.cancel()
        let hold = o.hold && said != nil
        if hold { held = o.id }
        if notify && !hold {
            link.send(["kind": "prompt.cancel", "promptId": o.id, "connectionGeneration": generation])
        }
        if said == nil { Log.add("prompt \(o.id.prefix(6)) closed: \(why)") }
        o.answer(said)
    }
}
