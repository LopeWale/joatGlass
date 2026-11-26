import Foundation

/// One question the glasses ask for a feature, answered by the wearer's
/// next sentence (or multi-clause dictation for `body` / `instruction`):
/// the Mac's prompt wire (prompted_input.py, the plugin's PromptedInputController).
/// The plugin draws the question card, opens the glasses mic for it and
/// acknowledges; the words so far show under the question (`prompt.partial`);
/// a tap on the card takes them immediately (`prompt-commit`). Either side can
/// cancel.
final class Prompts: BrainFeature {
    let name = "prompts"
    let uplinkKinds: Set<String> = ["prompt.ack", "prompt.cancel", "input", "device", "policy"]

    static let multiClauseFields: Set<String> = ["body", "instruction"]
    private static let trailingConnectorWords: Set<String> = [
        "and", "or", "but", "so", "because", "if", "like", "to", "for", "with", "a", "an", "the", "that",
    ]

    private struct Open {
        let id: String
        let field: String
        let askedAt: Date
        var listeningSince: Date?
        let answer: (String?) -> Void
        var segments: [String] = []
        var partial = ""
        var heardAfterAck = false
        var commitRequested = false
        let timeout: DispatchWorkItem
        var settleWorkItem: DispatchWorkItem?
        /// Answered, the card stays until the next question replaces it.
        let hold: Bool
    }

    private let link: PluginLink
    /// Quiet window after a finalized or volatile clause on `body` / `instruction`
    /// before auto-committing, pushed forward whenever volatile speech arrives.
    var multiClauseSettleSeconds: TimeInterval = 2.4
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
    /// the answered card stays up until the owner asks again or cancels.
    func ask(_ question: String, ownerKind: String, ownerId: String, field: String,
             seconds: Int = 30, hold: Bool = false, answer: @escaping (String?) -> Void) {
        finish(nil, notify: true, why: "replaced")
        // The glasses replace a held card with this question themselves.
        held = nil
        let id = ConfirmGate.hex(16)
        let now = Date()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let current = self.open, current.id == id else { return }
            let accumulated = Self.combine(segments: current.segments, partial: current.partial)
            if !accumulated.isEmpty {
                self.finish(accumulated, notify: true, why: "timeout with accumulated speech")
            } else {
                self.finish(nil, notify: true, why: "no answer in \(seconds) s")
            }
        }
        open = Open(
            id: id,
            field: field,
            askedAt: now,
            listeningSince: nil,
            answer: answer,
            timeout: timeout,
            settleWorkItem: nil,
            hold: hold
        )
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

    /// Words under the held card ("Liam · looking up…").
    func note(_ text: String) {
        guard let id = held else { return }
        link.send(["kind": "prompt.partial", "promptId": id, "connectionGeneration": generation,
                   "text": String(text.suffix(600))])
    }

    /// A finished sentence (or clause from `DictationTranscriber`):
    /// - If `spokeUntil` ended before `listeningSince ?? askedAt`, ignores the
    ///   stale pre-prompt utterance so the tail of `"JOAT, text Mom"` never
    ///   bleeds into the answer.
    /// - For `body` and `instruction` with live STT (`spokeUntil != nil`),
    ///   accumulates clauses across natural pauses and settles after `1.5s` of
    ///   quiet (or `0 ms` on tap `prompt-commit`).
    @discardableResult
    func heard(_ text: String, spokeUntil: Date? = nil) -> Bool {
        guard var o = open else { return false }
        let fence = o.listeningSince ?? o.askedAt
        if let spokeUntil, spokeUntil < fence {
            if o.segments.isEmpty && !o.heardAfterAck {
                o.partial = ""
                open = o
            }
            Log.add("prompt ignored stale pre-prompt speech: \(text.prefix(80))")
            return true
        }

        let stripped = WakeGate.strip(text)
        if let stripped {
            if stripped.isEmpty {
                Log.add("prompt yielded to standalone JOAT wake call")
                cancel("addressed JOAT")
                return false
            }
            if Navigation.isCancel(stripped) {
                Log.add("prompt cancelled by addressed voice command")
                cancel("voice cancel")
                return true
            }
        }

        let rawWords = stripped.flatMap { $0.isEmpty ? nil : $0 } ?? text
        let words = rawWords.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return true }

        if o.commitRequested {
            let full = Self.combine(segments: o.segments + [words], partial: "")
            Log.add("prompt answered via latched tap (\(full.count) chars)")
            finish(full, notify: true, why: "tapped")
            return true
        }

        if spokeUntil != nil, Self.multiClauseFields.contains(o.field) {
            o.segments.append(words)
            o.partial = ""
            let combined = Self.combine(segments: o.segments, partial: "")
            armSettleTimer(&o, trailingText: combined)
            open = o
            sendPartial(promptId: o.id, text: combined)
            Log.add("prompt clause \(o.segments.count) (\(combined.count) chars)")
            return true
        }

        let full = Self.combine(segments: o.segments + [words], partial: "")
        Log.add("prompt answered (\(full.count) chars)")
        finish(full, notify: true, why: "answered")
        return true
    }

    /// Words so far, under the question. Publishes the accumulated text + volatile
    /// clause and slides the settle timer forward so active speech is never cut off
    /// AND volatile-only speech (when ambient noise keeps VAD from emitting `.final`)
    /// still settles cleanly to review instead of hanging on listening.
    func hearing(_ text: String) {
        guard var o = open else { return }
        o.partial = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !o.partial.isEmpty, o.listeningSince != nil {
            o.heardAfterAck = true
        }
        let combined = Self.combine(segments: o.segments, partial: o.partial)
        if !combined.isEmpty {
            if o.commitRequested {
                armSettleTimer(&o, trailingText: combined, overrideDelay: min(0.5, max(0.05, multiClauseSettleSeconds)))
            } else if !o.segments.isEmpty || Self.multiClauseFields.contains(o.field) {
                armSettleTimer(&o, trailingText: combined)
            } else {
                armSettleTimer(&o, trailingText: combined, overrideDelay: max(multiClauseSettleSeconds, 2.6))
            }
        }
        open = o
        guard !combined.isEmpty else { return }
        sendPartial(promptId: o.id, text: combined)
    }

    func handle(kind: String, message: [String: Any]) {
        if kind == "device" {
            if let source = message.string("micSource"), source == "glasses" || source == "phone" { micSource = source }
            return
        }
        if kind == "policy" {
            if message.string("micMode") == "phone" {
                micSource = "phone"
            } else if let mode = message.string("micMode"), mode == "always" || mode == "on_demand" {
                if micSource != "glasses" && micSource != "phone" { micSource = "glasses" }
            }
            return
        }
        if kind == "prompt.cancel", let id = held, message.string("promptId") == id {
            held = nil
            Log.add("prompt \(id.prefix(6)) closed on the glasses while held")
            return
        }
        guard var o = open else { return }
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
                o.listeningSince = Date()
                open = o
                Log.add("prompt \(o.id.prefix(6)) on the glasses, listening")
            }
        case "prompt.cancel":
            guard message.string("promptId") == o.id else { return }
            #if DEBUG
            if DebugSeed.answersQuestions { Log.add("debug: the simulator closed the question; the seed answers it"); return }
            #endif
            finish(nil, notify: false, why: "cancelled on the glasses")
        case "input":
            // A tap on the listening card: cancel any settle timer and commit immediately (0 ms),
            // or latch commit if the final audio batch is still finishing STT.
            guard let event = message.object("event"), event.string("containerName") == "prompt-commit",
                  event.string("selectedName") == o.id else { return }
            let said = Self.combine(segments: o.segments, partial: o.partial)
            if said.isEmpty {
                o.commitRequested = true
                open = o
                Log.add("prompt commit latched awaiting in-flight speech")
                return
            }
            finish(said, notify: true, why: "tapped")
        default:
            break
        }
    }

    private func armSettleTimer(_ o: inout Open, trailingText: String = "", overrideDelay: TimeInterval? = nil) {
        o.settleWorkItem?.cancel()
        let promptId = o.id
        var delay = max(0.05, overrideDelay ?? multiClauseSettleSeconds)
        if overrideDelay == nil, delay >= 1.0 {
            let lastWord = trailingText
                .split(whereSeparator: { !$0.isLetter && $0 != "'" && $0 != "’" })
                .last
                .map { $0.lowercased() } ?? ""
            if Self.trailingConnectorWords.contains(lastWord) {
                delay += 0.8
            }
        }
        let work = DispatchWorkItem { [weak self] in
            guard let self, let current = self.open, current.id == promptId else { return }
            let full = Self.combine(segments: current.segments, partial: current.partial)
            guard !full.isEmpty else { return }
            Log.add("prompt multi-clause settled (\(full.count) chars, \(current.segments.count) segment(s))")
            self.finish(full, notify: true, why: "settled")
        }
        o.settleWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func sendPartial(promptId: String, text: String) {
        link.send([
            "kind": "prompt.partial",
            "promptId": promptId,
            "connectionGeneration": generation,
            "text": String(text.suffix(600)),
        ])
    }

    private static func combine(segments: [String], partial: String) -> String {
        let cleanPartial = partial.trimmingCharacters(in: .whitespacesAndNewlines)
        return (segments + [cleanPartial])
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    private func finish(_ said: String?, notify: Bool, why: String) {
        guard let o = open else { return }
        open = nil
        o.timeout.cancel()
        o.settleWorkItem?.cancel()
        let hold = o.hold && said != nil
        if hold { held = o.id }
        if notify && !hold {
            link.send(["kind": "prompt.cancel", "promptId": o.id, "connectionGeneration": generation])
        }
        if said == nil { Log.add("prompt \(o.id.prefix(6)) closed: \(why)") }
        o.answer(said)
    }
}
